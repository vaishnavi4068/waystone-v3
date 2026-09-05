"""Truncation test for look-ahead: every trade that ENTERED before the cut date must be identical
whether the strategy saw the full history or only the history up to the cut.  If a strategy peeked
past its signal bar, trades near the cut would differ.  Run:  python -m pytest tests -q"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wsbt import costs, data as D  # noqa: E402


def load(name: str):
    p = ROOT / "strategies" / name / "backtest.py"
    spec = importlib.util.spec_from_file_location(name, p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _compare(tr_full: pd.DataFrame, tr_cut: pd.DataFrame, cut: pd.Timestamp, hold_days: int, key="entry_date"):
    safe = cut - pd.Timedelta(days=hold_days * 2 + 5)      # trades that closed comfortably before the cut
    a = tr_full[tr_full[key] < safe].reset_index(drop=True)
    b = tr_cut[tr_cut[key] < safe].reset_index(drop=True)
    assert len(a) == len(b) and len(a) > 0, f"trade counts differ or empty: {len(a)} vs {len(b)}"
    pd.testing.assert_frame_equal(a[[key, "exit_date", "pnl"]], b[[key, "exit_date", "pnl"]], check_dtype=False)


@pytest.mark.parametrize("mode,max_hold", [("bb", 10), ("nr7", 2)])
def test_mean_reversion(mode, max_hold):
    m = load("01_mean_reversion")
    bars = D.synthetic_daily(1500, seed=3)
    cut = bars.index[1100]
    full = m.run(bars, mode, max_hold, None if mode == "bb" else 1.5, False, True, 25_000, 100_000, costs.US_ETF)[1]
    part = m.run(bars[bars.index <= cut], mode, max_hold, None if mode == "bb" else 1.5, False, True, 25_000, 100_000, costs.US_ETF)[1]
    _compare(full, part, cut, max_hold)


def test_calendar_tom():
    m = load("04_calendar_effects")
    bars = D.synthetic_daily(1200, seed=8)
    cut = bars.index[900]
    fomc = pd.DatetimeIndex([])
    full = m.run(bars, "tom", 4, 3, 2, fomc, costs.US_ETF, 100, 100_000)[1]
    part = m.run(bars[bars.index <= cut], "tom", 4, 3, 2, fomc, costs.US_ETF, 100, 100_000)[1]
    _compare(full, part, cut, 8)


def test_gex_gap_fade():
    m = load("02_gex_dealer_gamma")
    bars = D.synthetic_daily(300, s0=5000.0, seed=4)
    chain = D.synthetic_chain(bars["close"], strike_step=25.0, dtes=(14, 30))
    gex = m.daily_gex(chain)
    cut = bars.index[240]
    full = m.run(bars, gex, "gap_fade", 0.5, 0.002, 1.0, 0.002, 0.02, costs.MES, 1, 100_000)[1]
    part = m.run(bars[bars.index <= cut], gex[gex.index <= cut], "gap_fade", 0.5, 0.002, 1.0, 0.002, 0.02, costs.MES, 1, 100_000)[1]
    _compare(full, part, cut, 1)


def test_pead():
    m = load("08_pead_implied_move")
    syms = [f"E{i:02d}" for i in range(12)]
    frames = D.synthetic_panel(syms, n=1600, seed=23)
    first = next(iter(frames.values()))
    earn = D.synthetic_earnings(syms, str(first.index[0].date()), str(first.index[-1].date()))
    cut = first.index[1300]
    full = m.run(frames, earn, 0.75, 0.01, 10, 2.0, 8, True, False, 10, 100_000)[1]
    cutf = {s: f[f.index <= cut] for s, f in frames.items()}
    part = m.run(cutf, earn, 0.75, 0.01, 10, 2.0, 8, True, False, 10, 100_000)[1]
    full = full.sort_values(["entry_date", "symbol"]).reset_index(drop=True)
    part = part.sort_values(["entry_date", "symbol"]).reset_index(drop=True)
    _compare(full, part, cut, 10)


def test_sector_rotation_weights_stable():
    m = load("06_sector_rotation")
    frames = D.synthetic_panel(m.SECTORS, n=1500, seed=6)
    closes = D.closes_panel(frames)
    cut = closes.index[1200]
    w_full = m.weights_monthly(closes, 3, (63, 126, 252), 5, True)
    w_part = m.weights_monthly(closes[closes.index <= cut], 3, (63, 126, 252), 5, True)
    common = w_part.index[w_part.index < cut - pd.Timedelta(days=40)]
    pd.testing.assert_frame_equal(w_full.loc[common], w_part.loc[common])


def test_cvd_intraday():
    m = load("05_orderflow_cvd_mnq")
    from datetime import time as dtime
    bars = D.synthetic_intraday(days=12, seed=5)
    cut = bars.index[int(len(bars) * 0.8)]
    kw = dict(trade_start=dtime(9, 45), trade_end=dtime(15, 30), flat_at=dtime(15, 55), session_start=dtime(9, 30),
              cost=costs.MNQ, units=1, capital=50_000)
    full = m.run(bars, 1.5, 15, 1.5, 60, 0.0, **kw)[1]
    part = m.run(bars[bars.index <= cut], 1.5, 15, 1.5, 60, 0.0, **kw)[1]
    safe = cut - pd.Timedelta(days=2)
    a = full[full["entry_ts"] < safe].reset_index(drop=True)
    b = part[part["entry_ts"] < safe].reset_index(drop=True)
    assert len(a) == len(b) > 0
    pd.testing.assert_frame_equal(a[["entry_ts", "exit_ts", "pnl"]], b[["entry_ts", "exit_ts", "pnl"]])


def test_engine_signal_lag():
    """A target that 'knows' today's direction must not profit: it is filled at the next open."""
    import numpy as np
    from wsbt import engine, metrics
    bars = D.synthetic_daily(800, seed=1, regime=False)
    tgt = pd.Series(np.sign(bars["close"].diff().fillna(0)), index=bars.index)
    r, _, _ = engine.simulate_positions(bars, tgt, costs.US_ETF, {"units": 100})
    assert metrics.summary(r)["sharpe"] < 1.5           # a genuine look-ahead here would print Sharpe > 10
