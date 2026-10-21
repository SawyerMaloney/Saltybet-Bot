# SaltyBet Bot

Research + automation for [SaltyBet](https://www.saltybet.com) predictions.

Two eras live in this repo:

- **Legacy** (`legacy/`) — the original project: read the SaltyBet Twitch chat,
  parse match messages, and maintain an Elo rating per fighter.
- **Current** (`src/`) — backtesting and a live (dry-run-first) betting bot built
  on SaltyTrack predictions + SaltyBoy/SaltyBet pool data.

## Layout

```
.
├── src/               # all code
│   ├── bot.py             live bot / dry-run trader
│   ├── backtest.py        backtest + walk-forward + bias analysis (shared helpers)
│   ├── crawl.py           parallel SaltyBoy/SaltyTrack crawler (adaptive rate)
│   ├── market_model.py    what predicts the market price m
│   └── policy.py          direct policy classifier
├── requirements.txt
├── .env               # secrets (gitignored)
├── legacy/            # original Twitch + Elo project
├── data/              # sqlite + legacy dumps (gitignored)
└── logs/              # runtime logs (gitignored)
```

## Setup

```bash
pip install -r requirements.txt        # just: requests
# create .env with:
#   SALTY_BET_USERNAME=...
#   SALTY_BET_PASSWORD=...
```

Every script resolves its own paths from the repo root, so you can run them from
anywhere: `python3 src/bot.py`, `python3 src/backtest.py`, etc.

## Usage

### Live bot / dry-run trader

```bash
python3 src/bot.py                                 # login + print balance
python3 src/bot.py --watch                         # poll matches, print predictions
python3 src/bot.py --trade                         # trading loop, DRY-RUN
python3 src/bot.py --trade --execute               # actually place bets (high-conf only)
python3 src/bot.py --trade --wager-frac 0.02 --max-bet 5000
```

Decisions are written to `logs/decisions.jsonl`; the human log goes to stdout
(redirect it wherever you like).

Rule: bet the SaltyTrack pick only when `confidence == high` (skip otherwise),
fixed-fraction sizing, tournaments handled via the `/api/matchup` fallback.

### Background / overnight

Run the bot in the background yourself, e.g.:

```bash
nohup python3 src/bot.py --trade --interval 5 --min-confidence high \
      > logs/trade.log 2>&1 &
```

### Backtesting / research (offline)

```bash
python3 src/backtest.py crawl-track --pages 620 --pace 5   # pull predictions
python3 src/backtest.py crawl-boy --auto                    # pull final pools
python3 src/backtest.py analyze --live-only                 # ROI + calibration
python3 src/backtest.py walkforward --live-only             # expanding-window out-of-sample
python3 src/backtest.py bias                                # favourite-longshot test
python3 src/backtest.py strategy-c --live-only              # predicted-price EV
python3 src/crawl.py boy --start-page 1 --end-page 6660     # parallel crawler
python3 src/market_model.py                                 # what predicts m
python3 src/policy.py                                       # policy classifier
```

## Key findings (so far)

- SaltyTrack's `p` carries real information the market doesn't fully price:
  `logit(p)` is significant beyond `logit(m)` (z≈17).
- Realizable edge: ~+2.6% betting all picks; **+7.2% betting only
  high-confidence picks** (out-of-sample, t≈8).
- The oracle (knowing the final pool) is +15–27%, so a better `m̂` is the
  remaining upside, concentrated on **market underdogs**.
- Backtests use the real final pools for settlement — valid, since SaltyBet pays
  at the final pool ratio.

## Notes

- `data/` and `logs/` are gitignored (the DB is large and regenerable).
- Automated betting likely violates SaltyBet's ToS — test on a throwaway
  account, start tiny.
