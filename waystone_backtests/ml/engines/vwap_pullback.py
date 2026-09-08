"""Backtest port of the options bot's PRIMARY signal (ibkr_month/app.py) on 10-minute underlying bars.

What is copied verbatim from the live code:
  • scoring  — ATR%(14) and RVOL(20) per name at the current bar, min-max scaled ACROSS THE UNIVERSE to 0..10,
               score = 0.5*atr_score + 0.5*rvol_score; qualify if score > SCORE_THRESHOLD (5.0) and NOT in the
               skip band [8.0, 9.0); ranked by score.
  • signal   — check_vwap_pullback(): on the day's completed bars (>= 3), with prev = bar-1:
               LONG  if |prev.low  - prev.VWAP| <= 0.30*ATR and close > VWAP and close > prev.close
               SHORT if |prev.high - prev.VWAP| <= 0.30*ATR and close < VWAP and close < prev.close
  • timing   — cycles at every completed 10-min bar; no new entries at/after 15:00; everything flat at 15:45.
  • exits    — hard stop at -30 % of the entry premium, else the 15:45 sweep.

What is a PROXY (documented, parameterised, replaceable):
  the option leg.  The bot buys the first ITM monthly call/put (delta ~0.6-0.7, premium ~2-3 % of spot).
  Here the trade is marked on the UNDERLYING: P&L = delta * 100 * qty * (underlying move), cost = commission
  + slippage, and the -30 % premium stop becomes a stop `stop_pct_spot` % of spot against the entry
  (default 1.0 % ≈ -30 % of a 2.2 %-of-spot premium at delta 0.65).  Swap in real option bars from
  `tools/fetch_polygon.py option-bars` when you want the tape instead of the proxy — the trade list carries
  the underlying timestamps and prices you need to re-price every trade.

Output: a trade list with entry_time (fill at the open of the bar after the signal), exit_time, side, entry,
exit, pnl, cost, units, reason, symbol, score and the signal bar timestamp.  That list is the input to
ml/meta_label.py, which asks the only question that matters: which of these trades should we have skipped?
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time as dtime

import numpy as np
import pandas as pd

SCORE_THRESHOLD = 5.0
SCORE_SKIP_LOW, SCORE_SKIP_HIGH = 8.0, 9.0
VWAP_ATR = 0.30
HARD_SL_PCT = 0.30
TIME_EXIT = dtime(15, 45)
NO_NEW_ENTRIES_AFTER = dtime(15, 0)
ATR_PERIOD, RVOL_PERIOD = 14, 20


@dataclass
class Proxy:
    delta: float = 0.65            # option delta at entry
    premium_pct: float = 2.2       # premium as % of spot (sizes the notional and the stop)
    stop_pct_spot: float | None = None   # stop distance in % of spot; None -> derived from HARD_SL_PCT * premium / delta
    commission: float = 0.65       # $ per contract per side
    slippage: float = 0.03         # $ per share-equivalent per side on the underlying move (≈ half a $0.05 option spread / delta)
    qty: int = 1

    def stop_pct(self) -> float:
        if self.stop_pct_spot is not None:
            return self.stop_pct_spot
        return HARD_SL_PCT * self.premium_pct / self.delta   # move in the underlying that loses 30 % of the premium


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """ATR(14), RVOL(20), session VWAP and ATR% on a 10-min bar frame (index tz-aware or ET-naive)."""
    d = df.copy()
    pc = d["close"].shift(1)
    d["TR"] = pd.concat([d["high"] - d["low"], (d["high"] - pc).abs(), (d["low"] - pc).abs()], axis=1).max(axis=1)
    d["ATR"] = d["TR"].rolling(ATR_PERIOD).mean()
    d["ATR_pct"] = d["ATR"] / d["close"] * 100
    d["RVOL"] = d["volume"] / d["volume"].rolling(RVOL_PERIOD).mean()
    day = d.index.tz_localize(None).normalize() if d.index.tz is not None else d.index.normalize()
    tp = (d["high"] + d["low"] + d["close"]) / 3.0
    d["VWAP"] = (tp * d["volume"]).groupby(day.values).cumsum() / d["volume"].groupby(day.values).cumsum()
    d["_day"] = day
    d["_bar_in_day"] = d.groupby(day.values).cumcount() + 1
    return d


def rank_universe(rows: pd.DataFrame) -> pd.DataFrame:
    """rows: one row per symbol with atr_pct, rvol.  Same min-max scaling as app.rank_stocks()."""
    if len(rows) < 2:
        return rows.iloc[0:0]
    t = rows.copy()
    for col, sc in (("atr_pct", "atr_score"), ("rvol", "rvol_score")):
        mn, mx = t[col].min(), t[col].max()
        t[sc] = ((t[col] - mn) / (mx - mn)) * 10 if mx > mn else 5.0
    t["score"] = (t["atr_score"] * 0.5 + t["rvol_score"] * 0.5).round(2)
    q = t[(t["score"] > SCORE_THRESHOLD) & ~((t["score"] >= SCORE_SKIP_LOW) & (t["score"] < SCORE_SKIP_HIGH))]
    return q.sort_values("score", ascending=False)


def pullback_signal(day: pd.DataFrame) -> str | None:
    """day: enriched bars of the current session up to and including the completed signal bar."""
    if len(day) < 3:
        return None
    bar, prev = day.iloc[-1], day.iloc[-2]
    if pd.isna(bar["ATR"]) or pd.isna(bar["VWAP"]) or pd.isna(prev["VWAP"]):
        return None
    near = VWAP_ATR * bar["ATR"]
    if abs(prev["low"] - prev["VWAP"]) <= near and bar["close"] > bar["VWAP"] and bar["close"] > prev["close"]:
        return "LONG"
    if abs(prev["high"] - prev["VWAP"]) <= near and bar["close"] < bar["VWAP"] and bar["close"] < prev["close"]:
        return "SHORT"
    return None


def run_primary(frames: dict[str, pd.DataFrame], proxy: Proxy = Proxy(), max_positions: int = 5,
                one_per_symbol_per_day: bool = True, top_n: int = 25) -> pd.DataFrame:
    """frames: {symbol: 10-min OHLCV DataFrame} — the same bars for every name, aligned timestamps.
    Cycle at every completed bar (like the bot), rank the universe, walk the ranked names, fire the
    pullback rule, fill at the next bar's open, exit on the stop (evaluated on the underlying path) or
    at the 15:45 bar close.  Returns the trade list."""
    enriched = {s: enrich(f) for s, f in frames.items()}
    all_ts = sorted(set().union(*[set(f.index) for f in enriched.values()]))
    all_ts = pd.DatetimeIndex(all_ts)
    stop_pct = proxy.stop_pct() / 100.0
    trades = []
    open_pos: dict[str, dict] = {}
    traded_today: set[tuple] = set()
    cur_day = None
    pos_of = {s: {t: i for i, t in enumerate(f.index)} for s, f in enriched.items()}
    diffs = np.diff(all_ts.values).astype("timedelta64[m]").astype(float)
    bar_min = int(np.median(diffs[diffs > 0])) if len(diffs) else 10
    for ts in all_ts:
        t_local = ts.tz_localize(None) if ts.tz is not None else ts
        day = t_local.normalize()
        if day != cur_day:
            cur_day = day
            traded_today = set()
        tod = t_local.time()
        bar_end = (t_local + pd.Timedelta(minutes=bar_min)).time()
        # 1) manage open positions on this bar (the bar just completed)
        for sym in list(open_pos):
            p = open_pos[sym]
            f = enriched[sym]
            i = pos_of[sym].get(ts)
            if i is None:
                continue
            bar = f.iloc[i]
            if i < p["i_entry"]:
                continue
            side = p["side"]
            stop = p["stop"]
            hit = (side > 0 and bar["low"] <= stop) or (side < 0 and bar["high"] >= stop)
            exit_px, reason = None, None
            if hit:
                exit_px = stop
                if (side > 0 and bar["open"] < stop) or (side < 0 and bar["open"] > stop):
                    exit_px = bar["open"]
                reason = "stop"
            elif bar_end >= TIME_EXIT:
                exit_px, reason = bar["close"], "time_exit"      # the 15:45 sweep fills at ~the close of the bar ending after 15:45
            elif i == len(f) - 1:
                exit_px, reason = bar["close"], "end"
            if exit_px is not None:
                fill = exit_px - side * proxy.slippage
                move = (fill - p["entry"]) * side
                gross = move * proxy.delta * 100 * proxy.qty
                cost = 2 * proxy.commission * proxy.qty + 2 * proxy.slippage * proxy.delta * 100 * proxy.qty
                trades.append({"symbol": sym, "signal_ts": p["signal_ts"], "entry_time": p["entry_time"], "exit_time": ts,
                               "side": side, "entry": p["entry"], "exit": fill, "pnl": gross - 2 * proxy.commission * proxy.qty,
                               "cost": cost, "units": proxy.qty, "reason": reason, "score": p["score"],
                               "premium_est": p["premium"], "stop": stop, "bars_held": i - p["i_entry"]})
                del open_pos[sym]
        # 2) scan at this bar close for signals to fill at the next bar's open.  The bot's clock at this
        #    point is the bar END (start + bar_min), which is what the 15:00 cutoff is compared against.
        if bar_end >= NO_NEW_ENTRIES_AFTER or tod < dtime(9, 40):
            continue
        rows = []
        for sym, f in enriched.items():
            i = pos_of[sym].get(ts)
            if i is None:
                continue
            b = f.iloc[i]
            if pd.isna(b["ATR_pct"]) or pd.isna(b["RVOL"]):
                continue
            rows.append({"symbol": sym, "atr_pct": b["ATR_pct"], "rvol": b["RVOL"], "i": i})
        if len(rows) < 2:
            continue
        ranked = rank_universe(pd.DataFrame(rows)).head(top_n)
        for _, r in ranked.iterrows():
            sym = r["symbol"]
            if sym in open_pos or (one_per_symbol_per_day and (sym, day) in traded_today):
                continue
            if len(open_pos) >= max_positions:
                break
            f = enriched[sym]
            i = int(r["i"])
            day_bars = f.iloc[max(0, i - int(f.iloc[i]["_bar_in_day"]) + 1): i + 1]
            sig = pullback_signal(day_bars)
            if sig is None or i + 1 >= len(f):
                continue
            nxt = f.iloc[i + 1]
            nxt_day = f.index[i + 1]
            nxt_day = (nxt_day.tz_localize(None) if nxt_day.tz is not None else nxt_day).normalize()
            if nxt_day != day:
                continue
            side = 1 if sig == "LONG" else -1
            entry = float(nxt["open"]) + side * proxy.slippage
            premium = float(nxt["open"]) * proxy.premium_pct / 100 * 100 * proxy.qty
            open_pos[sym] = {"side": side, "entry": entry, "i_entry": i + 1, "entry_time": f.index[i + 1], "signal_ts": ts,
                             "stop": entry * (1 - side * stop_pct), "score": float(r["score"]), "premium": premium}
            traded_today.add((sym, day))
    out = pd.DataFrame(trades)
    if len(out):
        out = out.sort_values(["entry_time", "symbol"]).reset_index(drop=True)
    return out
