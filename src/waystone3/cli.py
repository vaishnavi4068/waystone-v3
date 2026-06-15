"""Waystone v3 CLI."""

from __future__ import annotations

import asyncio
from datetime import datetime

import typer
from rich.console import Console
from rich.table import Table

from waystone3.brokers.base import Broker
from waystone3.brokers.paper import PaperBroker
from waystone3.core.types import Timeframe
from waystone3.data.base import MarketDataSource
from waystone3.data.stub import StubDataSource
from waystone3.decision.engine import DecisionEngine
from waystone3.risk.guard import RiskGuard
from waystone3.runner.backtest import BacktestResult, run_backtest
from waystone3.runner.config import RunnerConfig, default_contributors
from waystone3.runner.cycle import CycleReport, run_cycle

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Waystone v3 momentum CLI")
console = Console()


@app.callback()
def _main() -> None:
    """Waystone v3 — nimble technical-momentum trading."""


def _parse_symbols(symbols: str) -> list[str]:
    return [s.strip().upper() for s in symbols.split(",") if s.strip()]


def _build_data_source(source: str) -> MarketDataSource:
    if source == "stub":
        return StubDataSource()
    if source == "yfinance":
        from waystone3.data.yfinance import YFinanceDataSource

        return YFinanceDataSource()
    if source == "polygon":
        from waystone3.data.polygon import PolygonDataSource

        return PolygonDataSource()
    raise typer.BadParameter(f"unknown data source {source!r}")


def _build_broker(broker: str) -> Broker:
    if broker == "paper":
        return PaperBroker()
    if broker == "alpaca":
        from waystone3.brokers.alpaca import AlpacaBroker

        return AlpacaBroker()
    raise typer.BadParameter(f"unknown broker {broker!r}")


def _render(report: CycleReport) -> None:
    table = Table(title="Momentum cycle")
    table.add_column("Symbol")
    table.add_column("Score", justify="right")
    table.add_column("Action")
    table.add_column("Qty", justify="right")
    table.add_column("Drivers")
    for row in report.rows:
        comp = row.composite
        if row.order is not None and row.intent is not None:
            action = f"[green]{row.intent.side.value.upper()}[/green]"
            qty = str(row.order.filled_qty)
        elif row.blocked is not None:
            action = "[red]BLOCKED[/red]"
            qty = row.blocked
        else:
            action = "hold"
            qty = "-"
        table.add_row(
            comp.symbol,
            f"{comp.score:.2f}",
            action,
            qty,
            ", ".join(comp.drivers[:4]),
        )
    console.print(table)
    console.print(f"[bold]{len(report.orders)}[/bold] order(s) placed.")


@app.command()
def run(
    symbols: str = typer.Option("AAPL,MSFT,NVDA", help="Comma-separated watchlist."),
    source: str = typer.Option("stub", help="Data source: stub | yfinance."),
    broker: str = typer.Option("paper", help="Broker: paper | alpaca."),
    timeframe: Timeframe = typer.Option(Timeframe.D1, help="Bar timeframe."),
    lookback: int = typer.Option(80, help="Bars of history per symbol."),
) -> None:
    """Run one momentum cycle and place paper orders."""
    watchlist = _parse_symbols(symbols)
    data = _build_data_source(source)
    brk = _build_broker(broker)
    guard = RiskGuard(is_paper=brk.is_paper)
    cfg = RunnerConfig(watchlist=watchlist, timeframe=timeframe, bars_lookback=lookback)
    engine = DecisionEngine(cfg.decision)

    report = asyncio.run(
        run_cycle(
            data=data,
            broker=brk,
            guard=guard,
            contributors=default_contributors(),
            weights=cfg.weights,
            engine=engine,
            watchlist=watchlist,
            timeframe=timeframe,
            bars_lookback=lookback,
        )
    )
    _render(report)


def _render_backtest(result: BacktestResult) -> None:
    m = result.metrics
    table = Table(title="Backtest")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Initial equity", f"${m.initial_equity:,.2f}")
    table.add_row("Final equity", f"${m.final_equity:,.2f}")
    table.add_row("Total return", f"{m.total_return:.2%}")
    table.add_row("Max drawdown", f"{m.max_drawdown:.2%}")
    table.add_row("Orders placed", str(m.num_trades))
    table.add_row("Closed trades", str(m.closed_trades))
    table.add_row("Win rate", f"{m.win_rate:.2%}")
    console.print(table)


