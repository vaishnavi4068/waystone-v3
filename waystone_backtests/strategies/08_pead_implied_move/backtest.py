#!/usr/bin/env python3
"""08 — Post-earnings-announcement drift, filtered by the size of the reaction vs the expected move.

For every earnings event: reaction return = first post-announcement close / last pre-announcement close - 1.
Expected move = implied_move_pct from the earnings file when present, else the symbol's own mean |reaction|
over its previous `--hist-events` events.  Long when the reaction beats the expected move upward (short,
optionally, when it misses downward); enter at the next open, hold `--hold` days with an ATR stop.

    python backtest.py --sp500 --max-symbols 120
    python backtest.py --symbols AAPL MSFT NVDA AMD
    python backtest.py --synthetic --grid
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wsbt import costs, data as D, engine, metrics as M, report  # noqa: E402
from wsbt.engine import TradeSpec  # noqa: E402

NAME = "08_pead_implied_move"


def atr(bars: pd.DataFrame, n: int = 14) -> pd.Series:
    tr = pd.concat([bars["high"] - bars["low"], (bars["high"] - bars["close"].shift()).abs(),
                    (bars["low"] - bars["close"].shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def reaction_table(bars: pd.DataFrame, earn: pd.DataFrame) -> pd.DataFrame:
    """One row per event: pre_date, reaction_date, pre_close, reaction_close, reaction_ret, gap."""
    idx = bars.index
    rows = []
    for _, e in earn.iterrows():
        d = pd.Timestamp(e["date"]).normalize()
        t = str(e.get("time", "unknown")).lower()
        if t == "bmo":
            after = idx[idx >= d]
            if len(after) == 0:
                continue
            r_day = after[0]
            before = idx[idx < r_day]
        else:                                           # amc or unknown: reaction is the NEXT session
            after = idx[idx > d]
            if len(after) == 0:
                continue
            r_day = after[0]
            before = idx[idx < r_day]
        if len(before) == 0:
            continue
        pre = before[-1]
        rows.append({"event_date": d, "time": t, "pre_date": pre, "reaction_date": r_day,
                     "pre_close": bars.at[pre, "close"], "reaction_close": bars.at[r_day, "close"],
                     "reaction_ret": bars.at[r_day, "close"] / bars.at[pre, "close"] - 1.0,
                     "gap": bars.at[r_day, "open"] / bars.at[pre, "close"] - 1.0,
                     "eps_est": e.get("eps_est", np.nan), "eps_act": e.get("eps_act", np.nan),
                     "implied_move_pct": e.get("implied_move_pct", np.nan)})
    cols = ["event_date", "time", "pre_date", "reaction_date", "pre_close", "reaction_close", "reaction_ret", "gap",
            "eps_est", "eps_act", "implied_move_pct"]
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows).sort_values("reaction_date").reset_index(drop=True)


def eps_surprise(e: pd.Series) -> float:
    """(actual - estimate) / |estimate|, floored at a 5c estimate so near-zero estimates don't explode.  NaN if missing."""
    if pd.isna(e.get("eps_est")) or pd.isna(e.get("eps_act")):
        return np.nan
    return float((e["eps_act"] - e["eps_est"]) / max(abs(float(e["eps_est"])), 0.05))


