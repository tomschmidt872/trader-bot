#!/usr/bin/env python3
"""Backtest des Event-Traders: Kurssprünge nach Quartalszahlen, long und short, mit Hebel.

Ereignis = 8-K mit Item 2.02 (Quartalszahlen) laut SEC, mit Uhrzeit.
Reaktionstag R = erster Handelstag, an dem der Markt die Zahlen sieht
(vor 9:30 New York -> selber Tag, danach -> nächster Handelstag).

Signal am Ende von Tag R:
  long   Kurslücke >= g, Kurs hält sich (Schluss >= Eröffnung), Volumen >= 1,5x
  short  Kurslücke <= -g, Kurs bleibt schwach (Schluss <= Eröffnung), Volumen >= 1,5x
Einstieg zum Schlusskurs von R, Ausstieg nach H Tagen zum Schlusskurs oder am
Stop (s x ATR, bei Kurslücke zum Eröffnungskurs).

Depot: N gleich große Slots, jeder mit Hebel L. Kosten 0,1 % pro Order,
Kreditzins 8 % p. a. auf geliehenes Geld, Leihgebühr 1 % p. a. für Shorts.
Fällt das Eigenkapital unter 25 % des Engagements -> Zwangsverkauf (Margin Call).
Fällt es auf 0 -> Depot vernichtet.
"""

import argparse
import datetime
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as bt  # noqa: E402
import bot  # noqa: E402
import insider  # noqa: E402

COST, RATE, BORROW, MAINT = 0.001, 0.08, 0.01, 0.25


