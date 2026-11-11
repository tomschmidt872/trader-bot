#!/usr/bin/env python3
"""Event-Bot: kurzfristiges Trading mit Hebel, long und short. Nur Spielgeld.

Läuft direkt nach bot.py und nutzt dessen frische Analyse (trader/sim/analysis.json)
und Ereignisse (trader/sim/events.json).

Einstieg (alle Bedingungen):
  - Auslöser in den letzten 24 Std.: Quartalszahlen (8-K), starke News, Insider-Kauf,
    Kongress-/Personen-Meldung – oder ein Kurssprung mit doppeltem Volumen
  - Kurs heute mindestens +/- move_pct % zum Vortag
  - Kurs über (long) bzw. unter (short) dem Tagesdurchschnitt VWAP
  - Ausbruch über das Hoch (long) bzw. unter das Tief (short) der ersten 30 Minuten
  - relatives Volumen mindestens 1,5x
  - nicht in der ersten und nicht in der letzten Stunde

Ausstieg (eine Regel reicht):
  - Stop: stop_atr x ATR gegen die Position
  - Ziel: target_atr x ATR in Richtung der Position
  - Break-even: ab +breakeven_atr x ATR wird der Stop auf den Einstieg gezogen
  - Zeit: nach max_hold_hours Stunden, spätestens im letzten Lauf vor Börsenschluss
  - Margin Call: Eigenkapital unter 25 % des Engagements -> alles schließen

Dateien in trader/sim/event/: portfolio.json, trades.csv, equity.csv, decisions.json
"""

import csv
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402

DIR = os.path.join(bot.SIM, "event")
DEFAULTS = {"start_cash": 1000, "leverage": 5, "slots": 5, "allow_short": True, "move_pct": 4, "min_rvol": 1.5,
            "stop_atr": 0.75, "target_atr": 1.5, "breakeven_atr": 0.75, "max_hold_hours": 4, "cost_pct": 0.1,
            "maintenance": 0.25, "min_dollar_volume": 50000000}
TRIGGER_TYPES = {"filing": "SEC-Meldung", "news": "Schlagzeile", "insider": "Insider", "congress": "Kongress",
                 "person": "Thiel/Trump-Familie"}


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def fmt(x, d=2):
    return ("%%.%df" % d % x).replace(".", ",")


def load(cfg):
    pf = bot.read_json(os.path.join(DIR, "portfolio.json"), None)
    if pf is None:
        pf = {"currency": "EUR", "type": "event", "cash": float(cfg["start_cash"]), "start_cash": float(cfg["start_cash"]),
              "leverage": cfg["leverage"], "started": now().isoformat(timespec="seconds"), "positions": {}, "dead": False}
    return pf


def pos_pnl_eur(p, price, fx):
    return p["side"] * p["shares"] * (price - p["entry"]) / fx


def equity(pf, prices, fx):
    return pf["cash"] + sum(p["margin_eur"] + pos_pnl_eur(p, prices.get(s, p["entry"]), fx) for s, p in pf["positions"].items())


def record(stamp, action, sym, p, price, fx, pnl, why):
    path = os.path.join(DIR, "trades.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["zeit", "aktion", "ticker", "stück", "kurs_usd", "eurusd", "wert_eur", "gewinn_eur", "grund",
                        "richtung", "hebel", "einsatz_eur"])
        w.writerow([stamp, action, sym, "%.6f" % p["shares"], "%.2f" % price, "%.4f" % fx,
                    "%.2f" % (p["shares"] * price / fx), "" if pnl is None else "%.2f" % pnl, why,
                    "Long" if p["side"] > 0 else "Short", p["leverage"], "%.2f" % p["margin_eur"]])


def triggers(events, stamp_dt):
    """{SYM: [(typ, titel)]} aus Ereignissen der letzten 24 Stunden."""
    out = {}
    cut = (stamp_dt - datetime.timedelta(hours=24)).isoformat()
    for e in events:
        t = e.get("time") or e.get("seen") or ""
        if t < cut or e.get("type") not in TRIGGER_TYPES:
            continue
        if e["type"] == "news" and not e.get("tone"):
            continue
        if e["type"] == "filing" and e.get("severity") == "info":
            continue
        out.setdefault(e["sym"], []).append((e["type"], e["title"], e.get("tone") or 0))
    return out


