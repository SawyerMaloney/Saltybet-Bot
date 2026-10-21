#!/usr/bin/env python3
"""
crawl.py -- parallel crawler with a shared, adaptive global rate limiter.

Why: throughput is bounded by the API's rate limit, not by worker count. N
workers all pull tokens from ONE limiter, so the global request rate is capped
and self-tunes: on HTTP 429 it backs off globally, on success it creeps back
toward the base interval. This speeds a crawl up (workers overlap latency)
without slamming the host.

Usage:
    python3 src/crawl.py track --start-page 188 --end-page 520 --workers 6 --interval 1.0
    python3 src/crawl.py boy   --start-page 1   --end-page 6660 --workers 8 --interval 0.1
"""

from __future__ import annotations

import argparse
import json
import queue
import sqlite3
import threading
import time

import requests

from backtest import DB, db as _db

try:
    _db().close()  # ensure schema exists
    con_schema = True
except Exception:
    con_schema = False

TRACK_URL = "https://saltytrack.com/api/recent"
BOY_URL = "https://salty-boy.com/api/match/"


class Limiter:
    """Shared global rate limiter with AIMD-style backoff."""

    def __init__(self, base_interval: float):
        self.lock = threading.Lock()
        self.base = base_interval
        self.interval = base_interval
        self.next_t = 0.0
        self.cooldown = 0.0
        self.n429 = 0

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                t = max(now, self.next_t, self.cooldown)
                self.next_t = t + self.interval
                wait = t - now
            if wait > 0:
                time.sleep(wait)
            with self.lock:
                if time.monotonic() >= self.cooldown:
                    return

    def on_429(self, secs: float = 30.0) -> None:
        with self.lock:
            self.cooldown = max(self.cooldown, time.monotonic() + secs)
            self.interval = min(self.interval * 1.5, 10.0)
            self.n429 += 1

    def on_ok(self) -> None:
        with self.lock:
            if self.interval > self.base:
                self.interval = max(self.base, self.interval * 0.98)


def worker(q: queue.Queue, limiter: Limiter, url: str, parse, insert, con_lock, con, label: str, counter: dict):
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) Chrome/154.0 Safari/537.36"
    while True:
        try:
            page = q.get_nowait()
        except queue.Empty:
            return
        for _ in range(6):
            limiter.acquire()
            try:
                r = session.get(url, params={"page": page, "per_page": 100 if "salty-boy" in url else 50}, timeout=25)
            except requests.RequestException:
                continue
            if r.status_code == 429:
                limiter.on_429(30)
                continue
            if r.status_code != 200:
                break
            try:
                data = r.json()
            except ValueError:
                break
            rows = parse(data)
            if rows:
                with con_lock:
                    con.executemany(insert, rows)
                    con.commit()
                limiter.on_ok()
                with con_lock:
                    counter["done"] += 1
                    counter["rows"] += len(rows)
                    if counter["done"] % 25 == 0:
                        print(f"  [{label}] pages={counter['done']} rows={counter['rows']} "
                              f"interval={limiter.interval:.2f}s 429s={limiter.n429}", flush=True)
            break
        q.task_done()


def parse_track(data):
    if not isinstance(data, dict):
        return []
    return [(
        m.get("id"), m.get("date"), m.get("red_name"), m.get("blue_name"),
        m.get("predicted"), m.get("win_probability_red"), m.get("win_probability_blue"),
        m.get("predicted_confidence"), m.get("prediction_provenance"),
        m.get("prediction_version"), m.get("winner_color"), m.get("winner_name"),
        int(bool(m.get("correct"))), int(bool(m.get("abstain"))), json.dumps(m),
    ) for m in data.get("matches", [])]


def parse_boy(data):
    if not isinstance(data, dict):
        return []
    return [(
        m.get("id"), m.get("date"), m.get("bet_red"), m.get("bet_blue"),
        m.get("winner"), m.get("colour"), m.get("fighter_red"),
        m.get("fighter_blue"), m.get("tier"), m.get("match_format"), json.dumps(m),
    ) for m in data.get("results", [])]


TRACK_INSERT = "INSERT OR REPLACE INTO track VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
BOY_INSERT = "INSERT OR REPLACE INTO boy VALUES (?,?,?,?,?,?,?,?,?,?,?)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["track", "boy"])
    ap.add_argument("--start-page", type=int, required=True)
    ap.add_argument("--end-page", type=int, required=True)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--interval", type=float, default=1.0, help="base seconds between ANY request (global)")
    args = ap.parse_args()

    con = sqlite3.connect(DB, check_same_thread=False)  # shared; guarded by con_lock
    con_lock = threading.Lock()
    limiter = Limiter(args.interval)
    q: queue.Queue = queue.Queue()
    for p in range(args.start_page, args.end_page + 1):
        q.put(p)

    url = TRACK_URL if args.kind == "track" else BOY_URL
    parse = parse_track if args.kind == "track" else parse_boy
    insert = TRACK_INSERT if args.kind == "track" else BOY_INSERT
    counter = {"done": 0, "rows": 0}

    print(f"[{args.kind}] pages {args.start_page}..{args.end_page} "
          f"workers={args.workers} base_interval={args.interval}s "
          f"(global cap ~{60/args.interval:.0f}/min)", flush=True)

    threads = [threading.Thread(target=worker, args=(q, limiter, url, parse, insert, con_lock, con, args.kind, counter), daemon=True)
               for _ in range(args.workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"[{args.kind}] DONE pages={counter['done']} rows={counter['rows']} "
          f"final_interval={limiter.interval:.2f}s total_429s={limiter.n429}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
