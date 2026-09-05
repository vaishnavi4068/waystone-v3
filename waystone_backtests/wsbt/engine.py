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
    entry: str = "open"                # "open" | "close" | "stop"  (stop: level in entry_level, fill if touched)
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


# ══════════════════════════════════════════════════════════════════════════════
# 3. Portfolio-of-weights simulator
# ══════════════════════════════════════════════════════════════════════════════
def simulate_weights(frames: dict[str, pd.DataFrame], weights: pd.DataFrame, cost_bps: float = 5.0,
                     capital: float = 100_000.0):
    """weights: rows = decision dates (at the close), cols = symbols, values = target weight (sum <= 1).
    Executed at the NEXT open; the first day's return is open->close, later days close->close.
    Turnover cost = cost_bps x |Δw| on each rebalance."""
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
    for i in range(1, len(idx)):
        want = w_target.iloc[i - 1]
        if not want.equals(w_prev):                            # rebalance at today's open
            delta = (want - held).abs().sum()
            turnover[i] = delta
            r = float((want * oc.iloc[i]).sum()) - delta * cost_bps / 1e4
            held = want.copy()
            w_prev = want.copy()
        else:
            r = float((held * cc.iloc[i]).sum())
        rets[i] = r
    daily_ret = pd.Series(rets, index=idx)
    trades = pd.DataFrame({"date": idx, "turnover": turnover})
    trades = trades[trades["turnover"] > 0]
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
