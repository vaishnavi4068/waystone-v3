"""Simulation primitives.  Three of them cover every strategy in this repo:

  simulate_positions  — one instrument, a target position decided at each close, filled at the next open.
  simulate_trades     — discrete trades with stop / target / time exits evaluated on daily OHLC.
  simulate_weights    — a portfolio of weights decided at each close, rebalanced at the next open.

All three return (daily_ret, trades, equity).  daily_ret is P&L / capital with a CONSTANT
capital base (no compounding) so that strategies are comparable and additive; equity is the
compounded curve for the drawdown statistics.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from .costs import CostModel
from . import metrics as M


def _units(size: dict, price: float) -> float:
    if "units" in size:
        return float(size["units"])
    if "notional" in size:
        return float(size["notional"]) / price
    raise ValueError("size must have 'units' or 'notional'")


# ══════════════════════════════════════════════════════════════════════════════
# 1. Target-position simulator
# ══════════════════════════════════════════════════════════════════════════════
def simulate_positions(bars: pd.DataFrame, target: pd.Series, cost: CostModel, size: dict,
                       capital: float = 100_000.0):
    """target[t] = desired signed position decided at the CLOSE of bar t (+1/-1/0 or any float).
    It is filled at the OPEN of bar t+1.  Marked to market at every close."""
    bars = bars.sort_index()
    tgt = target.reindex(bars.index).fillna(0.0).to_numpy(dtype=float)
    o, c = bars["open"].to_numpy(), bars["close"].to_numpy()
    n = len(bars)
    pos, units, entry_fill, entry_i = 0.0, 0.0, 0.0, -1
    pnl_day = np.zeros(n)
    trades = []
    prev_close = c[0]
    for i in range(1, n):
        want = tgt[i - 1]
        realised = 0.0
        if want != pos:
            if pos != 0.0:                                  # close the existing position at the open
                side = -1 if pos > 0 else 1
                fill = cost.fill(o[i], side)
                gross = (fill - entry_fill) * np.sign(pos) * units * cost.multiplier
                comm = 2 * cost.commission * units
                pnl = gross - comm
                trades.append({"entry_date": bars.index[entry_i], "exit_date": bars.index[i], "side": int(np.sign(pos)),
                               "units": units, "entry": entry_fill, "exit": fill, "pnl": pnl,
                               "days_held": i - entry_i, "reason": "signal"})
                # mark: from previous close to the exit fill
                realised += (fill - prev_close) * np.sign(pos) * units * cost.multiplier - comm
            if want != 0.0:
                side = 1 if want > 0 else -1
                entry_fill = cost.fill(o[i], side)
                units = _units(size, o[i]) * abs(want)
                entry_i = i
                # first day's mark: from the fill to the close
                realised += (c[i] - entry_fill) * np.sign(want) * units * cost.multiplier
            pos = want
        elif pos != 0.0:
            realised += (c[i] - prev_close) * np.sign(pos) * units * cost.multiplier
        pnl_day[i] = realised
        prev_close = c[i]
    if pos != 0.0:                                          # close out at the last close for reporting
        fill = cost.fill(c[-1], -1 if pos > 0 else 1)
        gross = (fill - entry_fill) * np.sign(pos) * units * cost.multiplier
        comm = 2 * cost.commission * units
        trades.append({"entry_date": bars.index[entry_i], "exit_date": bars.index[-1], "side": int(np.sign(pos)),
                       "units": units, "entry": entry_fill, "exit": fill, "pnl": gross - comm,
                       "days_held": n - 1 - entry_i, "reason": "end"})
        pnl_day[-1] += (fill - c[-1]) * np.sign(pos) * units * cost.multiplier - comm
    daily_ret = pd.Series(pnl_day / capital, index=bars.index)
    return daily_ret, pd.DataFrame(trades), M.equity_from_returns(daily_ret, capital)


# ══════════════════════════════════════════════════════════════════════════════
# 2. Discrete-trade simulator (stops, targets, time exits on daily OHLC)
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class TradeSpec:
    date: pd.Timestamp                 # the day the ENTRY is attempted (must be > signal day)
    side: int                          # +1 long, -1 short
    entry: str = "open"                # "open" | "close" | "stop" | "limit"  (stop/limit: level in entry_level, fill if touched)
    entry_level: float | None = None
    stop: float | None = None          # protective stop (price)
    target: float | None = None        # profit target (price)
    max_hold: int = 1                  # exit at the close of the max_hold-th day (entry day = 1)
    exit_on: Callable[[pd.Series, int], bool] | None = None   # evaluated at each close; True -> exit at that close
    trail_atr: float | None = None     # trailing stop distance in price units (if set, stop trails)
    tag: str = ""
    meta: dict = field(default_factory=dict)


def simulate_trades(bars: pd.DataFrame, specs: list[TradeSpec], cost: CostModel, size: dict,
                    capital: float = 100_000.0, max_concurrent: int = 1, conservative: bool = True):
    """Runs each TradeSpec against the daily OHLC path.  Same-day stop AND target -> stop wins
    when conservative=True.  Overlapping trades are allowed up to max_concurrent."""
    bars = bars.sort_index()
    idx = bars.index
    pos_of = {d: i for i, d in enumerate(idx)}
    o, h, l, c = (bars[k].to_numpy() for k in ("open", "high", "low", "close"))
    n = len(bars)
    pnl_day = np.zeros(n)
    open_trades: list[dict] = []
    trades = []
    specs_by_day: dict[int, list[TradeSpec]] = {}
    for s in specs:
        i = pos_of.get(pd.Timestamp(s.date))
        if i is not None and i > 0:
            specs_by_day.setdefault(i, []).append(s)

    def close_trade(tr, i, px, reason):
        fill = cost.fill(px, -tr["side"])
        gross = (fill - tr["entry_fill"]) * tr["side"] * tr["units"] * cost.multiplier
        comm = 2 * cost.commission * tr["units"]
        trades.append({"entry_date": idx[tr["i0"]], "exit_date": idx[i], "side": tr["side"], "units": tr["units"],
                       "entry": tr["entry_fill"], "exit": fill, "pnl": gross - comm, "days_held": i - tr["i0"] + 1,
                       "reason": reason, "tag": tr["spec"].tag, **tr["spec"].meta})
        return (fill - tr["mark"]) * tr["side"] * tr["units"] * cost.multiplier - comm

    for i in range(n):
        day_pnl = 0.0
        # ---- entries at the open (or stop-through-the-day) ----
        for s in specs_by_day.get(i, []):
            if len(open_trades) >= max_concurrent:
                continue
            fill = None
            if s.entry == "open":
                fill = cost.fill(o[i], s.side)
            elif s.entry == "close":
                fill = cost.fill(c[i], s.side)
            elif s.entry == "stop" and s.entry_level is not None:
                if s.side > 0 and h[i] >= s.entry_level:
                    fill = cost.fill(max(o[i], s.entry_level), 1)
                elif s.side < 0 and l[i] <= s.entry_level:
                    fill = cost.fill(min(o[i], s.entry_level), -1)
            elif s.entry == "limit" and s.entry_level is not None:      # passive: filled only if the day trades through
                if s.side > 0 and l[i] <= s.entry_level:
                    fill = cost.fill(min(o[i], s.entry_level), 1)
                elif s.side < 0 and h[i] >= s.entry_level:
                    fill = cost.fill(max(o[i], s.entry_level), -1)
            if fill is None:
                continue
            units = _units(size, fill)
            open_trades.append({"spec": s, "side": s.side, "units": units, "entry_fill": fill, "i0": i, "mark": fill,
                                "stop": s.stop, "target": s.target, "entered_at_close": s.entry == "close",
                                "best": fill})
        # ---- manage open trades through the day ----
        still = []
        for tr in open_trades:
            s = tr["spec"]
            side = tr["side"]
            exited = False
            if not (tr["i0"] == i and tr["entered_at_close"]):
                # trailing stop update uses the previous close's best excursion
                if s.trail_atr is not None:
                    tr["best"] = max(tr["best"], h[i]) if side > 0 else min(tr["best"], l[i])
                    trail = tr["best"] - s.trail_atr if side > 0 else tr["best"] + s.trail_atr
                    tr["stop"] = trail if tr["stop"] is None else (max(tr["stop"], trail) if side > 0 else min(tr["stop"], trail))
                hit_stop = tr["stop"] is not None and ((side > 0 and l[i] <= tr["stop"]) or (side < 0 and h[i] >= tr["stop"]))
                hit_tgt = tr["target"] is not None and ((side > 0 and h[i] >= tr["target"]) or (side < 0 and l[i] <= tr["target"]))
                if hit_stop and (conservative or not hit_tgt):
                    px = tr["stop"]
                    if tr["i0"] == i:                    # entered today: cannot be better than the open
                        px = min(px, o[i]) if side > 0 else max(px, o[i])
                    elif (side > 0 and o[i] < tr["stop"]) or (side < 0 and o[i] > tr["stop"]):
                        px = o[i]                        # gapped through the stop
                    day_pnl += close_trade(tr, i, px, "stop"); exited = True
                elif hit_tgt:
                    px = tr["target"]
                    if (side > 0 and o[i] > px) or (side < 0 and o[i] < px):
                        px = o[i]
                    day_pnl += close_trade(tr, i, px, "target"); exited = True
            if not exited:
                held = i - tr["i0"] + 1
                if (s.exit_on is not None and s.exit_on(bars.iloc[i], i)) or held >= s.max_hold or i == n - 1:
                    reason = "time" if held >= s.max_hold else ("rule" if s.exit_on is not None else "end")
                    day_pnl += close_trade(tr, i, c[i], reason); exited = True
            if not exited:
                day_pnl += (c[i] - tr["mark"]) * side * tr["units"] * cost.multiplier
                tr["mark"] = c[i]
                still.append(tr)
        open_trades = still
        pnl_day[i] = day_pnl
    daily_ret = pd.Series(pnl_day / capital, index=idx)
    return daily_ret, pd.DataFrame(trades), M.equity_from_returns(daily_ret, capital)


def simulate_trades_multi(frames: dict[str, pd.DataFrame], specs: dict[str, list[TradeSpec]], cost: CostModel,
                          size: dict, capital: float = 100_000.0, max_positions: int = 10, conservative: bool = True,
                          hedge: tuple[pd.DataFrame, float, CostModel] | None = None):
    """Portfolio version of `simulate_trades`: one OHLC frame per symbol, a GLOBAL cap of `max_positions`
    open trades and at most one open trade per symbol.  When more entries compete for a slot on the same
    day the lowest `spec.meta['rank']` wins (ties: symbol order).  Returns (daily_ret, trades, equity)
    on the union calendar; each trade row carries its symbol.

    hedge=(index_bars, ratio, index_cost): end-of-day beta hedge.  At every close the short index notional is
    reset to ratio x the marked long book; its P&L accrues close-to-close and each adjustment pays index costs.
    Intraday exposure on entry days is unhedged (that is how an EOD-hedged desk actually runs)."""
    calendar = sorted(set().union(*[set(f.index) for f in frames.values()]))
    idx = pd.DatetimeIndex(calendar)
    arr: dict[str, dict] = {}
    for sym, bars in frames.items():
        b = bars.sort_index()
        arr[sym] = {"pos": {d: i for i, d in enumerate(b.index)}, "bars": b,
                    **{k: b[k].to_numpy() for k in ("open", "high", "low", "close")}}
    by_day: dict[pd.Timestamp, list[tuple[str, TradeSpec]]] = {}
    for sym, lst in specs.items():
        if sym not in arr:
            continue
        for s in lst:
            d = pd.Timestamp(s.date)
            if d in arr[sym]["pos"] and arr[sym]["pos"][d] > 0:
                by_day.setdefault(d, []).append((sym, s))
    n = len(idx)
    pnl_day = np.zeros(n)
    hedge_pnl = np.zeros(n)
    open_trades: list[dict] = []
    trades = []
    h_close = h_units = h_prev_close = None
    if hedge is not None:
        h_bars, h_ratio, h_cost = hedge
        h_close = h_bars["close"].reindex(idx).ffill()
        h_units, h_prev_close = 0.0, float(h_close.iloc[0])

    def close_trade(tr, d, px, reason):
        fill = cost.fill(px, -tr["side"])
        gross = (fill - tr["entry_fill"]) * tr["side"] * tr["units"] * cost.multiplier
        comm = 2 * cost.commission * tr["units"]
        trades.append({"symbol": tr["sym"], "entry_date": tr["d0"], "exit_date": d, "side": tr["side"], "units": tr["units"],
                       "entry": tr["entry_fill"], "exit": fill, "pnl": gross - comm, "days_held": tr["held"],
                       "reason": reason, "tag": tr["spec"].tag, **tr["spec"].meta})
        return (fill - tr["mark"]) * tr["side"] * tr["units"] * cost.multiplier - comm

    for gi, d in enumerate(idx):
        day_pnl = 0.0
        busy = {tr["sym"] for tr in open_trades}
        cands = sorted(by_day.get(d, []), key=lambda t: (t[1].meta.get("rank", 0), t[0]))
        for sym, s in cands:
            if len(open_trades) >= max_positions or sym in busy:
                continue
            a = arr[sym]
            i = a["pos"][d]
            fill = None
            if s.entry == "open":
                fill = cost.fill(a["open"][i], s.side)
            elif s.entry == "close":
                fill = cost.fill(a["close"][i], s.side)
            elif s.entry == "stop" and s.entry_level is not None:
                if s.side > 0 and a["high"][i] >= s.entry_level:
                    fill = cost.fill(max(a["open"][i], s.entry_level), 1)
                elif s.side < 0 and a["low"][i] <= s.entry_level:
                    fill = cost.fill(min(a["open"][i], s.entry_level), -1)
            elif s.entry == "limit" and s.entry_level is not None:      # passive: filled only if the day trades through
                if s.side > 0 and a["low"][i] <= s.entry_level:
                    fill = cost.fill(min(a["open"][i], s.entry_level), 1)
                elif s.side < 0 and a["high"][i] >= s.entry_level:
                    fill = cost.fill(max(a["open"][i], s.entry_level), -1)
            if fill is None:
                continue
            if size.get("per_spec") and "notional" in s.meta:       # spec-level notional (e.g. vol-scaled slot)
                units = float(s.meta["notional"]) / fill
            else:
                units = _units(size, fill)
            open_trades.append({"spec": s, "sym": sym, "side": s.side, "units": units, "entry_fill": fill, "d0": d, "held": 0,
                                "mark": fill, "stop": s.stop, "target": s.target, "entered_at_close": s.entry == "close",
                                "best": fill, "new": True})
            busy.add(sym)
        still = []
        for tr in open_trades:
            a = arr[tr["sym"]]
            i = a["pos"].get(d)
            if i is None:                                   # symbol has no bar today (halt / data gap): carry
                still.append(tr)
                continue
            s, side = tr["spec"], tr["side"]
            tr["held"] += 1
            o, h, l, c = a["open"][i], a["high"][i], a["low"][i], a["close"][i]
            exited = False
            if not (tr["new"] and tr["entered_at_close"]):
                if s.trail_atr is not None:
                    tr["best"] = max(tr["best"], h) if side > 0 else min(tr["best"], l)
                    trail = tr["best"] - s.trail_atr if side > 0 else tr["best"] + s.trail_atr
                    tr["stop"] = trail if tr["stop"] is None else (max(tr["stop"], trail) if side > 0 else min(tr["stop"], trail))
                hit_stop = tr["stop"] is not None and ((side > 0 and l <= tr["stop"]) or (side < 0 and h >= tr["stop"]))
                hit_tgt = tr["target"] is not None and ((side > 0 and h >= tr["target"]) or (side < 0 and l <= tr["target"]))
                if hit_stop and (conservative or not hit_tgt):
                    px = tr["stop"]
                    if tr["new"]:
                        px = min(px, o) if side > 0 else max(px, o)
                    elif (side > 0 and o < tr["stop"]) or (side < 0 and o > tr["stop"]):
                        px = o
                    day_pnl += close_trade(tr, d, px, "stop"); exited = True
                elif hit_tgt:
                    px = tr["target"]
                    if (side > 0 and o > px) or (side < 0 and o < px):
                        px = o
                    day_pnl += close_trade(tr, d, px, "target"); exited = True
            if not exited:
                row = a["bars"].iloc[i]
                if (s.exit_on is not None and s.exit_on(row, i)) or tr["held"] >= s.max_hold or gi == n - 1:
                    reason = "time" if tr["held"] >= s.max_hold else ("rule" if s.exit_on is not None else "end")
                    day_pnl += close_trade(tr, d, c, reason); exited = True
            if not exited:
                day_pnl += (c - tr["mark"]) * side * tr["units"] * cost.multiplier
                tr["mark"] = c
                tr["new"] = False
                still.append(tr)
        open_trades = still
        if hedge is not None:
            px = float(h_close.iloc[gi])
            hp = h_units * (px - h_prev_close)                                   # yesterday's hedge, close -> close
            long_book = sum(tr["units"] * tr["mark"] * tr["side"] for tr in open_trades)
            target = -h_ratio * long_book / px if px > 0 else 0.0
            delta = abs(target - h_units)
            if delta > 0:
                hp -= delta * (px * h_cost.slippage_bps / 1e4 + h_cost.slippage_abs + h_cost.commission)
            h_units, h_prev_close = target, px
            hedge_pnl[gi] = hp
            day_pnl += hp
        pnl_day[gi] = day_pnl
    daily_ret = pd.Series(pnl_day / capital, index=idx)
    out = pd.DataFrame(trades)
    if hedge is not None:
        out.attrs["hedge_pnl"] = float(hedge_pnl.sum())
    return daily_ret, out, M.equity_from_returns(daily_ret, capital)


# ══════════════════════════════════════════════════════════════════════════════
# 3. Portfolio-of-weights simulator
# ══════════════════════════════════════════════════════════════════════════════
def simulate_weights(frames: dict[str, pd.DataFrame], weights: pd.DataFrame, cost_bps: float = 5.0,
                     capital: float = 100_000.0, cash_yield: pd.Series | None = None):
    """weights: rows = decision dates (at the close), cols = symbols, values = target weight (sum <= 1).
    Executed at the NEXT open; the first day's return is open->close, later days close->close.
    Turnover cost = cost_bps x |Δw| on each rebalance.

    cash_yield: optional annualised % series (e.g. ^IRX); the uninvested fraction earns it daily.

    Returns (daily_ret, trades, equity).  `trades` has one row per REBALANCE (date, turnover) plus
    `trades.attrs['legs']`: a DataFrame of per-symbol round trips (weight goes 0 -> >0 -> 0) with
    entry/exit dates and $ P&L net of that symbol's share of the turnover cost."""
    syms = list(weights.columns)
    closes = pd.DataFrame({s: frames[s]["close"] for s in syms}).sort_index()
    opens = pd.DataFrame({s: frames[s]["open"] for s in syms}).sort_index()
    idx = closes.index
    w_target = weights.reindex(idx).ffill().fillna(0.0)
    w_prev = pd.Series(0.0, index=syms)
    held = pd.Series(0.0, index=syms)                          # weights currently held (drift ignored)
    rets = np.zeros(len(idx))
    turnover = np.zeros(len(idx))
    cc = closes.pct_change().fillna(0.0)
    oc = (closes / opens - 1.0).fillna(0.0)
    gap = (opens / closes.shift() - 1.0).fillna(0.0)           # previous close -> today's open (old book's overnight)
    cy = None
    if cash_yield is not None:
        cy = (cash_yield.reindex(idx).ffill().bfill().fillna(0.0) / 100.0 / 252.0).to_numpy()
    open_leg: dict[str, dict] = {}
    legs: list[dict] = []
    for i in range(1, len(idx)):
        want = w_target.iloc[i - 1]
        if not want.equals(w_prev):                            # rebalance at today's open
            dw = (want - held).abs()
            delta = float(dw.sum())
            turnover[i] = delta
            contrib = held * gap.iloc[i] + want * oc.iloc[i] - dw * cost_bps / 1e4
            for s in syms:
                if held[s] == 0.0 and want[s] > 0.0:
                    open_leg[s] = {"symbol": s, "entry_date": idx[i], "pnl": 0.0, "max_weight": float(want[s])}
            held = want.copy()
            w_prev = want.copy()
        else:
            contrib = held * cc.iloc[i]
        r = float(contrib.sum())
        if cy is not None:
            r += max(0.0, 1.0 - float(held.abs().sum())) * float(cy[i])
        for s, leg in list(open_leg.items()):
            leg["pnl"] += float(contrib[s]) * capital
            leg["max_weight"] = max(leg["max_weight"], float(held[s]))
            if held[s] == 0.0 or i == len(idx) - 1:
                leg["exit_date"] = idx[i]
                leg["days_held"] = int((idx[i] - leg["entry_date"]).days)
                leg["reason"] = "rotation" if held[s] == 0.0 else "end"
                legs.append(open_leg.pop(s))
        rets[i] = r
    daily_ret = pd.Series(rets, index=idx)
    trades = pd.DataFrame({"date": idx, "turnover": turnover})
    trades = trades[trades["turnover"] > 0]
    trades.attrs["legs"] = pd.DataFrame(legs, columns=["symbol", "entry_date", "exit_date", "days_held", "max_weight", "pnl", "reason"])
    return daily_ret, trades, M.equity_from_returns(daily_ret, capital)


