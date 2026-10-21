#!/usr/bin/env python3
"""
backtest.py -- Backtest SaltyTrack predictions against SaltyBet final pools.

Validity: on SaltyBet you bet while pools are hidden but are paid at the FINAL
pool ratio, so using realised pools to compute returns measures the edge that
actually existed. SaltyTrack's live predictions were verified to match the
stored record (no lookahead).

Strategies:
  A  bet the model's pick                                   [REALIZABLE]
  B  bet the side with best EV using the FINAL pool         [ORACLE, upper bound]
  C  bet the side with best EV using a PREDICTED pool m_hat [REALIZABLE]
     m_hat = OLS(p, tier, format) trained on an earlier time slice.

Commands:
    python3 backtest.py crawl-track --pages 620 --pace 5
    python3 backtest.py crawl-boy --all --pace 0.2
    python3 backtest.py analyze [--live-only]
    python3 backtest.py bias                    # SaltyBoy-only favorite-longshot test
    python3 backtest.py strategy-c [--live-only] [--train-frac 0.6]
    python3 backtest.py run --pages 40
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DB = str(ROOT / "data" / "backtest.sqlite")
TRACK_URL = "https://saltytrack.com/api/recent"
BOY_URL = "https://salty-boy.com/api/match/"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/154.0 Safari/537.36"}
TRACK_PER_PAGE = 50
BOY_PER_PAGE = 100
MAX_BOY_ID = 700_000


# ---- storage ----------------------------------------------------------------


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.execute(
        """CREATE TABLE IF NOT EXISTS track (
            id INTEGER PRIMARY KEY, date TEXT, red_name TEXT, blue_name TEXT,
            predicted TEXT, p_red REAL, p_blue REAL, confidence TEXT,
            provenance TEXT, version TEXT, winner_color TEXT, winner_name TEXT,
            correct INTEGER, abstain INTEGER, raw TEXT)"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS boy (
            id INTEGER PRIMARY KEY, date TEXT, bet_red INTEGER, bet_blue INTEGER,
            winner INTEGER, colour TEXT, fighter_red INTEGER, fighter_blue INTEGER,
            tier TEXT, match_format TEXT, raw TEXT)"""
    )
    con.commit()
    return con


# ---- polite fetch -----------------------------------------------------------


def fetch_json(url: str, params: dict, max_retries: int = 6) -> dict | list | None:
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=25)
        except requests.RequestException:
            time.sleep(4 + attempt)
            continue
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 0)) or min(90, 12 * (attempt + 1))
            print(f"    429 rate limited; sleeping {wait}s", flush=True)
            time.sleep(wait)
            continue
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None
    return None


# ---- crawlers ---------------------------------------------------------------


def crawl_track(con: sqlite3.Connection, pages: int, pace: float, start_page: int = 1) -> None:
    page = start_page
    done = 0
    while done < pages:
        data = fetch_json(TRACK_URL, {"page": page, "per_page": TRACK_PER_PAGE})
        if data is None:
            print(f"  track page {page}: transient failure; retrying", flush=True)
            time.sleep(15)
            continue
        matches = data.get("matches", []) if isinstance(data, dict) else []
        if not matches:
            print(f"  track page {page}: empty/end -- stopping")
            break
        rows = [(
            m.get("id"), m.get("date"), m.get("red_name"), m.get("blue_name"),
            m.get("predicted"), m.get("win_probability_red"), m.get("win_probability_blue"),
            m.get("predicted_confidence"), m.get("prediction_provenance"),
            m.get("prediction_version"), m.get("winner_color"), m.get("winner_name"),
            int(bool(m.get("correct"))), int(bool(m.get("abstain"))), json.dumps(m),
        ) for m in matches]
        con.executemany("INSERT OR REPLACE INTO track VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
        print(f"  track page {page}: +{len(rows)} (date {matches[0].get('date','')[:10]})", flush=True)
        page += 1
        done += 1
        if done < pages:
            time.sleep(pace)


def crawl_boy(con: sqlite3.Connection, from_id: int, to_id: int, pace: float) -> None:
    start_page = max(1, from_id // BOY_PER_PAGE)
    end_page = to_id // BOY_PER_PAGE + 1
    print(f"  boy: ids {from_id}..{to_id} -> pages {start_page}..{end_page}")
    for page in range(start_page, end_page + 1):
        data = fetch_json(BOY_URL, {"page": page, "page_size": BOY_PER_PAGE})
        results = data.get("results", []) if isinstance(data, dict) else []
        if not results:
            print(f"  boy page {page}: empty; skipping", flush=True)
            continue
        rows = [(
            m.get("id"), m.get("date"), m.get("bet_red"), m.get("bet_blue"),
            m.get("winner"), m.get("colour"), m.get("fighter_red"),
            m.get("fighter_blue"), m.get("tier"), m.get("match_format"), json.dumps(m),
        ) for m in results]
        con.executemany("INSERT OR REPLACE INTO boy VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
        if page % 50 == 0:
            print(f"  boy page {page}: stored {page*BOY_PER_PAGE} matches", flush=True)
        time.sleep(pace)


# ---- shared analysis --------------------------------------------------------


@dataclass
class Joined:
    id: int
    date: str
    predicted: str
    p_red: float
    p_blue: float
    confidence: str
    version: str
    provenance: str
    winner_color: str
    bet_red: int
    bet_blue: int
    abstain: int
    tier: str
    match_format: str


def load_joined(con: sqlite3.Connection, live_only: bool = False) -> list[Joined]:
    q = """SELECT t.id, t.date, t.predicted, t.p_red, t.p_blue, t.confidence, t.version,
                  t.provenance, t.winner_color, b.bet_red, b.bet_blue, t.abstain,
                  b.tier, b.match_format
           FROM track t JOIN boy b ON t.id = b.id
           WHERE b.bet_red > 0 AND b.bet_blue > 0
             AND t.p_red IS NOT NULL AND t.p_blue IS NOT NULL
             AND t.winner_color IN ('red','blue')"""
    if live_only:
        q += " AND t.provenance = 'live'"
    q += " ORDER BY t.date"
    return [Joined(*r) for r in con.execute(q)]


def eval_strategy(rows: list[Joined], mode: str, min_edge: float = 0.0) -> dict:
    n = wins = 0
    staked = profit = 0.0
    for r in rows:
        if r.abstain:
            continue
        m_red = r.bet_red / (r.bet_red + r.bet_blue)
        m_blue = 1 - m_red
        p_red, p_blue = r.p_red / 100.0, r.p_blue / 100.0
        if mode == "pick":
            side = r.predicted
            if side not in ("red", "blue"):
                continue
        else:
            ev_red = (p_red - m_red) / m_red if m_red else -9
            ev_blue = (p_blue - m_blue) / m_blue if m_blue else -9
            side, ev = ("red", ev_red) if ev_red >= ev_blue else ("blue", ev_blue)
            if ev <= min_edge:
                continue
        own = r.bet_red if side == "red" else r.bet_blue
        opp = r.bet_blue if side == "red" else r.bet_red
        n += 1
        staked += 1.0
        if r.winner_color == side:
            wins += 1
            profit += opp / own
        else:
            profit -= 1.0
    return {"n": n, "wins": wins, "hit": wins / n if n else 0.0,
            "staked": staked, "profit": profit, "roi": profit / staked if staked else 0.0}


def calibration(rows: list[Joined]) -> list[tuple]:
    buckets = {}
    for r in rows:
        m_red = r.bet_red / (r.bet_red + r.bet_blue)
        p_red = r.p_red / 100.0
        b = min(int(p_red * 10), 9)
        d = buckets.setdefault(b, {"n": 0, "p": 0.0, "m": 0.0, "win": 0})
        d["n"] += 1; d["p"] += p_red; d["m"] += m_red
        d["win"] += 1 if r.winner_color == "red" else 0
    return [(f"{b*10:>2}%", d["n"], d["p"]/d["n"], d["m"]/d["n"], d["m"]/d["n"]-d["p"]/d["n"], d["win"]/d["n"])
            for b in sorted(buckets) for d in [buckets[b]]]


def report(rows: list[Joined], label: str, show_cal: bool = True) -> None:
    if not rows:
        print(f"  {label}: no rows"); return
    print(f"\n=== {label} (n={len(rows)}) ===")
    a = eval_strategy(rows, "pick")
    print(f"  Strategy A (model pick) [REALIZABLE]: n={a['n']} hit={a['hit']:.1%} ROI={a['roi']:+.2%}")
    for thr in (0.0, 0.10, 0.20):
        b = eval_strategy(rows, "value", thr)
        print(f"  Strategy B (EV>{thr:.0%}) [ORACLE]: n={b['n']} hit={b['hit']:.1%} ROI={b['roi']:+.2%}")
    if show_cal:
        print(f"    {'bucket':>7} {'n':>6} {'mean_p':>8} {'mean_m':>8} {'m-p':>8} {'red_win':>8}")
        for bucket, n, mp, mm, dev, wr in calibration(rows):
            print(f"    {bucket:>7} {n:>6} {mp:>8.3f} {mm:>8.3f} {dev:>+8.3f} {wr:>8.3f}")


def analyze(con: sqlite3.Connection, live_only: bool) -> None:
    rows = load_joined(con, live_only=live_only)
    print(f"Joined usable matches: {len(rows)}" + (" (live only)" if live_only else ""))
    report(rows, "ALL")
    for ver in sorted({r.version for r in rows}, key=lambda v: str(v)):
        subset = [r for r in rows if r.version == ver]
        report(subset, f"version={ver}", show_cal=False)
        a = eval_strategy(subset, "pick")
        if a["n"]:
            # hit rate + calibration summary
            print(f"    calibration: mean_p={sum(r.p_red for r in subset)/100/len(subset):.3f} "
                  f"mean_m={sum(r.bet_red/(r.bet_red+r.bet_blue) for r in subset)/len(subset):.3f}")
    for conf in ("high", "medium", "low"):
        subset = [r for r in rows if r.confidence == conf]
        if subset:
            report(subset, f"confidence={conf}", show_cal=False)


# ---- SaltyBoy-only favorite-longshot bias -----------------------------------


def analyze_bias(con: sqlite3.Connection) -> None:
    """Compare final pool implied probability vs realised outcome. No SaltyTrack."""
    rows = con.execute(
        "SELECT bet_red, bet_blue, colour, date FROM boy "
        "WHERE bet_red>0 AND bet_blue>0 AND colour IN ('Red','Blue')"
    ).fetchall()
    if not rows:
        print("no boy rows; run crawl-boy first"); return
    # bucket by red's implied probability
    buckets = {}
    for br, bb, colour, _date in rows:
        m_red = br / (br + bb)
        b = min(int(m_red * 10), 9)
        d = buckets.setdefault(b, {"n": 0, "m": 0.0, "win": 0})
        d["n"] += 1; d["m"] += m_red; d["win"] += 1 if colour == "Red" else 0
    print(f"SaltyBoy-only favourite-longshot test (n={len(rows):,} matches)\n")
    print(f"  {'red_implied':>11} {'n':>8} {'mean_m':>8} {'red_win':>8} {'m-win':>8} {'interpretation'}")
    for b in sorted(buckets):
        d = buckets[b]
        mm = d["m"] / d["n"]; wr = d["win"] / d["n"]; dev = mm - wr
        if b == 0:
            note = "red=heavy longshot -> overpriced" if dev > 0.02 else ("underpriced" if dev < -0.02 else "fair")
        elif b == 9:
            note = "red=heavy favorite -> underpriced" if dev < -0.02 else ("overpriced" if dev > 0.02 else "fair")
        else:
            note = ""
        print(f"  {b*10:>9}-{(b+1)*10:>3}% {d['n']:>8} {mm:>8.3f} {wr:>8.3f} {dev:>+8.3f}  {note}")

    # favorite-only view: is the side with higher implied prob under/over-priced?
    fav = {"n": 0, "m": 0.0, "win": 0}
    for br, bb, colour, _date in rows:
        m_red = br / (br + bb)
        m_fav = max(m_red, 1 - m_red)
        fav_side = "Red" if m_red >= 0.5 else "Blue"
        fav["n"] += 1; fav["m"] += m_fav; fav["win"] += 1 if colour == fav_side else 0
    print(f"\n  Overall favourite: implied={fav['m']/fav['n']:.3f}  "
          f"actual_win={fav['win']/fav['n']:.3f}  "
          f"edge={fav['win']/fav['n'] - fav['m']/fav['n']:+.3f}")
    print("  (positive edge => favourites are systematically underpriced => +EV to back them)")

    # by year: is the mispricing stable over time?
    years = {}
    for br, bb, colour, date in rows:
        y = (date or "")[:4]
        m_red = br / (br + bb)
        m_fav = max(m_red, 1 - m_red)
        fav_side = "Red" if m_red >= 0.5 else "Blue"
        d = years.setdefault(y, {"n": 0, "m": 0.0, "win": 0})
        d["n"] += 1; d["m"] += m_fav; d["win"] += 1 if colour == fav_side else 0
    print(f"\n  {'year':>6} {'n':>8} {'fav_implied':>12} {'fav_win':>8} {'edge':>8}")
    for y in sorted(years):
        d = years[y]
        print(f"  {y:>6} {d['n']:>8} {d['m']/d['n']:>12.3f} {d['win']/d['n']:>8.3f} "
              f"{d['win']/d['n'] - d['m']/d['n']:>+8.3f}")


# ---- Strategy C: predict the pool, then bet EV ------------------------------


def ols(X: list[list[float]], y: list[float], ridge: float = 1e-6) -> list[float]:
    """Solve normal equations (X^T X + ridge I) beta = X^T y via Gaussian elimination."""
    k = len(X[0])
    XtX = [[sum(X[i][a] * X[i][b] for i in range(len(X))) for b in range(k)] for a in range(k)]
    Xty = [sum(X[i][a] * y[i] for i in range(len(X))) for a in range(k)]
    for a in range(k):
        XtX[a][a] += ridge
    # augmented matrix, partial pivoting
    M = [XtX[a][:] + [Xty[a]] for a in range(k)]
    for col in range(k):
        piv = max(range(col, k), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            continue
        M[col], M[piv] = M[piv], M[col]
        pv = M[col][col]
        M[col] = [v / pv for v in M[col]]
        for r in range(k):
            if r != col and M[r][col] != 0:
                f = M[r][col]
                M[r] = [M[r][c] - f * M[col][c] for c in range(k + 1)]
    return [M[a][k] for a in range(k)]


def _solve(A: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting."""
    n = len(A)
    M = [A[i][:] + [b[i]] for i in range(n)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[piv] = M[piv], M[c]
        pv = M[c][c]
        if abs(pv) < 1e-12:
            continue
        for j in range(c, n + 1):
            M[c][j] /= pv
        for r in range(n):
            if r != c and M[r][c]:
                f = M[r][c]
                for j in range(c, n + 1):
                    M[r][j] -= f * M[c][j]
    return [M[i][n] for i in range(n)]


def logistic_fit(X: list[list[float]], y: list[float], iters: int = 60, ridge: float = 1e-6) -> list[float]:
    """Logistic regression via IRLS (Newton-Raphson)."""
    k = len(X[0])
    beta = [0.0] * k
    for _ in range(iters):
        eta = [sum(b * x for b, x in zip(beta, row)) for row in X]
        pr = [1.0 / (1.0 + math.exp(-e)) for e in eta]
        w = [max(q * (1 - q), 1e-9) for q in pr]
        z = [e + (yi - q) / wi for e, yi, q, wi in zip(eta, y, pr, w)]
        XtWX = [[sum(w[i] * X[i][a] * X[i][b] for i in range(len(X))) for b in range(k)] for a in range(k)]
        Xty = [sum(w[i] * X[i][a] * z[i] for i in range(len(X))) for a in range(k)]
        for a in range(k):
            XtWX[a][a] += ridge
        nb = _solve(XtWX, Xty)
        if max(abs(nb[i] - beta[i]) for i in range(k)) < 1e-9:
            beta = nb
            break
        beta = nb
    return beta


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z))


