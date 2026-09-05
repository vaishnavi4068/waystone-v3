"""Sample IBKR dump used for local KPI preview and tests (no TWS)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from waystone3.ibkr.export import assemble_report, publish_report
from waystone3.ibkr.models import AccountSnapshot, Book, DailyReport, Execution, PositionSnapshot
from waystone3.ibkr.settings import IbkrSettings
from waystone3.ibkr.store import LocalFsStore
from waystone3.ibkr.timeutil import NY, today_ny


def demo_executions(day: date) -> list[Execution]:
    noon = datetime(day.year, day.month, day.day, 12, 15, tzinfo=NY)
    return [
        Execution(
            exec_id="fut-1",
            perm_id=101,
            order_id=1,
            time=noon,
            account="U1234567",
            sec_type="FUT",
            symbol="ES",
            local_symbol="ESU6",
            con_id=1001,
            exchange="CME",
            expiry="20260918",
            multiplier="50",
            side="BOT",
            qty=2,
            price=5234.25,
            commission=4.60,
            commission_currency="USD",
            realized_pnl=0.0,
            client_id=1,
            book=Book.FUTURES,
        ),
        Execution(
            exec_id="fut-2",
            perm_id=102,
            order_id=2,
            time=noon + timedelta(hours=2),
            account="U1234567",
            sec_type="FUT",
            symbol="ES",
            local_symbol="ESU6",
            con_id=1001,
            exchange="CME",
            expiry="20260918",
            multiplier="50",
            side="SLD",
            qty=1,
            price=5240.00,
            commission=2.30,
            commission_currency="USD",
            realized_pnl=287.70,
            client_id=1,
            book=Book.FUTURES,
        ),
        Execution(
            exec_id="opt-1",
            perm_id=201,
            order_id=3,
            time=noon + timedelta(minutes=40),
            account="U1234567",
            sec_type="OPT",
            symbol="SPX",
            local_symbol="SPX   260918C05200000",
            con_id=2001,
            exchange="CBOE",
            expiry="20260918",
            strike=5200.0,
            right="C",
            multiplier="100",
            side="BOT",
            qty=1,
            price=18.40,
            commission=1.25,
            commission_currency="USD",
            realized_pnl=0.0,
            client_id=2,
            book=Book.OPTIONS,
        ),
        Execution(
            exec_id="opt-2",
            perm_id=202,
            order_id=4,
            time=noon + timedelta(hours=3),
            account="U1234567",
            sec_type="OPT",
            symbol="SPX",
            local_symbol="SPX   260918C05200000",
            con_id=2001,
            exchange="CBOE",
            expiry="20260918",
            strike=5200.0,
            right="C",
            multiplier="100",
            side="SLD",
            qty=1,
            price=21.10,
            commission=1.25,
            commission_currency="USD",
            realized_pnl=268.50,
            client_id=2,
            book=Book.OPTIONS,
        ),
    ]


def demo_positions() -> list[PositionSnapshot]:
    return [
        PositionSnapshot(
            account="U1234567",
            sec_type="FUT",
            symbol="ES",
            local_symbol="ESU6",
            con_id=1001,
            exchange="CME",
            expiry="20260918",
            multiplier="50",
            qty=1,
            avg_cost=5234.25,
            market_price=5238.50,
            market_value=261925.0,
            unrealized_pnl=212.50,
            book=Book.FUTURES,
        )
    ]


def demo_account() -> AccountSnapshot:
    return AccountSnapshot(
        account_id="U1234567",
        nlv=1_052_340.12,
        cash=248_110.55,
        buying_power=410_000.00,
        excess_liquidity=188_440.00,
        maint_margin=22_150.00,
        currency="USD",
    )


def demo_report(day: date | None = None, settings: IbkrSettings | None = None) -> DailyReport:
    target = day or today_ny()
    cfg = settings or IbkrSettings()
    return assemble_report(
        target,
        demo_executions(target),
        demo_positions(),
        demo_account(),
        cfg,
        tws_connected=False,
        generated_at=datetime(target.year, target.month, target.day, 18, 0, tzinfo=NY),
    )


def _history_future_fill(day: date, pnl: float, seq: int) -> Execution:
    noon = datetime(day.year, day.month, day.day, 12, 30, tzinfo=NY)
    return Execution(
        exec_id=f"hist-fut-{day.isoformat()}-{seq}",
        time=noon,
        account="U1234567",
        sec_type="FUT",
        symbol="NQ",
        local_symbol="NQU6",
        exchange="CME",
        expiry="20260918",
        multiplier="20",
        side="SLD" if pnl >= 0 else "BOT",
        qty=1,
        price=18_200.0,
        commission=2.25,
        realized_pnl=pnl,
        client_id=1,
        book=Book.FUTURES,
    )


def _history_option_fill(day: date, pnl: float, seq: int) -> Execution:
    noon = datetime(day.year, day.month, day.day, 13, 0, tzinfo=NY)
    return Execution(
        exec_id=f"hist-opt-{day.isoformat()}-{seq}",
        time=noon,
        account="U1234567",
        sec_type="OPT",
        symbol="SPX",
        local_symbol="SPX   260918C05200000",
        exchange="CBOE",
        expiry="20260918",
        strike=5200.0,
        right="C",
        multiplier="100",
        side="SLD" if pnl >= 0 else "BOT",
        qty=1,
        price=18.40,
        commission=1.25,
        realized_pnl=pnl,
        client_id=2,
        book=Book.OPTIONS,
    )


def seed_demo(out: Path, day: date | None = None, history_days: int = 0) -> DailyReport:
    """Write a published daily prefix plus prior weekdays so KPI pages have history."""
    from waystone3.ibkr.kpis import prior_weekdays

    target = day or today_ny()
    store = LocalFsStore(out)
    cfg = IbkrSettings()
    pattern = [240.0, -80.0, 190.0, -45.0, 310.0, 95.0, -130.0, 275.0]
    for i, hist in enumerate(prior_weekdays(target, history_days)):
        pnl = pattern[i % len(pattern)]
        fut = pattern[(i + 3) % len(pattern)] * 1.15
        report = assemble_report(
            hist,
            [_history_option_fill(hist, pnl, 1), _history_future_fill(hist, fut, 1)],
            [],
            demo_account(),
            cfg,
            tws_connected=False,
            generated_at=datetime(hist.year, hist.month, hist.day, 18, 0, tzinfo=NY),
        )
        publish_report(store, report)
    latest = demo_report(target, cfg)
    publish_report(store, latest)
    seed_compare_demo(store, target, latest.executions)
    return latest


def seed_compare_demo(store: LocalFsStore, day: date, fills: list[Execution]) -> None:
    """Write live + replay blotters for the three default algos (replay slightly off)."""
    from waystone3.ibkr.algo_registry import ensure_registry
    from waystone3.ibkr.compare import publish_blotter

    registry = ensure_registry(store)
    by_book = {
        "s5_options": [e for e in fills if e.book is Book.OPTIONS],
        "es_futures": [e for e in fills if e.book is Book.FUTURES and e.symbol == "ES"],
        "nq_futures": [e for e in fills if e.book is Book.FUTURES and e.symbol == "NQ"],
    }
    if not by_book["nq_futures"]:
        by_book["nq_futures"] = [_history_future_fill(day, 210.0, 9)]
    for algo in registry.algos:
        live = list(by_book.get(algo.id, []))
        replay = [
            row.model_copy(
                update={
                    "exec_id": f"replay-{row.exec_id}",
                    "price": round(row.price * (1.001 if i % 2 == 0 else 0.998), 4),
                    "realized_pnl": (
                        None if row.realized_pnl is None else round(row.realized_pnl * 0.92, 2)
                    ),
                }
            )
            for i, row in enumerate(live)
        ]
        if live and algo.id == "s5_options":
            extra = live[0].model_copy(
                update={
                    "exec_id": f"replay-extra-{live[0].exec_id}",
                    "price": live[0].price + 0.15,
                    "realized_pnl": 12.0,
                }
            )
            replay.append(extra)
        publish_blotter(store, algo.resolved_live(), day, live)
        publish_blotter(store, algo.resolved_replay(), day, replay)
