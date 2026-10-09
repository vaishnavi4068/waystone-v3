"""V221 paper-log and backtest-replay parsers (fixtures mirror the VM's 2026-10-01/02 logs)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from waystone3.hq.backtest_daily import parse_backtest_daily
from waystone3.hq.calendar import NY, session_date_for
from waystone3.hq.loader import classify_paper
from waystone3.hq.v221_log import parse_paper_log

FIXTURES = Path(__file__).parent / "fixtures" / "hq" / "raw"


def _paper(code: str, day: str):  # type: ignore[no-untyped-def]
    text = (FIXTURES / "paper" / code / f"{day}.log").read_text()
    return parse_paper_log(text, date.fromisoformat(day))


def test_nq_day_header_trade_and_summary() -> None:
    day = _paper("nq_v221", "2026-10-02")
    assert (day.params_fp, day.broker_account, day.broker_client_id, day.symbol) == (
        "7636aa0c66",
        "DUR842609",
        77,
        "NQZ6",
    )
    assert day.unparsed == []
    [trade] = day.trades
    assert trade.trade_no == 9
    assert (trade.direction, trade.contracts) == ("SHORT", Decimal(2))
    assert (trade.entry_px, trade.exit_px) == (Decimal("31077.75"), Decimal("31277.125"))
    assert trade.exit_reason == "yeah_its_failing"
    assert trade.points == Decimal("-199.375")
    assert trade.gross_pnl == Decimal("-7975.00")
    assert trade.commission == Decimal("8.96")
    assert trade.net_pnl == Decimal("-7983.96")
    assert trade.hold_min == Decimal(49)
    assert (trade.entry_slip_pts, trade.exit_slip_pts) == (Decimal("1.25"), Decimal("6.625"))
    assert trade.entry_ts == datetime(2026, 10, 2, 9, 31, 12, tzinfo=NY)
    assert trade.signal_bar_ts == datetime(2026, 10, 2, 9, 30, tzinfo=NY)

    assert day.summary is not None
    assert (day.summary.triggers, day.summary.entries, day.summary.closed) == (4, 1, 1)
    assert day.summary.net_pnl == Decimal("-7983.96")
    assert (day.nlv_start, day.nlv_end) == (Decimal("980248.09"), Decimal("972126.48"))
    assert day.consistent
    assert day.loss_cap_hit and day.loss_cap_blocks == 1
    assert day.gate_blocks == 1


def test_nq_signals_and_ops_events() -> None:
    day = _paper("nq_v221", "2026-10-02")
    outcomes = [(s.bar_ts.strftime("%H:%M"), s.side, s.outcome) for s in day.signals]
    assert outcomes == [
        ("09:30", "SHORT", "ENTERED"),
        ("11:01", "LONG", "BLOCKED"),
        ("13:14", "SHORT", "BLOCKED"),
        ("15:55", "LONG", "BLOCKED"),
    ]
    assert "daily loss limit" in (day.signals[1].reason or "")
    assert "cutoff" in (day.signals[3].reason or "")
    codes = [(e.category, e.code, e.severity) for e in day.events]
    assert ("BROKER", "IB_2103", "WARN") in codes
    assert ("BROKER", "IB_2104", "INFO") in codes
    assert ("RISK", "LOSS_CAP", "WARN") in codes
    assert ("CONNECTIVITY", "DISCONNECTED", "ERROR") in codes
    assert ("SYSTEM", "SIGTERM", "WARN") in codes
    assert [(f.role, f.action) for f in day.fills] == [("ENTRY", "SELL"), ("EXIT", "BUY")]


def test_r2_three_trades_reconcile_with_summary() -> None:
    day = _paper("r2_mnq", "2026-10-02")
    assert day.broker_client_id == 88
    assert [t.trade_no for t in day.trades] == [21, 22, 23]
    assert sum(t.net_pnl or 0 for t in day.trades) == Decimal("-601.16")
    assert [f.role for f in day.fills if f.role != "ENTRY"] == ["STOP", "EXIT", "FLATTEN"]
    assert day.checks()["net_match"] is True
    assert day.unparsed == []


def test_summary_mismatch_is_not_consistent() -> None:
    text = (FIXTURES / "paper" / "r2_mnq" / "2026-10-02.log").read_text()
    text = text.replace("closed 3", "closed 4")
    day = parse_paper_log(text, date(2026, 10, 2))
    assert not day.consistent
    assert day.checks()["closed_match"] is False


def test_intraday_log_without_summary_keeps_open_trade() -> None:
    text = (FIXTURES / "paper" / "nq_v221" / "2026-10-02.log").read_text()
    cut = text.split("2026-10-02 09:45:00")[0]
    day = parse_paper_log(cut, date(2026, 10, 2))
    [trade] = day.trades
    assert not trade.closed and trade.exit_ts is None
    assert day.summary is None


def test_unknown_lines_are_counted() -> None:
    day = parse_paper_log("2026-10-02 09:00:00 [NEWTAG] something new\n", date(2026, 10, 2))
    assert day.unparsed == [1]


def test_es_backtest_table() -> None:
    text = (FIXTURES / "backtest" / "ES_2026-10-01_back_daily.txt").read_text()
    bt = parse_backtest_daily(text, date(2026, 10, 1))
    assert (bt.params_fp, bt.bar_count, bt.vol_index) == ("f36bb2a138", 961, "VXN")
    assert bt.point_value == Decimal(50)
    assert bt.trades_reported == 2 and bt.total_net_reported == Decimal("-5468.00")
    assert bt.maxdd_reported == Decimal("0.0547")
    assert [(t.direction, t.exit_reason) for t in bt.trades] == [
        ("SHORT", "yeah_its_failing"),
        ("LONG", "SESSION_FLATTEN"),
    ]
    assert bt.trades[0].entry_ts == datetime(2026, 10, 1, 9, 31, tzinfo=NY)
    assert bt.trades[0].net_pnl == Decimal("-4434.00")
    assert bt.unparsed == []


def test_nq_backtest_key_value_lines() -> None:
    text = (FIXTURES / "backtest" / "NQ_2026-10-02_back_daily.txt").read_text()
    bt = parse_backtest_daily(text, date(2026, 10, 2))
    [trade] = bt.trades
    assert (trade.direction, trade.points, trade.contracts) == (
        "SHORT",
        Decimal("-150.00"),
        Decimal(2),
    )
    assert trade.exit_ts == datetime(2026, 10, 2, 10, 5, tzinfo=NY)
    assert bt.trades_reported == 1
    assert bt.unparsed == []


_NQ_1007_HEADER = "NQ same-day replay 2026-10-07\nparams fp: 7636aa0c66\n"
_NQ_1007_TOTAL = "\ntotal trades: 1  total net: -6399.00\n"


@pytest.mark.parametrize(
    "line",
    [
        "2026-10-07 09:31:00-04:00 SHORT -> 2026-10-07 15:55:00-04:00  pts -159.75  "
        "net $-6,399.00  reason=yeah_its_failing",
        "2026-10-07 09:31:00-04:00 -> 2026-10-07 15:55:00-04:00 SHORT -159.75 pts "
        "-$6,399.00 reason=yeah_its_failing",
        "entry=2026-10-07 09:31:00-04:00 exit=2026-10-07 15:55:00-04:00 dir=SHORT "
        "pts: -159.75 pnl: -6399.00 reason=yeah_its_failing",
        "#1 SHORT entry=2026-10-07T09:31:00-04:00 exit=2026-10-07T15:55:00-04:00 "
        "pts=-159.75 net=-6399.00 reason=yeah_its_failing",
    ],
)
def test_backtest_trade_with_offset_timestamps(line: str) -> None:
    bt = parse_backtest_daily(_NQ_1007_HEADER + line + _NQ_1007_TOTAL, date(2026, 10, 7))
    [trade] = bt.trades
    assert trade.seq == 1
    assert trade.direction == "SHORT"
    assert trade.entry_ts == datetime(2026, 10, 7, 9, 31, tzinfo=NY)
    assert trade.exit_ts == datetime(2026, 10, 7, 15, 55, tzinfo=NY)
    assert (trade.entry_px, trade.exit_px) == (None, None)
    assert trade.points == Decimal("-159.75")
    assert trade.net_pnl == Decimal("-6399.00")
    assert trade.exit_reason == "yeah_its_failing"
    assert bt.total_net_reported == Decimal("-6399.00")


def test_backtest_table_with_offset_timestamps_reads_no_price_from_the_clock() -> None:
    text = (
        "#  dir    entry_time                  exit_time                   pts      reason\n"
        "1  SHORT  2026-10-07 09:31:00-04:00   2026-10-07 15:55:00-04:00   -159.75  "
        "yeah_its_failing\n"
    )
    [trade] = parse_backtest_daily(text, date(2026, 10, 7)).trades
    assert trade.exit_ts == datetime(2026, 10, 7, 15, 55, tzinfo=NY)
    assert (trade.entry_px, trade.exit_px, trade.points) == (None, None, Decimal("-159.75"))


def test_session_date_rolls_after_18_et_and_over_weekends() -> None:
    holidays = {date(2026, 11, 26)}
    assert session_date_for(datetime(2026, 10, 2, 17, 59, tzinfo=NY), 18, holidays) == date(
        2026, 10, 2
    )
    assert session_date_for(datetime(2026, 10, 2, 18, 0, tzinfo=NY), 18, holidays) == date(
        2026, 10, 5
    )
    assert session_date_for(datetime(2026, 11, 25, 19, 0, tzinfo=NY), 18, holidays) == date(
        2026, 11, 27
    )


def test_classify_paper_objects() -> None:
    assert classify_paper("raw/paper/nq_v221/2026-10-02.log") == ("paper_log", date(2026, 10, 2))
    assert classify_paper("raw/paper/nq_v221/2026-10-02_info.txt") == (
        "paper_info",
        date(2026, 10, 2),
    )
    assert classify_paper("raw/paper/es_v221/events_2026-10-02.jsonl")[0] == "paper_events"
    assert classify_paper("raw/paper/es_v221/cron.log") == ("other", None)