def _features(r: Joined, use_features: bool) -> list[float]:
    p = r.p_red / 100.0
    f = [1.0, p]
    if use_features:
        f += [1.0 if r.tier == t else 0.0 for t in ("S", "A", "B", "P")]
        f += [1.0 if (r.match_format or "") == "tournament" else 0.0]
    return f


def strategy_c(con: sqlite3.Connection, live_only: bool, train_frac: float, use_features: bool) -> None:
    rows = load_joined(con, live_only=live_only)
    if len(rows) < 50:
        print(f"only {len(rows)} rows; crawl more first"); return
    split = int(len(rows) * train_frac)
    train, test = rows[:split], rows[split:]
    X = [_features(r, use_features) for r in train]
    y = [r.bet_red / (r.bet_red + r.bet_blue) for r in train]  # true m_red
    beta = ols(X, y)
    label = "p+tier+format" if use_features else "p only"
    print(f"Strategy C ({label}) | train={len(train)} test={len(test)} | dates "
          f"{train[0].date[:10]}..{train[-1].date[:10]} -> test {test[0].date[:10]}..{test[-1].date[:10]}")
    print(f"  fitted beta={['%.4f' % b for b in beta]}")

    def predicted_m_red(r: Joined) -> float:
        f = _features(r, use_features)
        z = sum(b * x for b, x in zip(beta, f))
        return min(max(z, 0.01), 0.99)

    def eval_c(min_edge: float) -> dict:
        n = wins = 0; staked = profit = 0.0
        for r in test:
            if r.abstain:
                continue
            m_hat = predicted_m_red(r)
            p_red, p_blue = r.p_red / 100.0, r.p_blue / 100.0
            ev_red = (p_red - m_hat) / m_hat
            ev_blue = (p_blue - (1 - m_hat)) / (1 - m_hat)
            side, ev = ("red", ev_red) if ev_red >= ev_blue else ("blue", ev_blue)
            if ev <= min_edge:
                continue
            own = r.bet_red if side == "red" else r.bet_blue
            opp = r.bet_blue if side == "red" else r.bet_red
            n += 1; staked += 1
            if r.winner_color == side:
                wins += 1; profit += opp / own
            else:
                profit -= 1
        return {"n": n, "hit": wins / n if n else 0, "roi": profit / staked if staked else 0}

    for thr in (0.0, 0.05, 0.10, 0.20):
        r = eval_c(thr)
        print(f"  Strategy C (m_hat, EV>{thr:.0%}) [REALIZABLE]: n={r['n']} hit={r['hit']:.1%} ROI={r['roi']:+.2%}")
    print("  --- same TEST slice, for comparison ---")
    report(test, "TEST slice", show_cal=False)