def main():
    cfg = dict(DEFAULTS, **bot.read_json(bot.CONFIG, {}).get("event_bot", {}))
    an = bot.read_json(os.path.join(bot.SIM, "analysis.json"), None)
    if not an:
        print("Event-Bot: keine Analyse vorhanden.")
        return
    stamp_dt = now()
    stamp = stamp_dt.isoformat(timespec="seconds")
    age = (stamp_dt - datetime.datetime.fromisoformat(an["time"])).total_seconds() / 60
    force = "--force" in sys.argv
    if (not an["market"]["open"] or age > 45) and not force:
        print("Event-Bot: Börse zu oder Analyse zu alt (%d Min.). Nichts zu tun." % age)
        return
    os.makedirs(DIR, exist_ok=True)
    pf = load(cfg)
    fx = an["fx"]
    rows = {r["sym"]: r for r in an["rows"]}
    prices = {s: r["price"] for s, r in rows.items()}
    ny = an["market"]["now_new_york"][11:16]
    closes = an["market"].get("closes", "16:00")
    ch, cm = map(int, closes.split(":"))
    nh, nm = map(int, ny.split(":"))
    mins_to_close = (ch * 60 + cm) - (nh * 60 + nm)
    mins_since_open = (nh * 60 + nm) - (9 * 60 + 30)
    trades, decisions = [], []
    costf = cfg["cost_pct"] / 100

    def close(sym, why, kind):
        p = pf["positions"].pop(sym)
        r = rows.get(sym, {})
        price = prices.get(sym, p["entry"])
        pnl = pos_pnl_eur(p, price, fx) - p["shares"] * price / fx * costf
        pf["cash"] += p["margin_eur"] + pnl
        record(stamp, "SCHLIESSEN", sym, p, price, fx, pnl, why)
        hours = (stamp_dt - datetime.datetime.fromisoformat(p["opened"])).total_seconds() / 3600
        story = ("%s-Position in %s nach %s Std. geschlossen: %s. Ergebnis %s € (%s %% auf den Einsatz von %s €, "
                 "mit %dx Hebel). Einstieg %s $, Ausstieg %s $." % (
                     "Long" if p["side"] > 0 else "Short", sym, fmt(hours, 1), why, fmt(pnl), fmt(pnl / p["margin_eur"] * 100, 1),
                     fmt(p["margin_eur"]), p["leverage"], fmt(p["entry"]), fmt(price)))
        t = {"action": "SCHLIESSEN", "sym": sym, "side": p["side"], "shares": p["shares"], "price": price,
             "value": p["margin_eur"], "pnl": pnl, "why": why}
        trades.append(t)
        decisions.append(dict(t, id="%s:SCHLIESSEN:%s" % (stamp, sym), time=stamp, kind=kind, bot="event", story=story,
                              name=p.get("name"), entry=p["entry"], bought=p["opened"], leverage=p["leverage"],
                              pnl_pct=pnl / p["margin_eur"] * 100, levels={"stop": p["stop"], "target": p["target"]},
                              total=r.get("total", 0), scores=r.get("scores", {}), reasons=r.get("reasons", {}),
                              ind=r.get("ind", {}), headlines=r.get("headlines", []), checks=p.get("checks", []),
                              trigger=p.get("trigger")))

    # 1. offene Positionen prüfen
    for sym, p in list(pf["positions"].items()):
        price = prices.get(sym)
        if price is None:
            continue
        a = p["atr"]
        fav = p["side"] * (price - p["entry"])
        if fav >= cfg["breakeven_atr"] * a and p["side"] * (p["stop"] - p["entry"]) < 0:
            p["stop"] = p["entry"]
            p["breakeven"] = True
        hours = (stamp_dt - datetime.datetime.fromisoformat(p["opened"])).total_seconds() / 3600
        if p["side"] * (price - p["stop"]) <= 0:
            close(sym, "Stop bei %s $ erreicht%s" % (fmt(p["stop"]), " (Break-even)" if p.get("breakeven") else ""),
                  "stop")
        elif p["side"] * (price - p["target"]) >= 0:
            close(sym, "Kursziel %s $ erreicht" % fmt(p["target"]), "target")
        elif hours >= cfg["max_hold_hours"] - 0.1:
            close(sym, "maximale Haltedauer von %d Std. erreicht" % cfg["max_hold_hours"], "time")
        elif mins_to_close <= 60:
            close(sym, "Börsenschluss naht – keine Position über Nacht", "time")

    # 2. Margin Call
    eq = equity(pf, prices, fx)
    gross = sum(p["shares"] * prices.get(s, p["entry"]) / fx for s, p in pf["positions"].items())
    if gross and eq < cfg["maintenance"] * gross:
        for sym in list(pf["positions"]):
            close(sym, "MARGIN CALL: Eigenkapital unter %d %% des Engagements – Zwangsverkauf" % (cfg["maintenance"] * 100),
                  "margin")
    eq = equity(pf, prices, fx)
    if eq <= 0:
        pf["dead"] = True

    # 3. neue Einstiege
    trig = triggers(bot.read_json(os.path.join(bot.SIM, "events.json"), []), stamp_dt)
    cands = []
    if not pf.get("dead") and 60 <= mins_since_open and mins_to_close > 75:
        for sym, r in rows.items():
            if sym in pf["positions"] or sym in ("SPY", "QQQ"):
                continue
            ind = r.get("ind", {})
            chg, rvol, vwap = ind.get("chg"), ind.get("rvol") or 0, ind.get("vwap")
            orh, orl, atr = ind.get("or_high"), ind.get("or_low"), ind.get("atr")
            if chg is None or not vwap or not orh or not atr:
                continue
            side = 1 if chg > 0 else -1
            if side < 0 and not cfg["allow_short"]:
                continue
            tg = [t for t in trig.get(sym, []) if t[2] * side >= 0]   # Meldung darf nicht gegen die Richtung sprechen
            if not tg and rvol >= 2 and abs(chg) >= cfg["move_pct"]:
                tg = [("move", "Kurssprung %s %% mit %sx Volumen ohne bekannte Meldung" % (fmt(chg, 1), fmt(rvol, 1)), side)]
            price = r["price"]
            checks = [
                {"rule": "Auslöser in den letzten 24 Std.", "ok": bool(tg), "val": tg[0][1][:80] if tg else "keiner"},
                {"rule": "Kursbewegung heute mindestens %s %%" % fmt(cfg["move_pct"], 0), "ok": abs(chg) >= cfg["move_pct"],
                 "val": "%s %%" % fmt(chg, 1)},
                {"rule": "Kurs %s Tagesdurchschnitt (VWAP)" % ("über" if side > 0 else "unter"),
                 "ok": side * (price - vwap) > 0, "val": "%s $ vs. %s $" % (fmt(price), fmt(vwap))},
                {"rule": "Ausbruch %s der ersten 30 Min." % ("über das Hoch" if side > 0 else "unter das Tief"),
                 "ok": (price > orh) if side > 0 else (price < orl),
                 "val": "%s $" % fmt(orh if side > 0 else orl)},
                {"rule": "Volumen mindestens %sx normal" % fmt(cfg["min_rvol"], 1), "ok": rvol >= cfg["min_rvol"],
                 "val": "%sx" % fmt(rvol, 1)},
                {"rule": "Genug Handel (mind. %d Mio. $ pro Tag)" % (cfg["min_dollar_volume"] / 1e6),
                 "ok": (ind.get("dollar_vol") or 0) >= cfg["min_dollar_volume"],
                 "val": "%d Mio. $" % ((ind.get("dollar_vol") or 0) / 1e6)},
                {"rule": "Keine Warnmeldung der SEC", "ok": not r.get("block"), "val": r.get("block") or "keine"},
            ]
            if all(c["ok"] for c in checks):
                cands.append((abs(chg) * rvol, sym, side, tg, checks))
    for _, sym, side, tg, checks in sorted(cands, reverse=True):
        if len(pf["positions"]) >= cfg["slots"]:
            break
        eq = equity(pf, prices, fx)
        margin = min(eq / cfg["slots"], pf["cash"])
        if margin < 20:
            break
        r = rows[sym]
        price, atr = r["price"], r["ind"]["atr"]
        shares = margin * cfg["leverage"] * fx / price
        cost = shares * price / fx * costf
        pf["cash"] -= margin + cost
        p = {"side": side, "shares": shares, "entry": price, "entry_fx": fx, "margin_eur": margin, "atr": atr,
             "leverage": cfg["leverage"], "stop": price - side * cfg["stop_atr"] * atr,
             "target": price + side * cfg["target_atr"] * atr, "opened": stamp, "name": r["name"],
             "trigger": [t[1] for t in tg][:3], "checks": checks}
        pf["positions"][sym] = p
        why = "%s: %s" % ("Long" if side > 0 else "Short", "; ".join(p["trigger"]))
        record(stamp, "ERÖFFNEN", sym, p, price, fx, None, why)
        story = ("Der Event-Bot setzt auf %s %s (%s), weil: %s. Heute %s %% mit %sx Volumen, Kurs %s VWAP und %s der "
                 "ersten 30 Minuten. Einsatz %s € mit %dx Hebel = %s € Engagement. Stop bei %s $ (Verlust ca. %s €), "
                 "Ziel bei %s $ (Gewinn ca. %s €). Spätestens nach %d Std. oder vor Börsenschluss wird geschlossen." % (
                     "steigende" if side > 0 else "fallende", r["name"], sym, "; ".join(p["trigger"]),
                     fmt(r["ind"]["chg"], 1), fmt(r["ind"]["rvol"], 1), "über" if side > 0 else "unter",
                     "Ausbruch über das Hoch" if side > 0 else "Ausbruch unter das Tief", fmt(margin), cfg["leverage"],
                     fmt(margin * cfg["leverage"]), fmt(p["stop"]),
                     fmt(shares * cfg["stop_atr"] * atr / fx), fmt(p["target"]), fmt(shares * cfg["target_atr"] * atr / fx),
                     cfg["max_hold_hours"]))
        t = {"action": "ERÖFFNEN", "sym": sym, "side": side, "shares": shares, "price": price, "value": margin, "why": why}
        trades.append(t)
        decisions.append(dict(t, id="%s:ERÖFFNEN:%s" % (stamp, sym), time=stamp, kind="open", bot="event", story=story,
                              name=r["name"], leverage=cfg["leverage"], levels={"stop": p["stop"], "target": p["target"]},
                              total=r["total"], scores=r["scores"], reasons=r["reasons"], ind=r["ind"],
                              headlines=r.get("headlines", []), checks=checks, trigger=p["trigger"]))

    bot.write_json(os.path.join(DIR, "portfolio.json"), pf)
    eq = equity(pf, prices, fx)
    path = os.path.join(DIR, "equity.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["zeit", "depotwert_eur", "cash_eur", "positionen", "spy", "eurusd"])
        w.writerow([stamp, "%.2f" % eq, "%.2f" % pf["cash"], len(pf["positions"]),
                    "%.2f" % an["regime"]["spy"] if an["regime"].get("spy") else "", "%.4f" % fx])
    dpath = os.path.join(DIR, "decisions.json")
    bot.write_json(dpath, (bot.read_json(dpath, []) + decisions)[-800:])
    rules = dict(cfg)
    bot.write_json(os.path.join(DIR, "status.json"), {"time": stamp, "equity": eq, "rules": rules,
                                                      "candidates": len(cands), "dead": pf.get("dead", False)})
    # Bot-Trades auch im gemeinsamen Ereignis-Feed
    epath = os.path.join(bot.SIM, "events.json")
    evs = bot.read_json(epath, [])
    for t in trades:
        evs.insert(0, {"id": "etrade:%s:%s:%s" % (stamp, t["action"], t["sym"]), "type": "trade", "sym": t["sym"],
                       "time": stamp, "seen": stamp, "tone": t["side"] if t["action"] == "ERÖFFNEN" else 0,
                       "title": "Event-Bot: %s %s %s (%dx)%s" % (
                           t["action"].capitalize(), "Long" if t["side"] > 0 else "Short", t["sym"], cfg["leverage"],
                           " – %s €" % fmt(t["pnl"]) if t.get("pnl") is not None else ""),
                       "detail": t["why"]})
    bot.write_json(epath, evs)
    print("Event-Bot: Depot %s €, %d Positionen, %d Trades, %d Kandidaten" % (fmt(eq), len(pf["positions"]), len(trades), len(cands)))
    for t in trades:
        print("  %s %s %s: %s" % (t["action"], "Long" if t["side"] > 0 else "Short", t["sym"], t["why"]))


if __name__ == "__main__":
    main()
