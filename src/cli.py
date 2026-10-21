#!/usr/bin/env python3
"""
cli.py -- interactive menu for the SaltyBet bot.

Run with no arguments:  python3 src/bot.py

Two screens:
  * Account status  -- account, current match, SaltyTrack prediction
  * Start trading   -- configure and run the bot in the foreground

Flags remain the scriptable path (used by the background service and tests).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import questionary
from questionary import Choice, Style

import config as cfg
from bot import (
    CONF_RANK, DEFAULT_HEADERS, ensure_balance, fetch_state, get_credentials,
    get_live_prediction, login, money, read_balance,
)

ROOT = cfg.ROOT
DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"

STYLE = Style([
    ("qmark", "fg:cyan bold"),
    ("question", "bold"),
    ("answer", "fg:cyan bold"),
    ("pointer", "fg:cyan bold"),
    ("highlighted", "fg:cyan bold"),
    ("selected", "fg:cyan"),
    ("instruction", "fg:gray"),
    ("text", ""),
    ("dim", "fg:gray"),
])

INSTR = "(↑/↓ move · enter select · ctrl-c cancel)"


def _instr(kind: str) -> str:
    # Arrow-key hints only make sense for list prompts.
    return INSTR if kind == "select" else ""


# ---- prompt helpers ---------------------------------------------------------


def choose(message: str, choices: list, default=None):
    """questionary select. `default` may be an index or a choice value."""
    if isinstance(default, int) and 0 <= default < len(choices):
        default = choices[default].value if isinstance(choices[default], Choice) else choices[default]
    ans = questionary.select(message, choices=choices, default=default,
                             style=STYLE, instruction=INSTR).ask()
    if ans is None:
        raise KeyboardInterrupt
    return ans


def confirm(message: str, default: bool = True) -> bool:
    ans = questionary.confirm(message, default=default, style=STYLE).ask()
    if ans is None:
        raise KeyboardInterrupt
    return ans


def prompt_text(message: str, default: str = "", validate=None) -> str:
    ans = questionary.text(message, default=default, validate=validate,
                           style=STYLE).ask()
    if ans is None:
        raise KeyboardInterrupt
    return ans


def prompt_number(message: str, default: float, kind=float):
    def check(v: str):
        try:
            kind(v)
        except ValueError:
            return "Enter a number"
        return True
    return kind(prompt_text(message, default=str(default), validate=check))


def header(title: str) -> None:
    print(f"\n{BOLD}=== {title} ==={RESET}")


# ---- session ----------------------------------------------------------------


def session():
    """Log in and return (requests session, creds, account state)."""
    import requests
    creds = get_credentials()
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    res = login(s, creds)
    if not res.ok:
        print(f"  {BOLD}login failed:{RESET} {res.detail}")
        print("  check the credentials in .env")
        return None, None, None
    return s, creds, read_balance(s)


# ---- screen: account status -------------------------------------------------


def screen_status() -> None:
    header("Account status")
    s, creds, st = session()
    if s is None:
        return

    print(f"  account   : user id {st.user_id or '<unknown>'}")
    print(f"  balance   : {money(st.balance)}")

    state = fetch_state(s)
    if not state:
        print("  match     : (state unavailable)")
        return
    p1, p2 = state.get("p1name", ""), state.get("p2name", "")
    print(f"  match     : {p1} [RED] vs {p2} [BLUE]   status={state.get('status')}")
    r_pool, b_pool = state.get("p1total"), state.get("p2total")
    if r_pool not in (None, "0", 0) and b_pool not in (None, "0", 0):
        print(f"  pools     : RED ${r_pool}   BLUE ${b_pool}")
    if state.get("remaining"):
        print(f"  remaining : {state['remaining']}")

    pred = get_live_prediction(p1, p2)
    if isinstance(pred, dict) and pred.get("suggestion"):
        pick = pred["suggestion"]
        pick_name = p1 if pick == "red" else p2
        print(f"  prediction: {pick.upper()} ({pick_name})")
        print(f"              confidence={pred.get('confidence')}  "
              f"red={pred.get('win_probability_red')}%  blue={pred.get('win_probability_blue')}%")
        extra = []
        if pred.get("tier"):
            extra.append(f"tier={pred['tier']}")
        if pred.get("abstain"):
            extra.append("abstain=True")
        if pred.get("model"):
            extra.append(f"model={pred['model']}")
        if extra:
            print(f"              {'  '.join(extra)}")
    else:
        why = pred.get("status") if isinstance(pred, dict) else "no data"
        print(f"  prediction: unavailable ({why})")


# ---- screen: start trading --------------------------------------------------


def screen_trade_foreground() -> None:
    header("Start trading (foreground)")
    settings = cfg.load()
    saved = cfg.CONFIG_PATH.exists()

    if saved:
        print(f"  Using saved defaults ({cfg.CONFIG_PATH}):")
        print(f"    min_confidence={settings.min_confidence}  "
              f"wager_frac={settings.wager_frac}  "
              f"max_bet={settings.max_bet}  "
              f"interval={settings.interval}s")
        print(f"  {DIM}Each prompt starts on its saved value -- press enter to keep it.{RESET}\n")
    else:
        print(f"  {DIM}No saved defaults yet (config/settings.json).{RESET}\n")

    mode = choose("Mode:", [
        Choice(title="Dry run", value=False,
               description="log what it would bet, place no real bets"),
        Choice(title="LIVE", value=True,
               description="place real bets (play money, but SaltyBet ToS risk)"),
    ], default=0)
    # Mode is never persisted: we don't want a saved file to silently re-arm
    # real bets, so it always starts on Dry run.
    execute = bool(mode)

    settings.min_confidence = choose(
        "Minimum confidence to bet:", list(CONF_RANK.keys()),
        default=list(CONF_RANK.keys()).index(settings.min_confidence))

    settings.wager_frac = prompt_number(
        "Wager per bet (fraction of balance, e.g. 0.02 = 2%):", settings.wager_frac)
    settings.max_bet = int(prompt_number(
        "Max bet per match (Salty Bucks):", settings.max_bet, int))
    settings.interval = int(prompt_number(
        "Poll interval (seconds):", settings.interval, int))

    errs = settings.validate()
    if errs:
        for e in errs:
            print(f"  invalid: {e}")
        return

    if confirm("Save these as defaults?", default=True):
        p = cfg.save(settings)
        print(f"  saved -> {p}")
    else:
        print(f"  {DIM}not saved -- using them for this run only{RESET}")

    print(f"\n{BOLD}starting bot (Ctrl-C to stop){RESET}\n")
    cmd = [sys.executable, str(ROOT / "src" / "bot.py"), "--trade",
           "--interval", str(settings.interval),
           "--min-confidence", settings.min_confidence,
           "--wager-frac", str(settings.wager_frac),
           "--max-bet", str(settings.max_bet)]
    if execute:
        cmd.append("--execute")
    print(f"{DIM}$ {' '.join(cmd)}{RESET}\n")
    try:
        subprocess.run(cmd, cwd=str(ROOT))
    except KeyboardInterrupt:
        print()


# ---- main menu --------------------------------------------------------------


def main_menu() -> int:
    while True:
        try:
            s, creds, st = session()
            bal = money(st.balance) if st else "unknown"
        except Exception:
            bal = "unknown"

        print(f"\n{BOLD}SaltyBet Bot{RESET}  {DIM}(balance {bal}){RESET}")
        action = choose("What do you want to do?", [
            Choice(title="Account status", value="status",
                   description="account, current match and SaltyTrack prediction"),
            Choice(title="Start trading", value="trade",
                   description="configure and run the bot in the foreground"),
            Choice(title="Quit", value="quit", description="exit"),
        ], default=0)

        try:
            if action == "status":
                screen_status()
            elif action == "trade":
                screen_trade_foreground()
            elif action == "quit":
                return 0
        except KeyboardInterrupt:
            print("\n  (cancelled)")
        try:
            input(f"\n{DIM}press Enter to continue...{RESET}")
        except (EOFError, KeyboardInterrupt):
            return 0


def main() -> int:
    try:
        return main_menu()
    except KeyboardInterrupt:
        print("\nbye")
        return 0