def earnings_dates(syms, ua):
    path = os.path.join(bt.CACHE, "earnings.json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 86400 * 3:
        return json.load(open(path))
    sec = insider.Sec(ua)
    t2c, _ = insider.ticker_maps(sec, bt.CACHE, [])
    out = {}
    for s in syms:
        cik = t2c.get(s.replace("-", "."))
        if not cik:
            continue
        try:
            r = sec.json("https://data.sec.gov/submissions/CIK%010d.json" % int(cik))["filings"]["recent"]
        except Exception as e:
            print("  %s: %s" % (s, e), file=sys.stderr)
            continue
        out[s] = sorted({a for f, a, i in zip(r["form"], r["acceptanceDateTime"], r["items"])
                         if f in ("8-K", "8-K/A") and "2.02" in (i or "")})
    json.dump(out, open(path, "w"))
    return out


def reaction_day(accepted_utc, days):
    """Index des Reaktionstags in der Liste der Handelstage (YYYY-MM-DD)."""
    alerts = bot.load_skill("trader-alerts/alerts.py", "alerts")
    t = datetime.datetime.strptime(accepted_utc[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    et = alerts.to_eastern(t)
    d = et.date().isoformat()
    after_open = (et.hour, et.minute) >= (9, 30)
    for i, x in enumerate(days):
        if x > d or (x == d and not after_open):
            return i
    return None


def build_events(hist, earn):
    ev = []
    for s, acc in earn.items():
        rows = hist.get(s)
        if not rows or len(rows) < 40:
            continue
        days = [bt.day(r[0]) for r in rows]
        o = [r[1] for r in rows]; h = [r[2] for r in rows]; l = [r[3] for r in rows]
        c = [r[4] for r in rows]; v = [r[5] for r in rows]
        for a in acc:
            R = reaction_day(a, days)
            if R is None or R < 21 or R >= len(rows) - 1:
                continue
            avg = sum(v[R - 20:R]) / 20
            trs = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(R - 14, R)]
            ev.append({"s": s, "R": R, "day": days[R], "gap": o[R] / c[R - 1] - 1, "intra": c[R] / o[R] - 1,
                       "rvol": v[R] / avg if avg else 0, "atr": sum(trs) / 14})
    return ev


def simulate(hist, events, p, start, end="9999"):
    bars = {s: rows for s, rows in hist.items()}
    idx = {s: {bt.day(r[0]): i for i, r in enumerate(rows)} for s, rows in hist.items()}
    days = [bt.day(r[0]) for r in hist["SPY"] if start <= bt.day(r[0]) < end]
    by_day = {}
    for e in events:
        if start <= e["day"] < end:
            if e["gap"] >= p["g"] and e["intra"] >= 0 and e["rvol"] >= 1.5:
                by_day.setdefault(e["day"], []).append((e, 1))
            elif p["short"] and e["gap"] <= -p["g"] and e["intra"] <= 0 and e["rvol"] >= 1.5:
                by_day.setdefault(e["day"], []).append((e, -1))
    eq, pos, curve, trades, dead, calls = 1000.0, [], [], [], False, 0
    for d in days:
        # Positionen bewerten, Stops, Ablauf
        for ps in list(pos):
            i = idx[ps["s"]].get(d)
            if i is None:
                continue
            o, h, l, c = bars[ps["s"]][i][1:5]
            side = ps["side"]
            px = None
            if ps["stop"]:
                if side > 0 and l <= ps["stop"]:
                    px = min(o, ps["stop"])
                elif side < 0 and h >= ps["stop"]:
                    px = max(o, ps["stop"])
            if px is None and i - ps["i0"] >= p["H"]:
                px = c
            mark = px if px is not None else c
            ps["pnl"] = side * ps["sh"] * (mark - ps["entry"])
            if px is not None:
                eq += ps["pnl"] - ps["sh"] * px * COST
                trades.append(ps["pnl"])
                pos.remove(ps)
        # Finanzierungskosten pro Tag
        for ps in pos:
            gross = ps["sh"] * ps["entry"]
            eq -= max(0, gross - ps["margin"]) * RATE / 252 + (gross * BORROW / 252 if ps["side"] < 0 else 0)
        open_pnl = sum(ps["pnl"] for ps in pos)
        gross = sum(ps["sh"] * ps["entry"] for ps in pos)
        if eq + open_pnl <= 0:
            dead = True
            curve.append(0.0)
            break
        if gross and (eq + open_pnl) < MAINT * gross:   # Margin Call: alles schließen
            calls += 1
            eq += open_pnl - gross * COST
            trades += [ps["pnl"] for ps in pos]
            pos = []
            if eq <= 0:
                dead = True
                curve.append(0.0)
                break
        # neue Signale zum Schluss
        free = p["N"] - len(pos)
        cands = sorted(by_day.get(d, []), key=lambda x: -abs(x[0]["gap"]) * x[0]["rvol"])
        for e, side in cands[:max(0, free)]:
            i = e["R"]
            c = bars[e["s"]][i][4]
            margin = (eq + sum(ps["pnl"] for ps in pos)) / p["N"]
            if margin <= 0:
                break
            sh = margin * p["L"] / c
            eq -= sh * c * COST
            pos.append({"s": e["s"], "side": side, "sh": sh, "entry": c, "i0": i, "pnl": 0.0, "margin": margin,
                        "stop": c - side * p["stop"] * e["atr"] if p["stop"] else None})
        curve.append(eq + sum(ps["pnl"] for ps in pos))
    peak, mdd = 0, 0
    for x in curve:
        peak = max(peak, x)
        mdd = min(mdd, x / peak - 1 if peak else 0)
    years = len(curve) / 252 or 1
    final = curve[-1] if curve else 1000
    return {"ret": (final / 1000 - 1) * 100, "cagr": ((max(final, 0) / 1000) ** (1 / years) - 1) * 100,
            "mdd": mdd * 100, "trades": len(trades), "win": sum(1 for t in trades if t > 0) / len(trades) * 100 if trades else 0,
            "dead": dead, "calls": calls, "final": final}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--write")
    a = ap.parse_args()
    cfg = json.load(open(os.path.join(os.path.dirname(bt.HERE), "trader", "bot.json")))
    syms = sorted(set(bot.norm(s) for s in cfg["universe"])) + ["SPY"]
    print("Lade Kurse (%d Jahre) und Quartalszahlen-Termine ..." % a.years, file=sys.stderr)
    hist = bt.load_history(syms, a.years)
    earn = earnings_dates([s for s in syms if s != "SPY"], bot.sec_user_agent())
    events = build_events(hist, earn)
    spy = hist["SPY"]
    days = [bt.day(r[0]) for r in spy]
    start = days[25]
    print("%d Quartalszahlen-Ereignisse, %s bis %s" % (len(events), start, days[-1]), file=sys.stderr)
    years = sorted({d[:4] for d in days if d >= start})
    res = []
    for g in (0.03, 0.05, 0.08):
        for H in (1, 3, 5, 10, 20):
            for stop in (0, 2, 3):
                for short in (True, False):
                    p = {"g": g, "H": H, "stop": stop, "short": short, "N": 5, "L": 1}
                    yr = [simulate(hist, events, p, y + "-01-01", str(int(y) + 1) + "-01-01")["ret"] for y in years]
                    full = simulate(hist, events, p, start)
                    res.append((statistics.median(yr), min(yr), full, p, yr))
    res.sort(key=lambda r: (r[0], r[1]), reverse=True)
    s0 = spy[25][4]
    spy_y = []
    for y in years:
        rs = [r[4] for r in spy if bt.day(r[0])[:4] == y]
        spy_y.append((rs[-1] / rs[0] - 1) * 100)
    print("\nS&P 500 halten: %+.0f %% gesamt, Jahre %s" % ((spy[-1][4] / s0 - 1) * 100, " ".join("%+.0f" % x for x in spy_y)))
    print("\nBeste Varianten OHNE Hebel (sortiert nach typischem Jahr):")
    print("%-34s %8s %8s %8s %7s %6s  Jahre" % ("Variante", "Median/J", "schlecht", "Gesamt", "MaxDD", "Trades"))
    for med, worst, full, p, yr in res[:10]:
        print("%-34s %+7.1f%% %+7.1f%% %+7.0f%% %+6.0f%% %6d  %s" % (
            "Lücke %d%%, %dT halten, Stop %s, %s" % (p["g"] * 100, p["H"], p["stop"] or "–", "L+S" if p["short"] else "nur L"),
            med, worst, full["ret"], full["mdd"], full["trades"], " ".join("%+.0f" % x for x in yr)))
    best = res[0][3]
    print("\nHebel mit der besten Variante (%s):" % best)
    lev = []
    for L in (1, 2, 3, 5, 7, 10):
        p = dict(best, L=L)
        f = simulate(hist, events, p, start)
        yr = [simulate(hist, events, p, y + "-01-01", str(int(y) + 1) + "-01-01") for y in years]
        lev.append((L, f, yr))
        print("  %2dx  gesamt %+9.0f %%  CAGR %+6.1f %%  MaxDD %+6.0f %%  Margin Calls %d  %s  Jahre %s" % (
            L, f["ret"], f["cagr"], f["mdd"], f["calls"], "VERNICHTET" if f["dead"] else "",
            " ".join(("%+.0f" % y["ret"]) + ("†" if y["dead"] else "") for y in yr)))
    if a.write:
        json.dump({"generated": datetime.date.today().isoformat(), "start": start, "end": days[-1], "events": len(events),
                   "years": years, "spy_years": spy_y, "spy_total": (spy[-1][4] / s0 - 1) * 100,
                   "best": best, "top": [{"p": p, "median": m, "worst": w, "full": f, "years": y} for m, w, f, p, y in res[:10]],
                   "leverage": [{"L": L, "full": f, "years": [x["ret"] for x in yr], "dead_years": [x["dead"] for x in yr]}
                                for L, f, yr in lev]},
                  open(a.write, "w"), indent=1)


if __name__ == "__main__":
    main()