@app.command()
def backtest(
    symbols: str = typer.Option("SPY", help="Comma-separated symbols."),
    start: datetime = typer.Option(..., help="Start date, e.g. 2023-01-01."),
    end: datetime = typer.Option(..., help="End date, e.g. 2024-01-01."),
    source: str = typer.Option("yfinance", help="Data source: stub | yfinance."),
    timeframe: Timeframe = typer.Option(Timeframe.D1, help="Bar timeframe."),
    lookback: int = typer.Option(80, help="Window size per step."),
) -> None:
    """Replay the momentum pipeline over history and report P&L."""
    watchlist = _parse_symbols(symbols)
    data = _build_data_source(source)
    result = asyncio.run(
        run_backtest(
            data=data,
            symbols=watchlist,
            timeframe=timeframe,
            start=start,
            end=end,
            lookback=lookback,
        )
    )
    _render_backtest(result)


@app.command()
def serve(
    symbols: str = typer.Option("AAPL,MSFT,NVDA", help="Comma-separated watchlist."),
    source: str = typer.Option("stub", help="Data source: stub | yfinance."),
    broker: str = typer.Option("paper", help="Broker: paper | alpaca."),
    cycles: int = typer.Option(3, help="Number of cycles to run."),
    timeframe: Timeframe = typer.Option(Timeframe.D1, help="Bar timeframe."),
    lookback: int = typer.Option(80, help="Bars of history per symbol."),
) -> None:
    """Run the Agent OS: cycles + reactive agents (analyst, tuner, supervisor, alerts)."""
    from waystone3.agent_os import serve as serve_os

    watchlist = _parse_symbols(symbols)
    data = _build_data_source(source)
    brk = _build_broker(broker)
    result = asyncio.run(
        serve_os(
            data=data,
            broker=brk,
            watchlist=watchlist,
            cycles=cycles,
            timeframe=timeframe,
            lookback=lookback,
        )
    )

    summary = Table(title="Agent OS run")
    summary.add_column("Metric")
    summary.add_column("Value", justify="right")
    summary.add_row("Cycles", str(result.cycles))
    summary.add_row("Orders placed", str(result.total_orders))
    summary.add_row("Trading enabled", str(result.trading_enabled))
    summary.add_row("Agent actions", str(len(result.actions)))
    summary.add_row("Alerts dispatched", str(len(result.alerts)))
    summary.add_row("Cycles judged", str(len(result.judgements)))
    summary.add_row("Final weights", str(result.final_weights))
    console.print(summary)

    if result.actions:
        actions = Table(title="Agent actions (audited)")
        actions.add_column("Kind")
        actions.add_column("Detail")
        actions.add_column("Status")
        for kind, detail, status in result.actions:
            actions.add_row(kind, detail, status)
        console.print(actions)


@app.command("arena-seed")
def arena_seed(
    players: str = typer.Option(..., help="Comma-separated player display names."),
) -> None:
    """Register players for the Arena (writes to WAYSTONE_DB) and print their tokens."""
    import os

    from waystone3.competition.arena import build_service_from_env, seed_players

    if not os.getenv("WAYSTONE_ADMIN_TOKEN"):
        raise typer.BadParameter("set WAYSTONE_ADMIN_TOKEN first")
    if not os.getenv("WAYSTONE_DB"):
        raise typer.BadParameter("set WAYSTONE_DB (a SQLite path) so seeded players persist")
    names = [n for n in (s.strip() for s in players.split(",")) if n]
    service = build_service_from_env()
    created = seed_players(service, names)

    table = Table(title="Arena players (hand each token to its player)")
    table.add_column("Player")
    table.add_column("Token")
    for row in created:
        table.add_row(row["display_name"], row["token"])
    console.print(table)


@app.command("arena-serve")
def arena_serve(
    host: str = typer.Option("0.0.0.0", help="Bind host."),
    port: int = typer.Option(9100, help="Bind port."),
) -> None:
    """Serve the Arena MCP server over HTTP (configured from env)."""
    from waystone3.mcp_server import run

    run(transport="http", host=host, port=port)


@app.command("api-serve")
def api_serve(
    host: str = typer.Option("0.0.0.0", help="Bind host."),
    port: int = typer.Option(9200, help="Bind port."),
) -> None:
    """Serve the read-only dashboard API (per-user, configured from env)."""
    from waystone3.api.app import run

    run(host=host, port=port)


if __name__ == "__main__":
    app()
