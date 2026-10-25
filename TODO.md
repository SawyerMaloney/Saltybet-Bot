# TODO

Ongoing work for this project. Keep it current: tick things off as they land,
and add new ones as they come up.

## Known problems

Three diagnosed problems, roughly in the order I would fix them. Each is
written up with the evidence (`file:line`), the mechanism, and the open
questions still to settle. We want to work through these in depth, so the
resolution is left open rather than pre-decided.

### 1. The decision loop is unprotected and errors have nowhere to go

**Today:**

- `src/bot.py:250` is a bare `while True:` around `client.state()`,
  `on_open()` and `on_resolve()`. There is **no `try`/`except` in the loop**, so
  any exception (malformed response, a bug in the reconciliation, a disk error)
  propagates out of `run()` and **kills the process**, with the traceback going
  to stdout — see problem 2 for why that is the wrong place for it.
- `src/bot.py:158`: `decision = "bet_placed" if ok else "bet_failed"`. A failed
  bet is logged as an `open` record into `decisions.jsonl`, the stream ROI is
  computed from. It is only kept out of the P&L because `on_resolve` happens to
  test `decision in ("would_bet", "bet_placed")` (`src/bot.py:203,216`). If that
  filter is ever slightly wrong, phantom stakes enter the ROI denominator with
  no marker. (None have occurred yet.)
- `src/saltybet.py:224` `state()` catches `RequestException`/`ValueError` and
  returns `None`; the loop then `sleep()`s and `continue`s. **No record is
  written at all.** A SaltyBet outage and a quiet stretch of matches look
  identical in the log, and there is no way to ask "how long was the bot unable
  to reach the site?" because it never says.
- `logs/errors.jsonl` does not exist.

**Why it matters.** The log is what has to answer "is the edge real?". A
measurement stream that silently drops failures, produced by a process that can
die leaving its only evidence in a terminal, is not a stream you can trust.
Debug data and measurement data have different lifetimes, consumers and
retention; they should not share a file. (Links to the "Split the error log"
item under *Provenance and versioning*.)

**Open questions to work through:**

- What is the record shape for an error? (severity, exception type, message,
  traceback, and the `state`/match context it happened in)
- Should a caught exception in the loop continue, or exit and let systemd
  restart? i.e. do we retry in-process or make supervision the retry mechanism?
- Does `bet_failed` belong in `decisions.jsonl` as a `decision` value at all, or
  should it move to the error stream while a matching record (with the same
  match key) stays in the measurement stream?
- Should a run of `state() is None` open and close a "connectivity blackout"
  record, so downtime is visible in the log itself rather than only in
  `journalctl`?

### 2. The process is attached to a terminal

**Today:** `src/bot.py:125` `log()` writes the JSONL record, then
`print(..., flush=True)` to stdout. The bot was launched by hand, so both fd1
and fd2 point at a pty (`/dev/pts/1`). Consequences:

1. The bot is in that terminal's process group. Closing the window sends
   SIGHUP and it dies — it survived 3d15h only because the window stayed open.
2. **`message` is not written to the JSONL** — only `ts`, `event` and the
   structured fields are. The rendered line lives only in pty scrollback and is
   discarded as it scrolls.
3. A pty write blocks when the buffer fills, so if the terminal stops draining,
   `print` stalls the poll loop: a self-inflicted outage with no error.

**Why it matters.** The process should not depend on a GUI window staying open,
and the readable line should not be the only record of it.

**What does *not* fix it.** A systemd user service detaches from the terminal and
captures stdout+stderr in the journal, but it **does not survive suspend**, and
`Linger=no` on this machine means it also dies at logout until
`loginctl enable-linger` is run. This problem is about attachment, not uptime.

**Open questions to work through:**

- Does `message` belong in the JSONL (so the log is self-describing), or should
  it stay derived at read time? Storing it is simplest but duplicates state.
- systemd user service vs. `nohup` + redirect vs. a system unit — which best
  supports "restart it with up-to-date code"?
- If we detach, where do the human-readable lines go so they are still usable?

### 3. The poll period drifts, so results are silently missed

