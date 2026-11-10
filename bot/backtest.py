#!/usr/bin/env python3
"""Backtest der technischen Regeln auf Tageskursen.

Getestet wird nur, was sich rückwirkend nachstellen lässt: Trend, Momentum
und Volumen aus bot.analyse(), Marktlage aus S&P 500 und VIX, dazu Stops,
Positionsgröße und Cash-Reserve. Signale (13F, Kongress, Insider), News und
Intraday-Werte fehlen, weil es dafür keine Historie gibt.

Ablauf pro Tag: Analyse mit dem Schlusskurs, Order zum Eröffnungskurs des
nächsten Tages, Stops gegen das Tagestief (bei Kurslücke zum Eröffnungskurs).
Pro Order 0,1 % Kosten/Slippage.

    python3 bot/backtest.py                 # alle Varianten vergleichen
    python3 bot/backtest.py --years 3
"""

import argparse
import json
import os
import pickle
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(os.path.dirname(HERE), "trader", "backtest_cache")
COST = 0.001
WINDOW = 260


def load_history(syms, years):
    os.makedirs(CACHE, exist_ok=True)
    out = {}
    for i, s in enumerate(syms):
        path = os.path.join(CACHE, "%s_%dy.json" % (s.replace("^", "_"), years))
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < 86400:
            with open(path) as fh:
                out[s] = json.load(fh)
            continue
        res = None
        for host in ("query1", "query2", "query1"):
            url = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=%dy&interval=1d" % (
                host, bot.urllib.parse.quote(s.replace(".", "-")), years)
            try:
                req = bot.urllib.request.Request(url, headers={"User-Agent": bot.UA})
                with bot.urllib.request.urlopen(req, timeout=30) as r:
                    res = json.loads(r.read().decode())["chart"]["result"][0]
                if res.get("timestamp"):
                    break
            except Exception as e:
                err = e
            res = None
            time.sleep(2)
        if not res:
            print("  %s: keine Daten" % s, file=sys.stderr)
            continue
        q = res["indicators"]["quote"][0]
        rows = [(t, o, h, l, c, v or 0) for t, o, h, l, c, v in zip(res["timestamp"], q["open"], q["high"], q["low"],
                                                                   q["close"], q["volume"]) if None not in (o, h, l, c)]
        out[s] = rows
        with open(path, "w") as fh:
            json.dump(rows, fh)
        time.sleep(0.3)
        if i % 20 == 0:
            print("  Kurse geladen: %d/%d" % (i + 1, len(syms)), file=sys.stderr)
    return out


