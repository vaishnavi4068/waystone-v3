#!/usr/bin/env python3
"""
v221_engine.py — faithful, restart-safe port of vxn_revisit.py::run_engine()
============================================================================
This is the validated V221 engine (Variant C: no-VXN entries + VXN-aware
exits + FnG/chop gate; 1,072 trades / $327,792 / MaxDD 23.1% / Sharpe 2.62
on the 2021-2023 real-VXN window), ported LINE FOR LINE from the reference
backtest so the live loop runs the same state machine.  Nothing here is a
reconstruction: every rule below is copied from vxn_revisit.py, and
test_v221.py::TestEngineParity replays the reference source itself against
this port on random bars and requires identical trade lists for all three
variants (A, B, C).

WHAT THE REFERENCE ACTUALLY DOES — and where the v1/v2 live engine diverged
---------------------------------------------------------------------------
  Brick size    ATR(14) of the SOURCE BARS, clamped to [0, 10]: brick =
                min(ATR14, 10).  Recomputed every bar.  (v1/v2: fixed 10.)
  Renko         Not grid-anchored, not standard.  One brick per bar at most.
                After an UP brick: up_box = brick above, down_box = HALF a
                brick below (asymmetric, half-brick reversal).  Brick closes
                land wherever the boxes were, not on a price grid.
                (v1/v2: grid-anchored close Renko, 2x reversal, multi-brick.)
  MCO           EMAs are updated on EVERY BAR using the LAST brick close
                (held constant between bricks) — a time-based EMA of a
                step function, not an EMA over the brick sequence.
                (v1/v2: EMA over bricks only.)
  Entry trigger STATE, not cross: flat AND office hours (09:30-16:00) AND
                mco > signal -> BUY (mco < signal -> SELL) AND a VOLUME SPIKE
                (bar volume >= +10% over the prior 20-bar mean; +25% after
                hours).  Fill at the NEXT bar's open ± 0.125.  Re-entry the
                bar after an exit is normal.  (v1/v2: MCO cross on a new
                brick, no volume condition.)
  Gate          BLOCKS an entry when FnG(prior day) <= 30 AND chop <= 0.03.
                Both must hold to block; a missing FnG means not blocked.
                (v1/v2 REQUIRED FnG <= 30 and chop <= 0.03 to trade — the
                OPPOSITE polarity.  The live book was trading only in the
                regime the backtest refuses.)
  chop          Rolling 20-DAY efficiency: |day_open[-1] - day_open[-20]| /
                (bricks in window x avg brick size), recomputed once per
                calendar day at the day change.  0.03 = the market went
                nowhere net over 20 days relative to the brick path.
                (v1/v2: (max-min)/mean over 20 bricks, which cannot exceed
                0.007 at index level.)
  Exits         Evaluated on every bar close except 16:xx.  In order:
                trailing profit take (max gain > 4/3/2 % and duration > 3
                bars and gain reversed vs 3 bars ago); VXN regime states
                (SHORT & takeOff, LONG & landing); gain <= -0.5% in a
                "going nowhere" regime; after-hours profitable-but-failing;
                after-hours min <= -0.4%; hard stop gain <= -0.8% ("fts").
                Gain is % of ENTRY PRICE (bar open), so at NQ 29,000 the
                -0.8% stop is ~230 points, not 30.  (v1/v2: MCO cross
                against + 3 x 10pt broker stop.)
  VXN regime    MA20/50/200 and a 60-bar correlation over the VXN value
                sampled PER OFFICE-HOURS BAR (the daily close repeated), so
                the regime only leaves "barf" for the first ~200 bars after
                the daily VXN value changes.  Ported as-is.
  Overnight     Positions are HELD; after-hours exits exist.  (v1/v2: flat
                at 16:55 / 15:55.)
  Costs         2 contracts, $4.50/contract, 0.125 pt/side.

TWO THINGS THE LIVE PORT CANNOT COPY EXACTLY — flagged, not hidden
-----------------------------------------------------------------
  1. VXN LOOK-AHEAD.  The reference merges the DAILY VXN close onto each
     bar with merge_asof(direction="backward") against a midnight timestamp,
     so every bar on day D sees day D's CLOSE — a value not known until
     16:15.  A live engine can only know D-1's close.  Live therefore feeds
     the prior-day close (vol_source="prior_day").  The regime/exit logic is
     identical; the input is one day older.  This is a property of the
     backtest, and it is one of the "look-ahead tests not yet run" its own
     header admits to.
  2. FILL PRICE.  The reference fills at next-bar open ± 0.125 and measures
     unrealised gain from the bar OPEN.  Live fills at market ~12 s after the
     bar closes and records the broker's fill; enter() takes that fill as
     entry_price so the gain% exits key off what was actually paid.

The bar set matters: ATR14 brick sizing, the volume spike, the daily open
price and the after-hours rules all assume the SAME session coverage and bar
size as the CSV the backtest ran on ([CONFIRM] with whoever owns it — the
engine defaults to 24h 1-minute bars, which is what the after-hours exit
rules imply).
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, time as dtime
from pathlib import Path

import numpy as np

# ── constants, verbatim from vxn_revisit.py ─────────────────────────────────
FAST, SLOW, SIG = 45, 60, 20
BRICK_MIN, BRICK_MAX = 0.0, 10.0
VOL_WINDOW = 20
NQ_CAPITAL, NQ_MULT, NQ_COMMISSION = 100000, 20.0, 4.50
FIXED_CONTRACTS = 2
SLIP_PTS = 0.125
ROLLING_WINDOW_DAYS = 20
FNG_THRESHOLD = 30
CHOP_THRESHOLD = 0.03

KEEP = "Keep Trucking"

# Restart-safety: the reference keeps every list unbounded.  Only the tails
# are ever read (14 TRs, 21 volumes, 60 closes, 200 vol prices, last MCO,
# last brick), so trimming with margin changes nothing.
_KEEP_CLOSES, _KEEP_VOLUMES, _KEEP_TR, _KEEP_VOLP = 400, 100, 100, 400


def ema_update(prev, price, period):
    if prev is None:
        return price
    k = 2 / (period + 1)
    return prev + k * (price - prev)


def office_or_after(dt) -> str:
    t = dt.time()
    return "officeHours" if dtime(9, 30) <= t <= dtime(16, 0) else "afterHours"


def get_prior_day_fng(target_date, fng_lookup, all_fng_dates):
    """Verbatim: the last FnG date STRICTLY before target_date, no staleness
    cutoff.  None if there is none."""
    idx = None
    lo, hi = 0, len(all_fng_dates) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if all_fng_dates[mid] < target_date:
            idx = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return fng_lookup[all_fng_dates[idx]] if idx is not None else None


@dataclass
class Trade:
    exit_time: str
    direction: str
    pts: float
    entry_time: str | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    reason: str | None = None


class V221Engine:
    """One instance = one instrument's State.  Feed closed bars in order via
    on_bar(); read .pending_signal / the returned exit reason; report fills
    with enter() / exit_filled().  to_dict()/from_dict() persist everything."""

    def __init__(self, use_vxn_entries: bool = False, use_vxn_exits: bool = True,
                 use_gate: bool = True):
        self.use_vxn_entries = use_vxn_entries
        self.use_vxn_exits = use_vxn_exits
        self.use_gate = use_gate
        # State() — same names as the reference
        self.prices_close: list[float] = []
        self.renko_current_price = None
        self.up_box = None
        self.down_box = None
        self.brick_size = (BRICK_MIN + BRICK_MAX) / 2
        self.renko_closes: list[float] = []
        self.fast_ema = None
        self.slow_ema = None
        self.mco_list: list[float] = []
        self.mco_sig_ema = None
        self.mco_sig_list: list[float] = []
        self.tr_list: list[float] = []
        self.atr = None
        self.vol_prices: list[float] = []
        self.vol_ma20: list[float] = []
        self.vol_ma50: list[float] = []
        self.vol_ma200: list[float] = []
        self.vol_ma_status = "barf"
        self.correlation_status = "no_correlation"
        self.algo_status = "going_no_where"
        self.volumes: list[float] = []
        self.position = None
        self.entry_price = None
        self.entry_fill_price = None
        self.entry_time = None
        self.pending_signal = None
        self.unrealized_gain_list: list[float] = []
        self.trade_duration = 0
        self.trades: list[dict] = []
        # run_engine() locals
        self.tr_prev_close = None
        self.daily_start_price: deque = deque(maxlen=ROLLING_WINDOW_DAYS)
        self.daily_bricks: deque = deque(maxlen=ROLLING_WINDOW_DAYS)
        self.daily_brick_size_sum: deque = deque(maxlen=ROLLING_WINDOW_DAYS)
        self.current_day = None
        self.day_brick_count = 0
        self.day_brick_size_total = 0.0
        self.day_open_price = None
        self.cached_chop_ratio = None
        # bookkeeping for the live loop
        self.bars_seen = 0
        self.bricks_total = 0
        self.last_bar_ts = None
        self.last_gate = None          # dict describing the last gate evaluation
        self.last_trigger = "meh"
        self.last_vol_spike = None

    # ── reference sub-functions ─────────────────────────────────────────────
    def update_vol_regime(self):
        s = self
        n = len(s.vol_prices)
        if n >= 20:
            s.vol_ma20.append(float(np.mean(s.vol_prices[-20:])))
        if n >= 50:
            s.vol_ma50.append(float(np.mean(s.vol_prices[-50:])))
        if n >= 200:
            s.vol_ma200.append(float(np.mean(s.vol_prices[-200:])))
            ma200, ma50, ma20, vp = s.vol_ma200[-1], s.vol_ma50[-1], s.vol_ma20[-1], s.vol_prices[-1]
            if ma200 < ma50 < ma20 and ma200 < vp and ma50 < vp and ma20 < vp:
                s.vol_ma_status = "Above"
            elif ma200 > ma50 > ma20 and ma200 > vp and ma50 > vp and ma20 > vp:
                s.vol_ma_status = "Below"
            else:
                s.vol_ma_status = "barf"
            if len(s.vol_prices) >= 60 and len(s.prices_close) >= 60:
                with np.errstate(divide="ignore", invalid="ignore"):   # constant series -> nan, handled below (same as reference)
                    corr = np.corrcoef(s.vol_prices[-60:], s.prices_close[-60:])[0, 1]
                if np.isnan(corr):
                    corr = 0
                if corr <= -0.40:
                    s.correlation_status = "negative_correlation"
                elif corr >= 0.70:
                    s.correlation_status = "positive_correlation"
                else:
                    s.correlation_status = "no_correlation"
            else:
                s.correlation_status = "no_correlation"
            if s.vol_ma_status == "Below" and s.correlation_status == "negative_correlation":
                s.algo_status = "takeOff"
            elif s.vol_ma_status == "Above" and s.correlation_status == "negative_correlation":
                s.algo_status = "landing"
            else:
                s.algo_status = "going_no_where"
        # trim the MA histories (only [-1] is ever read)
        for lst in (s.vol_ma20, s.vol_ma50, s.vol_ma200):
            if len(lst) > 5:
                del lst[:-5]

    def trade_hours_ok(self, action, party_status) -> bool:
        s = self
        if len(s.volumes) < VOL_WINDOW + 1:
            self.last_vol_spike = None
            return False
        avg_vol = np.mean(s.volumes[-(VOL_WINDOW + 1):-1])
        try:
            x = ((s.volumes[-1] - avg_vol) / avg_vol) * 100
        except ZeroDivisionError:
            x = 0
        threshold = 10 if party_status == "officeHours" else 25
        self.last_vol_spike = (round(float(x), 2), threshold)
        if action in ("BUY", "SELL"):
            return bool(x >= threshold)
        return False

    def _exit_vxn_aware(self, dt, party_status) -> str:
        s = self
        if not s.unrealized_gain_list:
            s.unrealized_gain_list.append(0)
        gl = s.unrealized_gain_list
        gain = gl[-1]
        if not (dt.hour <= 15 or dt.hour >= 17):
            return KEEP

        def reversed_from(k):
            return len(gl) > abs(k) and gl[k] > gl[-1]
        if max(gl) > 4 and s.trade_duration > 3:
            return "4_percent" if reversed_from(-3) else "profitable"
        elif max(gl) > 3 and s.trade_duration > 3:
            return "3_percent" if reversed_from(-3) else "profitable"
        elif max(gl) > 2 and s.trade_duration > 3:
            return "2_percent" if reversed_from(-3) else "profitable"
        elif s.position == "SHORT" and s.algo_status == "takeOff":
            return "takeOff"
        elif s.position == "LONG" and s.algo_status == "landing":
            return "landing"
        elif gain <= -0.5 and s.vol_ma_status == "barf" and s.correlation_status == "no_correlation" and s.algo_status == "going_no_where":
            return "yeah_its_failing"
        elif party_status == "afterHours" and max(gl) >= 0.5 and len(gl) >= 5:
            return "afterHours_profitable_but_failing" if reversed_from(-5) else "profitable"
        elif party_status == "afterHours" and min(gl) <= -0.4:
            return "afterHours_failing_in_the_afterHours"
        elif gain <= -0.8:
            return "fts"
        return KEEP

    def _exit_simple(self, dt, party_status) -> str:
        s = self
        if not s.unrealized_gain_list:
            s.unrealized_gain_list.append(0)
        gl = s.unrealized_gain_list
        gain = gl[-1]
        if not (dt.hour <= 15 or dt.hour >= 17):
            return KEEP

        def reversed_from(k):
            return len(gl) > abs(k) and gl[k] > gl[-1]
        if max(gl) > 4 and s.trade_duration > 3:
            return "4_percent" if reversed_from(-3) else "profitable"
        elif max(gl) > 3 and s.trade_duration > 3:
            return "3_percent" if reversed_from(-3) else "profitable"
        elif max(gl) > 2 and s.trade_duration > 3:
            return "2_percent" if reversed_from(-3) else "profitable"
        elif gain <= -0.8:
            return "fts"
        return KEEP

    # ── live-loop hooks (the reference does these implicitly) ───────────────
    def enter(self, direction: str, fill_price: float, dt, entry_price: float | None = None) -> None:
        """Reference: at the top of the NEXT bar, a pending signal becomes a
        position with entry_price = that bar's open and entry_fill_price =
        open ± slip.  Live passes the broker fill; entry_price defaults to it
        (the gain% exits then key off what was actually paid)."""
        self.position = "LONG" if direction == "BUY" else "SHORT"
        self.entry_price = float(entry_price if entry_price is not None else fill_price)
        self.entry_fill_price = float(fill_price)
        self.entry_time = str(dt)
        self.unrealized_gain_list = []
        self.trade_duration = 0
        self.pending_signal = None

    def cancel_pending(self) -> None:
        self.pending_signal = None

    def exit_filled(self, fill_price: float, dt, reason: str) -> dict:
        """Reference: exit_fill = close ∓ slip, pts vs entry_fill_price."""
        if self.position == "LONG":
            pts = float(fill_price) - self.entry_fill_price
        else:
            pts = self.entry_fill_price - float(fill_price)
        t = {"exit_time": str(dt), "direction": self.position, "pts": pts,
             "entry_time": self.entry_time, "entry_price": self.entry_fill_price,
             "exit_price": float(fill_price), "reason": reason}
        self.trades.append(t)
        self.position = None
        self.unrealized_gain_list = []
        self.trade_duration = 0
        return t

    # ── the bar loop body, verbatim in order ────────────────────────────────
    def on_bar(self, dt, open_, high, low, close, volume, vol_price,
               fng_lookup=None, all_fng_dates=None, backtest_fill: bool = False) -> dict:
        """Process ONE closed bar.  Returns a dict:
            {'exit': reason|None, 'signal': 'BUY'|'SELL'|None, 'brick': bool,
             'gate': {...}|None, 'gain': float|None}
        backtest_fill=True reproduces the reference exactly (pending signal
        fills at this bar's open ± slip; exits fill at close ∓ slip).  The
        live loop uses False and reports real fills via enter()/exit_filled()."""
        s = self
        party_status = office_or_after(dt)
        this_day = dt.date()
        out = {"exit": None, "signal": None, "brick": False, "gate": None, "gain": None,
               "party": party_status}

        if s.current_day is None:
            s.current_day = this_day
            s.day_open_price = close
        elif this_day != s.current_day:
            s.daily_start_price.append(s.day_open_price)
            s.daily_bricks.append(s.day_brick_count)
            s.daily_brick_size_sum.append(s.day_brick_size_total)
            if len(s.daily_start_price) >= 2:
                net_move = abs(s.daily_start_price[-1] - s.daily_start_price[0])
                total_bricks_window = sum(s.daily_bricks)
                total_size_window = sum(s.daily_brick_size_sum)
                avg_brick_size = (total_size_window / total_bricks_window) if total_bricks_window > 0 else 1
                total_brick_movement = total_bricks_window * avg_brick_size
                s.cached_chop_ratio = (net_move / total_brick_movement) if total_brick_movement > 0 else None
            s.current_day = this_day
            s.day_open_price = close
            s.day_brick_count = 0
            s.day_brick_size_total = 0.0

        if backtest_fill and s.pending_signal is not None and s.position is None:
            d = s.pending_signal
            fill = open_ + SLIP_PTS if d == "BUY" else open_ - SLIP_PTS
            self.enter(d, fill, dt, entry_price=open_)

        s.prices_close.append(float(close))
        s.volumes.append(float(volume))
        if party_status == "officeHours":
            s.vol_prices.append(float(vol_price))

        tr = max(high - low, abs(high - s.tr_prev_close) if s.tr_prev_close else 0,
                 abs(low - s.tr_prev_close) if s.tr_prev_close else 0)
        s.tr_prev_close = close
        s.tr_list.append(float(tr))
        if len(s.tr_list) >= 14:
            s.atr = float(np.mean(s.tr_list[-14:]))
            if s.atr >= BRICK_MAX:
                s.brick_size = BRICK_MAX
            elif s.atr <= BRICK_MIN:
                s.brick_size = BRICK_MIN if BRICK_MIN > 0 else BRICK_MAX
            else:
                s.brick_size = s.atr

        if s.renko_current_price is None:
            s.renko_current_price = close
            s.up_box = close + s.brick_size
            s.down_box = close - s.brick_size
        else:
            if close >= s.up_box:
                s.renko_current_price = s.up_box
                s.renko_closes.append(s.renko_current_price)
                s.up_box = s.renko_current_price + s.brick_size
                s.down_box = s.renko_current_price - s.brick_size / 2
                s.day_brick_count += 1
                s.day_brick_size_total += s.brick_size
                out["brick"] = True; s.bricks_total += 1
            elif close <= s.down_box:
                s.renko_current_price = s.down_box
                s.renko_closes.append(s.renko_current_price)
                s.down_box = s.renko_current_price - s.brick_size
                s.up_box = s.renko_current_price + s.brick_size / 2
                s.day_brick_count += 1
                s.day_brick_size_total += s.brick_size
                out["brick"] = True; s.bricks_total += 1

        s.bars_seen += 1
        s.last_bar_ts = str(dt)
        self._trim()

        if not s.renko_closes:
            return out

        renko_close = s.renko_closes[-1]
        s.fast_ema = ema_update(s.fast_ema, renko_close, FAST)
        s.slow_ema = ema_update(s.slow_ema, renko_close, SLOW)
        mco = s.fast_ema - s.slow_ema
        s.mco_list.append(mco)
        s.mco_sig_ema = ema_update(s.mco_sig_ema, mco, SIG)
        s.mco_sig_list.append(s.mco_sig_ema)

        if party_status == "officeHours":
            self.update_vol_regime()

        if s.position is not None:
            if s.position == "LONG":
                gain = ((close - s.entry_price) / s.entry_price) * 100
            else:
                gain = ((s.entry_price - close) / s.entry_price) * 100
            s.unrealized_gain_list.append(gain)
            s.trade_duration += 1
            out["gain"] = gain

            reason = self._exit_vxn_aware(dt, party_status) if s.use_vxn_exits else \
                self._exit_simple(dt, party_status)

            if reason not in (KEEP, "profitable"):
                out["exit"] = reason
                if backtest_fill:
                    exit_fill = close - SLIP_PTS if s.position == "LONG" else close + SLIP_PTS
                    self.exit_filled(exit_fill, dt, reason)
        else:
            if party_status != "officeHours" or len(s.mco_list) < 1 or len(s.mco_sig_list) < 1:
                trigger = "meh"
            elif s.use_vxn_entries:
                if s.mco_list[-1] > s.mco_sig_list[-1] and s.algo_status == "takeOff":
                    trigger = "BUY"
                elif s.mco_list[-1] < s.mco_sig_list[-1] and s.algo_status == "landing":
                    trigger = "SELL"
                else:
                    trigger = "meh"
            else:
                if s.mco_list[-1] > s.mco_sig_list[-1]:
                    trigger = "BUY"
                elif s.mco_list[-1] < s.mco_sig_list[-1]:
                    trigger = "SELL"
                else:
                    trigger = "meh"
            s.last_trigger = trigger

            if trigger in ("BUY", "SELL") and self.trade_hours_ok(trigger, party_status):
                blocked = False
                gate = None
                if s.use_gate:
                    fng_val = get_prior_day_fng(dt.date(), fng_lookup, all_fng_dates) \
                        if fng_lookup else None
                    fng_bad = (fng_val is not None and fng_val <= FNG_THRESHOLD)
                    chop_bad = (s.cached_chop_ratio is not None and s.cached_chop_ratio <= CHOP_THRESHOLD)
                    blocked = fng_bad and chop_bad
                    gate = {"fng": fng_val, "fng_bad": fng_bad, "chop": s.cached_chop_ratio,
                            "chop_bad": chop_bad, "blocked": blocked}
                s.last_gate = gate
                out["gate"] = gate
                if not blocked:
                    s.pending_signal = trigger
                    out["signal"] = trigger
                else:
                    out["signal"] = None
        return out

    def _trim(self):
        s = self
        if len(s.prices_close) > _KEEP_CLOSES:
            del s.prices_close[:-_KEEP_CLOSES]
        if len(s.volumes) > _KEEP_VOLUMES:
            del s.volumes[:-_KEEP_VOLUMES]
        if len(s.tr_list) > _KEEP_TR:
            del s.tr_list[:-_KEEP_TR]
        if len(s.vol_prices) > _KEEP_VOLP:
            del s.vol_prices[:-_KEEP_VOLP]
        if len(s.renko_closes) > 5:
            del s.renko_closes[:-5]
        if len(s.mco_list) > 5:
            del s.mco_list[:-5]
        if len(s.mco_sig_list) > 5:
            del s.mco_sig_list[:-5]
        if len(s.trades) > 5000:
            del s.trades[:-5000]

    def shift_prices(self, delta: float) -> None:
        """Contract roll: the reference ran on one continuous price series.
        Live, the new front month trades at a spread to the old one, so every
        price-anchored field is shifted by `delta` (new - old) — the same as
        back-adjusting the history — and the EMAs, regime and daily windows
        carry on uninterrupted.  Bricks/boxes move with the price; brick SIZE
        (ATR) does not."""
        d = float(delta)
        if self.renko_current_price is not None:
            self.renko_current_price += d
        if self.up_box is not None:
            self.up_box += d
        if self.down_box is not None:
            self.down_box += d
        self.renko_closes = [x + d for x in self.renko_closes]
        self.prices_close = [x + d for x in self.prices_close]
        if self.tr_prev_close is not None:
            self.tr_prev_close += d
        if self.day_open_price is not None:
            self.day_open_price += d
        self.daily_start_price = deque([x + d for x in self.daily_start_price],
                                       maxlen=ROLLING_WINDOW_DAYS)
        if self.entry_price is not None:
            self.entry_price += d
        if self.entry_fill_price is not None:
            self.entry_fill_price += d
        if self.fast_ema is not None:
            self.fast_ema += d
        if self.slow_ema is not None:
            self.slow_ema += d
        # mco = fast - slow is unchanged by a common shift; signal likewise.

    # ── views ────────────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        s = self
        return {"bars": s.bars_seen, "bricks": s.bricks_total, "brick_size": round(s.brick_size, 3),
                "atr": round(s.atr, 3) if s.atr is not None else None,
                "renko_close": s.renko_closes[-1] if s.renko_closes else None,
                "up_box": s.up_box, "down_box": s.down_box,
                "mco": round(s.mco_list[-1], 4) if s.mco_list else None,
                "signal": round(s.mco_sig_list[-1], 4) if s.mco_sig_list else None,
                "vol_ma_status": s.vol_ma_status, "correlation": s.correlation_status,
                "algo_status": s.algo_status, "chop": s.cached_chop_ratio,
                "position": s.position, "entry_price": s.entry_price,
                "trade_duration": s.trade_duration,
                "gain": s.unrealized_gain_list[-1] if s.unrealized_gain_list else None,
                "max_gain": max(s.unrealized_gain_list) if s.unrealized_gain_list else None,
                "min_gain": min(s.unrealized_gain_list) if s.unrealized_gain_list else None,
                "pending": s.pending_signal, "last_trigger": s.last_trigger,
                "vol_spike": s.last_vol_spike, "last_gate": s.last_gate}

    # ── persistence ─────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()}
        for k in ("daily_start_price", "daily_bricks", "daily_brick_size_sum"):
            d[k] = list(getattr(self, k))
        d["current_day"] = self.current_day.isoformat() if self.current_day else None
        d["_version"] = 1
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "V221Engine":
        e = cls(d.get("use_vxn_entries", False), d.get("use_vxn_exits", True), d.get("use_gate", True))
        for k, v in d.items():
            if k.startswith("_"):
                continue
            if k in ("daily_start_price", "daily_bricks", "daily_brick_size_sum"):
                setattr(e, k, deque(v, maxlen=ROLLING_WINDOW_DAYS))
            elif k == "current_day":
                from datetime import date
                e.current_day = date.fromisoformat(v) if v else None
            else:
                setattr(e, k, v)
        return e

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), default=str))
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path, **flags) -> "V221Engine":
        path = Path(path)
        if not path.exists():
            return cls(**flags)
        try:
            return cls.from_dict(json.loads(path.read_text()))
        except Exception:
            return cls(**flags)


# ══════════════════════════════════════════════════════════════════════════════
# Reference-equivalent replay + measure (for --reconcile and the parity test)
# ══════════════════════════════════════════════════════════════════════════════
def run_engine(bars, use_vxn_entries, use_vxn_exits, use_gate, fng_lookup=None,
               all_fng_dates=None) -> list[dict]:
    """Same signature and semantics as vxn_revisit.run_engine(): bars are
    dicts with datetime_et/open/high/low/close/volume/vol_price."""
    e = V221Engine(use_vxn_entries, use_vxn_exits, use_gate)
    for b in bars:
        e.on_bar(b["datetime_et"], b["open"], b["high"], b["low"], b["close"], b["volume"],
                 b["vol_price"], fng_lookup, all_fng_dates, backtest_fill=True)
    return [{"exit_time": t["exit_time"], "direction": t["direction"], "pts": t["pts"],
             "reason": t.get("reason")} for t in e.trades]


def measure(trades, mult=NQ_MULT, commission=NQ_COMMISSION, contracts=FIXED_CONTRACTS,
            capital=NQ_CAPITAL) -> dict:
    """Verbatim from the reference: equity, max DD, daily Sharpe."""
    trades_sorted = sorted(trades, key=lambda t: str(t["exit_time"]))
    n = len(trades_sorted)
    equity = capital
    peak = capital
    maxdd = 0.0
    daily_pnl = {}
    for t in trades_sorted:
        pnl = (t["pts"] * mult - commission) * contracts
        equity += pnl
        if equity > peak:
            peak = equity
        dd = ((peak - equity) / peak * 100) if peak > 0 else 0
        if dd > maxdd:
            maxdd = dd
        day = str(t["exit_time"])[:10]
        daily_pnl[day] = daily_pnl.get(day, 0) + pnl
    daily_series = np.array(list(daily_pnl.values()))
    sharpe = (daily_series.mean() / daily_series.std()) * np.sqrt(252) \
        if len(daily_series) > 1 and daily_series.std() > 0 else None
    total = equity - capital
    return dict(n=n, total=total, maxdd=maxdd, sharpe=sharpe, days=len(daily_pnl))