**Today:** `src/bot.py:272` ends the loop with `time.sleep(self.interval)`
(default 5, `src/bot.py:35`). Before that sleep, `on_open` does real network
work: `client.prediction(p1, p2)` then `client.balance()`. `prediction()` is
**up to two** SaltyTrack calls, because during brackets `/api/current-match`
returns `{"status":"tournament"}`, fails validation and falls back to
`/api/matchup` (`src/saltybet.py:243`). Each has a 15s timeout, and `balance()`
can additionally trigger a re-login. So the real period is `interval` + however
long those calls take, not a steady 5s.

**The window we have to hit.** Polling `state.json` every 0.5s across a live
match:

```
   0.1s  status=  open
  28.6s  status=locked
 120.5s  status=     1     <- result appears
 132.5s  status=  open     <- next match
```

The result is visible for **~12 seconds**.

**The evidence.** `resolve_missed` fired **6 times** in a ~40-hour run (~0.6% of
matches), all with `result_state_never_observed`. Awake-time misses clustered:
four within 45 minutes, then three within 27 minutes. Random poll phase against
a 12s window would spread out evenly, so something *changed* for a stretch —
most likely the period ballooning when SaltyTrack was slow, plus `state()`
returning `None` at the wrong moment. One was a real bet
(`Holy strider vs Chargeman len`, $24) whose outcome is unrecorded.

**Why it matters.** The cost is small today (~$24, and most misses were skips),
but ~1 in 100 matches silently drops out of the record, and the misses are
**not random** — they concentrate exactly when the infrastructure is degraded,
which is when you most want the record intact.

**Open questions to work through:**

- Decouple the jobs: a tight `state.json`-only poll loop that records
transitions, with the decision work (prediction, balance, bet) moved off the
critical path? If so, where does the pending/decision state live between them?
- Or keep one loop but record the actual period and detect/repair misses?
- On a miss, mark the bet **unmeasured** in the report rather than dropping it,
  so the ROI denominator stays honest?

### Also: the log cannot tell you the bot was down

The 38.7-hour gap in `logs/decisions.jsonl` was **not a bug** — the laptop was
suspended. `journalctl` shows suspend Oct 23 00:30:45 -> resume Oct 24
15:12:10 EDT, matching the log gap to within 9 seconds (an earlier 18.9h gap is
the same story). Of 116 hours of process uptime, ~58 were spent suspended.

Measured at the time of this diagnosis: 159 resolved bets, 86.8% hit rate,
**+7.60% ROI**, +$1,303, betting on 15.7% of matches.

The point is that **only `journalctl` tells you this.** The bot's own log has no
record of it; the only trace is an indirect `resolve_missed`. On resume the bot
should notice the clock jumped and write a record saying so.

`systemd-inhibit` does **not** prevent this trigger (GNOME 50's
monitor-disconnect / lid path — proven twice), and there is no lid setting left
to change. Reliable options are: keep an external display attached (proven), or
mask the sleep targets (`sudo systemctl mask sleep.target suspend.target
hibernate.target hybrid-sleep.target`). Still open: whether this runs on a
machine that stays awake at all.

## Tooling

- [ ] **Switch to `ruff` for linting and formatting.** We currently use
      `pyflakes` (installed ad hoc, not in `requirements.txt`). Ruff does
      linting *and* formatting in one fast tool and replaces flake8/black/
      isort. Plan: add `ruff` as a dev dependency, run `ruff check src/`,
      `ruff format src/`, and fix whatever it surfaces.