def settle(r: Joined, side: str) -> float:
    """Flat 1-unit return using the REAL final pools (valid: you are paid at final odds)."""
    own = r.bet_red if side == "red" else r.bet_blue
    opp = r.bet_blue if side == "red" else r.bet_red
    return (opp / own) if r.winner_color == side else -1.0


def walkforward(con: sqlite3.Connection, live_only: bool, train_frac: float,
                step_frac: float, min_edge: float, use_features: bool) -> None:
    rows = load_joined(con, live_only=live_only)
    if len(rows) < 500:
        print(f"only {len(rows)} rows; need more"); return
    n = len(rows)
    start = int(n * train_frac)
    step = max(200, int(n * step_frac))
    label = "p+tier+format" if use_features else "p only"
    print(f"Walk-forward | {label} | {n} matches | initial train={start} | step={step}")
    print(f"  {'test window':>23} {'n':>5} | {'C(wf) n':>7} {'ROI':>8} | {'A(pick) ROI':>11} | {'B(oracle) ROI':>13}")

    c_rets: list[float] = []
    a_rets: list[float] = []
    for ts in range(start, n, step):
        train, test = rows[:ts], rows[ts:ts + step]
        if len(train) < 200 or len(test) < 100:
            continue
        Xtr = [_features(r, use_features) for r in train]
        ytr = [1.0 if r.winner_color == "red" else 0.0 for r in train]
        b_pi = logistic_fit(Xtr, ytr)
        b_m = ols(Xtr, [r.bet_red / (r.bet_red + r.bet_blue) for r in train])

        c_sum = a_sum = b_sum = 0.0
        c_n = a_n = b_n = 0
        for r in test:
            if r.abstain:
                continue
            f = _features(r, use_features)
            pi = _sigmoid(sum(b * x for b, x in zip(b_pi, f)))
            mh = min(max(sum(b * x for b, x in zip(b_m, f)), 0.01), 0.99)

            # Strategy C: predicted price
            ev_r, ev_b = (pi - mh) / mh, ((1 - pi) - (1 - mh)) / (1 - mh)
            side, ev = ("red", ev_r) if ev_r >= ev_b else ("blue", ev_b)
            if ev > min_edge:
                ret = settle(r, side); c_sum += ret; c_n += 1; c_rets.append(ret)

            # Baseline A: bet the model's pick
            if r.predicted in ("red", "blue"):
                ret = settle(r, r.predicted); a_sum += ret; a_n += 1; a_rets.append(ret)

            # Oracle B: true m, model p
            mr = r.bet_red / (r.bet_red + r.bet_blue); mb = 1 - mr
            evr = (r.p_red / 100 - mr) / mr; evb = (r.p_blue / 100 - mb) / mb
            side_o = "red" if evr >= evb else "blue"
            b_sum += settle(r, side_o); b_n += 1

        c_roi = c_sum / c_n if c_n else 0.0
        a_roi = a_sum / a_n if a_n else 0.0
        b_roi = b_sum / b_n if b_n else 0.0
        print(f"  {test[0].date[:10]:>10}..{test[-1].date[:10]:<10} {len(test):>5} | "
              f"{c_n:>7} {c_roi:>+8.2%} | {a_roi:>+11.2%} | {b_roi:>+13.2%}")

    def pooled(rets: list[float], name: str) -> None:
        if not rets:
            print(f"  {name}: no bets"); return
        nn = len(rets); mean = sum(rets) / nn
        var = sum((x - mean) ** 2 for x in rets) / (nn - 1)
        se = math.sqrt(var / nn)
        print(f"  {name}: n={nn} ROI={mean:+.2%} +/- {1.96*se:.2%} (95% CI) t={mean/se:+.2f}")

    print("\nPooled out-of-sample:")
    pooled(c_rets, "Strategy C (walk-forward pi_hat + m_hat)")
    pooled(a_rets, "Strategy A (bet pick) baseline     ")


