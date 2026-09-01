"""Dict shapes the dashboard API returns for IBKR snapshots."""

from __future__ import annotations

from typing import Any

from waystone3.ibkr.models import AccountSnapshot, DailyReport, Execution, PositionSnapshot
from waystone3.ibkr.reader import list_published_days, today_is_published
from waystone3.ibkr.store import ReportStore
from waystone3.ibkr.timeutil import today_ny


def _side(raw: str) -> str:
    token = raw.strip().upper()
    if token in {"BOT", "BUY"}:
        return "buy"
    if token in {"SLD", "SELL"}:
        return "sell"
    return raw.lower()


def execution_dict(row: Execution) -> dict[str, Any]:
    return {
        "exec_id": row.exec_id,
        "time": row.time.isoformat(),
        "account": row.account,
        "sec_type": row.sec_type,
        "symbol": row.symbol,
        "local_symbol": row.local_symbol,
        "exchange": row.exchange,
        "expiry": row.expiry,
        "strike": row.strike,
        "right": row.right,
        "multiplier": row.multiplier,
        "side": row.side,
        "qty": row.qty,
        "price": row.price,
        "commission": row.commission,
        "realized_pnl": row.realized_pnl,
        "client_id": row.client_id,
        "book": row.book.value,
    }


def position_dict(row: PositionSnapshot) -> dict[str, Any]:
    return {
        "symbol": row.symbol,
        "local_symbol": row.local_symbol,
        "qty": row.qty,
        "avg_entry_price": row.avg_cost,
        "market_price": row.market_price,
        "unrealized_pnl": row.unrealized_pnl,
        "sec_type": row.sec_type,
        "expiry": row.expiry,
        "strike": row.strike,
        "right": row.right,
        "book": row.book.value,
        "exchange": row.exchange,
        "multiplier": row.multiplier,
        "avg_cost": row.avg_cost,
        "market_value": row.market_value,
    }


def order_dict(row: Execution) -> dict[str, Any]:
    return {
        "symbol": row.symbol,
        "local_symbol": row.local_symbol,
        "side": _side(row.side),
        "qty": row.qty,
        "status": "filled",
        "avg_fill_price": row.price,
        "submitted_at": row.time.isoformat(),
        "sec_type": row.sec_type,
        "expiry": row.expiry,
        "strike": row.strike,
        "right": row.right,
        "book": row.book.value,
        "commission": row.commission,
        "realized_pnl": row.realized_pnl,
        "exec_id": row.exec_id,
    }


def account_dict(row: AccountSnapshot) -> dict[str, Any]:
    return {
        "account_id": row.account_id,
        "nlv": row.nlv,
        "cash": row.cash,
        "buying_power": row.buying_power,
        "excess_liquidity": row.excess_liquidity,
        "maint_margin": row.maint_margin,
        "currency": row.currency,
        "equity": row.nlv,
    }


def report_dict(report: DailyReport, store: ReportStore) -> dict[str, Any]:
    return {
        "date": report.date,
        "generated_at": report.generated_at.isoformat(),
        "published": True,
        "today": today_ny().isoformat(),
        "today_published": today_is_published(store),
        "executions": [execution_dict(e) for e in report.executions],
        "positions": [position_dict(p) for p in report.positions],
        "account": account_dict(report.account),
        "summary": report.summary.model_dump(mode="json"),
        "manifest": report.manifest.model_dump(mode="json"),
    }


def days_dict(store: ReportStore) -> dict[str, Any]:
    days = [d.isoformat() for d in list_published_days(store)]
    today = today_ny().isoformat()
    return {
        "days": days,
        "latest": days[-1] if days else None,
        "today": today,
        "today_published": today in days,
    }