def build_specs(sym: str, bars: pd.DataFrame, rt: pd.DataFrame, move_mult: float, min_move: float, hold: int,
                stop_atr: float, hist_events: int, allow_short: bool, require_beat: bool, min_eps_surprise: float = 0.0,
                max_clv: float = 1.0, trend: str = "any", rank: str = "surprise", trend_sma: int = 200,
                gap_stop: bool = False, sizing: str = "equal", per_trade: float = 0.0, ref_atr_pct: float = 2.5) -> list[TradeSpec]:
    """min_eps_surprise > 0: longs need an EPS beat of at least that fraction (shorts a miss); events without EPS data are
    skipped.  max_clv < 1: skip reaction days that closed in the top of their range (the intraday overshoot fades).
    trend: 'below' = only names under their SMA before the event (out-of-favour beats drift most), 'above' = the reverse.
    gap_stop: exit if the stock gives back the whole earnings move (stop at the pre-announcement close) — the drift
    thesis is void once the gap has filled.  sizing='vol' scales the slot by ref_atr_pct / ATR% (clipped 0.25..1)."""
    a = atr(bars)
    sma = bars["close"].rolling(trend_sma).mean()
    idx = bars.index
    specs = []
    for k, e in rt.iterrows():
        implied = e["implied_move_pct"] / 100.0 if pd.notna(e["implied_move_pct"]) else np.nan
        if np.isnan(implied):
            prev = rt.iloc[max(0, k - hist_events):k]
            if len(prev) < min(4, hist_events):
                continue
            implied = float(prev["reaction_ret"].abs().mean())
        r = e["reaction_ret"]
        eps_s = eps_surprise(e)
        beat_ok = True
        if require_beat and not np.isnan(eps_s):
            beat_ok = (eps_s > 0) if r > 0 else (eps_s < 0)
        if min_eps_surprise > 0:
            if np.isnan(eps_s):
                continue
            beat_ok = beat_ok and ((eps_s >= min_eps_surprise) if r > 0 else (eps_s <= -min_eps_surprise))
        rd = e["reaction_date"]
        nxt = idx[idx > rd]
        if len(nxt) == 0 or np.isnan(a.get(rd, np.nan)):
            continue
        if trend != "any":
            s = sma.get(e["pre_date"], np.nan)
            if np.isnan(s):
                continue
            below = bars.at[e["pre_date"], "close"] < s
            if (trend == "below") != below:
                continue
        hi, lo, cl = bars.at[rd, "high"], bars.at[rd, "low"], bars.at[rd, "close"]
        clv = (cl - lo) / (hi - lo) if hi > lo else 0.5
        if max_clv < 1.0:
            top = clv if r > 0 else 1.0 - clv         # for a short the mirror image: closing at the low is the overshoot
            if top > max_clv:
                continue
        entry_day = nxt[0]
        thresh = max(move_mult * implied, min_move)
        if r >= thresh and beat_ok:
            side = 1
        elif allow_short and r <= -thresh and beat_ok:
            side = -1
        else:
            continue
        stop = cl - side * stop_atr * a.at[rd] if stop_atr else None
        if gap_stop:
            stop = float(e["pre_close"])
        surprise = abs(float(r)) / max(float(implied), 1e-6)
        meta_size = {}
        if sizing == "vol" and per_trade > 0:
            atr_pct = 100.0 * float(a.at[rd]) / cl
            meta_size["notional"] = per_trade * float(np.clip(ref_atr_pct / max(atr_pct, 1e-6), 0.25, 1.0))
        if rank == "eps":
            score = abs(eps_s) if not np.isnan(eps_s) else 0.0
        elif rank == "combo":                                 # both normalised roughly to "x expected": eps beat in 10% units
            score = surprise + (abs(eps_s) / 0.10 if not np.isnan(eps_s) else 0.0)
        else:
            score = surprise
        specs.append(TradeSpec(date=entry_day, side=side, entry="open", stop=stop, max_hold=hold, tag=sym,
                               meta={"rank": -score, "event_date": e["event_date"], "reaction_ret": round(float(r), 4),
                                     "expected_move": round(float(implied), 4), "surprise_x": round(surprise, 2),
                                     "eps_surprise": None if np.isnan(eps_s) else round(eps_s, 3), "clv": round(float(clv), 2),
                                     **meta_size}))
    return specs


