#!/usr/bin/env python3
"""Simulations-Trading-Bot. Kein echtes Geld, kein Broker.

Jeder Lauf:
  1. prüft, ob die NYSE offen ist (sonst nichts tun, außer mit --force)
  2. Signale anderer Investoren: 13F-Depots der Fonds aus bot.json
     (einmal pro Tag) und Aktien-Meldungen von Kongressmitgliedern
  3. Marktlage: SPY-Trend und VIX (Angstbarometer)
  4. pro Ticker:
       Trend      SMA 50/200, Golden/Death Cross, ADX (Trendstärke)
       Momentum   MACD, RSI, Stochastik, Bollinger-Bänder, 20-Tage-Rendite
       Volumen    relatives Volumen, OBV, Chaikin Money Flow,
                  Kauf- vs. Verkaufsvolumen, 52-Wochen-Ausbruch
       Intraday   VWAP, Opening Range (erste 30 Min.), Gap
       News       Stimmung der Schlagzeilen, wichtige 8-K-Ereignisse
  5. verkauft/kauft im virtuellen Depot und schreibt alles mit

Dateien (alle in trader/sim/):
  portfolio.json  Cash und Positionen
  trades.csv      jede simulierte Order mit Grund
  equity.csv      Depotwert pro Lauf
  analysis.json   die komplette Analyse des letzten Laufs
  report.md       der letzte Bericht

    python3 bot/bot.py            # ein Lauf
    python3 bot/bot.py --force    # auch wenn die Börse zu ist
    python3 bot/bot.py --reset    # virtuelles Depot neu starten
"""

import argparse
import csv
import datetime
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(ROOT, "skills")
CONFIG = os.path.join(ROOT, "trader", "bot.json")
SIM = os.path.join(ROOT, "trader", "sim")
UA = "Mozilla/5.0 (compatible; trader-sim-bot/1.0)"
CATEGORIES = ["Signale", "Trend", "Momentum", "Volumen", "Intraday", "News"]

DEFAULTS = {
    "start_cash": 10000,
    "investors": [{"name": "Berkshire Hathaway", "cik": "1067983", "top": 10}],
    "congress_members": ["pelosi"],
    "congress_days": 60,
    "watchlist": ["SPY", "QQQ"],
    "max_position_pct": 15,
    "cash_buffer_pct": 10,
    "min_order": 50,
    "buy_score": 6,
    "sell_score": -4,
    "stop_loss_pct": 8,
    "atr_stop": 2.5,
    "trailing_atr": 3.5,
    "news_hours": 48,
}

POSITIVE = re.compile(
    r"\b(beats?|tops|surge[sd]?|soar(s|ed)?|jump(s|ed)?|rall(y|ies|ied)|upgrade[sd]?|record (high|profit|revenue|sales)"
    r"|raise[sd]? (its )?(guidance|outlook|forecast|dividend)|buyback|outperform|strong(er)?|boost(s|ed)?"
    r"|climb(s|ed)?|beat expectations|wins?|approval|approved)\b", re.I)
NEGATIVE = re.compile(
    r"\b(miss(es|ed)?|plunge[sd]?|plummet(s|ed)?|fall(s|en)?|fell|drop(s|ped)?|sink(s)?|sank|tumble[sd]?|slump(s|ed)?"
    r"|downgrade[sd]?|cut(s)? (its )?(guidance|outlook|forecast|dividend)|lawsuit|sued|probe|investigation|recall"
    r"|weak(er|ness)?|layoffs?|bankrupt\w*|fraud|warns?|warning|halt(s|ed)?|selloff|sell-off)\b", re.I)
BLOCK_ITEMS = {"1.03": "Insolvenz", "1.05": "Cyberangriff", "3.01": "Delisting-Warnung",
               "4.02": "Bilanzen nicht verlässlich", "2.04": "Kreditkündigung"}
NOTE_ITEMS = {"2.02": "Quartalszahlen veröffentlicht", "5.02": "Wechsel im Vorstand",
              "2.01": "Übernahme/Verkauf", "1.01": "wichtiger Vertrag", "2.06": "Abschreibung"}


# ---------------------------------------------------------------- Hilfen

