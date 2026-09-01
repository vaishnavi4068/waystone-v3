"""Aggregate fills into futures vs options daily stats."""

from __future__ import annotations

from collections.abc import Sequence

from waystone3.ibkr.models import Book, BookStats, DailySummary, Execution


def _multiplier(raw: str | None) -> float:
    if not raw:
        return 1.0
    try:
        value = float(raw)
    except ValueError:
        return 1.0
    return value if value > 0 else 1.0


def _accumulate(stats: BookStats, fill: Execution) -> None:
    stats.fills += 1
    stats.qty += abs(fill.qty)
    stats.notional += abs(fill.qty * fill.price * _multiplier(fill.multiplier))
    if fill.commission is not None:
        stats.commission += fill.commission
    if fill.realized_pnl is not None:
        stats.realized_pnl += fill.realized_pnl


def summarize(day: str, fills: Sequence[Execution]) -> DailySummary:
    futures = BookStats()
    options = BookStats()
    other = BookStats()
    totals = BookStats()
    for fill in fills:
        if fill.book is Book.FUTURES:
            _accumulate(futures, fill)
        elif fill.book is Book.OPTIONS:
            _accumulate(options, fill)
        else:
            _accumulate(other, fill)
        _accumulate(totals, fill)
    return DailySummary(date=day, futures=futures, options=options, other=other, totals=totals)