def run(frames: dict, earnings: dict, move_mult: float, min_move: float, hold: int, stop_atr: float | None, hist_events: int,
        allow_short: bool, require_beat: bool, max_positions: int, capital: float, cost=costs.US_STOCK,
        start: str | None = None, end: str | None = None, min_eps_surprise: float = 0.0, max_clv: float = 1.0,
        trend: str = "any", rank: str = "surprise", hedge_ratio: float = 0.0, hedge_bars: pd.DataFrame | None = None,
        gap_stop: bool = False, sizing: str = "equal"):
    """One portfolio: global cap of max_positions concurrent names, highest `rank` score first.
    hedge_ratio > 0 shorts SPY = ratio x the long book at every close (strips the beta the event study says is most of
    the unconditioned 'drift')."""
    per_trade = capital / max_positions
    specs: dict[str, list[TradeSpec]] = {}
    sim_frames: dict[str, pd.DataFrame] = {}
    s0 = pd.Timestamp(start) if start else None
    e0 = pd.Timestamp(end) if end else None
    n_events = 0
    for sym, bars in frames.items():
        if sym not in earnings or len(bars) < 60:
            continue
        rt = reaction_table(bars, earnings[sym])
        if not len(rt):
            continue
        if s0 is not None:
            rt = rt[rt["reaction_date"] >= s0 - pd.Timedelta(days=5)].reset_index(drop=True)
        if e0 is not None:
            rt = rt[rt["reaction_date"] <= e0].reset_index(drop=True)
        n_events += len(rt)
        sp = build_specs(sym, bars, rt, move_mult, min_move, hold, stop_atr, hist_events, allow_short, require_beat,
                         min_eps_surprise=min_eps_surprise, max_clv=max_clv, trend=trend, rank=rank, gap_stop=gap_stop,
                         sizing=sizing, per_trade=per_trade)
        cut = bars
        if s0 is not None:
            cut = cut[cut.index >= s0]
            sp = [s for s in sp if pd.Timestamp(s.date) >= s0]
        if e0 is not None:
            cut = cut[cut.index <= e0]
        if not sp or not len(cut):
            continue
        specs[sym] = sp
        sim_frames[sym] = cut
    if not specs:
        empty = pd.Series(dtype=float)
        return empty, pd.DataFrame(), empty, n_events
    hedge = None
    if hedge_ratio > 0 and hedge_bars is not None:
        hedge = (hedge_bars, hedge_ratio, costs.US_ETF.scaled(cost.slippage_bps / max(costs.US_STOCK.slippage_bps, 1e-9)))
    size = {"notional": per_trade, "per_spec": sizing == "vol"}
    daily, tr, eq = engine.simulate_trades_multi(sim_frames, specs, cost, size, capital=capital,
                                                 max_positions=max_positions, hedge=hedge)
    return daily, tr, eq, n_events


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=[])
    ap.add_argument("--sp500", action="store_true")
    ap.add_argument("--list", default=None, help="symbol list CSV in data/ (e.g. nsdq250.csv)")
    ap.add_argument("--max-symbols", type=int, default=None)
    ap.add_argument("--move-mult", type=float, default=1.0, help="reaction must exceed this x expected move")
    ap.add_argument("--min-move", type=float, default=0.02, help="absolute floor on the reaction (2%%)")
    ap.add_argument("--hold", type=int, default=10)
    ap.add_argument("--stop-atr", type=float, default=2.0, help="ATR(14) stop off the reaction close (0 = none)")
    ap.add_argument("--hist-events", type=int, default=8)
    ap.add_argument("--short", action="store_true")
    ap.add_argument("--require-eps-beat", action="store_true")
    ap.add_argument("--min-eps-surprise", type=float, default=0.0,
                    help="longs need (act-est)/|est| >= this (0.10 = 10%% beat); events without EPS data are skipped when > 0")
    ap.add_argument("--max-clv", type=float, default=1.0, help="skip reaction days closing above this fraction of their range (1 = off)")
    ap.add_argument("--trend", choices=["any", "below", "above"], default="any", help="pre-event close vs SMA200 filter")
    ap.add_argument("--rank", choices=["surprise", "eps", "combo"], default="surprise", help="slot priority when entries compete")
    ap.add_argument("--hedge-ratio", type=float, default=0.0, help="EOD short SPY = ratio x long book (0 = unhedged)")
    ap.add_argument("--gap-stop", action="store_true", help="thesis stop at the pre-announcement close (gap filled = exit)")
    ap.add_argument("--sizing", choices=["equal", "vol"], default="equal", help="vol = slot scaled by 2.5%% / ATR%%")
    ap.add_argument("--max-positions", type=int, default=10)
    ap.add_argument("--capital", type=float, default=100_000.0)
    ap.add_argument("--cost-mult", type=float, default=1.0, help="scale commission + slippage (2.0 = stage-gate cost stress)")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--grid", action="store_true")
    a = ap.parse_args()
    if a.stop_atr is not None and a.stop_atr <= 0:
        a.stop_atr = None
    cost = costs.US_STOCK.scaled(a.cost_mult)

    if a.synthetic:
        syms = [f"E{i:02d}" for i in range(30)]
        frames = D.synthetic_panel(syms, n=2200, seed=23)
        first = next(iter(frames.values()))
        earnings = D.synthetic_earnings(syms, str(first.index[0].date()), str(first.index[-1].date()))
        for e in earnings.values():
            e.drop(columns=["implied_move_pct"], inplace=True)      # exercise the history-based expected move
        hedge_bars = None
    else:
        syms = list(a.symbols) + (D.load_symbol_list(max_symbols=a.max_symbols) if a.sp500 else [])
        if a.list:
            syms += D.load_symbol_list(a.list, a.max_symbols)
        syms = list(dict.fromkeys(syms))
        frames = D.load_many(syms, None, a.end, strict=False)          # full history: ATR / event history warm up before --start
        earnings = {}
        hedge_bars = None
        if a.hedge_ratio > 0:
            try:
                hedge_bars = D.load_daily("SPY", None, a.end)
            except D.DataMissing:
                print("  SPY not found — running unhedged")
        for s in list(frames):
            try:
                earnings[s] = D.load_earnings(s)
            except D.DataMissing:
                pass
        if not earnings:
            raise SystemExit("no earnings files — run: python tools/fetch_yf.py --earnings --symbols ... (or --list nsdq250.csv)")
    params = dict(move_mult=a.move_mult, min_move=a.min_move, hold=a.hold, stop_atr=a.stop_atr, hist_events=a.hist_events,
                  short=a.short, require_eps_beat=a.require_eps_beat, min_eps_surprise=a.min_eps_surprise, max_clv=a.max_clv,
                  trend=a.trend, rank=a.rank, hedge_ratio=a.hedge_ratio if hedge_bars is not None else 0.0,
                  gap_stop=a.gap_stop, sizing=a.sizing,
                  max_positions=a.max_positions, n_symbols=len(frames), list=a.list, cost_mult=a.cost_mult, start=a.start, end=a.end)
    kw = dict(min_eps_surprise=a.min_eps_surprise, max_clv=a.max_clv, trend=a.trend, rank=a.rank,
              hedge_ratio=a.hedge_ratio, hedge_bars=hedge_bars, gap_stop=a.gap_stop, sizing=a.sizing)
    r, tr, eq, n_events = run(frames, earnings, a.move_mult, a.min_move, a.hold, a.stop_atr, a.hist_events, a.short,
                              a.require_eps_beat, a.max_positions, a.capital, cost, a.start, a.end, **kw)
    extra = {"earnings_events": n_events, "symbols_with_earnings": len(earnings)}
    if len(tr):
        extra["exit_reasons"] = tr["reason"].value_counts().to_dict()
        extra["avg_reaction_traded_pct"] = round(100 * float(tr["reaction_ret"].mean()), 2)
        extra["symbols_traded"] = int(tr["symbol"].nunique())
        if "eps_surprise" in tr.columns:
            extra["median_eps_surprise_traded"] = round(float(pd.to_numeric(tr["eps_surprise"], errors="coerce").median()), 3)
        if r.attrs.get("hedge_pnl") is not None:
            extra["hedge_pnl"] = round(float(r.attrs["hedge_pnl"]), 2)
    report.save_and_print(NAME, r, tr, eq, params, extra, synthetic=a.synthetic)
    if a.grid and len(r):
        grid = {"move_mult": [0.75, 1.0, 1.5], "hold": [5, 10, 20]}
        tab, best, dsr, win = engine.grid_search(
            lambda move_mult, hold: run(frames, earnings, move_mult, a.min_move, hold, a.stop_atr, a.hist_events, a.short,
                                        a.require_eps_beat, a.max_positions, a.capital, cost, a.start, a.end, **kw)[0].reindex(r.index).fillna(0.0),
            grid, r.index)
        report.print_grid(tab, best, dsr, win)


if __name__ == "__main__":
    main()
