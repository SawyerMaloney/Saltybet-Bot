#!/usr/bin/env python3
"""
salty_bet_bot.py  --  SaltyBet auth + balance + match polling + dry-run trading.

Modes
  (default)     login, print balance
  --watch       poll matches, print SaltyTrack predictions
  --trade       trading loop, DRY-RUN BY DEFAULT (logs would-be bets); add
                --execute to actually place them.

Trading rule (the "skip-based" strategy validated by backtest):
  bet the SaltyTrack pick ONLY when confidence is high (and not abstain),
  fixed-fraction sizing, real pools used to score the dry run.

Tournament handling: if a live prediction isn't available/valid for the open
match (e.g. SaltyTrack reports status "tournament"), the match is SKIPPED and
logged with the reason, so skipped matches can be audited later.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "https://www.saltybet.com"
LOGIN_URL = f"{BASE}/authenticate?signin=1"
HOME_URL = f"{BASE}/"
STATE_URL = f"{BASE}/state.json"
BET_URL = f"{BASE}/ajax_place_bet.php"
SALTYTRACK_URL = "https://saltytrack.com/api/current-match"
MATCHUP_URL = "https://saltytrack.com/api/matchup"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "DNT": "1",
}

CONF_RANK = {"low": 1, "medium": 2, "high": 3}

ROOT = Path(__file__).resolve().parent.parent


# ---- .env loading -----------------------------------------------------------


def load_env(path: str | None = None) -> dict[str, str]:
    path = path or str(ROOT / ".env")
    env: dict[str, str] = {}
    if not os.path.exists(path):
        return env
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, _, value = line.partition("=")
            if key.strip():
                env[key.strip()] = value.strip().strip('"').strip("'")
    return env


@dataclass
class Credentials:
    username: str
    password: str


def mask(value: str, keep: int = 2) -> str:
    if not value:
        return "<empty>"
    return value[:keep] + "*" * max(0, len(value) - keep)


def get_credentials(verbose: bool = False) -> Credentials:
    env = load_env()
    username = os.environ.get("SALTY_BET_USERNAME") or env.get("SALTY_BET_USERNAME", "")
    password = os.environ.get("SALTY_BET_PASSWORD") or env.get("SALTY_BET_PASSWORD", "")
    if not username or not password:
        raise SystemExit("Missing SALTY_BET_USERNAME / SALTY_BET_PASSWORD in .env")
    if verbose:
        print(f"[env] loaded credentials for user {mask(username)}")
    return Credentials(username, password)


# ---- auth / balance ---------------------------------------------------------


@dataclass
class LoginResult:
    ok: bool
    detail: str
    location: str = ""


def login(session: requests.Session, creds: Credentials, verbose: bool = False) -> LoginResult:
    session.get(LOGIN_URL, timeout=20)
    resp = session.post(
        LOGIN_URL,
        headers={"Referer": LOGIN_URL, "Content-Type": "application/x-www-form-urlencoded"},
        data={"email": creds.username, "pword": creds.password, "authenticate": "signin"},
        allow_redirects=False,
        timeout=20,
    )
    location = resp.headers.get("location", "")
    if verbose:
        print(f"[login] HTTP {resp.status_code} -> {location}")
    if "error=" in location:
        return LoginResult(False, location.split("error=", 1)[-1].replace("+", " "), location)
    if resp.status_code in (301, 302, 303) or "PHPSESSID" in session.cookies:
        return LoginResult(True, "authenticated", location)
    return LoginResult(False, f"unexpected HTTP {resp.status_code}", location)


@dataclass
class AccountState:
    user_id: str
    balance: int | None
    balance_raw: str


def read_balance(session: requests.Session, verbose: bool = False) -> AccountState:
    html = session.get(HOME_URL, timeout=20).text
    uid = re.search(r'id="u"[^>]*value\s*=\s*"([^"]*)"', html)
    span = re.search(r'id="balance">([\d,]+)</span>', html)
    hidden = re.search(r'id="b"[^>]*value\s*=\s*"([\d,]*)', html)
    raw = span.group(1) if span else (hidden.group(1).strip() if hidden and hidden.group(1).strip() else "")
    bal: int | None = None
    if raw:
        try:
            bal = int(raw.replace(",", ""))
        except ValueError:
            bal = None
    return AccountState(uid.group(1).strip() if uid else "", bal, raw)


def money(balance: int | None) -> str:
    return f"${balance:,}" if balance is not None else "unknown"


def ensure_balance(session, creds, verbose=False) -> int | None:
    """Read balance; if the session looks dead, re-auth once and retry."""
    st = read_balance(session, verbose=False)
    if st.balance is None and not st.user_id:
        if verbose:
            print("[auth] session looks dead; re-authenticating")
        if login(session, creds, verbose=verbose).ok:
            st = read_balance(session, verbose=False)
    return st.balance


# ---- market data ------------------------------------------------------------


def fetch_state(session: requests.Session) -> dict | None:
    try:
        d = session.get(STATE_URL, timeout=15).json()
        if isinstance(d, dict) and "status" in d:
            return d
    except (requests.RequestException, ValueError):
        return None
    return None


def _get_json(url, params=None):
    try:
        return requests.get(url, params=params, timeout=15, headers=DEFAULT_HEADERS).json()
    except (requests.RequestException, ValueError):
        return None


def _prediction_valid(pred, p1, p2) -> bool:
    if not isinstance(pred, dict):
        return False
    red = (pred.get("red") or {}).get("name", "")
    blue = (pred.get("blue") or {}).get("name", "")
    if not red or not blue:
        return False
    if red.strip().lower() != p1.strip().lower() or blue.strip().lower() != p2.strip().lower():
        return False
    return pred.get("suggestion") in ("red", "blue")


def get_live_prediction(p1: str, p2: str) -> dict | None:
    """Live prediction for the open match.

    /api/current-match works during matchmaking but returns only
    {"status":"tournament"} during brackets. Fall back to /api/matchup by
    name, which reproduces the recorded live prediction (verified).
    """
    cur = _get_json(SALTYTRACK_URL)
    if _prediction_valid(cur, p1, p2):
        return cur
    matchup = _get_json(MATCHUP_URL, {"red": p1, "blue": p2})
    if _prediction_valid(matchup, p1, p2):
        return matchup
    return cur or matchup


def prediction_for(pred: dict | None, p1: str, p2: str) -> tuple[str, str]:
    """Returns (suggestion, reason); suggestion '' on failure."""
    if not isinstance(pred, dict):
        return "", "no_prediction"
    if _prediction_valid(pred, p1, p2):
        return pred["suggestion"], "ok"
    red = (pred.get("red") or {}).get("name", "")
    blue = (pred.get("blue") or {}).get("name", "")
    if not red or not blue:
        return "", f"not_active({pred.get('status')})"
    if red.strip().lower() != p1.strip().lower() or blue.strip().lower() != p2.strip().lower():
        return "", "name_mismatch"
    return "", "no_suggestion"


def place_bet(session, creds, side_player: str, wager: int, verbose=False) -> tuple[bool, str]:
    """side_player in {'player1','player2'}; success == body ends with '1'."""

    def _do() -> str:
        r = session.post(
            BET_URL,
            headers={
                "Referer": f"{BASE}/",
                "Origin": BASE,
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            data={"selectedplayer": side_player, "wager": str(wager)},
            timeout=20,
        )
        return r.text.strip()

    body = _do()
    if body.endswith("1"):
        return True, body
    if verbose:
        print(f"[bet] failed (body={body!r}); re-auth + retry")
    if login(session, creds, verbose=verbose).ok:
        body = _do()
        if body.endswith("1"):
            return True, body
    return False, body


# ---- trading loop -----------------------------------------------------------


def _pools(state: dict) -> tuple[int, int]:
    def num(x):
        try:
            return int(str(x).replace(",", ""))
        except (ValueError, TypeError):
            return 0
    return num(state.get("p1total")), num(state.get("p2total"))


class Trader:
    def __init__(self, session, creds, execute=False, wager_frac=0.02, max_bet=5000,
                 min_conf="high", log_path=None, interval=5, verbose=False):
        self.session = session
        self.creds = creds
        self.execute = execute
        self.wager_frac = wager_frac
        self.max_bet = max_bet
        self.min_rank = CONF_RANK.get(min_conf, 3)
        self.log_path = log_path or str(ROOT / "logs" / "decisions.jsonl")
        Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
        self.interval = interval
        self.verbose = verbose
        self.pending: dict | None = None

    def log(self, event: str, message: str, **fields) -> None:
        """Write a structured record to jsonl and a readable line to stdout."""
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event}
        rec.update(fields)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        print(f"[{rec['ts']}] {event.upper():7} {message}", flush=True)

    def on_open(self, state: dict) -> None:
        p1, p2 = state.get("p1name", ""), state.get("p2name", "")
        pred = get_live_prediction(p1, p2)
        suggestion, reason = prediction_for(pred, p1, p2)
        conf = (pred or {}).get("confidence")
        abstain = bool((pred or {}).get("abstain"))
        balance = ensure_balance(self.session, self.creds, verbose=self.verbose)

        decision, side_player, wager, chosen = "skip", None, 0, ""
        if reason != "ok":
            decision, chosen = "skip", reason
        elif abstain:
            decision, chosen = "skip", "abstain"
        elif CONF_RANK.get(conf, 0) < self.min_rank:
            decision, chosen = "skip", f"confidence_{conf}"
        elif not balance or balance <= 0:
            decision, chosen = "skip", "no_balance"
        else:
            wager = max(1, min(int(balance * self.wager_frac), self.max_bet))
            side_player = "player1" if suggestion == "red" else "player2"
            chosen = suggestion
            decision = "would_bet" if not self.execute else "bet"

        if self.execute and decision == "bet":
            ok, body = place_bet(self.session, self.creds, side_player, wager, verbose=self.verbose)
            decision = "bet_placed" if ok else "bet_failed"
            if not ok:
                chosen = f"{chosen}|body={body[:20]}"

        self.pending = {
            "key": f"{p1}|{p2}", "p1": p1, "p2": p2,
            "suggestion": suggestion, "confidence": conf,
            "side_player": side_player, "wager": wager, "decision": decision,
            "resolved": False,
        }

        # human-readable summary
        pick = "-"
        if suggestion == "red":
            pick = f"Red ({p1})"
        elif suggestion == "blue":
            pick = f"Blue ({p2})"
        msg = (f"{p1} [RED] vs {p2} [BLUE] | "
               f"pick={pick} conf={conf} | "
               f"decision={decision.upper()} ({chosen}) | "
               f"balance={money(balance)} wager=${wager}")
        self.log(
            "open", msg, p1=p1, p2=p2, red_name=p1, blue_name=p2,
            status=state.get("status"), x=state.get("x"),
            remaining=state.get("remaining"), confidence=conf, suggestion=suggestion,
            decision=decision, reason=chosen, balance=money(balance), wager=wager,
        )

    def on_resolve(self, state: dict) -> None:
        pend = self.pending
        if not pend or pend["resolved"]:
            return
        p1, p2 = pend["p1"], pend["p2"]
        red_won = str(state.get("status")) == "1"
        winner = p1 if red_won else p2
        winner_color = "RED" if red_won else "BLUE"
        p1_pool, p2_pool = _pools(state)
        won = None
        profit = None
        if pend["wager"] > 0 and pend["side_player"]:
            side_fighter = p1 if pend["side_player"] == "player1" else p2
            won = (winner == side_fighter)
            own = p1_pool if pend["side_player"] == "player1" else p2_pool
            opp = p2_pool if pend["side_player"] == "player1" else p1_pool
            if pend["decision"] in ("would_bet", "bet_placed"):
                profit = round(pend["wager"] * (opp / own), 2) if won and own else (-pend["wager"] if not won else None)
        pend["resolved"] = True

        # what did we pick, and did it win?
        if pend["suggestion"] == "red":
            pick_desc = f"Red ({p1})"
        elif pend["suggestion"] == "blue":
            pick_desc = f"Blue ({p2})"
        else:
            pick_desc = "none"

        if pend["decision"] in ("would_bet", "bet_placed"):
            outcome = "WON" if won else "LOST"
            msg = (f"{p1} [RED] vs {p2} [BLUE] -> WINNER: {winner} [{winner_color}] | "
                   f"our pick={pick_desc} -> {outcome} | "
                   f"wager=${pend['wager']} profit={'+' if (profit or 0) >= 0 else ''}${profit} | "
                   f"pools RED=${p1_pool:,} BLUE=${p2_pool:,}")
        else:
            msg = (f"{p1} [RED] vs {p2} [BLUE] -> WINNER: {winner} [{winner_color}] | "
                   f"our pick={pick_desc} (skipped: {pend['decision']}) | "
                   f"pools RED=${p1_pool:,} BLUE=${p2_pool:,}")

        self.log(
            "resolve", msg, p1=p1, p2=p2, red_name=p1, blue_name=p2,
            winner=winner, winner_color=winner_color,
            decision=pend["decision"], side=pend["side_player"] or "-",
            pick=pick_desc, wager=pend["wager"], won=won, profit=profit,
            p1_pool=p1_pool, p2_pool=p2_pool,
        )

    def run(self) -> None:
        mode = "EXECUTE" if self.execute else "DRY-RUN"
        print(f"[trade] {mode} | min_conf={list(CONF_RANK)[self.min_rank-1]} "
              f"wager_frac={self.wager_frac} max_bet={self.max_bet} log={self.log_path}")
        while True:
            state = fetch_state(self.session)
            if state is None:
                time.sleep(self.interval)
                continue
            status = str(state.get("status", "")).strip()
            key = f"{state.get('p1name','')}|{state.get('p2name','')}"
            if status == "open":
                if self.pending is None or self.pending["key"] != key or self.pending["resolved"]:
                    self.on_open(state)
            elif status in ("1", "2"):
                if self.pending and self.pending["key"] == key:
                    self.on_resolve(state)
            time.sleep(self.interval)


# ---- legacy watch mode ------------------------------------------------------


def format_prediction(pred: dict | None) -> str:
    if not pred:
        return "unavailable"
    parts = [f"pick={pred.get('suggestion')}", f"conf={pred.get('confidence')}",
             f"red={pred.get('win_probability_red')}%", f"blue={pred.get('win_probability_blue')}%",
             f"abstain={pred.get('abstain')}"]
    return " - ".join(parts)


class MatchWatcher:
    def __init__(self, session, creds, verbose=False):
        self.session, self.creds, self.verbose = session, creds, verbose
        self.last_key = None
        self.prev_status = None

    def balance(self) -> str:
        return money(ensure_balance(self.session, self.creds, verbose=self.verbose))

    def step(self, state: dict) -> list[str]:
        lines = []
        status = str(state.get("status", "")).strip()
        p1, p2 = state.get("p1name", ""), state.get("p2name", "")
        key = f"{p1}|{p2}"
        is_result = status in ("1", "2")
        if not is_result and (status == "open" or self.last_key is None) and key != self.last_key:
            pred = get_live_prediction(p1, p2)
            self.last_key = key
            lines.append(f"Match started: {p1} [RED] vs {p2} [BLUE]. "
                         f"Saltytrack prediction: {format_prediction(pred)}. | balance: {self.balance()}")
        if is_result and self.prev_status not in ("1", "2") and key == self.last_key:
            red_won = status == "1"
            winner = p1 if red_won else p2
            color = "RED" if red_won else "BLUE"
            r_pool, b_pool = _pools(state)
            lines.append(f"Match ended: {p1} [RED] vs {p2} [BLUE] -> WINNER: {winner} [{color}]. "
                         f"pools RED=${r_pool:,} BLUE=${b_pool:,} | balance: {self.balance()}")
        self.prev_status = status
        return lines

    def run(self, interval=5):
        print(f"[watch] polling {STATE_URL} every {interval}s")
        while True:
            state = fetch_state(self.session)
            if state is not None:
                for line in self.step(state):
                    print(line, flush=True)
            time.sleep(interval)


# ---- main -------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="SaltyBet bot (PoC)")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--watch", action="store_true", help="poll and print predictions")
    ap.add_argument("--trade", action="store_true", help="trading loop (dry-run unless --execute)")
    ap.add_argument("--execute", action="store_true", help="actually place bets (with --trade)")
    ap.add_argument("--wager-frac", type=float, default=0.02)
    ap.add_argument("--max-bet", type=int, default=5000)
    ap.add_argument("--min-confidence", choices=["low", "medium", "high"], default="high")
    ap.add_argument("--log", default=str(ROOT / "logs" / "decisions.jsonl"))
    ap.add_argument("--interval", type=int, default=5)
    args = ap.parse_args()

    creds = get_credentials(verbose=args.verbose)
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    result = login(session, creds, verbose=args.verbose)

    if args.trade:
        if not result.ok:
            print(f"login: FAILED ({result.detail})")
            return 1
        try:
            Trader(session, creds, execute=args.execute, wager_frac=args.wager_frac,
                   max_bet=args.max_bet, min_conf=args.min_confidence, log_path=args.log,
                   interval=args.interval, verbose=args.verbose).run()
        except KeyboardInterrupt:
            print("\n[trade] stopped")
        return 0

    if args.watch:
        if not result.ok:
            print(f"login: FAILED ({result.detail})")
            return 1
        try:
            MatchWatcher(session, creds, verbose=args.verbose).run(interval=args.interval)
        except KeyboardInterrupt:
            print("\n[watch] stopped")
        return 0

    print(f"login: {'OK' if result.ok else 'FAILED'} ({result.detail})")
    if not result.ok:
        return 1
    st = read_balance(session, verbose=args.verbose)
    print(f"user id: {st.user_id or '<unknown>'}")
    print(f"balance: {money(st.balance)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
