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
    return pd.DataFrame(rows).sort_values("reaction_date").reset_index(drop=True)


def build_specs(sym: str, bars: pd.DataFrame, rt: pd.DataFrame, move_mult: float, min_move: float, hold: int,
                stop_atr: float, hist_events: int, allow_short: bool, require_beat: bool) -> list[TradeSpec]:
    a = atr(bars)
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
        beat_ok = True
        if require_beat and pd.notna(e["eps_est"]) and pd.notna(e["eps_act"]):
            beat_ok = (e["eps_act"] > e["eps_est"]) if r > 0 else (e["eps_act"] < e["eps_est"])
        nxt = idx[idx > e["reaction_date"]]
        if len(nxt) == 0 or np.isnan(a.get(e["reaction_date"], np.nan)):
            continue
        entry_day = nxt[0]
        thresh = max(move_mult * implied, min_move)
        if r >= thresh and beat_ok:
            side = 1
        elif allow_short and r <= -thresh and beat_ok:
            side = -1
        else:
            continue
        ref = bars.at[e["reaction_date"], "close"]
        stop = ref - side * stop_atr * a.at[e["reaction_date"]]
        specs.append(TradeSpec(date=entry_day, side=side, entry="open", stop=stop, max_hold=hold, tag=sym,
                               meta={"symbol": sym, "event_date": e["event_date"], "reaction_ret": round(float(r), 4),
                                     "expected_move": round(float(implied), 4)}))
    return specs


def run(frames: dict, earnings: dict, move_mult: float, min_move: float, hold: int, stop_atr: float, hist_events: int,
        allow_short: bool, require_beat: bool, max_positions: int, capital: float):
    per_trade = capital / max_positions
    rets, trades, n_events = [], [], 0
    for sym, bars in frames.items():
        if sym not in earnings or len(bars) < 60:
            continue
        rt = reaction_table(bars, earnings[sym])
        n_events += len(rt)
        specs = build_specs(sym, bars, rt, move_mult, min_move, hold, stop_atr, hist_events, allow_short, require_beat)
        if not specs:
            continue
        r, tr, _ = engine.simulate_trades(bars, specs, costs.US_STOCK, {"notional": per_trade}, capital=capital, max_concurrent=1)
        rets.append(r)
        if len(tr):
            trades.append(tr)
    if not rets:
        empty = pd.Series(dtype=float)
        return empty, pd.DataFrame(), empty, n_events
    daily = pd.concat(rets, axis=1).fillna(0.0).sum(axis=1).sort_index()
    tr = pd.concat(trades, ignore_index=True) if trades else pd.DataFrame()
    return daily, tr, M.equity_from_returns(daily, capital), n_events


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=[])
    ap.add_argument("--sp500", action="store_true")
    ap.add_argument("--max-symbols", type=int, default=120)
    ap.add_argument("--move-mult", type=float, default=1.0, help="reaction must exceed this x expected move")
    ap.add_argument("--min-move", type=float, default=0.02, help="absolute floor on the reaction (2%%)")
    ap.add_argument("--hold", type=int, default=10)
    ap.add_argument("--stop-atr", type=float, default=2.0)
    ap.add_argument("--hist-events", type=int, default=8)
    ap.add_argument("--short", action="store_true")
    ap.add_argument("--require-eps-beat", action="store_true")
    ap.add_argument("--max-positions", type=int, default=10)
    ap.add_argument("--capital", type=float, default=100_000.0)
    ap.add_argument("--start", default=None)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--grid", action="store_true")
    a = ap.parse_args()

    if a.synthetic:
        syms = [f"E{i:02d}" for i in range(30)]
        frames = D.synthetic_panel(syms, n=2200, seed=23)
        first = next(iter(frames.values()))
        earnings = D.synthetic_earnings(syms, str(first.index[0].date()), str(first.index[-1].date()))
        for e in earnings.values():
            e.drop(columns=["implied_move_pct"], inplace=True)      # exercise the history-based expected move
    else:
        syms = list(a.symbols) + (D.load_symbol_list(max_symbols=a.max_symbols) if a.sp500 else [])
        frames = D.load_many(syms, a.start, strict=False)
        earnings = {}
        for s in list(frames):
            try:
                earnings[s] = D.load_earnings(s)
            except D.DataMissing:
                pass
        if not earnings:
            raise SystemExit("no earnings files — run: python tools/fetch_yf.py --earnings --symbols ... (or --sp500)")
    params = dict(move_mult=a.move_mult, min_move=a.min_move, hold=a.hold, stop_atr=a.stop_atr, hist_events=a.hist_events,
                  short=a.short, require_eps_beat=a.require_eps_beat, max_positions=a.max_positions, n_symbols=len(frames))
    r, tr, eq, n_events = run(frames, earnings, a.move_mult, a.min_move, a.hold, a.stop_atr, a.hist_events, a.short,
                              a.require_eps_beat, a.max_positions, a.capital)
    extra = {"earnings_events": n_events, "symbols_with_earnings": len(earnings)}
    if len(tr):
        extra["exit_reasons"] = tr["reason"].value_counts().to_dict()
        extra["avg_reaction_traded_pct"] = round(100 * float(tr["reaction_ret"].mean()), 2)
    report.save_and_print(NAME, r, tr, eq, params, extra, synthetic=a.synthetic)
    if a.grid and len(r):
        grid = {"move_mult": [0.75, 1.0, 1.5], "hold": [5, 10, 20]}
        tab, best, dsr, win = engine.grid_search(
            lambda move_mult, hold: run(frames, earnings, move_mult, a.min_move, hold, a.stop_atr, a.hist_events, a.short,
                                        a.require_eps_beat, a.max_positions, a.capital)[0].reindex(r.index).fillna(0.0), grid, r.index)
        report.print_grid(tab, best, dsr, win)


if __name__ == "__main__":
    main()