- [ ] **Add a test suite.** There is none, which is the biggest devx gap.
      `bot.py` is pure policy now, so the decision rule ("high confidence and
      not abstain -> bet"), the sizing math and the log record shape are all
      trivially unit-testable. This is what makes the refactors safe.
- [ ] **Add a `Makefile`** (or `justfile`) so the common tasks are one word:
      `make test`, `make lint`, `make run`, `make report`, `make service`.
- [ ] **Add a `version --bump` command** to the CLI so bumping the policy
      epoch is cheap enough that we actually do it deliberately.

## Provenance and versioning

The point of versioning here is to be able to say *"calculate ROI from this
point"* and have the answer be trustworthy. So the version tracks the
**policy**, not the code:

- **major** = the policy changed (which picks we take, the sizing rule, where
  the prediction comes from). ROI is *not* comparable across a major bump —
  that is exactly why you bump, to mark the boundary.
- **minor** = implementation changed, policy did not (refactors, logging,
  new CLI screens, bug fixes). ROI *is* comparable across it.

- [ ] **Add a `VERSION` file** (e.g. `1.0.0`) as the single source of truth,
      read by the code and committed so bumps are deliberate.
- [ ] **Stamp every log record with a provenance tuple:**
      `version` (our policy epoch), `sha` (exact code, also the rollback
      target), `upstream_model` (SaltyTrack's model), `match_id` (the shared
      SaltyBoy/SaltyTrack id, for joins and idempotency).
      Stamp per record, not "the current version" — otherwise a rollback
      silently corrupts comparability.
- [ ] **Track SaltyTrack's upstream model version.** They change models
      (`model_v5` -> `model_v6`), which changes what "high confidence" means,
      so ROI spanning an upstream change is partly apples-to-oranges and we
      do not control when it happens. Note: the live `/api/current-match`
      endpoint does *not* expose the version string, only `model: "ml"` —
      `/api/recent` (settled matches) does. So refresh it at the session
      level from `/api/recent` and stamp it on that session's records.
- [ ] **Split the error log out of the decision log.** `logs/decisions.jsonl`
      is the *measurement* stream and must stay clean; `logs/errors.jsonl`
      takes exceptions, failed bets, auth failures, 429s and API errors. The
      stream you measure from must not be the stream you debug from. See
      *Known problems -> 1* for the concrete holes this has to close (the
      unprotected loop, `bet_failed` in the decision stream, and the invisible
      `state() is None` path).

## Running in the background

- [ ] **Run the bot as a systemd user service.** The unit name
      (`saltybot.service`) is the stable identifier: it survives reboots and
      code changes, so "restart it with up to date code" is a one-liner.
      Gives auto-restart on crash, journal logging, and no PID hunting.
- [ ] **Include the linger check in the install step.** `Linger=no` on this
      machine, so a user service dies at logout and does not come back after
      a reboot until `sudo loginctl enable-linger $USER` is run. One line,
      but without it the failure mode costs an evening.
- [ ] **Add a deploy script** (pull -> restart -> health check -> roll back
      to the previous SHA on failure). CI/CD itself is optional; the script
      is the reusable part and can be run by hand.
- [ ] ~~Reconcile matches missed while the bot was down.~~ **Not needed**:
      we are not placing bets, and for ROI purposes what the bot *did not
      do* while it was off is not interesting. Revisit only if we start
      trading for real.

## Observability

- [ ] **Add `status`** to the CLI: is it running, balance, current match,
      last decision, uptime.
- [ ] **Add `report`**: ROI for the current policy epoch — bets, hit rate,
      ROI, sliced by confidence / tier / match format. `--since 2.0` is the
      "calculate ROI from this point" case, and works once `version` is on
      every record.
- [ ] **Add `errors`**: tail and filter the error stream.
- [ ] **Add `watch`**: a live tail of decisions.
- [ ] **Give every CLI command a `--json` mode** so other tooling can consume
      the output.

## The bot

- [ ] **Measure live ROI from `logs/decisions.jsonl`.** The backtest says
      ~+7.2% for high-confidence picks, but that is historical. The live bot
      has been running with real bets; a summary over the decision log would
      tell us what is actually happening.
- [x] ~~Exercise the missed-resolve path in a real run.~~ **Done**: it fired 6
      times in a ~40-hour run. The mechanism works, but the misses themselves
      are now a diagnosed problem — see *Known problems -> 3. The poll period
      drifts*.
- [ ] **Decide the final wager-sizing rule.** We use a fixed fraction. Exact
      Kelly needs `m` (the market price), which is hidden while betting.

## Research

- [ ] **Build a better `m̂` (market price model).** The oracle — knowing the
      final pool — is worth +15–27% ROI, but only +2.6% is reachable with the
      current `m̂`; the gap is the main remaining upside. The oracle's edge
      concentrates on *market underdogs* the crowd underrates, so that is
      where `m̂` needs to be sharp. Signals that are crowd-side rather than
      fighter-side (popularity, momentum, hype) are the untested candidates.
- [ ] **Re-measure the edge over time.** The market's favourite mispricing
      shrank from -0.032 (2023) to -0.021 (2026), consistent with tools like
      SaltyTrack being adopted. The +2.6%/+7.2% figures are measured on
      Aug–Oct 2026 only; they will decay, and we should keep checking.
