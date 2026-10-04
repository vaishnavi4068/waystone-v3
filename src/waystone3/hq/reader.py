"""Read-only queries over the HQ ``api.*`` views, shared by the dashboard API and MCP.

Every method returns plain JSON-ready dicts (numbers as float, dates as ISO strings).
Connections are opened per call as ``waystone_read`` with read-only transactions.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

KPI_WINDOWS = ("DAY", "WEEK", "MTD", "ITD")


def secret_manager_value(resource: str) -> str:
    """Read ``projects/P/secrets/S/versions/V`` with the workload's own credentials."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(credentials)  # type: ignore[no-untyped-call]
    response = session.get(f"https://secretmanager.googleapis.com/v1/{resource}:access", timeout=10)
    response.raise_for_status()
    return base64.b64decode(response.json()["payload"]["data"]).decode()


def hq_dsn_from_env(
    env: Mapping[str, str] | None = None,
    fetch_secret: Callable[[str], str] = secret_manager_value,
) -> str | None:
    """``WAYSTONE_HQ_DSN``, or one built from ``WAYSTONE_HQ_DB_*``; None when unset.

    The password comes from ``WAYSTONE_HQ_DB_PASSWORD`` or, when that is empty, from the
    Secret Manager version named by ``WAYSTONE_HQ_DB_PASSWORD_SECRET``.
    """
    e = os.environ if env is None else env
    dsn = e.get("WAYSTONE_HQ_DSN", "").strip()
    if dsn:
        return dsn
    host = e.get("WAYSTONE_HQ_DB_HOST", "").strip()
    if not host or host.startswith("__"):  # unfilled manifest placeholder
        return None
    password = e.get("WAYSTONE_HQ_DB_PASSWORD", "")
    secret = e.get("WAYSTONE_HQ_DB_PASSWORD_SECRET", "").strip()
    if not password and secret:
        password = fetch_secret(secret)
    return make_conninfo(
        host=host,
        port=e.get("WAYSTONE_HQ_DB_PORT", "5432"),
        dbname=e.get("WAYSTONE_HQ_DB_NAME", "waystone"),
        user=e.get("WAYSTONE_HQ_DB_USER", "waystone_read"),
        password=password,
        sslmode=e.get("WAYSTONE_HQ_DB_SSLMODE", "require"),
    )


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


