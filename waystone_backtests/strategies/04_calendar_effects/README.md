# 04 · Calendar effects — turn-of-month, FOMC drift, opex week

**Return driver:** predictable institutional flows and event timing. Month-end pension and 401(k) inflows
plus index rebalancing lift the index in the last few and first few sessions of the month (Lakonishok &
Smidt 1988; still measurable). The equity premium has historically been earned disproportionately in the
24 hours before FOMC announcements (Lucca & Moench 2015, "the pre-FOMC announcement drift"). Returns in
monthly options-expiration week are weaker than in the week after (dealer re-hedging). None of this has
anything to do with trend or mean reversion, which is why it diversifies the book, and all of it is cheap to
run: a handful of trades a month with near-zero friction on MES.

## Book
| | |
|---|---|
| Instruments | **MES** (or ES) — plugs straight into the futures bracket layer; SPY for the backtest if you have no futures history |
| Holding period | 2–7 sessions |
| Direction | Long only (the effects are one-sided) |

## Data we need
| Data | Source | File |
|---|---|---|
| Daily MES/ES bars, front contract stitched by volume | **Polygon** `python tools/fetch_polygon.py futures --root MES --start 2019-05-01 --resolution 1session` (`--root ES` for history before MES listed in May 2019) | `data/daily/MES.csv` |
| or SPY daily OHLC | `python tools/fetch_yf.py --symbols SPY --start 2000-01-01` | `data/daily/SPY.csv` |
| FOMC decision dates | `data/fomc_dates.csv` (2015–2026 hand-entered from the Fed's calendar — **verify against federalreserve.gov/monetarypolicy/fomccalendars.htm before trusting the `fomc` mode**; extend it backwards for a longer test) | `data/fomc_dates.csv` |

## Rule sketch
- **`tom`** — exposure on the last `pre` (4) and first `post` (3) trading days of each month: buy at the open
  of the first such day, sell at the open of the day after the last one. Everything else flat.
- **`fomc`** — exposure for the `k` (2) sessions ending on the decision day: buy at the open of D−(k−1), sell
  at the close of D (the model exits at the next open; on daily bars that is the closest approximation — the
  live version should exit at 14:00 ET, just before the statement, which is where the documented drift ends).
- **`opex`** — hold the index every day except Monday–Friday of the monthly third-Friday week. Compare with the
  buy-and-hold line the report prints: the strategy is only interesting if skipping that week *adds* to
  buy-and-hold.
- **`all`** — union of `tom` and `fomc` exposure.

All signals are calendar-known in advance; the simulator still fills at the next open after the decision
close so nothing depends on the bar being traded.

## What the backtest measures
Per-trade P&L on a fixed unit (100 SPY shares, or 1 MES contract with `--futures` when the bars are MES),
IBKR commissions and slippage, plus the buy-and-hold benchmark over the same period so you can judge the
effect *per day of exposure* — a calendar sleeve with 35 % exposure and a Sharpe of 0.8 is better than it looks.

## Run
```bash
python strategies/04_calendar_effects/backtest.py --synthetic --mode all
python tools/fetch_polygon.py futures --root MES --start 2019-05-01 --resolution 1session
python strategies/04_calendar_effects/backtest.py --symbol MES --futures --mode tom --grid
python tools/fetch_yf.py --symbols SPY --start 2000-01-01                 # longer history via SPY
python strategies/04_calendar_effects/backtest.py --symbol SPY --mode tom --grid
python strategies/04_calendar_effects/backtest.py --symbol SPY --mode fomc --fomc-days-before 2 --grid
python strategies/04_calendar_effects/backtest.py --symbol SPY --mode opex
```

## Parameters to tune
`pre` / `post` (the grid tries 2/4/6 × 1/3/5), `fomc_days_before` (1/2/3/5). That is deliberately all; calendar
effects are the easiest thing in finance to overfit, and the published windows are the hypothesis.

## Known weaknesses
The FOMC drift weakened after 2015 and is contested in more recent samples; run it on 2000–2014 and 2015–now
separately before believing either. Turn-of-month is robust across decades but small per trade, so it is a
ballast sleeve, not a P&L engine. Holidays that shift the FOMC day are handled (next trading day), but the
date file itself is the single point of failure — check it.