def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, doc):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_skill(rel, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SKILLS, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_skill(rel, args):
    """Startet ein Skill-Skript im Projektordner und liefert dessen JSON."""
    cmd = [sys.executable, "-I", os.path.join(SKILLS, rel)] + args + ["--json"]
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=900)
    try:
        return json.loads(out.stdout)
    except ValueError:
        raise RuntimeError((out.stderr or out.stdout).strip()[-300:] or "keine Ausgabe")


def log(msg):
    print(msg, flush=True)


def clip(x, lo, hi):
    return max(lo, min(hi, x))


# ---------------------------------------------------------------- Signale anderer

def investor_signals(cfg, errors):
    """{TICKER: [Grund, ...]} aus den 13F-Depots, Cache einmal pro Tag."""
    today = datetime.date.today().isoformat()
    out = {}
    for inv in cfg["investors"]:
        cache = os.path.join(SIM, "13f_%s.json" % inv["cik"])
        doc = read_json(cache, {})
        if doc.get("fetched") != today:
            try:
                data = run_skill("trader-copycat/edgar13f.py",
                                 ["copy", inv["cik"], "--top", str(inv.get("top", 10)), "--dry-run"])
                doc = {"fetched": today, "data": data}
                write_json(cache, doc)
            except Exception as e:  # alter Cache bleibt nutzbar
                errors.append("13F %s: %s" % (inv["name"], e))
        for w in (doc.get("data") or {}).get("weights", []):
            if w.get("ticker"):
                out.setdefault(w["ticker"], []).append(
                    "%s hält %.1f %%" % (inv["name"], w.get("pct_of_13f", 0)))
    return out


def congress_signals(cfg, errors):
    """Neue Kongress-Meldungen einlesen, im Signalspeicher sammeln, alte löschen."""
    store_path = os.path.join(SIM, "signals.json")
    store = read_json(store_path, {"congress": []})
    seen = {(s["doc_id"], s["ticker"], s["type"], s["date"]) for s in store["congress"]}
    args = ["new", "--read", "--state", os.path.join(SIM, "ptr_state.json"),
            "--pdf-dir", os.path.join(SIM, "ptr_pdfs")]
    for m in cfg["congress_members"]:
        args += ["--member", m]
    try:
        data = run_skill("trader-pelosi/ptr.py", args)
        for rep in data.get("reports", []):
            r = rep.get("report") or {}
            for row in r.get("rows", []):
                if not row.get("ticker") or row.get("type") not in ("P", "S"):
                    continue
                key = (r.get("doc_id"), row["ticker"], row["type"], row.get("date"))
                if key in seen:
                    continue
                seen.add(key)
                store["congress"].append({
                    "doc_id": r.get("doc_id"), "member": r.get("name"), "ticker": row["ticker"].upper(),
                    "type": row["type"], "date": row.get("date"), "amount": row.get("amount"),
                    "filed": r.get("filed")})
    except Exception as e:
        errors.append("Kongress: %s" % e)
    cutoff = (datetime.date.today() - datetime.timedelta(days=cfg["congress_days"])).isoformat()
    store["congress"] = [s for s in store["congress"] if (s.get("filed") or s.get("date") or "") >= cutoff]
    write_json(store_path, store)
    buys, sells = {}, {}
    for s in store["congress"]:
        target = buys if s["type"] == "P" else sells
        target.setdefault(s["ticker"], []).append("%s %s (%s)" % (
            s["member"], "kauft" if s["type"] == "P" else "verkauft", s.get("amount") or "?"))
    return buys, sells


# ---------------------------------------------------------------- Kursdaten