class HqReader:
    def __init__(self, dsn: str, *, connect_timeout: int = 5) -> None:
        self.dsn = dsn
        self.connect_timeout = connect_timeout

    def _rows(self, query: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with psycopg.connect(
            self.dsn,
            row_factory=dict_row,
            autocommit=True,
            connect_timeout=self.connect_timeout,
            options="-c default_transaction_read_only=on -c statement_timeout=15000",
        ) as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [{k: _plain(v) for k, v in row.items()} for row in rows]

    def _one(self, query: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self._rows(query, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------ strategies
    def strategies(self) -> list[dict[str, Any]]:
        return self._rows(
            """
            SELECT s.*,
                   sc.as_of_date, sc.trading_days, sc.trades, sc.net_pnl, sc.return_pct,
                   sc.equity_end, sc.red_count, sc.amber_count, sc.green_count, sc.overall_gate,
                   ds.session_date AS last_session, ds.paper_status, ds.backtest_status,
                   ds.sync_status
            FROM api.v_strategy s
            LEFT JOIN LATERAL (
                SELECT * FROM api.v_scorecard c
                WHERE c.strategy_code = s.strategy_code AND c.kpi_window = 'ITD'
                ORDER BY c.as_of_date DESC LIMIT 1
            ) sc ON true
            LEFT JOIN LATERAL (
                SELECT * FROM api.v_day_status d
                WHERE d.strategy_code = s.strategy_code
                ORDER BY d.session_date DESC LIMIT 1
            ) ds ON true
            ORDER BY s.is_active DESC, s.strategy_code
            """
        )

    def strategy(self, code: str) -> dict[str, Any] | None:
        return next((s for s in self.strategies() if s["strategy_code"] == code), None)

    # ------------------------------------------------------------ KPIs
    def kpi_dates(self, code: str) -> list[str]:
        rows = self._rows(
            "SELECT DISTINCT as_of_date FROM api.v_scorecard WHERE strategy_code = %s "
            "ORDER BY as_of_date",
            (code,),
        )
        return [r["as_of_date"] for r in rows]

    def _as_of(self, code: str, as_of: date | None) -> str | None:
        row = self._one(
            "SELECT max(as_of_date) AS d FROM api.v_scorecard "
            "WHERE strategy_code = %s AND (%s::date IS NULL OR as_of_date <= %s::date)",
            (code, as_of, as_of),
        )
        return row["d"] if row else None

    def kpis(self, code: str, as_of: date | None = None) -> dict[str, Any]:
        """Scorecard + KPI rows for every window on one as-of date (latest by default)."""
        day = self._as_of(code, as_of)
        if day is None:
            return {"strategy_code": code, "as_of_date": None, "scorecard": [], "kpis": []}
        scorecard = self._rows(
            "SELECT * FROM api.v_scorecard WHERE strategy_code = %s AND as_of_date = %s "
            "ORDER BY array_position(%s::text[], kpi_window)",
            (code, day, list(KPI_WINDOWS)),
        )
        kpis = self._rows(
            "SELECT kpi_window, section, section_name, sort_order, kpi_code, label, description, "
            "direction, green_at, amber_at, unit, num_value, text_value, status, window_start, "
            "window_end FROM api.v_kpi WHERE strategy_code = %s AND as_of_date = %s "
            "ORDER BY array_position(%s::text[], kpi_window), section, sort_order",
            (code, day, list(KPI_WINDOWS)),
        )
        return {"strategy_code": code, "as_of_date": day, "scorecard": scorecard, "kpis": kpis}

    # ------------------------------------------------------------ daily data
    def daily_pnl(
        self, code: str, start: date | None = None, end: date | None = None
    ) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM api.v_daily_pnl WHERE strategy_code = %s "
            "AND (%s::date IS NULL OR session_date >= %s::date) "
            "AND (%s::date IS NULL OR session_date <= %s::date) ORDER BY session_date",
            (code, start, start, end, end),
        )

    def paper_trades(
        self, code: str, start: date | None = None, end: date | None = None
    ) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM api.v_paper_trades WHERE strategy_code = %s "
            "AND (%s::date IS NULL OR session_date >= %s::date) "
            "AND (%s::date IS NULL OR session_date <= %s::date) "
            "ORDER BY session_date, entry_ts NULLS LAST, trade_no",
            (code, start, start, end, end),
        )

    def daily_sync(
        self, code: str | None = None, start: date | None = None, end: date | None = None
    ) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM api.v_daily_sync WHERE (%s::text IS NULL OR strategy_code = %s) "
            "AND (%s::date IS NULL OR session_date >= %s::date) "
            "AND (%s::date IS NULL OR session_date <= %s::date) "
            "ORDER BY session_date DESC, strategy_code",
            (code, code, start, start, end, end),
        )

    def sync_dates(self) -> list[str]:
        rows = self._rows("SELECT DISTINCT session_date FROM api.v_daily_sync ORDER BY 1")
        return [r["session_date"] for r in rows]

    def comparison(self, code: str, day: date) -> dict[str, Any]:
        sync = self._one(
            "SELECT * FROM api.v_daily_sync WHERE strategy_code = %s AND session_date = %s",
            (code, day),
        )
        matches = self._rows(
            "SELECT * FROM api.v_comparison WHERE strategy_code = %s AND session_date = %s "
            "ORDER BY match_seq",
            (code, day),
        )
        backtest = self._rows(
            "SELECT * FROM api.v_backtest_trades WHERE strategy_code = %s AND session_date = %s "
            "ORDER BY trade_seq",
            (code, day),
        )
        return {
            "strategy_code": code,
            "session_date": day.isoformat(),
            "sync": sync,
            "matches": matches,
            "paper_trades": self.paper_trades(code, day, day),
            "backtest_trades": backtest,
        }

    def returns(self, code: str) -> dict[str, Any]:
        return {
            "strategy_code": code,
            "weekly": self._rows(
                "SELECT * FROM api.v_returns_weekly WHERE strategy_code = %s ORDER BY week_start",
                (code,),
            ),
            "monthly": self._rows(
                "SELECT * FROM api.v_returns_monthly WHERE strategy_code = %s ORDER BY month_start",
                (code,),
            ),
            "sync_rolling": self._one(
                "SELECT * FROM api.v_sync_rolling WHERE strategy_code = %s "
                "ORDER BY as_of_date DESC LIMIT 1",
                (code,),
            ),
        }

    def day_status(
        self, code: str | None = None, start: date | None = None, end: date | None = None
    ) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM api.v_day_status WHERE (%s::text IS NULL OR strategy_code = %s) "
            "AND (%s::date IS NULL OR session_date >= %s::date) "
            "AND (%s::date IS NULL OR session_date <= %s::date) "
            "ORDER BY session_date DESC, strategy_code",
            (code, code, start, start, end, end),
        )

    def load_health(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM api.v_load_health ORDER BY job")
