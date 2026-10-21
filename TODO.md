# TODO

Ongoing work for this project. Keep it current: tick things off as they land,
and add new items as they come up.

## Tooling

- [ ] **Switch to `ruff` for linting and formatting.** We currently use
      `pyflakes` (installed ad hoc, not in `requirements.txt`). Ruff does
      linting *and* formatting in one fast tool and replaces flake8/black/
      isort. Plan: add `ruff` as a dev dependency, run `ruff check src/`,
      `ruff format src/`, and fix whatever it surfaces.

## The bot

- [ ] **Measure live ROI from `logs/decisions.jsonl`.** The backtest says
      ~+7.2% for high-confidence picks, but that is historical. The live bot
      has been running with real bets; a summary script over the decision log
      would tell us what is actually happening.
- [ ] **Exercise the missed-resolve path in a real run.** The `resolve_missed`
      logic is implemented and unit-tested but has not yet triggered
      naturally, so it is unverified against real timing.
- [ ] **Decide the final wager-sizing rule.** We use a fixed fraction. Exact
      Kelly needs `m` (the market price), which is hidden while betting.

## Background operation

- [ ] **Move the bot into the background and let the CLI report on it.** The
      intended end state: the bot runs persistently and writes a log; the CLI
      becomes the way to ask "what is the bot doing right now". `src/service.py`
      (a systemd user-service manager) was written and then removed as
      premature — rebuild it against the final design once it is clear whether
      the log the CLI reads should be the journal or a file.

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
