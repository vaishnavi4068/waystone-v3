# 06 · Relative value — sector rotation on the SPDR sector ETFs

**Return driver:** cross-sectional momentum, which is a different animal from the *time-series* momentum
V221 trades. Time-series momentum asks "is NQ going up?"; cross-sectional asks "which sectors are going up
*relative to the others*?" and is long the leaders whatever the index does. Monthly cadence, 11 liquid ETFs
with penny spreads, and a 200-day filter that takes the sleeve to cash in bear markets. It is the
low-maintenance, low-friction end of the book — the place where the FinBERT sentiment score can later be
added as a tilt on the ranking.

## Book
| | |
|---|---|
| Instruments | XLK XLF XLV XLE XLI XLY XLP XLU XLB XLRE XLC (shares) — equity execution via the options bot's stock path, or simply the IBKR rebalance tool |
| Holding period | One month per rebalance |
| Direction | Long only; cash for slots that fail the trend filter |

## Data we need
| Data | Source | File |
|---|---|---|
| Daily OHLC for the 11 sector ETFs and SPY, 2010+ (XLRE from 2015, XLC from 2018 — the code handles late starts) | `python tools/fetch_yf.py --sectors --start 2005-01-01` | `data/daily/XLK.csv` … `data/daily/SPY.csv` |

## Rule sketch
1. On the last trading day of each month compute for every sector the **blended momentum**: mean of the
   3-, 6- and 12-month returns, each measured up to 5 days ago (`skip`, to avoid the short-term reversal).
2. Rank; take the top `N` (3).
3. For each selected sector, hold `1/N` weight **only if** its close is above its 200-day SMA; otherwise that
   slot is cash.
4. Execute at the **next open**; hold until the next month-end.
5. Turnover cost 5 bps of the weight traded (commission + spread on penny-wide ETFs).

## What the backtest measures
Portfolio daily returns (first day open→close, then close→close), turnover per rebalance, and the SPY
buy-and-hold benchmark over the same period. A rotation sleeve earns its place if it beats SPY on drawdown
first and Sharpe second; CAGR alone is not the point.

## Run
```bash
python strategies/06_sector_rotation/backtest.py --synthetic
python tools/fetch_yf.py --sectors --start 2005-01-01
python strategies/06_sector_rotation/backtest.py --top-n 3 --grid
python strategies/06_sector_rotation/backtest.py --top-n 3 --lookbacks 126 252 --no-sma-filter
```

## Parameters to tune
`top_n` (2/3/4), the SMA filter on/off (the default grid), `lookbacks` (single 126 vs the blend), `skip`
(0/5/21). Anything beyond that is curve-fitting eleven assets.

## Known weaknesses
Momentum crashes on sharp reversals (Mar–Apr 2020, Nov 2020 rotation): the SMA filter reduces the damage but
the sleeve will lag when leadership flips. Eleven assets is a small cross-section, so results are noisy —
run it from 2005 if you can, and read the synthetic result as nothing more than proof the mechanics work
(the synthetic panel has slow-moving drift regimes, which favour momentum by construction).
