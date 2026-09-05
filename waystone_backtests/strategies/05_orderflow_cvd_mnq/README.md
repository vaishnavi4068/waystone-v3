# 05 · Order flow — CVD divergence at VWAP bands on MNQ

**Return driver:** absorption. When price makes a new session low but the cumulative volume delta (buying
minus selling volume) does not — sellers are hitting bids and someone is taking the other side without
letting price fall — the move is being absorbed and tends to revert to VWAP. It is a microstructure
signal: it comes from *who is trading*, not from where price has been, so it is independent of the
Renko/MCO logic in V221 even though both run on the same MNQ feed.

## Book
| | |
|---|---|
| Instruments | **MNQ** (NQ once proven) — plugs directly into the v3 futures layer (`v221_engine`-style `on_bar` interface, brackets, telemetry) |
| Holding period | Minutes; target VWAP, time stop 60 bars, flat by 15:55 |
| Direction | Both |

## Data we need
| Data | Source | File |
|---|---|---|
| MNQ 1-minute OHLCV, as much history as you want | **Polygon** `python tools/fetch_polygon.py futures --root MNQ --start 2025-01-01 --resolution 1min` (quarterly contracts fetched and stitched front-by-volume; `tools/ib_fetch_bars.py` on the VM is the fallback) | `data/intraday/MNQ_1min.csv` |
| **Real aggressor-side volume** (`bid_volume`, `ask_volume` per bar) | Polygon `/futures/v1/trades/{ticker}` returns every trade; if the feed carries the aggressor side, bucket by minute and add the two columns (check one day before assuming it does). Otherwise record live on the VM with `reqTickByTickData("AllLast")` | same file, two extra columns |

Without the extra columns the backtest falls back to a **delta proxy** from bar shape (`volume × (close−open)/(high−low)`), and prints `delta_source: proxy`. The proxy is correlated with true delta but noticeably noisier; treat proxy results as a screen, not a verdict.

## Rule sketch
1. Session reset at 09:30 ET (`--session rth`) or 18:00 (`--session globex`): CVD, VWAP and the VWAP σ-band
   (volume-weighted standard deviation of typical price) all restart.
2. At the close of bar *t* (09:45–15:30 only, after `lookback` bars of session):
   **long** if `low_t` is the lowest low of the last `lookback` (15) bars **and** `close_t < VWAP − k·σ` (k = 1.5)
   **and** `CVD_t` is above the lowest CVD of the window (higher low = divergence).
   **short** mirrored above `VWAP + k·σ`.
3. Entry at the open of bar *t+1*. Stop = `low_t − stop_atr × ATR(14, 1-min)` (1.5). Target = VWAP at signal time.
   Time stop 60 bars. Flat at 15:55. One position at a time.
4. Same-bar stop and target → stop wins. Costs: $0.62/side + one tick slippage per side (MNQ = $2/pt).

## What the backtest measures
Bar-by-bar simulation on 1-minute data, P&L aggregated per session for the Sharpe/drawdown statistics
(so `days` in the report = sessions). `trades.csv` carries entry/exit timestamps, stop, target and reason for
every trade; `exit_reasons` in the report is the first thing to read — a healthy version has target exits
outnumbering stops.

## Run
```bash
python strategies/05_orderflow_cvd_mnq/backtest.py --synthetic --grid
python tools/fetch_polygon.py futures --root MNQ --start 2025-01-01 --resolution 1min
python strategies/05_orderflow_cvd_mnq/backtest.py --symbol MNQ --k-sigma 1.5 --lookback 15 --grid
```

## Parameters to tune
`k_sigma` (1.0 / 1.5 / 2.0), `stop_atr` (1.0 / 1.5 / 2.5), `lookback` (10 / 15 / 30), `max_bars`, and the
`cvd_margin` (how much higher the CVD low must be to count as a divergence). Nine combinations in the default
grid; the deflated Sharpe is printed for them.

## Known weaknesses
The synthetic generator has a small mean-reversion pull baked in so the strategy fires; **do not read anything
into the synthetic numbers**. On real data the proxy delta will produce more false divergences than tick data;
if the proxy version shows nothing, that is not conclusive — the tick-recorded version is the real test, and it
costs a week of recording on the VM. Slippage of one tick per side is realistic for 1–2 MNQ; scale to NQ only
after the fill statistics from paper confirm it.
