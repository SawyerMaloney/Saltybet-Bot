#!/usr/bin/env python3
"""
bot.py  --  the bot's policy: when to bet, how much, and what to log.

This module has no CLI concerns (no argparse) and no HTTP details (those
live in saltybet.py). Everything here takes a SaltyBet client and does the
bot's work, so it can be driven from the CLI, from a test, or from a
background runner without shelling out.

Trading rule (the "skip-based" strategy validated by backtest):
  bet the SaltyTrack pick ONLY when confidence is high (and not abstain),
  fixed-fraction sizing, real pools used to score the dry run.

Tournament handling: if a live prediction isn't available/valid for the open
match (e.g. SaltyTrack reports status "tournament"), the match is SKIPPED and
logged with the reason, so skipped matches can be audited later.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from saltybet import (
    CONF_RANK, ROOT, STATE_URL, SaltyBet, money, pools, prediction_for,
)

# The bot's defaults. cli.py seeds its command-line flags from these.
DEFAULTS = {
    "min_confidence": "high",
    "wager_frac": 0.02,
    "max_bet": 5000,
    "interval": 5,
}


# ---- trading loop -----------------------------------------------------------


class Trader:
    def __init__(self, client: SaltyBet, execute=False, wager_frac=0.02, max_bet=5000,
                 min_conf="high", log_path=None, interval=5, verbose=False):
        self.client = client
        self.execute = execute
        self.wager_frac = wager_frac
        self.max_bet = max_bet
        self.min_rank = CONF_RANK.get(min_conf, 3)
        self.log_path = log_path or str(ROOT / "logs" / "decisions.jsonl")
        Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
        self.interval = interval
        self.verbose = verbose
        self.pending: dict | None = None
        # Persisted so a restart can't silently lose an outstanding bet.
        self.pending_path = Path(self.log_path).parent / "pending.json"

    # -- outstanding-bet bookkeeping ------------------------------------

    def _save_pending(self) -> None:
        if not self.pending or self.pending.get("resolved"):
            return
        try:
            self.pending_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.pending_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.pending))
            tmp.replace(self.pending_path)
        except OSError:
            pass

    def _load_pending(self) -> dict | None:
        try:
            data = json.loads(self.pending_path.read_text())
        except (OSError, ValueError):
            return None
        if not data.get("p1") or not data.get("p2"):
            return None
        data["resolved"] = False
        return data

    def _clear_pending(self) -> None:
        try:
            self.pending_path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _pick_desc(pend: dict) -> str:
        if pend.get("suggestion") == "red":
            return f"Red ({pend['p1']})"
        if pend.get("suggestion") == "blue":
            return f"Blue ({pend['p2']})"
        return "none"

    def note_missed_resolve(self, observed: str) -> None:
        """Our pending match's result state was never observed.

        Two ways this happens:
          * the '1'/'2' state lasted less than our poll interval, so we went
            straight from 'locked' to the next match being 'open';
          * the process restarted while the bet was outstanding, losing the
            in-memory state.

        We cannot know who won or what the pools were, so we record the gap
        rather than guessing.
        """
        pend = self.pending
        if not pend or pend.get("resolved"):
            return
        pend["resolved"] = True
        self._clear_pending()
        p1, p2 = pend["p1"], pend["p2"]
        pick_desc = self._pick_desc(pend)
        self.log(
            "resolve_missed",
            f"{p1} [RED] vs {p2} [BLUE] -> RESULT NOT OBSERVED | "
            f"our pick={pick_desc} decision={pend['decision'].upper()} "
            f"wager=${pend['wager']} | missed: {observed} | outcome UNKNOWN",
            p1=p1, p2=p2, red_name=p1, blue_name=p2,
            decision=pend["decision"], side=pend["side_player"] or "-",
            pick=pick_desc, wager=pend["wager"], won=None, profit=None,
            missed=True, missed_reason="result_state_never_observed", observed=observed,
        )

    def log(self, event: str, message: str, **fields) -> None:
        """Write a structured record to jsonl and a readable line to stdout."""
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event}
        rec.update(fields)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        print(f"[{rec['ts']}] {event.upper():7} {message}", flush=True)

    def on_open(self, state: dict) -> None:
        p1, p2 = state.get("p1name", ""), state.get("p2name", "")
        pred = self.client.prediction(p1, p2)
        suggestion, reason = prediction_for(pred, p1, p2)
        conf = (pred or {}).get("confidence")
        abstain = bool((pred or {}).get("abstain"))
        balance = self.client.balance()

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
            ok, body = self.client.place_bet(side_player, wager)
            decision = "bet_placed" if ok else "bet_failed"
            if not ok:
                chosen = f"{chosen}|body={body[:20]}"

        self.pending = {
            "key": f"{p1}|{p2}", "p1": p1, "p2": p2,
            "suggestion": suggestion, "confidence": conf,
            "side_player": side_player, "wager": wager, "decision": decision,
            "resolved": False,
        }
        self._save_pending()

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
        p1_pool, p2_pool = pools(state)
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
        self._clear_pending()

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

        # A previous run may have left a bet outstanding. Carry it over so we
        # can notice that its result was never observed (and log that).
        restored = self._load_pending()
        if restored:
            self.pending = restored
            kind = "bet" if restored.get("wager") else "match"
            print(f"[trade] restored outstanding {kind} from a previous run: "
                  f"{restored['p1']} vs {restored['p2']} "
                  f"(wager ${restored['wager']}, decision {restored['decision']})", flush=True)

        while True:
            state = self.client.state()
            if state is None:
                time.sleep(self.interval)
                continue
            status = str(state.get("status", "")).strip()
            key = f"{state.get('p1name','')}|{state.get('p2name','')}"

            if status == "open":
                if self.pending and not self.pending.get("resolved") and self.pending["key"] != key:
                    # The previous match's result state was never observed.
                    self.note_missed_resolve(f"next match opened ({key})")
                if self.pending is None or self.pending["key"] != key or self.pending.get("resolved"):
                    self.on_open(state)

            elif status in ("1", "2"):
                if self.pending and not self.pending.get("resolved") and self.pending["key"] != key:
                    # A result arrived, but for a different match than ours.
                    self.note_missed_resolve(f"result observed for a different match ({key})")
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
    def __init__(self, client: SaltyBet, verbose=False):
        self.client = client
        self.verbose = verbose
        self.last_key = None
        self.prev_status = None

    def balance(self) -> str:
        return money(self.client.balance())

    def step(self, state: dict) -> list[str]:
        lines = []
        status = str(state.get("status", "")).strip()
        p1, p2 = state.get("p1name", ""), state.get("p2name", "")
        key = f"{p1}|{p2}"
        is_result = status in ("1", "2")
        if not is_result and (status == "open" or self.last_key is None) and key != self.last_key:
            pred = self.client.prediction(p1, p2)
            self.last_key = key
            lines.append(f"Match started: {p1} [RED] vs {p2} [BLUE]. "
                         f"Saltytrack prediction: {format_prediction(pred)}. | balance: {self.balance()}")
        if is_result and self.prev_status not in ("1", "2") and key == self.last_key:
            red_won = status == "1"
            winner = p1 if red_won else p2
            color = "RED" if red_won else "BLUE"
            r_pool, b_pool = pools(state)
            lines.append(f"Match ended: {p1} [RED] vs {p2} [BLUE] -> WINNER: {winner} [{color}]. "
                         f"pools RED=${r_pool:,} BLUE=${b_pool:,} | balance: {self.balance()}")
        self.prev_status = status
        return lines

    def run(self, interval=5):
        print(f"[watch] polling {STATE_URL} every {interval}s")
        while True:
            state = self.client.state()
            if state is not None:
                for line in self.step(state):
                    print(line, flush=True)
            time.sleep(interval)


# ---- entry points (called by cli.py) -----------------------------------------


def run_trade(client: SaltyBet, *, execute: bool = False,
              wager_frac: float = DEFAULTS["wager_frac"],
              max_bet: int = DEFAULTS["max_bet"],
              min_conf: str = DEFAULTS["min_confidence"],
              log_path: str | None = None,
              interval: int = DEFAULTS["interval"],
              verbose: bool = False) -> None:
    """Run the trading loop until interrupted.

    execute=False logs what it would bet; execute=True places real bets.
    """
    trader = Trader(client, execute=execute, wager_frac=wager_frac,
                    max_bet=max_bet, min_conf=min_conf, log_path=log_path,
                    interval=interval, verbose=verbose)
    try:
        trader.run()
    except KeyboardInterrupt:
        print("\n[trade] stopped")


def run_watch(client: SaltyBet, *, interval: int = DEFAULTS["interval"],
              verbose: bool = False) -> None:
    """Poll matches and print predictions until interrupted."""
    watcher = MatchWatcher(client, verbose=verbose)
    try:
        watcher.run(interval=interval)
    except KeyboardInterrupt:
        print("\n[watch] stopped")
