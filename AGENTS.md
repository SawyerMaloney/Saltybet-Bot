# AGENTS.md

Guidance for AI agents (and myself) working in this repository.

## Present options before implementing

**Do not start implementing a feature, refactor, or architectural change until
the user has seen the options and chosen one.**

When the user asks "can we do X?" or "I want to build Y", the expected response
is:

1. Research the space (what libraries exist, what's feasible, what the
   trade-offs are).
2. Present the concrete options, with trade-offs and a recommendation.
3. **Wait for the user to pick.** Then implement only what was chosen.

This applies even when the implementation seems obvious, when a library clearly
dominates, or when the user has previously approved a similar change. It also
applies to *how* to build something: if there are several viable approaches
(e.g. an interactive menu library, a CLI framework, a plain argparse layer),
surface the choice rather than picking silently.

### What went wrong (2026-10-21)

The user asked for a CLI with interactive menus. Instead of presenting library
options and a design, I went straight ahead and:

- installed `questionary` and wrote `src/cli.py`, `src/config.py`,
  `src/service.py`,
- modified `src/bot.py` to route into the menu,
- and started testing the screens.

The user's response: *"you should present me with the options, not go forward
with implementations I haven't accepted."*

The lesson: **propose, then wait.** A working implementation the user did not
ask for is still unwanted work, and it pre-empts their judgment on the
design. If the work is already in flight when the objection lands, offer to
revert it rather than continuing.

## "Record to memory"

When the user says **"record to memory"** (or any close variant: "remember
this", "note this down", "save this for next time"), do not just acknowledge
it — write it to a file in the repo so it survives the session.

Where it goes, in order of preference:

1. **`AGENTS.md`** — for guidance that should always be in context (workflow
   rules, conventions, standing preferences). This is the default.
2. **A linked `MEMORY.md`** — for a longer, topic-organised collection.
   `AGENTS.md` should link to it (e.g. `See [MEMORY.md](MEMORY.md)`) so the
   pointer is discoverable.
3. **A topic file, `memory_name.md`** — for a self-contained subject area
   (e.g. `saltybet_api.md`, `backtest_methodology.md`, `logging.md`).
   One file per subject, referenced from `AGENTS.md` or `MEMORY.md`.

Conventions:

- Create the file if it doesn't exist; don't invent a parallel location.
- Link every memory file from `AGENTS.md` (or `MEMORY.md`) so it's findable
  later without knowing the filename.
- Write down the *why* along with the *what* — a bare conclusion loses its
  value once the context is gone.
- Keep memory files markdown, in the repo, and committed (they're meant to be
  read by future sessions/agents). Never put secrets in them.
- After writing, tell the user the file path and what was recorded.

## Other ground rules

- Verify claims against the live systems before asserting them (e.g. actual
  HTTP responses, actual logged output) rather than reasoning from memory.
- Run and test code before reporting it as working.
- Keep secrets out of git: `.env` and any captured credentials are gitignored.
- The bot's live betting is play money, but automating it likely violates
  SaltyBet's ToS — keep that caveat visible when advising on it.
