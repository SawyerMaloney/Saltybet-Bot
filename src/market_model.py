#!/usr/bin/env python3
"""
m_signal.py -- Initial investigation: what predicts the market's implied price m?

m = the crowd's final implied P(red wins) = bet_red/(bet_red+bet_blue).
We build CAUSAL (pre-match only) features over the full 665k match history and
measure how much of logit(m) they explain on a held-out future slice.

Features (all known at open, no leakage):
  elo_diff      : difference in running Elo (reconstructed from winners)
  winrate_diff  : difference in prior win rate
  exp_ratio     : log of prior appearance counts (experience/popularity)
  share_diff    : difference in prior average crowd implied prob (reputation)
  tier dummies, tournament flag

Prediction R^2 is a SCREEN, not the goal (edge lives in the tails).
"""

from __future__ import annotations

import math
import sqlite3

from backtest import DB, ols


def r2(X, y, beta):
    pred = [sum(b * x for b, x in zip(beta, row)) for row in X]
    ybar = sum(y) / len(y)
    ss_res = sum((a - p) ** 2 for a, p in zip(y, pred))
    ss_tot = sum((a - ybar) ** 2 for a in y)
    return 1 - ss_res / ss_tot


def build(con, limit=None):
    rows = con.execute(
        "SELECT date, bet_red, bet_blue, winner, fighter_red, fighter_blue, tier, match_format "
        "FROM boy WHERE bet_red>0 AND bet_blue>0 ORDER BY date"
    ).fetchall()
    elo, apps, wins, share = {}, {}, {}, {}
    feats = []
    for date, br, bb, winner, fr, fb, tier, fmt in rows:
        m = br / (br + bb)
        if not (0.001 < m < 0.999):
            continue
        er, eb = elo.get(fr, 1000.0), elo.get(fb, 1000.0)
        ar, ab = apps.get(fr, 0), apps.get(fb, 0)
        wr = wins.get(fr, 0) / ar if ar else 0.5
        wb = wins.get(fb, 0) / ab if ab else 0.5
        sr = share.get(fr, 0) / ar if ar else 0.5
        sb = share.get(fb, 0) / ab if ab else 0.5
        feats.append((date, math.log(m / (1 - m)), er - eb, wr - wb,
                      math.log((ar + 1) / (ab + 1)), sr - sb, tier, fmt,
                      fr, fb, m))
        # causal updates (after the match)
        exp = 1 / (1 + 10 ** ((eb - er) / 400))
        won = 1.0 if winner == fr else 0.0
        elo[fr] = er + 32 * (won - exp)
        elo[fb] = eb + 32 * ((1 - won) - (1 - exp))
        apps[fr] = ar + 1; apps[fb] = ab + 1
        wins[fr] = wins.get(fr, 0) + won; wins[fb] = wins.get(fb, 0) + (1 - won)
        share[fr] = share.get(fr, 0) + m; share[fb] = share.get(fb, 0) + (1 - m)
    if limit:
        feats = feats[::max(1, len(feats) // limit)][:limit]
    return feats


def make_X(feats, groups):
    X = []
    for f in feats:
        date, y, elod, wrd, expr, shd, tier, fmt, *_ = f
        row = [1.0]
        if "elo" in groups: row.append(elod)
        if "wr" in groups: row.append(wrd)
        if "exp" in groups: row.append(expr)
        if "share" in groups: row.append(shd)
        if "meta" in groups:
            row += [1.0 if tier == t else 0.0 for t in ("S", "A", "B", "P")]
            row.append(1.0 if fmt == "tournament" else 0.0)
        X.append(row)
    return X


def main():
    con = sqlite3.connect(DB)
    feats = build(con)
    print(f"matches with usable m: {len(feats):,}  ({feats[0][0][:10]} .. {feats[-1][0][:10]})")
    y = [f[1] for f in feats]
    n = len(feats)
    split = int(n * 0.7)
    ytr, yte = y[:split], y[split:]
    print(f"train={split} test={n-split} (time split, test = most recent 30%)\n")

    print("Test R^2 on logit(m) by feature set:")
    combos = [
        ("meta only (tier+format)", {"meta"}),
        ("elo", {"elo"}),
        ("winrate", {"wr"}),
        ("experience", {"exp"}),
        ("prior share (reputation)", {"share"}),
        ("meta+elo", {"meta", "elo"}),
        ("meta+elo+wr+exp+share (all)", {"meta", "elo", "wr", "exp", "share"}),
    ]
    for name, groups in combos:
        X = make_X(feats, groups)
        beta = ols(X[:split], ytr)
        print(f"  {name:>34}: test R^2 = {r2(X[split:], yte, beta):.4f}")

    # simple univariate correlations with logit(m) on the test slice
    print("\nUnivariate correlation with logit(m) (test slice):")
    for i, lab in enumerate(["elo_diff", "winrate_diff", "exp_ratio", "share_diff"]):
        col = [f[2 + i] for f in feats[split:]]
        my = sum(yte) / len(yte); mc = sum(col) / len(col)
        cov = sum((a - my) * (b - mc) for a, b in zip(yte, col))
        sy = math.sqrt(sum((a - my) ** 2 for a in yte))
        sc = math.sqrt(sum((b - mc) ** 2 for b in col))
        print(f"  {lab:>13}: corr = {cov/(sy*sc):+.4f}")

    # add SaltyTrack p where available (v5/v6 subset)
    print("\nIncremental value of SaltyTrack p (join on id, v5/v6 live subset):")
    trows = {}
    for tid, pr, pb, prov in con.execute(
        "SELECT id, p_red, p_blue, provenance FROM track WHERE p_red IS NOT NULL"):
        trows[tid] = pr / 100.0
    ids = [r[0] for r in con.execute("SELECT id FROM boy ORDER BY date")]
    # rebuild feats with ids
    rows = con.execute(
        "SELECT id, date, bet_red, bet_blue, winner, fighter_red, fighter_blue, tier, match_format "
        "FROM boy WHERE bet_red>0 AND bet_blue>0 ORDER BY date").fetchall()
    elo, apps, wins, share = {}, {}, {}, {}
    rows2 = []
    for rid, date, br, bb, winner, fr, fb, tier, fmt in rows:
        m = br / (br + bb)
        if not (0.001 < m < 0.999): continue
        er, eb = elo.get(fr, 1000.0), elo.get(fb, 1000.0)
        ar, ab = apps.get(fr, 0), apps.get(fb, 0)
        wr = wins.get(fr, 0) / ar if ar else 0.5
        wb = wins.get(fb, 0) / ab if ab else 0.5
        sr = share.get(fr, 0) / ar if ar else 0.5
        sb = share.get(fb, 0) / ab if ab else 0.5
        if rid in trows and 0.001 < trows[rid] < 0.999:
            rows2.append((rid, math.log(m / (1 - m)), er - eb, wr - wb,
                          math.log((ar + 1) / (ab + 1)), sr - sb, tier, fmt,
                          math.log(trows[rid] / (1 - trows[rid]))))
        exp = 1 / (1 + 10 ** ((eb - er) / 400)); won = 1.0 if winner == fr else 0.0
        elo[fr] = er + 32 * (won - exp); elo[fb] = eb + 32 * ((1 - won) - (1 - exp))
        apps[fr] = ar + 1; apps[fb] = ab + 1
        wins[fr] = wins.get(fr, 0) + won; wins[fb] = wins.get(fb, 0) + (1 - won)
        share[fr] = share.get(fr, 0) + m; share[fb] = share.get(fb, 0) + (1 - m)
    n2 = len(rows2); s2 = int(n2 * 0.7)
    y2 = [r[1] for r in rows2]
    for name, use_p in (("features only", False), ("features + logit(p)", True)):
        X = []
        for r in rows2:
            row = [1.0, r[2], r[3], r[4], r[5]] + \
                  [1.0 if r[6] == t else 0.0 for t in ("S", "A", "B", "P")] + \
                  [1.0 if r[7] == "tournament" else 0.0]
            if use_p: row.append(r[8])
            X.append(row)
        beta = ols(X[:s2], y2[:s2])
        print(f"  {name:>22}: test R^2 = {r2(X[s2:], y2[s2:], beta):.4f}  (n={n2})")


if __name__ == "__main__":
    main()
