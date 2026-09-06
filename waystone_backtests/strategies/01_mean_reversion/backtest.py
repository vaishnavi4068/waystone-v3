#!/usr/bin/env python3
"""01 — Mean reversion / range compression on liquid ETFs and large caps.

Three modes (see README.md):
  bb        Bollinger fade on one symbol: %b < 0 with close > SMA200 -> long next open; exit when close > SMA20
            or after max_hold.
  nr7       Narrow-range-7 expansion on one symbol: day t has the narrowest range of the last 7 -> next day
            buy-stop at high_t (+tick) / sell-stop at low_t (-tick); ATR stop; exit at the close of day max_hold.
  pullback  Cross-sectional pullback portfolio: every name in the universe with close > SMA(trend) and
            RSI(rsi_len) < rsi_entry is a candidate at the close; the most oversold fill the free slots at the
            next open (equal notional, global cap max_positions, one trade per name).  Exit at the close when
            RSI > rsi_exit or close > SMA(exit_sma), or after max_hold days.  Optional ATR stop.

    python backtest.py --symbol SPY --mode bb
    python backtest.py --symbol QQQ --mode nr7 --max-hold 2 --grid
    python backtest.py --mode pullback --universe nsdq250.csv --max-positions 10
    python backtest.py --synthetic --mode bb
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wsbt import costs, data as D, engine, report  # noqa: E402
from wsbt.engine import TradeSpec  # noqa: E402

NAME = "01_mean_reversion"


def indicators(bars: pd.DataFrame) -> pd.DataFrame:
    df = bars.copy()
    df["sma200"] = df["close"].rolling(200).mean()
    df["sma20"] = df["close"].rolling(20).mean()
    sd = df["close"].rolling(20).std(ddof=0)
    df["bb_lo"], df["bb_hi"] = df["sma20"] - 2 * sd, df["sma20"] + 2 * sd
    df["pct_b"] = (df["close"] - df["bb_lo"]) / (df["bb_hi"] - df["bb_lo"])
    tr = pd.concat([df["high"] - df["low"], (df["high"] - df["close"].shift()).abs(),
                    (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    df["range"] = df["high"] - df["low"]
    df["nr7"] = df["range"] == df["range"].rolling(7).min()
    return df


def specs_bb(df: pd.DataFrame, max_hold: int, allow_short: bool, stop_atr: float | None) -> list[TradeSpec]:
    out = []
    idx = df.index
    for i in range(200, len(df) - 1):
        row = df.iloc[i]
        if np.isnan(row["pct_b"]) or np.isnan(row["sma200"]):
            continue
        nxt = idx[i + 1]
        if row["pct_b"] < 0 and row["close"] > row["sma200"]:
            stop = row["close"] - stop_atr * row["atr"] if stop_atr else None
            out.append(TradeSpec(date=nxt, side=1, entry="open", stop=stop, max_hold=max_hold,
                                 exit_on=lambda r, j: r["close"] > r["sma20"], tag="bb_long",
                                 meta={"signal_date": idx[i], "pct_b": round(row["pct_b"], 3)}))
        elif allow_short and row["pct_b"] > 1 and row["close"] < row["sma200"]:
            stop = row["close"] + stop_atr * row["atr"] if stop_atr else None
            out.append(TradeSpec(date=nxt, side=-1, entry="open", stop=stop, max_hold=max_hold,
                                 exit_on=lambda r, j: r["close"] < r["sma20"], tag="bb_short",
                                 meta={"signal_date": idx[i], "pct_b": round(row["pct_b"], 3)}))
    return out


def specs_nr7(df: pd.DataFrame, max_hold: int, stop_atr: float, tick: float, trend_filter: bool) -> list[TradeSpec]:
    out = []
    idx = df.index
    for i in range(200, len(df) - 1):
        row = df.iloc[i]
        if not row["nr7"] or np.isnan(row["atr"]):
            continue
        nxt = idx[i + 1]
        long_ok = (not trend_filter) or row["close"] > row["sma200"]
        short_ok = (not trend_filter) or row["close"] < row["sma200"]
        if long_ok:
            out.append(TradeSpec(date=nxt, side=1, entry="stop", entry_level=row["high"] + tick,
                                 stop=row["high"] + tick - stop_atr * row["atr"], max_hold=max_hold, tag="nr7_long",
                                 meta={"signal_date": idx[i], "range": round(row["range"], 3)}))
        if short_ok:
            out.append(TradeSpec(date=nxt, side=-1, entry="stop", entry_level=row["low"] - tick,
                                 stop=row["low"] - tick + stop_atr * row["atr"], max_hold=max_hold, tag="nr7_short",
                                 meta={"signal_date": idx[i], "range": round(row["range"], 3)}))
    return out


def run(bars: pd.DataFrame, mode: str, max_hold: int, stop_atr: float | None, allow_short: bool,
        trend_filter: bool, notional: float, capital: float, cost: costs.CostModel):
    df = indicators(bars)
    if mode == "bb":
        specs = specs_bb(df, max_hold, allow_short, stop_atr)
        max_conc = 1
    else:
        specs = specs_nr7(df, max_hold, stop_atr or 1.5, tick=0.01, trend_filter=trend_filter)
        max_conc = 1            # buy-stop and sell-stop on the same day act as an OCO: first fill wins
    return engine.simulate_trades(df, specs, cost, {"notional": notional}, capital=capital, max_concurrent=max_conc)


# ── pullback portfolio ─────────────────────────────────────────────────────────
def rsi(close: pd.Series, length: int) -> pd.Series:
    """Wilder RSI."""
    delta = close.diff()
    up = delta.clip(lower=0.0)
    dn = -delta.clip(upper=0.0)
    avg_up = up.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
    avg_dn = dn.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
    rs = avg_up / avg_dn.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    return out.fillna(100.0).where(avg_dn.notna())


def pullback_indicators(bars: pd.DataFrame, trend_sma: int, rsi_len: int, exit_sma: int) -> pd.DataFrame:
    df = bars.copy()
    df["trend"] = df["close"].rolling(trend_sma).mean()
    df["exit_sma"] = df["close"].rolling(exit_sma).mean()
    df["rsi"] = rsi(df["close"], rsi_len)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - df["close"].shift()).abs(),
                    (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    df["dvol"] = (df["close"] * df["volume"]).rolling(60).median()
    df["mom"] = df["close"] / df["close"].shift(126) - 1.0
    return df


def specs_pullback(sym: str, df: pd.DataFrame, rsi_entry: float, rsi_exit: float, max_hold: int, stop_atr: float | None,
                   min_dollar_vol: float, start: pd.Timestamp | None, end: pd.Timestamp | None,
                   regime: pd.Series | None, per_trade: float, sizing: str, ref_atr_pct: float = 1.5,
                   earnings: np.ndarray | None = None, max_atr_pct: float = 0.0, entry_atr: float = 0.0,
                   rank: str = "rsi") -> list[TradeSpec]:
    """Candidates decided at the close of day t, attempted at the open of t+1.  rank = RSI (lower = more oversold).
    sizing='vol' scales the notional by ref_atr_pct / ATR% (clipped to [0.25, 1]) so every slot carries similar risk.
    earnings: sorted event dates — no entry if a print lands inside the holding window (signal .. signal + max_hold + 1 sessions).
    max_atr_pct: skip names whose ATR(14) is above this % of price (tail-risk screen; 0 = off).
    entry_atr: 0 = market-on-open; >0 = resting limit at close - entry_atr x ATR, filled only if the next day trades through
    (passive entry: better price, no spread crossing, fewer fills)."""
    out: list[TradeSpec] = []
    idx = df.index
    trend, r, close, atr, dvol, mom = (df[k].to_numpy() for k in ("trend", "rsi", "close", "atr", "dvol", "mom"))

    def exit_rule(row: pd.Series, _j: int) -> bool:
        return bool(row["rsi"] > rsi_exit or row["close"] > row["exit_sma"])

    for i in range(len(df) - 1):
        nxt = idx[i + 1]
        if start is not None and nxt < start:
            continue
        if end is not None and nxt > end:
            break
        if np.isnan(trend[i]) or np.isnan(r[i]) or np.isnan(atr[i]):
            continue
        if regime is not None and not bool(regime.get(idx[i], True)):
            continue
        if min_dollar_vol and (np.isnan(dvol[i]) or dvol[i] < min_dollar_vol):
            continue
        if max_atr_pct and 100.0 * atr[i] / close[i] > max_atr_pct:
            continue
        if earnings is not None and len(earnings):
            last = idx[min(i + max_hold + 1, len(idx) - 1)]
            j = int(np.searchsorted(earnings, np.datetime64(idx[i])))
            if j < len(earnings) and earnings[j] <= np.datetime64(last):
                continue
        if close[i] > trend[i] and r[i] < rsi_entry:
            stop = close[i] - stop_atr * atr[i] if stop_atr else None
            notional = per_trade
            if sizing == "vol":
                atr_pct = 100.0 * atr[i] / close[i]
                notional = per_trade * float(np.clip(ref_atr_pct / max(atr_pct, 1e-6), 0.25, 1.0))
            entry = "limit" if entry_atr > 0 else "open"
            level = close[i] - entry_atr * atr[i] if entry_atr > 0 else None
            out.append(TradeSpec(date=nxt, side=1, entry=entry, entry_level=level, stop=stop, max_hold=max_hold, exit_on=exit_rule,
                                 tag="pullback", meta={"rank": float(r[i]) if rank == "rsi" else -float(np.nan_to_num(mom[i], nan=-9.0)),
                                                       "notional": round(notional, 2),
                                                       "signal_date": idx[i], "rsi": round(float(r[i]), 2)}))
    return out


def market_regime(frames: dict[str, pd.DataFrame], symbol: str | None, trend_sma: int) -> pd.Series | None:
    """Index-level trend switch: new entries only while `symbol` closes above its SMA(trend_sma)."""
    if not symbol:
        return None
    bars = frames.get(symbol)
    if bars is None:
        bars = D.load_daily(symbol)
    return bars["close"] > bars["close"].rolling(trend_sma).mean()


def load_regime(path: str | None) -> pd.Series | None:
    """Optional CSV (date,on) from 07_breadth_regime: no NEW entries while on == 0.  Open trades are managed as usual."""
    if not path:
        return None
    p = Path(path)
    if not p.is_absolute():
        p = D.DATA_DIR / p if (D.DATA_DIR / p).exists() else report.RESULTS / p
    df = pd.read_csv(p, parse_dates=["date"]).set_index("date").sort_index()
    col = "on" if "on" in df.columns else df.columns[0]
    return df[col].astype(float).gt(0.5)


def run_pullback(frames: dict[str, pd.DataFrame], *, trend_sma: int, rsi_len: int, rsi_entry: float, rsi_exit: float,
                 exit_sma: int, max_hold: int, stop_atr: float | None, max_positions: int, min_dollar_vol: float,
                 capital: float, cost: costs.CostModel, start: str | None, end: str | None, regime: pd.Series | None,
                 sizing: str = "equal", max_new_per_day: int = 0, earnings: dict[str, np.ndarray] | None = None,
                 max_atr_pct: float = 0.0, entry_atr: float = 0.0, rank: str = "rsi", hedge_ratio: float = 0.0,
                 hedge_symbol: str = "SPY"):
    s0 = pd.Timestamp(start) if start else None
    e0 = pd.Timestamp(end) if end else None
    per_trade = capital / max_positions
    specs: dict[str, list[TradeSpec]] = {}
    sim_frames: dict[str, pd.DataFrame] = {}
    for sym, bars in frames.items():
        if len(bars) < trend_sma + 5:
            continue
        df = pullback_indicators(bars, trend_sma, rsi_len, exit_sma)     # warm up on pre-window history
        specs[sym] = specs_pullback(sym, df, rsi_entry, rsi_exit, max_hold, stop_atr, min_dollar_vol, s0, e0, regime,
                                    per_trade, sizing, earnings=(earnings or {}).get(sym), max_atr_pct=max_atr_pct,
                                    entry_atr=entry_atr, rank=rank)
        cut = df
        if s0 is not None:
            cut = cut[cut.index >= s0]
        if e0 is not None:
            cut = cut[cut.index <= e0]
        if len(cut):
            sim_frames[sym] = cut
    if max_new_per_day > 0:                                             # keep only the N most oversold per day
        by_day: dict[pd.Timestamp, list[tuple[str, TradeSpec]]] = {}
        for sym, lst in specs.items():
            for s in lst:
                by_day.setdefault(pd.Timestamp(s.date), []).append((sym, s))
        specs = {}
        for d, lst in by_day.items():
            for sym, s in sorted(lst, key=lambda t: t[1].meta["rank"])[:max_new_per_day]:
                specs.setdefault(sym, []).append(s)
    hedge = None
    if hedge_ratio > 0:
        hb = frames.get(hedge_symbol)
        if hb is None:
            hb = D.load_daily(hedge_symbol)
        hedge = (hb, hedge_ratio, costs.US_ETF.scaled(cost.slippage_bps / max(costs.US_STOCK.slippage_bps, 1e-9)))
    return engine.simulate_trades_multi(sim_frames, specs, cost, {"notional": per_trade, "per_spec": True}, capital=capital,
                                        max_positions=max_positions, hedge=hedge)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--mode", choices=["bb", "nr7", "pullback"], default="bb")
    ap.add_argument("--max-hold", type=int, default=None, help="time exit in sessions (default bb 10, nr7 2, pullback 6)")
    ap.add_argument("--stop-atr", type=float, default=None, help="protective stop in ATR(14) (bb/pullback: optional; nr7: default 1.5)")
    ap.add_argument("--short", action="store_true", help="bb: also fade %%b > 1 below SMA200")
    ap.add_argument("--no-trend-filter", action="store_true", help="nr7: ignore the SMA200 direction filter")
    ap.add_argument("--notional", type=float, default=25_000.0)
    ap.add_argument("--capital", type=float, default=100_000.0)
    ap.add_argument("--cost-mult", type=float, default=1.0, help="scale commission + slippage (2.0 = stage-gate cost stress)")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--grid", action="store_true")
    # pullback portfolio
    ap.add_argument("--universe", default=None, help="pullback: symbol list CSV in data/ (e.g. nsdq250.csv)")
    ap.add_argument("--symbols", nargs="*", default=[], help="pullback: explicit symbols (added to --universe)")
    ap.add_argument("--max-symbols", type=int, default=None)
    ap.add_argument("--trend-sma", type=int, default=200)
    ap.add_argument("--rsi-len", type=int, default=2)
    ap.add_argument("--rsi-entry", type=float, default=10.0)
    ap.add_argument("--rsi-exit", type=float, default=70.0)
    ap.add_argument("--exit-sma", type=int, default=5)
    ap.add_argument("--max-positions", type=int, default=10)
    ap.add_argument("--min-dollar-vol", type=float, default=50e6, help="60-day median $ volume floor (liquidity / slippage)")
    ap.add_argument("--regime-file", default=None, help="CSV date,on from 07_breadth_regime; blocks new entries when off")
    ap.add_argument("--market-filter", default=None, help="index symbol (e.g. SPY): new entries only while it is above its SMA(trend)")
    ap.add_argument("--sizing", choices=["equal", "vol"], default="equal", help="vol: scale each slot by 1.5%% / ATR%% (risk parity)")
    ap.add_argument("--max-new-per-day", type=int, default=0, help="cap on new entries per day (0 = no cap)")
    ap.add_argument("--no-avoid-earnings", action="store_true", help="allow entries whose holding window contains an earnings print")
    ap.add_argument("--max-atr-pct", type=float, default=0.0, help="skip names with ATR(14) above this %% of price (0 = off)")
    ap.add_argument("--entry-atr", type=float, default=0.0, help="0 = market at next open; k>0 = limit at close - k x ATR(14)")
    ap.add_argument("--rank", choices=["rsi", "momentum"], default="rsi", help="slot priority: most oversold, or strongest 6-month return")
    ap.add_argument("--hedge-ratio", type=float, default=0.0, help="EOD short SPY = ratio x long book (0 = unhedged)")
    a = ap.parse_args()

    if a.mode == "pullback":
        if a.max_hold is None:
            a.max_hold = 6
        cost = costs.US_STOCK.scaled(a.cost_mult)
        if a.synthetic:
            frames = D.synthetic_panel([f"P{i:02d}" for i in range(40)], n=1500, seed=11)
        else:
            syms = list(a.symbols) + (D.load_symbol_list(a.universe, a.max_symbols) if a.universe else [])
            syms = list(dict.fromkeys(s for s in syms))
            frames = D.load_many(syms, None, None, strict=False)     # full history: indicators warm up before --start
        regime = None if a.synthetic else load_regime(a.regime_file)
        if not a.synthetic and a.market_filter:
            mkt = market_regime(frames, a.market_filter, a.trend_sma)
            regime = mkt if regime is None else (regime.reindex(mkt.index).ffill().fillna(True) & mkt)
        earnings: dict[str, np.ndarray] = {}
        if not a.synthetic and not a.no_avoid_earnings:
            for sym in frames:
                try:
                    earnings[sym] = np.sort(D.load_earnings(sym)["date"].to_numpy(dtype="datetime64[ns]"))
                except D.DataMissing:
                    pass
        params = dict(mode="pullback", universe=a.universe, n_symbols=len(frames), trend_sma=a.trend_sma, rsi_len=a.rsi_len,
                      rsi_entry=a.rsi_entry, rsi_exit=a.rsi_exit, exit_sma=a.exit_sma, max_hold=a.max_hold, stop_atr=a.stop_atr,
                      max_positions=a.max_positions, min_dollar_vol=a.min_dollar_vol, regime_file=a.regime_file,
                      market_filter=a.market_filter, sizing=a.sizing, max_new_per_day=a.max_new_per_day,
                      avoid_earnings=not a.no_avoid_earnings, names_with_earnings=len(earnings), max_atr_pct=a.max_atr_pct,
                      entry_atr=a.entry_atr, rank=a.rank, hedge_ratio=a.hedge_ratio,
                      cost_mult=a.cost_mult, start=a.start, end=a.end)
        r, tr, eq = run_pullback(frames, trend_sma=a.trend_sma, rsi_len=a.rsi_len, rsi_entry=a.rsi_entry, rsi_exit=a.rsi_exit,
                                 exit_sma=a.exit_sma, max_hold=a.max_hold, stop_atr=a.stop_atr, max_positions=a.max_positions,
                                 min_dollar_vol=0.0 if a.synthetic else a.min_dollar_vol, capital=a.capital, cost=cost,
                                 start=a.start, end=a.end, regime=regime, sizing=a.sizing, max_new_per_day=a.max_new_per_day,
                                 earnings=earnings, max_atr_pct=a.max_atr_pct, entry_atr=a.entry_atr, rank=a.rank,
                                 hedge_ratio=a.hedge_ratio)
        extra = {}
        if len(tr):
            active_days = max(1, int((r != 0).sum()))
            extra = {"exit_reasons": tr["reason"].value_counts().to_dict(), "symbols_traded": int(tr["symbol"].nunique()),
                     "avg_concurrent_positions": round(float(tr["days_held"].sum()) / active_days, 2)}
            if "hedge_pnl" in tr.attrs:
                extra["hedge_pnl"] = round(tr.attrs["hedge_pnl"], 2)
        report.save_and_print(f"{NAME}_{a.mode}", r, tr, eq, params, extra, synthetic=a.synthetic)
        return

    bars = D.synthetic_daily(2500, seed=1) if a.synthetic else D.load_daily(a.symbol, a.start, a.end)
    if a.max_hold is None:
        a.max_hold = 2 if a.mode == "nr7" else 10
    cost = costs.US_ETF.scaled(a.cost_mult)
    params = dict(symbol=a.symbol if not a.synthetic else "SYNTH", mode=a.mode, max_hold=a.max_hold, stop_atr=a.stop_atr,
                  short=a.short, trend_filter=not a.no_trend_filter, notional=a.notional, cost_mult=a.cost_mult)
    r, tr, eq = run(bars, a.mode, a.max_hold, a.stop_atr, a.short, not a.no_trend_filter, a.notional, a.capital, cost)
    report.save_and_print(f"{NAME}_{a.mode}", r, tr, eq, params, synthetic=a.synthetic)
    if a.grid:
        grid = {"max_hold": [3, 5, 10], "stop_atr": [None, 2.0]} if a.mode == "bb" else {"max_hold": [1, 2, 3], "stop_atr": [1.0, 1.5, 2.5]}
        tab, best, dsr, win = engine.grid_search(
            lambda max_hold, stop_atr: run(bars, a.mode, max_hold, stop_atr, a.short, not a.no_trend_filter, a.notional, a.capital, cost)[0],
            grid, bars.index)
        report.print_grid(tab, best, dsr, win)


if __name__ == "__main__":
    main()
