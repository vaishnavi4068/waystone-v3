# 02 · Dealer gamma exposure (GEX) — index regime from option positioning

**Return driver:** market-maker hedging flow. When dealers are net **long gamma** (customers have sold
them calls / bought puts that are now far OTM), their delta hedging sells rallies and buys dips, which
pins the index and produces intraday mean reversion. When they are net **short gamma** the hedging
chases price and produces intraday trend and larger ranges. This is orthogonal to both current
strategies: it reads the *regime* from positioning rather than inferring it from price, which is
exactly what V221's chop gate and the VWAP book's "is today a trend day" question are trying to do.

## Book
| | |
|---|---|
| Instruments | SPX/SPY options for the signal; execute in **MES/ES** (or SPY) — plugs into the futures bracket layer |
| Holding period | Intraday: open → close (or to the target level) |
| Direction | Both |

## Data we need
| Data | Source | File |
|---|---|---|
| Daily SPX option-chain snapshot with **open interest, IV and greeks** | **Polygon** `python tools/fetch_polygon.py chain-snapshot --underlying SPX --max-dte 60` — run nightly (cron 16:30 ET). Polygon serves OI/IV/greeks only in snapshots, never historically, so this is how true-OI history gets built | `data/chains/SPX_chain.csv` — `date,spot,expiry,strike,right,oi,iv,gamma,…` |
| **Back history** for the backtest | **Polygon flat files** (S3 `us_options_opra/day_aggs_v1/`): `python tools/polygon_flatfile_gex.py --flatfile-dir data/flatfiles --start 2023-01-01` inverts IV from each contract's close against I:SPX and weights gamma by **volume** — a flow-gamma proxy, marked `oi_source=volume`. Or buy true OI history from CBOE DataShop and convert to the same columns | same file |
| Daily OHLC of what you trade | `python tools/fetch_polygon.py indices --symbols SPX` (I:SPX) or `… futures --root MES --resolution 1session` | `data/daily/I_SPX.csv` / `MES.csv` |
| Intraday version (later) | `python tools/fetch_polygon.py indices --symbols SPX --minute` | `data/intraday/I_SPX_1min.csv` |

Start the nightly snapshot **today** — every day you wait is a day of true-OI history you won't have. Run
the flat-file proxy and the snapshot series side by side for a month so you know how different "volume
gamma" and "OI gamma" actually are before trusting the proxy backtest.

## Rule sketch
1. For every contract in the snapshot with 0 < DTE ≤ 60: Black–Scholes gamma from spot, strike, DTE and IV
   (or the provider's gamma if present).
2. **GEX per contract** = gamma × OI × 100 × spot² × 0.01 — dollars of dealer delta change per 1 % move —
   with calls **+** and puts **−** (the SpotGamma convention: dealers are assumed long the calls and short the puts customers hold).
3. Per day: `total_gex`, its 60-day **z-score** (`gex_z`), the **flip level** (strike where cumulative GEX
   crosses zero), the **max-gamma strike** (largest |GEX|), call wall and put wall.
4. **Mode `gap_fade`** — for day *t+1*, using the chain from the close of day *t*:
   if `gex_z > +thr` and |opening gap| > `gap_min` (0.2 %), **fade** the gap toward the prior close (target =
   prior close); if `gex_z < −thr`, go **with** the gap. Protective stop `stop_atr × ATR(14)`; flat at the close.
5. **Mode `wall_revert`** — positive-GEX days only: at the open, trade **toward** the max-gamma strike if it is
   0.2–2 % away; target = that strike; stop = ATR; flat at the close.

## What the backtest measures
Daily-OHLC approximation of an intraday trade: entry at the open, target/stop resolved conservatively on the
day's high/low, otherwise exit at the close. One MES contract per trade, IBKR costs and one-tick slippage.
`gex_daily.csv` is written alongside the results so you can inspect the regime series itself.

## Run
```bash
python strategies/02_gex_dealer_gamma/backtest.py --synthetic --mode gap_fade      # mechanics
python strategies/02_gex_dealer_gamma/backtest.py --synthetic --mode wall_revert
# with real data
python strategies/02_gex_dealer_gamma/backtest.py --underlying SPX --bars-symbol I:SPX --mode gap_fade --grid
```

## Parameters to tune
`thr_z` (0 / 0.5 / 1.0), `gap_min`, `stop_atr`, `max_dte` in the GEX sum (30/45/60), and — the one that
matters most — the sign convention: some desks flip the put sign for 0-DTE-heavy days. Treat every
convention change as a new trial.

## Known weaknesses
The daily-bar approximation over-credits the target/stop resolution; the real trade is intraday and should
be re-run on 1-minute bars once the chain history exists (the engine is the same). OI is a once-a-day
number; on 0-DTE-dominated days it understates the gamma that matters. And GEX is a **crowded** indicator —
its value is as a regime input to the other sleeves at least as much as a standalone trade.
