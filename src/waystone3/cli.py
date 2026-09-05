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
from waystone3.workspace.runtime import DEFAULT_DASHBOARD_USERS

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
    players: str = typer.Option(
        ",".join(DEFAULT_DASHBOARD_USERS),
        help="Comma-separated team member names (max 5). "
        "Defaults to Mark, Manoj, Brent, Akash, Kole.",
    ),
    passwords: str = typer.Option(
        "",
        help="Optional comma-separated passwords. Omitting uses each user's default.",
    ),
) -> None:
    """Add team members (default password is lowercase name + 1234) and print credentials."""
    import os

    from waystone3.workspace.runtime import build_service_from_env, seed_members
    from waystone3.workspace.workspace import WorkspaceError

    if not os.getenv("WAYSTONE_ADMIN_TOKEN"):
        raise typer.BadParameter("set WAYSTONE_ADMIN_TOKEN first")
    if not os.getenv("WAYSTONE_DB"):
        raise typer.BadParameter("set WAYSTONE_DB (a SQLite path) so members persist")
    names = [n for n in (s.strip() for s in players.split(",")) if n]
    pw_list: list[str | None] | None = None
    if passwords.strip():
        pw_list = [p.strip() or None for p in passwords.split(",")]
        if len(pw_list) != len(names):
            raise typer.BadParameter("passwords count must match --players")
    service = build_service_from_env()
    try:
        created = seed_members(service, names, pw_list)
    except (ValueError, WorkspaceError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    table = Table(title="Team members (hand each password + token privately)")
    table.add_column("Member")
    table.add_column("Dashboard password")
    table.add_column("MCP token")
    for row in created:
        table.add_row(row["name"], row["password"], row["token"])
    console.print(table)
    console.print(
        "[dim]Dashboard: sign in with member name + password. "
        "Claude MCP: Authorization: Bearer <token>.[/dim]"
    )


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


@app.command("ibkr-collect")
def ibkr_collect() -> None:
    """Merge today's TWS fills into the local execId ledger (read-only)."""
    from waystone3.ibkr.collect import collect

    result = collect()
    console.print(
        f"IBKR ledger {result.day.isoformat()}: {len(result.executions)} fill(s), "
        f"{result.new_fills} new, {len(result.positions)} open position(s)."
    )


@app.command("ibkr-export")
def ibkr_export(
    dry_run: bool = typer.Option(False, "--dry-run", help="Write a local tree instead of GCS."),
    date: str | None = typer.Option(
        None, "--date", help="America/New_York calendar day YYYY-MM-DD (default: today)."
    ),
) -> None:
    """Collect from TWS and publish the daily dump (GCS, or local with --dry-run)."""
    from datetime import date as date_cls

    from waystone3.ibkr.export import export_day

    day = date_cls.fromisoformat(date) if date else None
    try:
        report = export_day(day=day, dry_run=dry_run)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    dest = "dry-run tree" if dry_run else "GCS"
    console.print(
        f"Published {report.date} to {dest}: {report.summary.totals.fills} fill(s), "
        f"realized PnL {report.summary.totals.realized_pnl:.2f}."
    )


@app.command("ibkr-seed-demo")
def ibkr_seed_demo(
    out: str = typer.Option(
        "./reports/demo",
        "--out",
        help="Local directory mirroring GCS keys (set IBKR_REPORTS_LOCAL_DIR to this).",
    ),
    date: str | None = typer.Option(
        None,
        "--date",
        help="America/New_York day YYYY-MM-DD (default: staged week ending 2026-08-14).",
    ),
) -> None:
    """Write staged sample dumps (week of 10 Aug 2026) so dashboards render locally."""
    from datetime import date as date_cls
    from pathlib import Path

    from waystone3.ibkr.demo import seed_demo

    day = date_cls.fromisoformat(date) if date else None
    report = seed_demo(Path(out), day, history_days=40)
    console.print(
        f"Wrote STAGED dump for {report.date} (+ 40 weekdays) under {out}.\n"
        f"  Week of 10 Aug 2026 is marked staged on Daily, Compare, Options KPIs, Futures KPIs.\n"
        f"  Also seeded 3 algos (s5_options, nq_futures, es_futures) live vs replay.\n"
        f"  export IBKR_REPORTS_LOCAL_DIR={out}\n"
        f"  uv run waystone3 api-serve"
    )


@app.command("algo-list")
def algo_list() -> None:
    """List onboarded paper algos from the report store (GCS or local)."""
    from waystone3.ibkr.algo_registry import ensure_registry
    from waystone3.ibkr.store import build_report_store_from_env

    store = build_report_store_from_env()
    if store is None:
        raise typer.BadParameter("set IBKR_REPORTS_BUCKET or IBKR_REPORTS_LOCAL_DIR")
    registry = ensure_registry(store)
    table = Table(title="Paper algos")
    table.add_column("Id")
    table.add_column("Name")
    table.add_column("Book")
    table.add_column("Live prefix")
    table.add_column("Replay prefix")
    table.add_column("On")
    for row in registry.algos:
        table.add_row(
            row.id,
            row.name,
            row.book.value,
            row.resolved_live(),
            row.resolved_replay(),
            "yes" if row.enabled else "no",
        )
    console.print(table)


@app.command("algo-register")
def algo_register(
    algo_id: str = typer.Option(..., "--id", help="Lowercase id, e.g. nq_futures."),
    name: str = typer.Option(..., help="Display name."),
    book: str = typer.Option("futures", help="futures | options | other"),
    live_prefix: str = typer.Option("", help="GCS/local prefix for IBKR daily logs."),
    replay_prefix: str = typer.Option("", help="GCS/local prefix for same-day replay."),
    client_id: int | None = typer.Option(None, help="Optional IBKR clientId filter."),
    notes: str = typer.Option("", help="Optional description."),
) -> None:
    """Onboard a new algo so Daily Compare can join its live log and replay."""
    from waystone3.ibkr.algo_registry import AlgoConfig, ensure_registry, save_registry
    from waystone3.ibkr.models import Book
    from waystone3.ibkr.store import build_report_store_from_env

    store = build_report_store_from_env()
    if store is None:
        raise typer.BadParameter("set IBKR_REPORTS_BUCKET or IBKR_REPORTS_LOCAL_DIR")
    try:
        book_enum = Book(book)
    except ValueError as exc:
        raise typer.BadParameter("book must be futures, options, or other") from exc
    registry = ensure_registry(store)
    try:
        algo = registry.upsert(
            AlgoConfig(
                id=algo_id,
                name=name,
                book=book_enum,
                live_prefix=live_prefix,
                replay_prefix=replay_prefix,
                client_id=client_id,
                notes=notes,
            )
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    save_registry(store, registry)
    console.print(
        f"Onboarded {algo.id} ({algo.name}). "
        f"Live {algo.resolved_live()}/dt=YYYY-MM-DD/  "
        f"Replay {algo.resolved_replay()}/dt=YYYY-MM-DD/"
    )


if __name__ == "__main__":
    app()
