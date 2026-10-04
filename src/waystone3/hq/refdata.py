"""Reference data the loader reads once per run (strategies, settings, calendar, KPIs)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from waystone3.hq import kpis
from waystone3.hq.db import Conn, Row


def dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass(frozen=True)
class Strategy:
    strategy_id: int
    code: str
    asset_class: str
    instrument_root: str
    paper_prefix: str
    backtest_prefix: str | None
    backtest_file_prefix: str | None
    paper_start_date: date | None


@dataclass
class RefData:
    strategies: dict[int, Strategy] = field(default_factory=dict)
    settings: dict[int, list[Row]] = field(default_factory=dict)
    holidays: set[date] = field(default_factory=set)
    instruments: dict[str, int] = field(default_factory=dict)
    kpi_defs: dict[str, kpis.KpiDef] = field(default_factory=dict)

    def settings_for(self, strategy_id: int, day: date) -> Row:
        rows = self.settings.get(strategy_id)
        if not rows:
            raise LookupError(f"no ref.strategy_settings row for strategy {strategy_id}")
        applicable = [r for r in rows if r["valid_from"] <= day]
        return applicable[-1] if applicable else rows[0]


def load_ref(conn: Conn) -> RefData:
    ref = RefData()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM ref.strategy WHERE is_active ORDER BY strategy_id")
        for r in cur.fetchall():
            ref.strategies[r["strategy_id"]] = Strategy(
                r["strategy_id"],
                r["strategy_code"],
                r["asset_class"],
                r["instrument_root"],
                r["gcs_paper_prefix"],
                r["gcs_backtest_prefix"],
                r["backtest_file_prefix"],
                r["paper_start_date"],
            )
        cur.execute("SELECT * FROM ref.strategy_settings ORDER BY strategy_id, valid_from")
        for r in cur.fetchall():
            ref.settings.setdefault(r["strategy_id"], []).append(r)
        cur.execute("SELECT holiday_date FROM ref.trading_holiday WHERE exchange = 'CME'")
        ref.holidays = {r["holiday_date"] for r in cur.fetchall()}
        cur.execute("SELECT symbol, instrument_id FROM ref.instrument")
        ref.instruments = {r["symbol"]: r["instrument_id"] for r in cur.fetchall()}
        cur.execute("SELECT * FROM ref.kpi_definition")
        ref.kpi_defs = {
            r["kpi_code"]: kpis.KpiDef(
                r["kpi_code"],
                r["direction"],
                None if r["green_at"] is None else float(r["green_at"]),
                None if r["amber_at"] is None else float(r["amber_at"]),
                r["is_critical"],
            )
            for r in cur.fetchall()
        }
    conn.commit()
    return ref