# ══════════════════════════════════════════════════════════════════════════════
# Walk-forward + tiny grid search with deflated Sharpe accounting
# ══════════════════════════════════════════════════════════════════════════════
def split_is_oos(index: pd.DatetimeIndex, train_frac: float = 0.6):
    cut = index[int(len(index) * train_frac)]
    return (index[0], cut), (cut, index[-1])


def grid_search(run_fn: Callable[..., pd.Series], grid: dict, index: pd.DatetimeIndex, train_frac: float = 0.6):
    """run_fn(**params) -> daily_ret Series over the full sample.  Evaluates every combination on the
    in-sample window, picks the best by Sharpe, reports it out-of-sample, and deflates the IS Sharpe by
    the number of trials.  Returns (table, best_params, dsr)."""
    (is0, is1), (oos0, oos1) = split_is_oos(index, train_frac)
    rows = []
    keys = list(grid)
    for combo in itertools.product(*[grid[k] for k in keys]):
        params = dict(zip(keys, combo))
        r = run_fn(**params)
        r_is, r_oos = r[(r.index >= is0) & (r.index < is1)], r[(r.index >= oos0) & (r.index <= oos1)]
        rows.append({**params, "is_sharpe": M.summary(r_is).get("sharpe"), "oos_sharpe": M.summary(r_oos).get("sharpe"),
                     "is_sr_pp": M.sharpe_per_period(r_is), "oos_maxdd": M.summary(r_oos).get("max_drawdown_pct"),
                     "_r": r})
    tab = pd.DataFrame(rows)
    best = tab.sort_values("is_sharpe", ascending=False).iloc[0]
    r_best = best["_r"]
    r_is = r_best[(r_best.index >= is0) & (r_best.index < is1)]
    dsr = M.deflated_sharpe(M.sharpe_per_period(r_is), len(r_is), len(tab), float(tab["is_sr_pp"].var(ddof=1)) if len(tab) > 1 else 0.0,
                            skew=float(r_is.skew()) if len(r_is) > 2 else 0.0, kurt=float(r_is.kurt() + 3) if len(r_is) > 3 else 3.0)
    best_params = {k: (best[k].item() if hasattr(best[k], "item") else best[k]) for k in keys}
    return tab.drop(columns=["_r"]), best_params, dsr, (is0, is1, oos0, oos1)
