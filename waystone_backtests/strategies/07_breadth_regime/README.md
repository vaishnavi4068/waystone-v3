# 07 · Breadth — a regime switch for the other sleeves

**Return driver:** participation. An index rally carried by a handful of mega-caps while most stocks are
below their 50-day average is a different regime from one where 70 % of names are rising, and momentum
strategies (V221, VWAP pullback, sector rotation) historically do better in the second. Breadth is not a
trade; it is the switch that decides which sleeves are allowed to trade, and this backtest measures how
much the switch is worth by applying it to the simplest possible base sleeve.

## Book
| | |
|---|---|
| Instruments | Any — the output is a daily ON/OFF series (`results/07_breadth_regime_breadth.csv`) that the futures and options bots can read as a gate |
| Holding period | Regime-level: switches a few times a year |
| Direction | Filter for long exposure; inverse for shorts if you want it |

## Data we need
| Data | Source | File |
|---|---|---|
| Daily closes for 100–500 S&P 500 constituents | `cp <options-bot>/sp500.csv data/` then `python tools/fetch_yf.py --sp500 --max-symbols 150 --start 2005-01-01` (500 names takes ~20 min with Yahoo's pacing; 150 by weight is a good approximation) | `data/daily/AAPL.csv` … |
| SPY daily OHLC | `python tools/fetch_yf.py --symbols SPY` | `data/daily/SPY.csv` |

Survivorship: the current constituent list applied to history overstates breadth in the past (dead names are
missing). Fine for a regime filter, not for a stock-picking backtest; if you want it clean, use a
point-in-time constituent history (Norgate or similar).

## Rule sketch
1. **% above 50-day**: each day, the share of constituents (with data) whose close is above their 50-day SMA.
2. **McClellan oscillator**: ratio-adjusted net advances `1000 × (adv − dec) / (adv + dec)`, EMA19 − EMA39.
3. **Base sleeve**: long SPY (100 % of capital) when SPY > its 200-day SMA, else cash. Signal at the close,
   executed at the next open.
4. **Filters** compared side by side:
   `pct50` — base **and** % above 50-day > 50; `mcclellan` — base **and** oscillator > 0;
   `thrust` — base **and** (breadth rose over the last 10 days **or** is above 60 %);
   `pct50_only` — breadth alone, no price trend.
5. The report prints a comparison table (CAGR, Sharpe, max drawdown, exposure, number of switches) for all of
   them plus SPY buy-and-hold, and the full statistics for the `--mode` you choose.

## What the backtest measures
Whether adding breadth to a trend filter improves risk-adjusted return and, more importantly, drawdown, per
unit of exposure. The interesting output is the *difference* between rows, not any row on its own.

## Run
```bash
python strategies/07_breadth_regime/backtest.py --synthetic
python tools/fetch_yf.py --sp500 --max-symbols 150 --start 2005-01-01
python tools/fetch_yf.py --symbols SPY --start 2005-01-01
python strategies/07_breadth_regime/backtest.py --max-symbols 150 --mode pct50 --thr 50
```

## Parameters to tune
`thr` (40 / 50 / 60), the SMA length in the breadth measure (50 vs 20), the McClellan spans. Keep it to a
handful — a regime filter with ten parameters is a curve fit with extra steps.

## Using it in the live bots
Write the daily `pct_above_50` and `mcclellan` values to a small JSON the bots read at startup, exactly like
the Fear & Greed cache in the futures bot (`state/fng.json`), and add `breadth_ok` to the entry gate with its
value logged on every signal line — so that, three months from now, you can measure what it did.

## Known weaknesses
Breadth lags at turns (it confirms a rally rather than predicting it), it is survivorship-biased on the
current constituent list, and on the synthetic data the "SPY" series is not tied to the constituents, so the
synthetic comparison table is noise — mechanics only.
