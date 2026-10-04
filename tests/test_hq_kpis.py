"""KPI engine: workbook Daily Calc, scorecard statuses/gate, returns and sync matching."""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal

from waystone3.hq import kpis
from waystone3.hq.calendar import NY
from waystone3.hq.compare import LOSS_CAP_BLOCKED, TradeRef, daily_sync, match_trades

SETTINGS = kpis.Settings(
    starting_capital=100_000, session_minutes=1380, model_slip_rt_per_contract=12.5
)


def _trade(day: date, net: float, slip: float = 0.0, hold: float = 60) -> kpis.ClosedTrade:
    return kpis.ClosedTrade(day, net, net + 9, 9, slip, net + 9 + slip, hold, 2)


def test_daily_calc_equity_drawdown_and_resets() -> None:
    trades = [
        _trade(date(2026, 9, 28), 1000),
        _trade(date(2026, 9, 29), -3000),
        _trade(date(2026, 10, 5), 500),
    ]
    rows = kpis.daily_calc(trades, SETTINGS, date(2026, 9, 28), date(2026, 10, 6), set())
    assert [r.session_date.isoformat() for r in rows][:3] == [
        "2026-09-28",
        "2026-09-29",
        "2026-09-30",
    ]
    assert len(rows) == 7
    first, second = rows[0], rows[1]
    assert first.equity_end == 101_000 and first.dd_itd == 0
    assert second.equity_end == 98_000
    assert math.isclose(second.dd_itd, 1 - 98_000 / 101_000)
    assert second.uw_days_itd == 1 and rows[2].uw_days_itd == 2
    monday = rows[5]
    assert monday.session_date == date(2026, 10, 5)
    assert monday.peak_week == 98_500 and monday.dd_week == 0 and monday.uw_days_week == 0
    assert monday.uw_days_itd == 5


def test_window_kpis_and_statuses() -> None:
    days = [date(2026, 9, 28 + i) for i in range(3)] + [date(2026, 10, 1), date(2026, 10, 2)]
    pnl = [1200, -400, 800, -200, 600]
    trades = [_trade(d, p, slip=25) for d, p in zip(days, pnl, strict=True)]
    rows = kpis.daily_calc(trades, SETTINGS, days[0], days[-1], set())
    res = kpis.window_kpis("ITD", rows, trades, SETTINGS, days[0], days[-1])
    rets = [r.daily_return for r in rows]
    mean = sum(rets) / 5
    sd = math.sqrt(sum((x - mean) ** 2 for x in rets) / 4)
    assert math.isclose(res.values["fut_sharpe"], mean / sd * math.sqrt(252))  # type: ignore[arg-type]
    assert res.values["fut_profit_factor"] == 2600 / 600
    assert res.values["fut_win_rate"] == 3 / 5
    assert res.values["fut_calmar"] == "n/a"
    assert math.isclose(res.values["fut_slippage_realism"], 12.5 / (125 / 10))  # type: ignore[arg-type]
    assert math.isclose(res.values["fut_time_in_market"], 300 / (5 * 1380))  # type: ignore[arg-type]
    assert res.trades == 5 and math.isclose(res.net_pnl, 2000)

    defs = {
        "fut_trade_count": kpis.KpiDef("fut_trade_count", "Higher", 200, 100, True),
        "fut_profit_factor": kpis.KpiDef("fut_profit_factor", "Higher", 1.5, 1.2, True),
        "fut_win_rate": kpis.KpiDef("fut_win_rate", "Higher", 0.5, 0.4, False),
        "fut_calmar": kpis.KpiDef("fut_calmar", "Higher", 0.75, 0.5, False),
    }
    scored = kpis.score_window(res, defs)
    assert scored.statuses["fut_profit_factor"] == "GREEN"
    assert scored.statuses["fut_calmar"] == "NA"
    assert scored.statuses["fut_trade_count"] == "RED"
    assert scored.gate is not None and scored.gate.startswith("INSUFFICIENT SAMPLE — 5 trades")
    week = kpis.score_window(
        kpis.window_kpis("WEEK", rows, trades, SETTINGS, days[0], days[-1]), defs
    )
    assert week.statuses["fut_trade_count"] == "INFO" and week.gate is None


def test_text_outcomes_map_like_the_workbook() -> None:
    kpi = kpis.KpiDef("x", "Lower", 0.3, 0.5, True)
    assert kpis.status_for("No losses", kpi) == "GREEN"
    assert kpis.status_for("Gross ≤ 0", kpi) == "RED"
    assert kpis.status_for("n/a", kpi) == "NA"
    assert kpis.status_for(0.4, kpi) == "AMBER"


