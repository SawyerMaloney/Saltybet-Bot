#!/usr/bin/env python3
"""
cli.py -- the command-line surface for the SaltyBet bot.

Owns everything CLI-shaped: argument parsing, the interactive menu, and the
wiring that turns user choices into calls into the bot's logic.

  python3 src/cli.py                     interactive menu
  python3 src/cli.py --trade             run the bot (dry-run)
  python3 src/cli.py --trade --execute   run the bot, placing real bets
  python3 src/cli.py --watch             just print predictions

The bot's own logic lives in bot.py; all SaltyBet interaction lives in
saltybet.py.
"""

from __future__ import annotations

import argparse
import sys

import questionary
from questionary import Choice

import bot
from saltybet import ROOT, SaltyBet, money, pools

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
INSTR = "(↑/↓ move · enter select · ctrl-c cancel)"


# ---- prompt helpers ---------------------------------------------------------


def choose(message: str, choices: list):
    """A questionary select.

    We pass no `default` and no custom style: questionary renders a choice
    matching `default` in reverse video (its own radio-list behaviour) and
    marks the cursor with the '»' pointer, so the cursor is the only
    indicator that moves.
    """
    ans = questionary.select(message, choices=choices, instruction=INSTR).ask()
    if ans is None:
        raise KeyboardInterrupt
    return ans


def confirm(message: str, default: bool = True) -> bool:
    ans = questionary.confirm(message, default=default).ask()
    if ans is None:
        raise KeyboardInterrupt
    return ans


def prompt_text(message: str, default: str = "", validate=None) -> str:
    ans = questionary.text(message, default=default, validate=validate).ask()
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


# ---- screens ----------------------------------------------------------------


def screen_status(sb: SaltyBet) -> None:
    header("Account status")
    st = sb.account()
    print(f"  account   : user id {st.user_id or '<unknown>'}")
    print(f"  balance   : {money(st.balance)}")

    state = sb.state()
    if not state:
        print("  match     : (state unavailable)")
        return
    p1, p2 = state.get("p1name", ""), state.get("p2name", "")
    print(f"  match     : {p1} [RED] vs {p2} [BLUE]   status={state.get('status')}")
    r_pool, b_pool = pools(state)
    if r_pool and b_pool:
        print(f"  pools     : RED ${r_pool:,}   BLUE ${b_pool:,}")
    if state.get("remaining"):
        print(f"  remaining : {state['remaining']}")

    pred = sb.prediction(p1, p2)
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


def screen_trade(sb: SaltyBet) -> None:
    header("Start trading (foreground)")

    mode = choose("Mode:", [
        Choice(title="Dry run", value=False,
               description="log what it would bet, place no real bets"),
        Choice(title="LIVE", value=True,
               description="place real bets (play money, but SaltyBet ToS risk)"),
    ])
    # LIVE is deliberately second: the cursor starts on Dry run, so real bets
    # are never one stray Enter away.
    execute = bool(mode)

    # Strongest filter first, so the cursor starts on the safest choice.
    min_confidence = choose(
        "Minimum confidence to bet:",
        [Choice(title=c, value=c) for c in ("high", "medium", "low")])

    wager_frac = prompt_number(
        "Wager per bet (fraction of balance, e.g. 0.02 = 2%):", bot.DEFAULTS["wager_frac"])
    max_bet = int(prompt_number(
        "Max bet per match (Salty Bucks):", bot.DEFAULTS["max_bet"], int))
    interval = int(prompt_number(
        "Poll interval (seconds):", bot.DEFAULTS["interval"], int))

    if not (0 < wager_frac <= 1) or max_bet < 1 or interval < 1:
        print("  invalid values -- aborting")
        return

    print(f"\n{BOLD}starting bot (Ctrl-C to stop){RESET}\n")
    bot.run_trade(sb, execute=execute, wager_frac=wager_frac, max_bet=max_bet,
                  min_conf=min_confidence, interval=interval)


# ---- interactive menu --------------------------------------------------------


def menu() -> int:
    sb = SaltyBet()
    res = sb.login()
    if not res.ok:
        print(f"  {BOLD}login failed:{RESET} {res.detail}")
        print("  check the credentials in .env")
        return 1

    while True:
        bal = money(sb.balance())
        print(f"\n{BOLD}SaltyBet Bot{RESET}  {DIM}(balance {bal}){RESET}")
        action = choose("What do you want to do?", [
            Choice(title="Account status", value="status",
                   description="account, current match and SaltyTrack prediction"),
            Choice(title="Start trading", value="trade",
                   description="configure and run the bot in the foreground"),
            Choice(title="Quit", value="quit", description="exit"),
        ])

        try:
            if action == "status":
                screen_status(sb)
            elif action == "trade":
                screen_trade(sb)
            elif action == "quit":
                return 0
        except KeyboardInterrupt:
            print("\n  (cancelled)")
        try:
            input(f"\n{DIM}press Enter to continue...{RESET}")
        except (EOFError, KeyboardInterrupt):
            return 0


# ---- argument parsing -------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="SaltyBet bot. Run with no arguments for the interactive menu.")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--watch", action="store_true", help="poll and print predictions")
    ap.add_argument("--trade", action="store_true", help="run the bot (dry-run unless --execute)")
    ap.add_argument("--execute", action="store_true", help="actually place bets (with --trade)")
    ap.add_argument("--wager-frac", type=float, default=bot.DEFAULTS["wager_frac"])
    ap.add_argument("--max-bet", type=int, default=bot.DEFAULTS["max_bet"])
    ap.add_argument("--min-confidence", choices=["low", "medium", "high"],
                    default=bot.DEFAULTS["min_confidence"])
    ap.add_argument("--log", default=str(ROOT / "logs" / "decisions.jsonl"))
    ap.add_argument("--interval", type=int, default=bot.DEFAULTS["interval"])
    ap.add_argument("--menu", action="store_true", help="force the interactive menu")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # No mode selected -> interactive menu.
    if args.menu or not (args.trade or args.watch):
        return menu()

    sb = SaltyBet(verbose=args.verbose)
    res = sb.login()
    if not res.ok:
        print(f"login: FAILED ({res.detail})")
        return 1

    if args.trade:
        bot.run_trade(sb, execute=args.execute, wager_frac=args.wager_frac,
                      max_bet=args.max_bet, min_conf=args.min_confidence,
                      log_path=args.log, interval=args.interval, verbose=args.verbose)
        return 0

    if args.watch:
        bot.run_watch(sb, interval=args.interval, verbose=args.verbose)
        return 0

    st = sb.account()
    print(f"user id: {st.user_id or '<unknown>'}")
    print(f"balance: {money(st.balance)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
