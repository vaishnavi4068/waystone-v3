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
    """Write a published daily prefix plus prior weekdays so options KPIs have history."""
    from waystone3.ibkr.kpis import prior_weekdays

    target = day or today_ny()
    store = LocalFsStore(out)
    cfg = IbkrSettings()
    pattern = [240.0, -80.0, 190.0, -45.0, 310.0, 95.0, -130.0, 275.0]
    for i, hist in enumerate(prior_weekdays(target, history_days)):
        pnl = pattern[i % len(pattern)]
        report = assemble_report(
            hist,
            [_history_option_fill(hist, pnl, 1)],
            [],
            demo_account(),
            cfg,
            tws_connected=False,
            generated_at=datetime(hist.year, hist.month, hist.day, 18, 0, tzinfo=NY),
        )
        publish_report(store, report)
    latest = demo_report(target, cfg)
    publish_report(store, latest)
    return latest