def day(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def precompute(hist):
    """Pro Ticker und Tag die Punkte (einmal berechnet, für alle Varianten genutzt)."""
    path = os.path.join(CACHE, "scores.pkl")
    key = sorted((s, len(r), r[-1][0]) for s, r in hist.items())
    if os.path.exists(path):
        with open(path, "rb") as fh:
            k, data = pickle.load(fh)
        if k == key:
            return data
    data = {}
    for n, (s, rows) in enumerate(hist.items()):
        if s in ("SPY", "^VIX") or len(rows) < WINDOW + 5:
            continue
        per = {}
        for t in range(WINDOW, len(rows)):
            sl = rows[t - WINDOW + 1:t + 1]
            d = {"price": sl[-1][4], "o": [x[1] for x in sl], "h": [x[2] for x in sl], "l": [x[3] for x in sl],
                 "c": [x[4] for x in sl], "v": [x[5] for x in sl]}
            try:
                cat, ind = bot.analyse(s, d, None, False)
            except (ZeroDivisionError, ValueError, IndexError, TypeError):
                continue
            per[day(rows[t][0])] = ({k: v[0] for k, v in cat.items()}, ind.get("atr"))
        data[s] = per
        if n % 10 == 0:
            print("  Analyse: %d/%d" % (n + 1, len(hist)), file=sys.stderr)
    with open(path, "wb") as fh:
        pickle.dump((key, data), fh)
    return data


def regimes(hist):
    spy = hist["SPY"]
    vix = {day(r[0]): r[4] for r in hist.get("^VIX", [])}
    out, closes = {}, []
    for r in spy:
        closes.append(r[4])
        d = day(r[0])
        s50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None
        s200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else None
        mode = "normal"
        if s200 and r[4] < s200:
            mode = "risk-off"
        elif s50 and r[4] < s50:
            mode = "vorsichtig"
        v = vix.get(d)
        if v is not None:
            if v >= 30:
                mode = "risk-off"
            elif v >= 22 and mode == "normal":
                mode = "vorsichtig"
        out[d] = mode
    return out


def simulate(hist, scores, reg, p, start_day):
    bars = {s: {day(r[0]): r for r in rows} for s, rows in hist.items() if s in scores}
    days = [day(r[0]) for r in hist["SPY"] if day(r[0]) >= start_day]
    cash, pos, trades, curve, cooldown = 1000.0, {}, [], [], {}
    pending = []
    for i, d in enumerate(days):
        # 1. Orders vom Vortag zur Eröffnung ausführen
        for act, s in pending:
            b = bars[s].get(d)
            if not b:
                continue
            if act == "sell" and s in pos:
                ps = pos.pop(s)
                px = b[1] * (1 - COST)
                cash += ps["sh"] * px
                trades.append((ps["sh"] * (px - ps["entry"]), "score"))
                cooldown[s] = i + p["cooldown"]
            elif act == "buy" and s not in pos:
                eq = cash + sum(ps["sh"] * bars[x][d][4] if d in bars[x] else ps["sh"] * ps["last"] for x, ps in pos.items())
                size = eq * p["pos_pct"] / 100 * (0.5 if reg.get(days[i - 1]) == "vorsichtig" else 1)
                amt = min(size, cash - eq * 0.10)
                if amt < 25:
                    continue
                px = b[1] * (1 + COST)
                atr = scores[s].get(days[i - 1], (None, None))[1] or px * 0.02
                pos[s] = {"sh": amt / px, "entry": px, "peak": px, "atr": atr, "day": i, "last": px,
                          "stop": stop_level(px, atr, p)}
                cash -= amt
        pending = []
        # 2. Stops im Tagesverlauf
        for s, ps in list(pos.items()):
            b = bars[s].get(d)
            if not b:
                continue
            o, h, l, c = b[1], b[2], b[3], b[4]
            trail = ps["peak"] - p["trail_atr"] * ps["atr"] if p["trail_atr"] and ps["peak"] > ps["entry"] * (1 + p["trail_after"]) else 0
            lvl = max(ps["stop"], trail)
            if l <= lvl:
                px = min(o, lvl) * (1 - COST)
                cash += ps["sh"] * px
                trades.append((ps["sh"] * (px - ps["entry"]), "trail" if lvl == trail else "stop"))
                pos.pop(s)
                cooldown[s] = i + p["cooldown"]
                continue
            ps["peak"] = max(ps["peak"], h)
            ps["last"] = c
        # 3. Analyse zum Schluss -> Orders für morgen
        mode = reg.get(d, "normal")
        for s, ps in pos.items():
            sc = scores[s].get(d)
            if sc and sum(sc[0].values()) <= p["sell"] and i - ps["day"] >= p["min_hold"]:
                pending.append(("sell", s))
        if mode != "risk-off" or not p["regime"]:
            cands = []
            for s, per in scores.items():
                sc = per.get(d)
                if not sc or s in pos or cooldown.get(s, -1) > i:
                    continue
                sc, _ = sc
                tot = sum(sc.values())
                if tot >= p["buy"] and sc["Trend"] >= p["min_trend"] and sc["Volumen"] >= 0:
                    cands.append((tot, s))
            free = p["max_pos"] - len(pos) + sum(1 for a, _ in pending if a == "sell")
            for _, s in sorted(cands, reverse=True)[:max(0, min(free, p["max_new"]))]:
                pending.append(("buy", s))
        eq = cash + sum(ps["sh"] * ps["last"] for ps in pos.values())
        curve.append(eq)
    peak, mdd = 0, 0
    for e in curve:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
    wins = sum(1 for t in trades if t[0] > 0)
    return {"ret": (curve[-1] / 1000 - 1) * 100, "mdd": mdd * 100, "trades": len(trades),
            "win": wins / len(trades) * 100 if trades else 0, "stops": sum(1 for t in trades if t[1] == "stop"),
            "days": len(days)}


def stop_level(px, atr, p):
    if p["stop_mode"] == "alt":          # bisherige Logik: der ENGERE der beiden Stops
        return max(px * (1 - p["stop_pct"] / 100), px - p["stop_atr"] * atr)
    dist = min(max(p["stop_atr"] * atr, px * p["stop_min"] / 100), px * p["stop_pct"] / 100)
    return px - dist


BASE = {"buy": 4, "min_trend": 1, "sell": -4, "stop_mode": "alt", "stop_pct": 8, "stop_atr": 2.5, "stop_min": 0,
        "trail_atr": 3.5, "trail_after": 0.0, "cooldown": 0, "min_hold": 0, "pos_pct": 15, "max_pos": 6,
        "max_new": 99, "regime": True}

NEW = {"stop_mode": "neu", "stop_atr": 2.5, "stop_min": 5, "stop_pct": 10, "trail_atr": 5, "cooldown": 5, "buy": 6,
       "max_pos": 6, "pos_pct": 15}

VARIANTS = [
    ("Alte Regeln (bis 10.11.)", {}),
    ("Neue Regeln (ab 10.11.)", NEW),
]


def spy_stats(spy, a, b):
    rs = [r[4] for r in spy if a <= day(r[0]) < b]
    peak, mdd = 0, 0
    for x in rs:
        peak = max(peak, x)
        mdd = min(mdd, x / peak - 1)
    return {"ret": (rs[-1] / rs[0] - 1) * 100, "mdd": mdd * 100}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=3, help="Kurshistorie (das erste Jahr dient nur zum Aufwärmen)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write", help="Ergebnis als JSON speichern (fürs Dashboard)")
    a = ap.parse_args()
    cfg = json.load(open(os.path.join(os.path.dirname(HERE), "trader", "bot.json")))
    syms = sorted(set(bot.norm(s) for s in cfg["universe"])) + ["SPY", "^VIX"]
    print("Lade %d Kursreihen ..." % len(syms), file=sys.stderr)
    hist = load_history(syms, a.years)
    scores = precompute(hist)
    reg = regimes(hist)
    spy = hist["SPY"]
    start = day(spy[WINDOW][0])
    mid = day(spy[min(WINDOW + 246, len(spy) - 1)][0])
    end = day(spy[-1][0])
    h1 = {s: [r for r in rows if day(r[0]) < mid] for s, rows in hist.items()}
    periods = [("Gesamt", start, "9999", hist), ("Jahr 1", start, mid, h1), ("Jahr 2", mid, "9999", hist)]
    out = {"generated": day(time.time()), "start": start, "mid": mid, "end": end, "stocks": len(scores), "periods": []}
    for label, a_, b_, h in periods:
        row = {"label": label, "from": a_, "to": end if b_ == "9999" else b_, "spy": spy_stats(spy, a_, b_), "variants": []}
        for name, over in VARIANTS:
            r = simulate(h, scores, reg, dict(BASE, **over), a_)
            r["name"] = name
            row["variants"].append(r)
        out["periods"].append(row)
    if a.write:
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1, ensure_ascii=False)
    if a.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return
    for p in out["periods"]:
        print("\n%s: %s bis %s" % (p["label"], p["from"], p["to"]))
        print("  %-28s %+7.1f %%   max. Rückgang %+6.1f %%" % ("S&P 500 halten", p["spy"]["ret"], p["spy"]["mdd"]))
        for v in p["variants"]:
            print("  %-28s %+7.1f %%   max. Rückgang %+6.1f %%   %d Trades, %.0f %% im Plus" % (
                v["name"], v["ret"], v["mdd"], v["trades"], v["win"]))


if __name__ == "__main__":
    main()
