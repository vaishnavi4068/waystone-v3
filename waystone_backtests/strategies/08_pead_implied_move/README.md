# 08 · Post-earnings drift, filtered by the implied-move miss

**Return driver:** under-reaction to information. After a large earnings surprise, prices keep drifting in the
direction of the surprise for days to weeks (Bernard & Thomas 1989; still one of the most persistent
anomalies). The options market gives you a free, forward-looking yardstick for "large": the **implied move**
priced into the straddle before the print. A reaction that exceeds what the market had priced is the cleanest
definition of a surprise, and it is the one signal in this set that uses the option market as an *input*
rather than as the instrument. It is event-driven and multi-day, so it is uncorrelated with everything
intraday you run.

## Book
| | |
|---|---|
| Instruments | S&P 500 stocks (shares) — the turnover scanner already surfaces the post-earnings names; execution through the options bot's stock path or plain IBKR orders |
| Holding period | 5–20 sessions |
| Direction | Long by default; `--short` mirrors it on big misses |

## Data we need
| Data | Source | File |
|---|---|---|
| Daily OHLC per stock | `python tools/fetch_yf.py --sp500 --max-symbols 120 --start 2012-01-01` | `data/daily/<SYM>.csv` |
| Earnings dates with before/after-market timing (+ EPS estimate/actual) | `python tools/fetch_yf.py --earnings --sp500 --max-symbols 120` (Yahoo's `earnings_dates`; ~40 events per name) | `data/earnings/<SYM>.csv` |
| **Implied move before each print** — the point of the strategy | **Polygon** `python tools/fetch_polygon.py implied-move --symbols AAPL MSFT …` (after the two Yahoo pulls): for each event it finds the first listed expiry after the print and the strike nearest the pre-print close, pulls the call and put daily bars for the pre-print session, and writes ATM straddle close / spot as `implied_move_pct` | same file, column filled |

Without the implied-move column the backtest uses the stock's **own history** (mean |reaction| over the last
8 events) as the expected move. The Polygon straddle uses last-trade closes of two contracts on the pre-print
day rather than mids; for liquid names that is within a few percent of the true implied move, which is all
the filter needs.

## Rule sketch
1. For each event find the **pre** close (last session before the announcement is public) and the **reaction**
   session (the announcement day for `bmo`, the next session for `amc`/unknown). `reaction_ret = reaction close /
   pre close − 1`.
2. `expected_move` = `implied_move_pct` if present, else the mean |reaction| of the previous 8 events (needs ≥ 4).
3. **Long** if `reaction_ret ≥ max(move_mult × expected_move, min_move)` (defaults 1.0× and 2 %). With
   `--require-eps-beat` the EPS actual must also beat the estimate. **Short** (`--short`) on the mirror image.
4. Entry at the **open of the session after the reaction day** (so the bot has the full reaction bar before
   acting). Stop at `2 × ATR(14)` below the reaction close. Exit at the close after `hold` sessions (10).
5. Position size `capital / max_positions` per trade (10 % of capital each, up to 10 names).

## What the backtest measures
Portfolio daily returns summed across names, per-trade P&L with reaction size and expected move recorded in
`trades.csv`, exit-reason counts, number of events considered. `--grid` sweeps `move_mult × hold` (9 trials) and
prints the deflated Sharpe.

## Run
```bash
python strategies/08_pead_implied_move/backtest.py --synthetic --grid
python tools/fetch_yf.py --sp500 --max-symbols 120 --start 2012-01-01
python tools/fetch_yf.py --earnings --sp500 --max-symbols 120
python tools/fetch_polygon.py implied-move --symbols AAPL MSFT NVDA AMD     # or the whole list from data/sp500.csv
python strategies/08_pead_implied_move/backtest.py --sp500 --max-symbols 120 --grid
python strategies/08_pead_implied_move/backtest.py --sp500 --max-symbols 120 --short --require-eps-beat
```

## Parameters to tune
`move_mult` (0.75 / 1.0 / 1.5), `hold` (5 / 10 / 20), `stop_atr`, `min_move`, EPS-beat requirement, and the
`hist_events` window for the proxy. Keep the universe fixed while you tune; changing the universe is the
easiest way to fool yourself here.

## Known weaknesses
Yahoo earnings timestamps are occasionally wrong (a `bmo` mislabelled as `amc` means the reaction bar is one
session off — the trade then enters a day late, which hurts but does not create look-ahead). Survivorship in
the constituent list flatters the long side. The drift has weakened in large caps since ~2010; the implied-move
filter is what is supposed to restore it, so the version with real implied moves is the one that counts.
