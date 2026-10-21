# TODO

Ongoing work for this project. Keep it current: tick things off as they land,
and add new ones as they come up.

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
      stream you measure from must not be the stream you debug from.

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
- [ ] **Exercise the missed-resolve path in a real run.** The `resolve_missed`
      logic is implemented and unit-tested but has not yet triggered
      naturally, so it is unverified against real timing.
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
