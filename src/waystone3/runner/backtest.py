"""Backtest — replay the *same* pipeline over historical bars.

Walks a sliding window across history, feeding each window through
``score -> fuse -> decide`` against a :class:`PaperBroker` marked to each bar's close.
There is no parallel strategy implementation: the live cycle and the backtest share
:func:`waystone3.runner.cycle.run_cycle`, so what you backtest is what you trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from waystone3.brokers.paper import PaperBroker
from waystone3.core.types import Bar, OrderSide, Timeframe
from waystone3.data.base import MarketDataSource
from waystone3.decision.engine import DecisionConfig, DecisionEngine
from waystone3.risk.guard import RiskGuard
from waystone3.runner.config import default_contributors, default_weights
from waystone3.runner.cycle import run_cycle
from waystone3.signals.base import SignalContributor


class _WindowDataSource:
    """Serves history up to a moving index so ``run_cycle`` sees only the past."""

    name = "window"

    def __init__(self, histories: dict[str, list[Bar]], lookback: int) -> None:
        self.histories = histories
        self.lookback = lookback
        self.index = 0

    async def get_bars(
        self, symbol: str, timeframe: Timeframe, lookback: int
    ) -> list[Bar]:
        series = self.histories.get(symbol, [])
        return series[: self.index + 1][-self.lookback :]

    async def get_history(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        return self.histories.get(symbol, [])


@dataclass(frozen=True)
class BacktestMetrics:
    initial_equity: Decimal
    final_equity: Decimal
    total_return: Decimal
    max_drawdown: Decimal
    num_trades: int
    closed_trades: int
    win_rate: Decimal


@dataclass(frozen=True)
class BacktestResult:
    metrics: BacktestMetrics
    equity_curve: list[Decimal] = field(default_factory=list)
    realized_pnls: list[Decimal] = field(default_factory=list)


def _max_drawdown(equity: list[Decimal]) -> Decimal:
    peak = Decimal("-Infinity")
    worst = Decimal(0)
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            dd = (peak - e) / peak
            worst = max(worst, dd)
    return worst


async def run_backtest(
    *,
    data: MarketDataSource,
    symbols: list[str],
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
    lookback: int = 80,
    initial_cash: Decimal = Decimal(100_000),
    contributors: list[SignalContributor] | None = None,
    weights: dict[str, float] | None = None,
    decision: DecisionConfig | None = None,
) -> BacktestResult:
    contributors = contributors or default_contributors()
    weights = weights or default_weights()

    raw = {s: await data.get_history(s, timeframe, start, end) for s in symbols}
    histories = {s: bars for s, bars in raw.items() if bars}
    if not histories:
        raise ValueError("no historical bars for any requested symbol")

    length = min(len(b) for b in histories.values())
    # align to the most recent `length` bars so the index refers to comparable steps
    histories = {s: b[-length:] for s, b in histories.items()}
    active = list(histories)

    window = _WindowDataSource(histories, lookback)
    broker = PaperBroker(initial_cash=initial_cash)
    guard = RiskGuard(is_paper=True)
    engine = DecisionEngine(decision)

    equity_curve: list[Decimal] = []
    realized: list[Decimal] = []
    num_orders = 0

    for i in range(length):
        window.index = i
        pre = {p.symbol: p for p in await broker.get_positions()}
        report = await run_cycle(
            data=window,
            broker=broker,
            guard=guard,
            contributors=contributors,
            weights=weights,
            engine=engine,
            watchlist=active,
            timeframe=timeframe,
            bars_lookback=lookback,
        )
        for row in report.rows:
            if row.order is None or row.intent is None:
                continue
            num_orders += 1
            if row.intent.side == OrderSide.SELL and row.order.avg_fill_price is not None:
                held = pre.get(row.intent.symbol)
                if held is not None and held.qty > 0:
                    pnl = (row.order.avg_fill_price - held.avg_entry_price) * row.order.filled_qty
                    realized.append(pnl)

        for s in active:
            broker.set_mark(s, histories[s][i].close)
        acct = await broker.get_account()
        equity_curve.append(acct.equity)

    final_equity = equity_curve[-1] if equity_curve else initial_cash
    wins = sum(1 for p in realized if p > 0)
    win_rate = Decimal(wins) / Decimal(len(realized)) if realized else Decimal(0)
    metrics = BacktestMetrics(
        initial_equity=initial_cash,
        final_equity=final_equity,
        total_return=(final_equity - initial_cash) / initial_cash,
        max_drawdown=_max_drawdown(equity_curve),
        num_trades=num_orders,
        closed_trades=len(realized),
        win_rate=win_rate,
    )
    return BacktestResult(metrics=metrics, equity_curve=equity_curve, realized_pnls=realized)
