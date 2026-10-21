#!/usr/bin/env python3
"""
policy.py -- Can we reach the edge WITHOUT modeling m?

Direct policy learning: predict the EXPECTED RETURN of betting the model's pick
from features known at open, then bet only when the prediction clears a
threshold. This never estimates the market price m; it learns which *regions*
historically paid.

Target (training only, uses realised pools): return of betting p's pick =
    +opp/own if the pick won, else -1

Compared against:
  A      : bet every pick (the shipped baseline)
  oracle : bet the pick only when TRUE EV > 0 (uses the real m) -- skip-ceiling

Walk-forward: expanding window, fit on past, act on the next block.
"""

from __future__ import annotations

import math
import sqlite3

from backtest import DB, load_joined, ols


def feats(r, with_meta=True):
    p = r.p_red / 100.0
    lp = math.log(p / (1 - p)) if 0 < p < 1 else 0.0
    f = [1.0, lp, abs(p - 0.5)]
    if with_meta:
        f += [1.0 if r.tier == t else 0.0 for t in ("S", "A", "B", "P")]
        f += [1.0 if (r.match_format or "") == "tournament" else 0.0]
        f += [1.0 if r.confidence == "high" else 0.0,
              1.0 if r.confidence == "medium" else 0.0]
    return f


def pick_return(r):
    """Realised return of betting the model's pick (needs a valid side)."""
    if r.predicted not in ("red", "blue"):
        return None
    own = r.bet_red if r.predicted == "red" else r.bet_blue
    opp = r.bet_blue if r.predicted == "red" else r.bet_red
    return (opp / own) if r.winner_color == r.predicted else -1.0


def true_ev_pick(r):
    """TRUE EV of betting the model's pick, using the real pool (oracle)."""
    if r.predicted == "red":
        m, p = r.bet_red / (r.bet_red + r.bet_blue), r.p_red / 100.0
    elif r.predicted == "blue":
        m, p = r.bet_blue / (r.bet_red + r.bet_blue), r.p_blue / 100.0
    else:
        return None
    return (p - m) / m


def stats(rets, name):
    n = len(rets)
    if n < 2:
        print(f"  {name}: n={n}")
        return
    mean = sum(rets) / n
    var = sum((x - mean) ** 2 for x in rets) / (n - 1)
    se = math.sqrt(var / n)
    print(f"  {name:<42} n={n:>6} ROI={mean:+.2%} +/- {1.96*se:.2%} t={mean/se:+.2f}")


def main():
    con = sqlite3.connect(DB)
    rows = [r for r in load_joined(con, live_only=True) if not r.abstain]
    n = len(rows)
    start = int(n * 0.4)
    step = max(200, int(n * 0.1))
    print(f"Policy learning | n={n} | expanding window, step={step}")
    print(f"  {'test window':>23} {'n':>5} | {'policy n':>8} {'ROI':>8} | {'A ROI':>8} | {'oracle-skip ROI':>15}")

    pol = {"0%": [], "2%": [], "5%": []}
    a_all, orc = [], []
    for ts in range(start, n, step):
        train, test = rows[:ts], rows[ts:ts + step]
        if len(train) < 200 or len(test) < 100:
            continue
        ytr = [pick_return(r) for r in train if pick_return(r) is not None]
        Xtr = [feats(r) for r in train if pick_return(r) is not None]
        beta = ols(Xtr, ytr)

        pf = {"0%": [0.0, 0], "2%": [0.0, 0], "5%": [0.0, 0]}
        af = [0.0, 0]; of = [0.0, 0]
        for r in test:
            ret = pick_return(r)
            if ret is None:
                continue
            pred = sum(b * x for b, x in zip(beta, feats(r)))
            af[0] += ret; af[1] += 1; a_all.append(ret)
            for thr, key in ((0.0, "0%"), (0.02, "2%"), (0.05, "5%")):
                if pred > thr:
                    pf[key][0] += ret; pf[key][1] += 1; pol[key].append(ret)
            ev = true_ev_pick(r)
            if ev is not None and ev > 0:      # oracle skip: know the price
                of[0] += ret; of[1] += 1; orc.append(ret)
        line = f"  {test[0].date[:10]:>10}..{test[-1].date[:10]:<10} {len(test):>5} |"
        for key in ("0%", "2%", "5%"):
            v = pf[key][0] / pf[key][1] if pf[key][1] else 0.0
            line += f" {pf[key][1]:>8} {v:>+8.2%} |" if key == "0%" else f" {v:>+8.2%} |"
        line += f" {af[0]/af[1]:>+8.2%} | {of[0]/of[1] if of[1] else 0:>+15.2%}"
        print(line)

    print("\nPooled out-of-sample:")
    for key in ("0%", "2%", "5%"):
        stats(pol[key], f"Policy: bet pick if predicted ret > {key}")
    stats(a_all, "Strategy A: bet every pick")
    stats(orc, "Oracle: bet pick only if TRUE EV > 0")


if __name__ == "__main__":
    main()