def test_weekly_and_monthly_returns() -> None:
    trades = [_trade(date(2026, 9, 30), 1000), _trade(date(2026, 10, 1), -500)]
    rows = kpis.daily_calc(trades, SETTINGS, date(2026, 9, 28), date(2026, 10, 6), set())
    weeks = kpis.period_returns(rows, trades, SETTINGS, "week")
    assert [(w.period_start, w.period_end, w.trades) for w in weeks] == [
        (date(2026, 9, 28), date(2026, 10, 2), 2),
        (date(2026, 10, 5), date(2026, 10, 9), 0),
    ]
    assert weeks[0].net_pnl == 500 and weeks[0].win_rate == 0.5
    months = kpis.period_returns(rows, trades, SETTINGS, "month")
    assert [m.key for m in months] == [date(2026, 9, 1), date(2026, 10, 1)]
    assert months[1].start_equity == 101_000


def _ref(key: int, direction: str, entry: str, exit_: str, net: str, reason: str) -> TradeRef:
    day = date(2026, 10, 1)
    return TradeRef(
        key,
        direction,
        datetime.combine(day, datetime.strptime(entry, "%H:%M").time(), NY),
        datetime.combine(day, datetime.strptime(exit_, "%H:%M").time(), NY),
        None,
        Decimal(net),
        reason,
    )


def test_matching_excludes_loss_cap_blocked_backtest_trades() -> None:
    paper = [_ref(1, "SHORT", "09:31", "12:18", "-4358.96", "yeah_its_failing")]
    bt = [
        _ref(10, "SHORT", "09:31", "12:40", "-4434", "yeah_its_failing"),
        _ref(11, "LONG", "12:42", "15:55", "-1034", "SESSION_FLATTEN"),
    ]
    cap_at = datetime(2026, 10, 1, 12, 43, tzinfo=NY)
    matches = match_trades(paper, bt, cap_at)
    assert [(m.match_type, m.unmatched_reason) for m in matches] == [
        ("MATCHED", None),
        ("BACKTEST_ONLY", LOSS_CAP_BLOCKED),
    ]
    assert matches[0].exit_gap_min == Decimal("-22.00")
    row = daily_sync(
        paper, matches, loss_cap_hit=True, pct_threshold=Decimal("0.15"), usd_floor=Decimal(0)
    )
    assert row.bt_trades == 1 and row.bt_net_pnl == Decimal("-4434")
    assert row.pnl_delta == Decimal("75.04")
    assert (row.exit_reason_match, row.loss_cap_hit, row.sync_status) == ("Y", "Y", "OK")


def test_sync_flags_and_missing_backtest() -> None:
    paper = [_ref(1, "SHORT", "09:31", "10:20", "-7983.96", "yeah_its_failing")]
    bt = [_ref(10, "SHORT", "09:31", "10:05", "-6008.96", "yeah_its_failing")]
    row = daily_sync(
        paper,
        match_trades(paper, bt, None),
        loss_cap_hit=True,
        pct_threshold=Decimal("0.15"),
        usd_floor=Decimal(0),
    )
    assert row.sync_status == "FLAG" and row.pnl_delta_pct == Decimal("-0.3287")
    floor = daily_sync(
        paper,
        match_trades(paper, bt, None),
        loss_cap_hit=True,
        pct_threshold=Decimal("0.15"),
        usd_floor=Decimal(5000),
    )
    assert floor.sync_status == "OK"
    missing = daily_sync(
        paper, None, loss_cap_hit=False, pct_threshold=Decimal("0.15"), usd_floor=Decimal(0)
    )
    assert missing.sync_status == "N/A" and missing.bt_trades is None


def test_sync_rolling_block() -> None:
    points = [
        kpis.SyncPoint(date(2026, 9, 24), 1, -4358.96, -4434, 0.0169, "OK"),
        kpis.SyncPoint(date(2026, 9, 28), 1, 2266.04, 2466, -0.0811, "OK"),
        kpis.SyncPoint(date(2026, 10, 2), 1, -7983.96, -6008.96, -0.3287, "FLAG"),
        kpis.SyncPoint(date(2026, 10, 5), 0, 0, None, None, "N/A"),
    ]
    last = kpis.sync_rolling(points)[-1]
    assert last.days_logged == 4 and last.all_time_trades == 3
    assert math.isclose(last.all_time_live_pnl, -10076.88)
    assert last.all_time_flags == 1 and last.flag_rate == 1 / 3
    assert last.last7_flags == 1 and math.isclose(last.last7_live_pnl, -5717.92)
