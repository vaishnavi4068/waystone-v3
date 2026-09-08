# 03 · Volatility carry — put credit spreads gated by the VIX term structure

**Return driver:** the variance risk premium — index implied volatility is, on average, higher than the
volatility that is subsequently realised, so systematically *selling* insurance earns a premium, punctuated
by sharp losses when it doesn't. The term structure (VIX vs VIX3M) tells you when the premium is being
paid normally (contango) versus when the market is already in stress (backwardation), which is when
you stand aside. This is the mirror image of the VWAP options book, which *buys* premium.

## Book
| | |
|---|---|
| Instruments | **XSP** (1/10 SPX, cash-settled, penny-wide) to start; SPX once sizing justifies it. Plugs into the options bot's chain/quote/order layer — with spreads this tight, the 3 % spread gate is not the problem it is on single names |
| Holding period | 45 DTE entry → 21 DTE or 50 % of max profit; typically 10–25 days |
| Direction | Short put spread (defined risk = width − credit) |

## Data we need
| Data | Source | File |
|---|---|---|
| SPX, VIX, VIX3M daily OHLC | **Polygon** `python tools/fetch_polygon.py indices --symbols SPX VIX VIX3M --start 2008-01-01` (Yahoo `^GSPC ^VIX ^VIX3M` also works) | `data/daily/I_SPX.csv`, `I_VIX.csv`, `I_VIX3M.csv` |
| **Real daily option prices for the two legs** | **Polygon**, automatic with `--pricing polygon`: the backtest builds the OCC tickers (`O:XSP260918P00500000`; SPX monthlies fall back to `SPXW`), pulls daily bars per contract through `/v2/aggs`, caches them in `data/option_bars/`, and uses the entry-day **open** for the credit and each day's **close** for the mark. Days without a bar fall back to the model and are counted in the report | `data/option_bars/*.csv` |

## Rule sketch
1. At each close compute `ratio = VIX / VIX3M` and the 1-year percentile of VIX.
2. **Gate** (evaluated at the close of day *t*): `ratio < 0.95` (contango) **and** VIX percentile ≥ 0.15
   (don't sell the very cheapest vol). Flat when the gate fails; never more than one spread open.
3. **Entry** at the open of day *t+1*: choose the nearest monthly expiry with ≥ 45 DTE; sell the put whose
   Black–Scholes delta is −0.15 at the skewed IV; buy the put 1 % of spot lower (5 XSP points / 50 SPX).
4. **Mark** daily at the close with spot and VIX (IV = VIX × (1 + 2.0 × %OTM) — a put-wing skew model).
5. **Exit** at the close when: value ≤ 50 % of the credit (take profit), or value ≥ 3 × credit (loss stop at
   2× credit), or DTE ≤ 21, or expiry.
6. Costs: $0.65/contract/leg commission and $0.02 (XSP) / $0.05 (SPX) slippage per leg per side.

## What the backtest measures — and what it does not
With `--pricing polygon` the credit and every daily mark come from **traded option prices** (last-trade
closes, not mids — small difference on XSP/SPX, larger on illiquid far-OTM longs; the report counts how many
marks fell back to the model). With `--pricing model` (the default, and always in `--synthetic`) the marks
are Black–Scholes-with-VIX: fine for the *average* premium, understates crash losses. Both modes print the
SPX buy-and-hold benchmark and the fraction of days the gate was open. Strike selection (15-delta) is
model-based in both modes — that decides *which* contract to trade, not how it is priced.

## Run
```bash
python strategies/03_vol_carry_put_spreads/backtest.py --synthetic            # mechanics
export POLYGON_API_KEY=...
python tools/fetch_polygon.py indices --symbols SPX VIX VIX3M --start 2008-01-01
python strategies/03_vol_carry_put_spreads/backtest.py --spx I:SPX --vix I:VIX --vix3m I:VIX3M --product XSP --grid            # model marks, fast
python strategies/03_vol_carry_put_spreads/backtest.py --spx I:SPX --vix I:VIX --vix3m I:VIX3M --product XSP --pricing polygon  # real marks
```

## Parameters to tune
`ratio_max` (0.90 / 0.95 / 1.00), `delta` (0.10 / 0.15 / 0.20), `take_profit` (0.5 / 0.75), `dte_entry`
(30 / 45), `width_pct`, `stop_mult`. The `--grid` default is 18 trials; the deflated Sharpe in the output already
accounts for them.

## Known weaknesses
Short premium has a negative skew by construction: many small wins, occasional large losses (Feb 2018,
Mar 2020, Aug 2024). The gate reduces but does not remove that; size so a 3× credit loss is survivable and
never run it without the loss stop. Sizing and margin are IBKR-specific and not modelled here.
