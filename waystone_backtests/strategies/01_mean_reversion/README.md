# 01 · Mean reversion / range compression

**Return driver:** short-horizon mean reversion in liquid index ETFs and large caps — the tendency of a
2–3 day oversold move inside an uptrend to snap back, and of a compressed range to expand.
This is the *opposite* driver to V221 and the VWAP pullback (both continuation), which is why it
belongs in the book.

## Book
| | |
|---|---|
| Instruments | SPY, QQQ, IWM, DIA, sector ETFs; large caps with tight spreads (AAPL, MSFT, NVDA …). Not the turnover-scanner names — event-driven gaps break mean reversion. |
| Layer it plugs into | Options bot's equity execution (shares) — or the option leg if you insist, but the edge is small and spreads matter |
| Holding period | 1–10 days |
| Direction | Long-only by default (`bb`); `nr7` trades both sides with the SMA200 filter |

## Data we need
| Data | Source | File |
|---|---|---|
| Daily OHLCV, 10+ years | `python tools/fetch_yf.py --symbols SPY QQQ IWM` | `data/daily/SPY.csv` |
| Nothing else | | |

## Rule sketch

**Mode `bb` (Connors-style Bollinger fade)**
1. Indicators at the close of day *t*: SMA200, SMA20, Bollinger(20, 2σ), %b = (close − lower) / (upper − lower), ATR(14).
2. Signal: `%b < 0` (close below the lower band) **and** `close > SMA200` (only fade dips in an uptrend).
3. Entry: next day's **open**.
4. Exit: at the close of the first day where `close > SMA20`, or after `max_hold` days (default 10). Optional protective stop `--stop-atr` (Connors runs without one; the SMA200 filter is the risk control).
5. Short side (`--short`): `%b > 1` and `close < SMA200`, mirrored.

**Mode `nr7` (narrow range 7 expansion)**
1. Day *t* is NR7 if its high−low range is the smallest of the last seven days.
2. Next day place a buy-stop at `high_t + 1 tick` and a sell-stop at `low_t − 1 tick` (one-cancels-other; the simulator lets only one fill).
3. Protective stop at `stop_atr × ATR(14)` from the entry level (default 1.5); exit at the close of day `max_hold` (default 2).
4. `--no-trend-filter` allows both sides regardless of SMA200; default only trades in the SMA200 direction.

## What the backtest measures
Constant-notional positions (`--notional 25000`, default), IBKR-style costs ($0.005/share + 1 bp slippage),
signal at close *t*, fill at open *t+1* (or at the stop level intraday for `nr7`), same-day stop-and-target
resolved conservatively (stop wins). Reports CAGR, Sharpe, drawdown, win rate, profit factor, average hold,
and — with `--grid` — an in-sample/out-of-sample split with the **deflated Sharpe** for the number of
parameter combinations tried.

## Run
```bash
python strategies/01_mean_reversion/backtest.py --synthetic --mode bb          # mechanics check, no data needed
python tools/fetch_yf.py --symbols SPY QQQ IWM --start 2008-01-01
python strategies/01_mean_reversion/backtest.py --symbol SPY --mode bb --grid
python strategies/01_mean_reversion/backtest.py --symbol QQQ --mode nr7 --max-hold 2 --grid
```

## Parameters to tune (and the trial count they cost)
`max_hold` (3/5/10), `stop_atr` (none/2), Bollinger width (2σ is standard; 1.5σ trades more), SMA200 vs SMA100 filter.
Every combination is a trial for the deflated-Sharpe gate — keep the grid small and the reason for each value written down.

## Known weaknesses
Mean reversion loses in crash regimes (2008, Mar 2020): the SMA200 filter helps but lags. The `nr7` mode
is a breakout trade and will look bad on random-walk data (you'll see negative Sharpe in `--synthetic`); it only
earns on instruments with genuine range-expansion behaviour, so test it on ES/NQ daily bars as well as ETFs.
