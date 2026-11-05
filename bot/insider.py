"""Insider-Signale aus SEC EDGAR. Nur Standardbibliothek.

Zwei Quellen:
  1. Unternehmens-Insider: Form 4 (Kauf/Verkauf über die Börse) aller
     Vorstände, Aufsichtsräte und 10-%-Eigner der beobachteten Firmen.
  2. Beobachtete Personen (z. B. Peter Thiel, Trump-Familie): ihre eigenen
     Meldungen als Form 4, Schedule 13D/13G (Beteiligung über 5 %) und
     Form 144 (geplanter Verkauf).

Alles wird in trader/sim/insider.json zwischengespeichert, sodass jede
Meldung nur einmal geladen wird.
"""

import datetime
import json
import os
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

ROLE = {"isDirector": "Aufsichtsrat/Direktor", "isOfficer": "Vorstand", "isTenPercentOwner": "Großaktionär (10 %)"}


class Sec:
    def __init__(self, ua, pause=0.12):
        self.ua, self.pause, self.last = ua, pause, 0.0

    def get(self, url):
        for attempt in range(3):
            wait = self.pause - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            req = urllib.request.Request(url, headers={"User-Agent": self.ua, "Accept-Encoding": "identity"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code in (429, 503) and attempt < 2:
                    time.sleep(2 + 3 * attempt)
                    continue
                raise
        raise RuntimeError("SEC nicht erreichbar: " + url)

    def json(self, url):
        return json.loads(self.get(url).decode("utf-8"))


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _find(root, name):
    for el in root.iter():
        if _local(el.tag) == name:
            return el
    return None


def _text(el, *path):
    for p in path:
        if el is None:
            return None
        el = next((c for c in el if _local(c.tag) == p), None)
    if el is None:
        return None
    v = el.find("./{*}value") if len(el) else None
    return ((v.text if v is not None else el.text) or "").strip() or None


def _num(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_form4(xml_bytes):
    root = ET.fromstring(xml_bytes)
    issuer = _find(root, "issuer")
    sym = (_text(issuer, "issuerTradingSymbol") or "").upper().split(",")[0].strip() or None
    owner = _find(root, "reportingOwner")
    name = _text(owner, "reportingOwnerId", "rptOwnerName") if owner is not None else None
    rel = _find(root, "reportingOwnerRelationship")
    roles = []
    if rel is not None:
        for k, label in ROLE.items():
            if (_text(rel, k) or "").lower() in ("1", "true"):
                roles.append(_text(rel, "officerTitle") if k == "isOfficer" and _text(rel, "officerTitle") else label)
    txs = []
    for t in root.iter():
        if _local(t.tag) != "nonDerivativeTransaction":
            continue
        code = _text(t, "transactionCoding", "transactionCode")
        if code not in ("P", "S"):
            continue
        shares = _num(_text(t, "transactionAmounts", "transactionShares"))
        price = _num(_text(t, "transactionAmounts", "transactionPricePerShare"))
        txs.append({"code": code, "date": _text(t, "transactionDate"), "shares": shares, "price": price,
                    "value": (shares or 0) * (price or 0)})
    return {"sym": sym, "issuer": _text(issuer, "issuerName"), "owner": name, "role": ", ".join(roles), "txs": txs}


def parse_generic(xml_bytes):
    """Schedule 13D/13G und Form 144: Emittent und ggf. Prozent."""
    text = xml_bytes.decode("utf-8", "replace")
    def tag(n):
        m = re.search(r"<(?:\w+:)?%s>\s*([^<]+)" % n, text)
        return m.group(1).strip() if m else None
    pct = _num(tag("classPercent")) or _num(tag("percentOfClass"))
    return {"issuer_cik": (tag("issuerCik") or tag("issuerCIK") or "").lstrip("0") or None,
            "issuer": tag("issuerName") or tag("nameOfIssuer"), "sym": (tag("issuerTradingSymbol") or "").upper() or None,
            "pct": pct}


def doc_url(cik, acc, primary):
    raw = re.sub(r"^xsl[^/]+/", "", primary or "")
    return "https://www.sec.gov/Archives/edgar/data/%s/%s/%s" % (int(cik), acc.replace("-", ""), raw)


def index_url(cik, acc):
    return "https://www.sec.gov/Archives/edgar/data/%s/%s/" % (int(cik), acc.replace("-", ""))


def ticker_maps(sec, sim, errors):
    path = os.path.join(sim, "company_tickers.json")
    today = datetime.date.today().isoformat()
    cache = {}
    try:
        with open(path, encoding="utf-8") as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        pass
    if cache.get("fetched") != today:
        try:
            data = sec.json("https://www.sec.gov/files/company_tickers.json")
            cache = {"fetched": today, "rows": [[v["ticker"], v["cik_str"], v["title"]] for v in data.values()]}
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(cache, fh)
        except Exception as e:
            errors.append("SEC-Tickerliste: %s" % e)
    t2c, c2t = {}, {}
    for t, c, _ in cache.get("rows", []):
        t = t.upper().replace("-", ".")
        t2c.setdefault(t, c)
        c2t.setdefault(str(c), t)
    return t2c, c2t


def recent_filings(sub, forms, since):
    r = sub.get("filings", {}).get("recent", {})
    for f, acc, d, doc in zip(r.get("form", []), r.get("accessionNumber", []), r.get("filingDate", []),
                              r.get("primaryDocument", [])):
        if d < since:
            break
        if f in forms:
            yield f, acc, d, doc


def collect(cfg, ua, sim, tickers, errors):
    """Liefert ({SYM: [(punkte, grund)]}, [ereignisse], {extra_ticker})."""
    sec = Sec(ua)
    path = os.path.join(sim, "insider.json")
    try:
        with open(path, encoding="utf-8") as fh:
            store = json.load(fh)
    except (OSError, ValueError):
        store = {}
    seen = store.get("seen", {})
    tx, ppl = store.get("tx", []), store.get("people", [])
    today = datetime.date.today()
    since_co = (today - datetime.timedelta(days=cfg.get("insider_days", 14))).isoformat()
    since_pp = (today - datetime.timedelta(days=cfg.get("people_days", 90))).isoformat()
    t2c, c2t = ticker_maps(sec, sim, errors)

    # 1. Unternehmens-Insider (Form 4)
    budget = cfg.get("insider_max_docs", 250)
    for sym in tickers:
        cik = t2c.get(sym.upper().replace("-", "."))
        if not cik:
            continue
        try:
            sub = sec.json("https://data.sec.gov/submissions/CIK%010d.json" % int(cik))
        except Exception as e:
            errors.append("Insider %s: %s" % (sym, e))
            continue
        n = 0
        for form, acc, filed, doc in recent_filings(sub, ("4", "4/A"), since_co):
            if acc in seen or budget <= 0 or n >= 15:
                continue
            n += 1
            budget -= 1
            seen[acc] = filed
            try:
                f4 = parse_form4(sec.get(doc_url(cik, acc, doc)))
            except Exception:
                continue
            for t in f4["txs"]:
                tx.append(dict(t, acc=acc, sym=sym, owner=f4["owner"], role=f4["role"], filed=filed,
                               url=index_url(cik, acc)))

    # 2. Beobachtete Personen
    for p in cfg.get("people", []):
        try:
            sub = sec.json("https://data.sec.gov/submissions/CIK%010d.json" % int(p["cik"]))
        except Exception as e:
            errors.append("%s: %s" % (p["name"], e))
            continue
        forms = ("4", "4/A", "SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A",
                 "SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A", "144")
        for form, acc, filed, doc in recent_filings(sub, forms, since_pp):
            key = "p:%s:%s" % (p["cik"], acc)
            if key in seen:
                continue
            seen[key] = filed
            try:
                raw = sec.get(doc_url(p["cik"], acc, doc))
            except Exception:
                continue
            base = {"person": p["name"], "form": form, "filed": filed, "acc": acc,
                    "url": index_url(p["cik"], acc)}
            try:
                if form.startswith("4"):
                    f4 = parse_form4(raw)
                    for t in f4["txs"]:
                        ppl.append(dict(base, kind=t["code"], sym=f4["sym"], issuer=f4["issuer"], date=t["date"],
                                        shares=t["shares"], price=t["price"], value=t["value"]))
                elif doc.endswith(".xml"):
                    g = parse_generic(raw)
                    sym = g["sym"] or c2t.get(g["issuer_cik"] or "")
                    ppl.append(dict(base, kind="144" if form == "144" else "13", sym=sym, issuer=g["issuer"],
                                    pct=g["pct"], date=filed))
            except ET.ParseError:
                continue

    # aufräumen
    cut = (today - datetime.timedelta(days=45)).isoformat()
    cut_pp = (today - datetime.timedelta(days=max(cfg.get("people_days", 90), 45))).isoformat()
    tx = [t for t in tx if (t.get("filed") or "") >= cut]
    ppl = [p for p in ppl if (p.get("filed") or "") >= cut_pp]
    seen = {k: v for k, v in seen.items() if v >= cut_pp}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"seen": seen, "tx": tx, "people": ppl}, fh, indent=1, ensure_ascii=False)

    # Signale und Ereignisse
    signals, events, extra = {}, [], set()
    window = (today - datetime.timedelta(days=30)).isoformat()
    by_sym = {}
    for t in tx:
        if t["filed"] >= window:
            by_sym.setdefault(t["sym"], []).append(t)
    for sym, ts in by_sym.items():
        buyers = {t["owner"] for t in ts if t["code"] == "P"}
        sellers = {t["owner"] for t in ts if t["code"] == "S"}
        bval = sum(t["value"] for t in ts if t["code"] == "P")
        sval = sum(t["value"] for t in ts if t["code"] == "S")
        if len(buyers) >= 2:
            signals.setdefault(sym, []).append((3, "%d Insider kaufen (%s $)" % (len(buyers), _m(bval))))
        elif buyers:
            b = next(t for t in ts if t["code"] == "P")
            signals.setdefault(sym, []).append((2, "Insider kauft: %s%s für %s $" % (
                b["owner"], " (%s)" % b["role"] if b["role"] else "", _m(bval))))
        elif len(sellers) >= 3:
            signals.setdefault(sym, []).append((-1, "%d Insider verkaufen (%s $)" % (len(sellers), _m(sval))))
    for t in tx:
        if t["code"] == "P" or t["value"] >= 1e6:
            events.append({"id": "insider:%s:%s:%s:%s" % (t["acc"], t["code"], t["date"], t["shares"]),
                           "type": "insider", "sym": t["sym"], "time": (t["filed"]) + "T12:00:00+00:00",
                           "title": "%s%s %s %s Aktien für %s $" % (
                               t["owner"], " (%s)" % t["role"] if t["role"] else "",
                               "kauft" if t["code"] == "P" else "verkauft", _n(t["shares"]), _m(t["value"])),
                           "detail": "Form 4 · gehandelt am %s zu %s $" % (t["date"], _p(t["price"])),
                           "link": t["url"], "tone": 1 if t["code"] == "P" else -1})
    for p in ppl:
        sym = p.get("sym")
        what = {"P": "kauft", "S": "verkauft", "13": "meldet Großbeteiligung", "144": "plant Verkauf (Form 144)"}[p["kind"]]
        if p["kind"] in ("P", "S"):
            title = "%s %s %s Aktien %s für %s $" % (p["person"], what, _n(p["shares"]), sym or p.get("issuer") or "", _m(p["value"]))
        elif p["kind"] == "13":
            title = "%s %s an %s%s" % (p["person"], what, p.get("issuer") or sym or "?",
                                        " (%s %%)" % _p(p["pct"]) if p.get("pct") else "")
        else:
            title = "%s %s: %s" % (p["person"], what, p.get("issuer") or sym or "?")
        events.append({"id": "person:%s:%s:%s" % (p["acc"], p["kind"], p.get("date")), "type": "person",
                       "sym": (sym or "–").upper().replace(".", "-"), "time": p["filed"] + "T12:00:00+00:00", "title": title,
                       "detail": "%s · eingereicht %s" % (p["form"], p["filed"]), "link": p["url"],
                       "tone": 1 if p["kind"] in ("P", "13") else -1})
        if not sym:
            continue
        sym = sym.upper().replace(".", "-")
        extra.add(sym)
        pts = {"P": 2, "13": 2, "S": -1, "144": -1}[p["kind"]]
        signals.setdefault(sym, []).append((pts, title))
    # pro Person und Aktie nur den stärksten Eintrag, insgesamt gedeckelt
    for sym, items in signals.items():
        uniq = {}
        for pts, why in items:
            k = why.split(" ")[0:2].__str__()
            if k not in uniq or abs(pts) > abs(uniq[k][0]):
                uniq[k] = (pts, why)
        signals[sym] = list(uniq.values())
    return signals, events, extra


def _m(v):
    v = v or 0
    return "%.1f Mio." % (v / 1e6) if v >= 1e6 else "%.0f Tsd." % (v / 1e3) if v >= 1e3 else "%.0f" % v


def _n(v):
    return "{:,.0f}".format(v or 0).replace(",", ".")


def _p(v):
    return ("%.2f" % v).replace(".", ",") if v is not None else "?"