# ---- cli --------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="SaltyTrack vs SaltyBet pool backtester")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("crawl-track"); p1.add_argument("--pages", type=int, default=40)
    p1.add_argument("--pace", type=float, default=5.0); p1.add_argument("--start-page", type=int, default=1)

    p2 = sub.add_parser("crawl-boy"); p2.add_argument("--all", action="store_true")
    p2.add_argument("--auto", action="store_true"); p2.add_argument("--from-id", type=int)
    p2.add_argument("--to-id", type=int); p2.add_argument("--pace", type=float, default=0.2)

    p3 = sub.add_parser("analyze"); p3.add_argument("--live-only", action="store_true")
    p4 = sub.add_parser("bias")
    p5 = sub.add_parser("strategy-c")
    p5.add_argument("--live-only", action="store_true")
    p5.add_argument("--train-frac", type=float, default=0.6)
    p5.add_argument("--no-features", action="store_true")

    p7 = sub.add_parser("walkforward")
    p7.add_argument("--live-only", action="store_true")
    p7.add_argument("--train-frac", type=float, default=0.4)
    p7.add_argument("--step-frac", type=float, default=0.1)
    p7.add_argument("--min-edge", type=float, default=0.0)
    p7.add_argument("--no-features", action="store_true")

    p6 = sub.add_parser("run"); p6.add_argument("--pages", type=int, default=40)
    p6.add_argument("--track-pace", type=float, default=5.0); p6.add_argument("--boy-pace", type=float, default=1.0)

    args = ap.parse_args()
    con = db()

    if args.cmd == "crawl-track":
        crawl_track(con, args.pages, args.pace, args.start_page)
    elif args.cmd == "crawl-boy":
        if args.all:
            crawl_boy(con, 1, MAX_BOY_ID, args.pace)
        elif args.auto:
            row = con.execute("SELECT MIN(id), MAX(id) FROM track").fetchone()
            if not row or row[0] is None:
                print("no predictions stored; run crawl-track first"); return 1
            crawl_boy(con, row[0], row[1], args.pace)
        else:
            crawl_boy(con, args.from_id, args.to_id, args.pace)
    elif args.cmd == "analyze":
        analyze(con, args.live_only)
    elif args.cmd == "bias":
        analyze_bias(con)
    elif args.cmd == "strategy-c":
        strategy_c(con, args.live_only, args.train_frac, not args.no_features)
    elif args.cmd == "walkforward":
        walkforward(con, args.live_only, args.train_frac, args.step_frac, args.min_edge, not args.no_features)
    elif args.cmd == "run":
        crawl_track(con, args.pages, args.track_pace)
        row = con.execute("SELECT MIN(id), MAX(id) FROM track").fetchone()
        crawl_boy(con, row[0], row[1], args.boy_pace)
        analyze(con, live_only=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
