#!/usr/bin/env python3
"""
saltybet.py -- the single boundary between this project and SaltyBet.

Everything that talks to saltybet.com (or to the SaltyTrack/SaltyBoy
prediction endpoints we use alongside it) lives here. Callers -- the CLI
menu, the trading bot, a future background service -- go through the
`SaltyBet` client and never touch requests, cookies or endpoints directly.

The client owns one `requests.Session`, so the PHPSESSID cookie is managed
in exactly one place and a dead session is re-authenticated in exactly one
place too.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
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

ROOT = Path(__file__).resolve().parent.parent

# Confidence ordering used when deciding whether a pick is worth taking.
CONF_RANK = {"low": 1, "medium": 2, "high": 3}


# ---- credentials ------------------------------------------------------------


def load_env(path: str | None = None) -> dict[str, str]:
    """Read a .env file into a dict. Real environment variables win later."""
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
    """Partially hide a secret for safe logging."""
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


# ---- small pure helpers -----------------------------------------------------


def money(balance: int | None) -> str:
    """Format a Salty Bucks balance."""
    return f"${balance:,}" if balance is not None else "unknown"


def pools(state: dict) -> tuple[int, int]:
    """(red_pool, blue_pool) from a state.json dict."""
    def num(x):
        try:
            return int(str(x).replace(",", ""))
        except (ValueError, TypeError):
            return 0
    return num(state.get("p1total")), num(state.get("p2total"))


def prediction_valid(pred, p1: str, p2: str) -> bool:
    """Is this prediction about THIS match, and does it name a side?"""
    if not isinstance(pred, dict):
        return False
    red = (pred.get("red") or {}).get("name", "")
    blue = (pred.get("blue") or {}).get("name", "")
    if not red or not blue:
        return False
    if red.strip().lower() != p1.strip().lower() or blue.strip().lower() != p2.strip().lower():
        return False
    return pred.get("suggestion") in ("red", "blue")


def prediction_for(pred: dict | None, p1: str, p2: str) -> tuple[str, str]:
    """(suggestion, reason). suggestion is '' when the prediction is unusable."""
    if not isinstance(pred, dict):
        return "", "no_prediction"
    if prediction_valid(pred, p1, p2):
        return pred["suggestion"], "ok"
    red = (pred.get("red") or {}).get("name", "")
    blue = (pred.get("blue") or {}).get("name", "")
    if not red or not blue:
        return "", f"not_active({pred.get('status')})"
    if red.strip().lower() != p1.strip().lower() or blue.strip().lower() != p2.strip().lower():
        return "", "name_mismatch"
    return "", "no_suggestion"


# ---- the client -------------------------------------------------------------


@dataclass
class LoginResult:
    ok: bool
    detail: str
    location: str = ""


@dataclass
class AccountState:
    user_id: str
    balance: int | None
    balance_raw: str


class SaltyBet:
    """A logged-in SaltyBet account, and the only way to talk to it."""

    def __init__(self, creds: Credentials | None = None, verbose: bool = False):
        self.creds = creds or get_credentials(verbose=verbose)
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    # -- auth ------------------------------------------------------------

    def login(self) -> LoginResult:
        """Authenticate. Success == 302 whose Location carries no `error=`."""
        self.session.get(LOGIN_URL, timeout=20)
        resp = self.session.post(
            LOGIN_URL,
            headers={"Referer": LOGIN_URL,
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"email": self.creds.username, "pword": self.creds.password,
                  "authenticate": "signin"},
            allow_redirects=False,
            timeout=20,
        )
        location = resp.headers.get("location", "")
        if self.verbose:
            print(f"[login] HTTP {resp.status_code} -> {location}")
        if "error=" in location:
            detail = location.split("error=", 1)[-1].replace("+", " ")
            return LoginResult(False, detail, location)
        if resp.status_code in (301, 302, 303) or "PHPSESSID" in self.session.cookies:
            return LoginResult(True, "authenticated", location)
        return LoginResult(False, f"unexpected HTTP {resp.status_code}", location)

    def ensure_login(self) -> LoginResult:
        """Log in if we have no session yet; returns the login result."""
        if "PHPSESSID" in self.session.cookies and self.account().user_id:
            return LoginResult(True, "already authenticated")
        return self.login()

    # -- reads -----------------------------------------------------------

    def account(self) -> AccountState:
        """The logged-in account: user id and balance, read from the home page."""
        html = self.session.get(HOME_URL, timeout=20).text
        uid = re.search(r'id="u"[^>]*value\s*=\s*"([^"]*)"', html)
        span = re.search(r'id="balance">([\d,]+)</span>', html)
        hidden = re.search(r'id="b"[^>]*value\s*=\s*"([\d,]*)', html)
        raw = span.group(1) if span else (
            hidden.group(1).strip() if hidden and hidden.group(1).strip() else "")
        bal: int | None = None
        if raw:
            try:
                bal = int(raw.replace(",", ""))
            except ValueError:
                bal = None
        return AccountState(uid.group(1).strip() if uid else "", bal, raw)

    def balance(self) -> int | None:
        """Current balance; re-authenticates once if the session has died."""
        st = self.account()
        if st.balance is None and not st.user_id:
            if self.verbose:
                print("[auth] session looks dead; re-authenticating")
            if self.login().ok:
                st = self.account()
        return st.balance

    def state(self) -> dict | None:
        """The current match state (GET /state.json)."""
        try:
            d = self.session.get(STATE_URL, timeout=15).json()
            if isinstance(d, dict) and "status" in d:
                return d
        except (requests.RequestException, ValueError):
            return None
        return None

    def prediction(self, p1: str, p2: str) -> dict | None:
        """The live SaltyTrack prediction for the match p1 vs p2.

        /api/current-match works during matchmaking but returns only
        {"status":"tournament"} during brackets, so fall back to
        /api/matchup by name -- which reproduces the recorded live
        prediction (verified).
        """
        cur = self._get_json(SALTYTRACK_URL)
        if prediction_valid(cur, p1, p2):
            return cur
        matchup = self._get_json(MATCHUP_URL, {"red": p1, "blue": p2})
        if prediction_valid(matchup, p1, p2):
            return matchup
        return cur or matchup

    def _get_json(self, url: str, params=None):
        try:
            return requests.get(url, params=params, timeout=15,
                                headers=DEFAULT_HEADERS).json()
        except (requests.RequestException, ValueError):
            return None

    # -- writes ----------------------------------------------------------

    def place_bet(self, side_player: str, wager: int) -> tuple[bool, str]:
        """Place a bet. side_player is 'player1' (red) or 'player2' (blue).

        Success == the response body ends with '1'. On failure the session
        is re-authenticated and the bet is retried once.
        """
        def _do() -> str:
            r = self.session.post(
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
        if self.verbose:
            print(f"[bet] failed (body={body!r}); re-auth + retry")
        if self.login().ok:
            body = _do()
            if body.endswith("1"):
                return True, body
        return False, body