def yahoo(sym, rng, interval):
    path = "/v8/finance/chart/%s?range=%s&interval=%s" % (urllib.parse.quote(sym.replace(".", "-")), rng, interval)
    for i, host in enumerate(("query1", "query2", "query1")):
        try:
            req = urllib.request.Request("https://%s.finance.yahoo.com%s" % (host, path), headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                res = json.loads(r.read().decode())["chart"]["result"][0]
            break
        except urllib.error.HTTPError:
            if i == 2:
                raise
            time.sleep(2 + 3 * i)
    q = res["indicators"]["quote"][0]
    bars = [(o, h, l, c, v or 0) for o, h, l, c, v in zip(q.get("open") or [], q.get("high") or [],
                                                           q.get("low") or [], q.get("close") or [],
                                                           q.get("volume") or [])
            if None not in (o, h, l, c)]
    return res["meta"], bars


def daily(sym):
    meta, bars = yahoo(sym, "1y", "1d")
    if len(bars) < 30:
        raise RuntimeError("zu wenig Kursdaten")
    price = float(meta.get("regularMarketPrice") or bars[-1][3])
    name = meta.get("shortName") or meta.get("longName") or sym
    return {"price": price, "name": name, "prev_close": meta.get("chartPreviousClose"),
            "o": [b[0] for b in bars], "h": [b[1] for b in bars], "l": [b[2] for b in bars],
            "c": [b[3] for b in bars], "v": [b[4] for b in bars]}


def intraday(sym):
    _, bars = yahoo(sym, "1d", "5m")
    return bars


# ---------------------------------------------------------------- Indikatoren

def sma(xs, n, end=None):
    xs = xs if end is None else xs[:end]
    return sum(xs[-n:]) / n if len(xs) >= n else None


def ema_series(xs, n):
    k, out = 2 / (n + 1), [xs[0]]
    for x in xs[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def wilder(xs, n):
    if len(xs) < n:
        return []
    out = [sum(xs[:n]) / n]
    for x in xs[n:]:
        out.append((out[-1] * (n - 1) + x) / n)
    return out


def rsi(xs, n=14):
    d = [b - a for a, b in zip(xs, xs[1:])]
    ag, al = wilder([max(x, 0) for x in d], n), wilder([max(-x, 0) for x in d], n)
    if not ag:
        return None
    return 100.0 if al[-1] == 0 else 100 - 100 / (1 + ag[-1] / al[-1])


def atr(h, l, c, n=14):
    tr = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    w = wilder(tr, n)
    return w[-1] if w else None


def adx(h, l, c, n=14):
    """(ADX, +DI, -DI)"""
    pdm, mdm, tr = [], [], []
    for i in range(1, len(c)):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm.append(up if up > dn and up > 0 else 0)
        mdm.append(dn if dn > up and dn > 0 else 0)
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    str_, sp, sm = wilder(tr, n), wilder(pdm, n), wilder(mdm, n)
    if len(str_) < n:
        return None, None, None
    pdi = [100 * p / t if t else 0 for p, t in zip(sp, str_)]
    mdi = [100 * m / t if t else 0 for m, t in zip(sm, str_)]
    dx = [100 * abs(p - m) / (p + m) if p + m else 0 for p, m in zip(pdi, mdi)]
    a = wilder(dx, n)
    return (a[-1] if a else None), pdi[-1], mdi[-1]


def stochastic(h, l, c, n=14, d=3):
    ks = []
    for i in range(len(c) - n - d + 1, len(c)):
        hh, ll = max(h[i - n + 1:i + 1]), min(l[i - n + 1:i + 1])
        ks.append(100 * (c[i] - ll) / (hh - ll) if hh > ll else 50)
    return ks[-1], sum(ks[-d:]) / d


def analyse(sym, d, ib, market_open):
    """Punkte und Begründungen pro Kategorie für einen Ticker."""
    price = d["price"]
    o, h, l, c, v = d["o"], d["h"], d["l"], d["c"][:-1] + [price], d["v"]
    cat = {k: [0, []] for k in CATEGORIES}

    def add(k, pts, why):
        cat[k][0] += pts
        cat[k][1].append(why)

    ind = {"price": price}

    # Trend
    s50, s200 = sma(c, 50), sma(c, 200)
    ind.update(sma50=s50, sma200=s200)
    if s50:
        add("Trend", 1 if price > s50 else -1, "Kurs %s SMA50" % (">" if price > s50 else "<"))
    if s50 and s200:
        add("Trend", 1 if s50 > s200 else -1, "SMA50 %s SMA200" % (">" if s50 > s200 else "<"))
        p50, p200 = sma(c, 50, -10), sma(c, 200, -10)
        if p50 and p200:
            if p50 <= p200 and s50 > s200:
                add("Trend", 1, "Golden Cross (letzte 10 Tage)")
            elif p50 >= p200 and s50 < s200:
                add("Trend", -1, "Death Cross (letzte 10 Tage)")
    a, pdi, mdi = adx(h, l, c)
    ind.update(adx=a, plus_di=pdi, minus_di=mdi)
    if a is not None:
        if a >= 25:
            up = pdi > mdi
            add("Trend", 1 if up else -1, "ADX %.0f: starker %s" % (a, "Aufwärtstrend" if up else "Abwärtstrend"))
        else:
            cat["Trend"][1].append("ADX %.0f: kein klarer Trend" % a)

    # Momentum
    macd = [x - y for x, y in zip(ema_series(c, 12), ema_series(c, 26))]
    hist = macd[-1] - ema_series(macd, 9)[-1]
    ind["macd_hist"] = hist
    add("Momentum", 1 if hist > 0 else -1, "MACD %s" % ("positiv" if hist > 0 else "negativ"))
    r = rsi(c)
    ind["rsi"] = r
    if r is not None:
        if r < 30:
            add("Momentum", 1, "RSI %.0f überverkauft" % r)
        elif r > 70:
            add("Momentum", -1, "RSI %.0f überkauft" % r)
        else:
            cat["Momentum"][1].append("RSI %.0f neutral" % r)
    k, dd = stochastic(h, l, c)
    ind.update(stoch_k=k, stoch_d=dd)
    if k < 20 and k > dd:
        add("Momentum", 1, "Stochastik %.0f dreht aus überverkauft" % k)
    elif k > 80 and k < dd:
        add("Momentum", -1, "Stochastik %.0f dreht aus überkauft" % k)
    mid = sma(c, 20)
    sd = (sum((x - mid) ** 2 for x in c[-20:]) / 20) ** 0.5
    ind.update(bb_upper=mid + 2 * sd, bb_lower=mid - 2 * sd, bb_width=4 * sd / mid * 100)
    if price < mid - 2 * sd:
        add("Momentum", 1, "unter dem unteren Bollinger-Band")
    elif price > mid + 2 * sd:
        add("Momentum", -1, "über dem oberen Bollinger-Band")
    roc = (price / c[-21] - 1) * 100 if len(c) > 21 else 0
    ind["roc20"] = roc
    if roc > 5:
        add("Momentum", 1, "+%.1f %% in 20 Tagen" % roc)
    elif roc < -5:
        add("Momentum", -1, "%.1f %% in 20 Tagen" % roc)

    # Volumen
    avg_v = sum(v[-21:-1]) / 20
    elapsed = 1.0
    if market_open and ib:
        elapsed = clip(len(ib) * 5 / 390, 0.05, 1.0)
    rvol = (v[-1] / elapsed) / avg_v if avg_v else None
    ind["rvol"] = rvol
    day_up = price >= c[-2]
    if rvol is not None:
        if rvol >= 1.5:
            add("Volumen", 1 if day_up else -1, "%.1fx Volumen an einem %s-Tag" % (rvol, "Plus" if day_up else "Minus"))
        else:
            cat["Volumen"][1].append("Volumen %.1fx normal" % rvol)
    obv = [0]
    for i in range(1, len(c)):
        obv.append(obv[-1] + (v[i] if c[i] > c[i - 1] else -v[i] if c[i] < c[i - 1] else 0))
    obv_trend = (obv[-1] - obv[-21]) / (avg_v * 20) if avg_v else 0
    ind["obv_trend"] = obv_trend
    if obv_trend > 0.1:
        add("Volumen", 1, "OBV steigt (Akkumulation)")
    elif obv_trend < -0.1:
        add("Volumen", -1, "OBV fällt (Distribution)")
    mfv = sum(((c[i] - l[i]) - (h[i] - c[i])) / (h[i] - l[i]) * v[i] for i in range(-20, 0) if h[i] > l[i])
    cmf = mfv / sum(v[-20:]) if sum(v[-20:]) else 0
    ind["cmf"] = cmf
    if cmf > 0.05:
        add("Volumen", 1, "Chaikin Money Flow +%.2f (Geld fließt rein)" % cmf)
    elif cmf < -0.05:
        add("Volumen", -1, "Chaikin Money Flow %.2f (Geld fließt raus)" % cmf)
    upv = sum(v[i] for i in range(-20, 0) if c[i] > c[i - 1])
    dnv = sum(v[i] for i in range(-20, 0) if c[i] < c[i - 1])
    ratio = upv / dnv if dnv else 2.0
    ind["buy_sell_ratio"] = ratio
    if ratio > 1.3:
        add("Volumen", 1, "Kaufdruck: %.1fx mehr Volumen an Plus-Tagen" % ratio)
    elif ratio < 0.77:
        add("Volumen", -1, "Verkaufsdruck: %.1fx mehr Volumen an Minus-Tagen" % (1 / ratio))
    hi52, lo52 = max(h[-252:]), min(l[-252:])
    ind.update(high52=hi52, low52=lo52)
    if price >= hi52 * 0.98 and (rvol or 0) >= 1.2:
        add("Volumen", 1, "Ausbruch am 52-Wochen-Hoch mit Volumen")
    elif price <= lo52 * 1.02:
        add("Volumen", -1, "nahe 52-Wochen-Tief")

    # Intraday
    if ib and len(ib) >= 3:
        pv = sum((b[1] + b[2] + b[3]) / 3 * b[4] for b in ib)
        vol = sum(b[4] for b in ib)
        vwap = pv / vol if vol else None
        ind["vwap"] = vwap
        if vwap:
            add("Intraday", 1 if price > vwap else -1, "Kurs %s VWAP %.2f" % (">" if price > vwap else "<", vwap))
        if len(ib) > 6:
            or_h, or_l = max(b[1] for b in ib[:6]), min(b[2] for b in ib[:6])
            ind.update(or_high=or_h, or_low=or_l)
            if price > or_h:
                add("Intraday", 1, "Ausbruch über Opening Range %.2f" % or_h)
            elif price < or_l:
                add("Intraday", -1, "unter Opening Range %.2f" % or_l)
        gap = (ib[0][0] / c[-2] - 1) * 100
        ind["gap"] = gap
        if abs(gap) >= 2:
            cat["Intraday"][1].append("Gap %+.1f %% zur Eröffnung" % gap)
    else:
        cat["Intraday"][1].append("keine Intraday-Daten")

    ind["atr"] = atr(h, l, c)
    return cat, ind


# ---------------------------------------------------------------- News und Ereignisse

def news_scores(universe, names, cfg, errors):
    """{TICKER: (punkte, [gründe], block_grund, [schlagzeilen])}"""
    out = {s: [0, [], None, []] for s in universe}
    try:
        data = run_skill("trader-news/news.py", ["--tickers"] + universe + [
            "--hours", str(cfg["news_hours"]), "--all", "--dry-run"])
    except Exception as e:
        errors.append("News: %s" % e)
        return out
    pats = {}
    for s in universe:
        words = [re.escape(s)]
        first = re.sub(r"[^A-Za-z]", "", (names.get(s) or "").split(" ")[0])
        if len(first) >= 4:
            words.append(re.escape(first))
        pats[s] = re.compile(r"\b(%s)\b" % "|".join(words), re.I if len(words) > 1 else 0)
    seen_titles = set()
    for it in data.get("items", []):
        s = (it.get("ticker") or "").upper()
        if s not in out:
            continue
        if it.get("kind") == "filing":
            for item in it.get("items", []):
                if item in BLOCK_ITEMS:
                    out[s][2] = "8-K: " + BLOCK_ITEMS[item]
                    out[s][0] -= 3
                    out[s][1].append("8-K: " + BLOCK_ITEMS[item])
                elif item in NOTE_ITEMS:
                    out[s][1].append("8-K: " + NOTE_ITEMS[item])
            continue
        text = "%s %s" % (it.get("title", ""), it.get("summary", ""))
        if not pats[s].search(text) or (s, it.get("title")) in seen_titles:
            continue
        seen_titles.add((s, it.get("title")))
        tone = len(POSITIVE.findall(it.get("title", ""))) - len(NEGATIVE.findall(it.get("title", "")))
        out[s][3].append({"title": it.get("title"), "time": it.get("time"), "link": it.get("link"),
                          "tone": tone})
        out[s][0] += clip(tone, -1, 1)
    for s, row in out.items():
        n, pos, neg = len(row[3]), sum(1 for x in row[3] if x["tone"] > 0), sum(1 for x in row[3] if x["tone"] < 0)
        if n:
            row[1].insert(0, "%d Schlagzeilen: %d positiv, %d negativ" % (n, pos, neg))
        if row[2] is None:
            row[0] = clip(row[0], -2, 2)
    return out


def market_regime(errors):
    reg = {"mode": "normal", "why": []}
    try:
        spy = daily("SPY")
        c = spy["c"][:-1] + [spy["price"]]
        s50, s200 = sma(c, 50), sma(c, 200)
        reg.update(spy=spy["price"], spy_sma50=s50, spy_sma200=s200)
        if s200 and spy["price"] < s200:
            reg["mode"] = "risk-off"
            reg["why"].append("S&P 500 unter 200-Tage-Linie")
        elif s50 and spy["price"] < s50:
            reg["mode"] = "vorsichtig"
            reg["why"].append("S&P 500 unter 50-Tage-Linie")
        else:
            reg["why"].append("S&P 500 im Aufwärtstrend")
    except Exception as e:
        errors.append("SPY: %s" % e)
    try:
        vix = daily("^VIX")["price"]
        reg["vix"] = vix
        if vix >= 30:
            reg["mode"] = "risk-off"
            reg["why"].append("VIX %.1f: Panik" % vix)
        elif vix >= 22:
            if reg["mode"] == "normal":
                reg["mode"] = "vorsichtig"
            reg["why"].append("VIX %.1f: nervös" % vix)
        else:
            reg["why"].append("VIX %.1f: ruhig" % vix)
    except Exception as e:
        errors.append("VIX: %s" % e)
    return reg


# ---------------------------------------------------------------- Depot

def load_portfolio(cfg):
    return read_json(os.path.join(SIM, "portfolio.json"),
                     {"cash": float(cfg["start_cash"]), "start_cash": float(cfg["start_cash"]),
                      "started": now_utc().isoformat(timespec="seconds"), "positions": {}})


def record_trade(stamp, action, sym, shares, price, reason, pnl=None):
    path = os.path.join(SIM, "trades.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["zeit", "aktion", "ticker", "stück", "kurs", "wert", "gewinn", "grund"])
        w.writerow([stamp, action, sym, "%.4f" % shares, "%.2f" % price, "%.2f" % (shares * price),
                    "" if pnl is None else "%.2f" % pnl, reason])


def run(cfg, force=False):
    os.makedirs(SIM, exist_ok=True)
    stamp = now_utc().isoformat(timespec="seconds")
    alerts = load_skill("trader-alerts/alerts.py", "alerts")
    status = alerts.market_status(now_utc())
    write_json(os.path.join(SIM, "status.json"), {"checked": stamp, "market": status, "forced": force})
    if not status["open"] and not force:
        log("Börse zu (%s, New York %s). Nächste Öffnung: %s. Nichts zu tun."
            % (status["reason"], status["now_new_york"], status.get("next_open")))
        return

    errors = []
    inv = investor_signals(cfg, errors)
    cbuy, csell = congress_signals(cfg, errors)
    regime = market_regime(errors)
    pf = load_portfolio(cfg)

    universe = sorted(set(inv) | set(cbuy) | set(csell) | set(cfg["watchlist"]) | set(pf["positions"]))
    log("Analysiere %d Ticker ..." % len(universe))
    data, intr = {}, {}
    for i, sym in enumerate(universe):
        if i:
            time.sleep(0.4)
        try:
            data[sym] = daily(sym)
        except (urllib.error.URLError, RuntimeError, KeyError, IndexError, TypeError, ValueError) as e:
            errors.append("Kurs %s: %s" % (sym, e))
            continue
        try:
            intr[sym] = intraday(sym)
        except (urllib.error.URLError, KeyError, IndexError, TypeError, ValueError):
            intr[sym] = []
    news = news_scores(list(data), {s: d["name"] for s, d in data.items()}, cfg, errors)

    rows = []
    for sym, d in data.items():
        try:
            cat, ind = analyse(sym, d, intr.get(sym), status["open"])
        except (ZeroDivisionError, IndexError, ValueError, TypeError) as e:
            errors.append("Analyse %s: %s" % (sym, e))
            continue
        for src, pts in ((inv, 2), (cbuy, 2), (csell, -1)):
            if sym in src:
                cat["Signale"][0] += pts
                cat["Signale"][1] += src[sym]
        n = news[sym]
        cat["News"][0] += n[0]
        cat["News"][1] += n[1]
        scores = {k: v[0] for k, v in cat.items()}
        rows.append({"sym": sym, "name": d["name"], "price": d["price"], "total": sum(scores.values()),
                     "scores": scores, "reasons": {k: v[1] for k, v in cat.items()}, "ind": ind,
                     "block": n[2], "headlines": n[3][:5]})
    rows.sort(key=lambda r: -r["total"])
    by_sym = {r["sym"]: r for r in rows}

    # Verkäufe zuerst
    trades = []
    for sym, pos in list(pf["positions"].items()):
        r = by_sym.get(sym)
        if not r:
            continue
        price = r["price"]
        pos["peak"] = max(pos.get("peak", pos["entry"]), price)
        a = pos.get("atr") or 0
        stop = max(pos["entry"] * (1 - cfg["stop_loss_pct"] / 100), pos["entry"] - cfg["atr_stop"] * a)
        trail = pos["peak"] - cfg["trailing_atr"] * a if a else 0
        why = None
        if price <= stop:
            why = "Stop-Loss bei %.2f" % stop
        elif trail and price <= trail and pos["peak"] > pos["entry"]:
            why = "Trailing-Stop bei %.2f (Hoch %.2f)" % (trail, pos["peak"])
        elif r["block"]:
            why = "Warnsignal: %s" % r["block"]
        elif r["total"] <= cfg["sell_score"]:
            weak = [k for k, s in r["scores"].items() if s < 0]
            why = "Score %d, schwach bei: %s" % (r["total"], ", ".join(weak))
        if why:
            pnl = pos["shares"] * (price - pos["entry"])
            pf["cash"] += pos["shares"] * price
            record_trade(stamp, "VERKAUF", sym, pos["shares"], price, why, pnl)
            trades.append({"action": "VERKAUF", "sym": sym, "shares": pos["shares"], "price": price,
                           "value": pos["shares"] * price, "pnl": pnl, "why": why})
            del pf["positions"][sym]

    # Käufe nach Score
    equity = pf["cash"] + sum(p["shares"] * by_sym[s]["price"] if s in by_sym else p["shares"] * p["entry"]
                              for s, p in pf["positions"].items())
    buffer = equity * cfg["cash_buffer_pct"] / 100
    target = equity * cfg["max_position_pct"] / 100
    if regime["mode"] == "vorsichtig":
        target /= 2
    for r in rows:
        if regime["mode"] == "risk-off":
            break
        sc = r["scores"]
        if (r["total"] < cfg["buy_score"] or sc["Trend"] < 1 or sc["Volumen"] < 0 or r["block"]
                or r["sym"] in pf["positions"]):
            continue
        amount = min(target, pf["cash"] - buffer)
        if amount < cfg["min_order"]:
            break
        shares = amount / r["price"]
        pf["cash"] -= amount
        pf["positions"][r["sym"]] = {"shares": shares, "entry": r["price"], "peak": r["price"],
                                     "atr": r["ind"].get("atr"), "bought": stamp, "name": r["name"]}
        strong = ["%s %+d" % (k, s) for k, s in sc.items() if s]
        why = "Score %d (%s)" % (r["total"], ", ".join(strong))
        record_trade(stamp, "KAUF", r["sym"], shares, r["price"], why)
        trades.append({"action": "KAUF", "sym": r["sym"], "shares": shares, "price": r["price"],
                       "value": amount, "why": why})

    write_json(os.path.join(SIM, "portfolio.json"), pf)
    equity = pf["cash"] + sum(p["shares"] * by_sym.get(s, {"price": p["entry"]})["price"]
                              for s, p in pf["positions"].items())
    eq_path = os.path.join(SIM, "equity.csv")
    new = not os.path.exists(eq_path)
    with open(eq_path, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["zeit", "depotwert", "cash", "positionen", "spy"])
        w.writerow([stamp, "%.2f" % equity, "%.2f" % pf["cash"], len(pf["positions"]),
                    "%.2f" % regime["spy"] if regime.get("spy") else ""])

    write_json(os.path.join(SIM, "analysis.json"), {
        "time": stamp, "market": status, "regime": regime, "equity": equity, "cash": pf["cash"],
        "start_cash": pf["start_cash"], "trades": trades, "rows": rows, "errors": errors})
    report(stamp, status, regime, pf, equity, rows, trades, errors)


def report(stamp, status, regime, pf, equity, rows, trades, errors):
    perf = (equity / pf["start_cash"] - 1) * 100
    L = ["# Bot-Bericht %s" % stamp, "",
         "Börse: %s (New York %s)" % ("offen" if status["open"] else "zu, Lauf erzwungen", status["now_new_york"]),
         "Marktlage: **%s** (%s)" % (regime["mode"], "; ".join(regime["why"])),
         "", "**Depotwert: %.2f $** (Start %.2f $, %+.2f %%), Cash %.2f $" % (
             equity, pf["start_cash"], perf, pf["cash"]), "", "## Orders in diesem Lauf", ""]
    for t in trades:
        extra = " (%+.2f $)" % t["pnl"] if "pnl" in t else ""
        L.append("- %s %s %.2f Stk. @ %.2f = %.2f $%s — %s" % (
            t["action"], t["sym"], t["shares"], t["price"], t["value"], extra, t["why"]))
    if not trades:
        L.append("- keine")
    L += ["", "## Positionen", "", "| Ticker | Stück | Einstand | Kurs | Wert | G/V |", "|---|---|---|---|---|---|"]
    by_sym = {r["sym"]: r for r in rows}
    for s, p in sorted(pf["positions"].items()):
        price = by_sym.get(s, {"price": p["entry"]})["price"]
        L.append("| %s | %.2f | %.2f | %.2f | %.2f | %+.1f %% |" % (
            s, p["shares"], p["entry"], price, p["shares"] * price, (price / p["entry"] - 1) * 100))
    if not pf["positions"]:
        L.append("| – | | | | | |")
    L += ["", "## Rangliste", "",
          "| Ticker | Gesamt | " + " | ".join(CATEGORIES) + " |", "|---" * (len(CATEGORIES) + 2) + "|"]
    for r in rows:
        L.append("| %s | **%d** | %s |" % (r["sym"], r["total"], " | ".join("%+d" % r["scores"][k] for k in CATEGORIES)))
    L += ["", "## Details", ""]
    for r in rows[:8]:
        L.append("**%s** (%s) %.2f $, Score %d%s" % (r["sym"], r["name"], r["price"], r["total"],
                                                     " — GESPERRT: " + r["block"] if r["block"] else ""))
        for k in CATEGORIES:
            if r["reasons"][k]:
                L.append("- %s %+d: %s" % (k, r["scores"][k], "; ".join(r["reasons"][k])))
        for hl in r["headlines"][:3]:
            L.append("  - %s %s" % ({1: "▲", -1: "▼"}.get(clip(hl["tone"], -1, 1), "•"), hl["title"]))
        L.append("")
    if errors:
        L += ["## Hinweise", ""] + ["- " + e for e in errors] + [""]
    L += ["_Simulation mit verzögerten Kursen. Keine Anlageberatung._", ""]
    text = "\n".join(L)
    with open(os.path.join(SIM, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    log(text)


def main():
    ap = argparse.ArgumentParser(description="Simulations-Trading-Bot")
    ap.add_argument("--force", action="store_true", help="auch laufen, wenn die Börse zu ist")
    ap.add_argument("--reset", action="store_true", help="virtuelles Depot neu starten")
    a = ap.parse_args()
    if not os.path.exists(CONFIG):
        write_json(CONFIG, DEFAULTS)
        log("Neue Einstellungen angelegt: %s" % CONFIG)
    cfg = dict(DEFAULTS, **read_json(CONFIG, {}))
    if a.reset:
        for f in ("portfolio.json", "trades.csv", "equity.csv", "report.md", "analysis.json"):
            try:
                os.remove(os.path.join(SIM, f))
            except OSError:
                pass
        log("Virtuelles Depot zurückgesetzt.")
        return
    run(cfg, force=a.force)


if __name__ == "__main__":
    main()
