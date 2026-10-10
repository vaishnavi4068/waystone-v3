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

    def paper_dates(self, code: str | None = None) -> list[str]:
        rows = self._rows(
            "SELECT DISTINCT session_date FROM api.v_day_status WHERE paper_status <> 'NONE' "
            "AND (%s::text IS NULL OR strategy_code = %s) ORDER BY 1",
            (code, code),
        )
        return [r["session_date"] for r in rows]

    def paper_day(self, code: str | None, day: date) -> dict[str, Any]:
        """Everything the paper engine did on one session: trades, fills, signals, events.

        ``freshness`` says how current it is: when the VM last synced the log to GCS, the
        last timestamped line the engine wrote, and when the loader read it.
        """
        scope = (code, code, day)
        where = "(%s::text IS NULL OR s.strategy_code = %s) AND {alias}.session_date = %s"
        trades = self._rows(
            "SELECT * FROM api.v_paper_trades s WHERE "
            + where.format(alias="s")
            + " ORDER BY entry_ts, strategy_code",
            scope,
        )
        fills = self._rows(
            "SELECT s.strategy_code, f.fill_ts, i.symbol AS instrument, f.action, f.quantity, "
            "f.price, f.commission, f.leg_role, f.exec_id, f.order_ref "
            "FROM core.paper_fill f JOIN ref.strategy s USING (strategy_id) "
            "LEFT JOIN ref.instrument i USING (instrument_id) WHERE "
            + where.format(alias="f")
            + " ORDER BY f.fill_ts, s.strategy_code",
            scope,
        )
        signals = self._rows(
            "SELECT s.strategy_code, e.signal_bar_ts, e.side, e.signal_px, e.outcome, "
            "e.block_reason FROM core.signal_event e JOIN ref.strategy s USING (strategy_id) "
            "WHERE " + where.format(alias="e") + " ORDER BY e.signal_bar_ts, s.strategy_code",
            scope,
        )
        events = self._rows(
            "SELECT s.strategy_code, o.event_ts, o.category, o.code, o.severity, o.message "
            "FROM core.ops_event o JOIN ref.strategy s USING (strategy_id) "
            "WHERE " + where.format(alias="o") + " ORDER BY o.event_ts, s.strategy_code",
            scope,
        )
        freshness = self._rows(
            "SELECT s.strategy_code, s.display_name, d.paper_status, d.paper_loaded_at, "
            "d.finalized_at, d.checks->'paper' AS checks, f.gcs_uri, f.gcs_updated_at, "
            "f.size_bytes, f.loaded_at AS file_loaded_at, f.parse_status, "
            "(SELECT max(l.line_ts) FROM raw.source_line l WHERE l.file_id = f.file_id) "
            "AS last_log_line_ts, "
            "ss.daily_loss_cap, ss.starting_capital, ss.max_trades_per_day, ss.flatten_time "
            "FROM ref.strategy s "
            "LEFT JOIN ops.day_status d ON d.strategy_id = s.strategy_id "
            "AND d.session_date = %s "
            "LEFT JOIN LATERAL (SELECT * FROM raw.source_file rf "
            "WHERE rf.strategy_id = s.strategy_id AND rf.session_date = %s "
            "AND rf.source_kind = 'paper_log' AND rf.is_current "
            "ORDER BY rf.loaded_at DESC LIMIT 1) f ON true "
            "LEFT JOIN LATERAL (SELECT * FROM ref.strategy_settings x "
            "WHERE x.strategy_id = s.strategy_id AND x.valid_from <= %s "
            "ORDER BY x.valid_from DESC LIMIT 1) ss ON true "
            "WHERE s.is_active AND s.asset_class = 'future' "
            "AND (%s::text IS NULL OR s.strategy_code = %s) ORDER BY s.strategy_id",
            (day, day, day, code, code),
        )
        return {
            "session_date": day.isoformat(),
            "strategy_code": code,
            "freshness": freshness,
            "last_paper_load": self._one(
                "SELECT * FROM api.v_load_health WHERE job IN ('paper', 'backfill') "
                "ORDER BY finished_at DESC NULLS LAST LIMIT 1"
            ),
            "trades": trades,
            "fills": fills,
            "signals": signals,
            "events": events,
        }

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
            "context": self.day_context(code, day),
        }

    def day_context(self, code: str, day: date) -> dict[str, Any]:
        """Settings, backtest header, signal counts and log checks behind one session."""
        settings = self._one(
            "SELECT ss.point_value, ss.default_contracts, ss.commission_rt_per_contract, "
            "ss.model_slip_rt_per_contract, ss.flatten_time, ss.daily_loss_cap "
            "FROM ref.strategy_settings ss JOIN ref.strategy s USING (strategy_id) "
            "WHERE s.strategy_code = %s AND ss.valid_from <= %s "
            "ORDER BY ss.valid_from DESC LIMIT 1",
            (code, day),
        )
        run = self._one(
            "SELECT r.config_label, r.params_fp, r.point_value, r.flatten_time, r.daily_loss_cap, "
            "r.trades_reported, r.total_net_reported, r.status, r.bar_count "
            "FROM core.backtest_run r JOIN ref.strategy s USING (strategy_id) "
            "WHERE s.strategy_code = %s AND r.session_date = %s",
            (code, day),
        )
        live_params = self._one(
            "SELECT c.params_fp, c.config_label, c.params "
            "FROM core.paper_trade t JOIN ref.strategy s USING (strategy_id) "
            "JOIN ref.strategy_config c "
            "ON c.strategy_id = t.strategy_id AND c.params_fp = t.params_fp "
            "WHERE s.strategy_code = %s AND t.session_date = %s LIMIT 1",
            (code, day),
        )
        signals = self._rows(
            "SELECT e.outcome, e.block_reason, count(*) AS n "
            "FROM core.signal_event e JOIN ref.strategy s USING (strategy_id) "
            "WHERE s.strategy_code = %s AND e.session_date = %s "
            "GROUP BY 1, 2 ORDER BY 1, 2",
            (code, day),
        )
        status = self._one(
            "SELECT paper_status, backtest_status, sync_status, checks, paper_loaded_at, "
            "backtest_loaded_at, finalized_at FROM api.v_day_status "
            "WHERE strategy_code = %s AND session_date = %s",
            (code, day),
        )
        return {
            "settings": settings,
            "backtest_run": run,
            "live_params": live_params,
            "signals": signals,
            "day_status": status,
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

    # ------------------------------------------------------------ sentiment
    def sentiment_dates(self) -> list[str]:
        rows = self._rows("SELECT DISTINCT session_date FROM api.v_sentiment_snapshot ORDER BY 1")
        return [r["session_date"] for r in rows]

    def sentiment_range(
        self, start: date | None = None, end: date | None = None
    ) -> list[dict[str, Any]]:
        """Whole-session rows (slot DAY) with each day's top recommendation, newest first."""
        return self._rows(
            "SELECT s.session_date, s.is_final, s.fng_cnn, s.fng_replica, s.fng_prior_day, s.vix, "
            "s.vix_term_ratio, s.vol_spike, s.narrative_score, s.narrative_dispersion, "
            "s.narrative_n, s.kill_hits, s.events, s.regime, s.data_gaps, s.best_strategy, "
            "s.headline, s.summary, s.policy_version, s.computed_at, "
            "(SELECT count(*) FROM api.v_sentiment_snapshot i "
            " WHERE i.session_date = s.session_date"
            " AND i.slot_label <> 'DAY') AS intervals, "
            "(SELECT json_agg(json_build_object('strategy_code', r.strategy_code, "
            " 'verdict', r.verdict, "
            " 'size_mult', r.size_mult, 'rank', r.rank) ORDER BY r.rank) "
            " FROM api.v_sentiment_recommendation r WHERE r.session_date = s.session_date "
            " AND r.slot_label = 'DAY') AS verdicts "
            "FROM api.v_sentiment_snapshot s WHERE s.slot_label = 'DAY' "
            "AND (%s::date IS NULL OR s.session_date >= %s::date) "
            "AND (%s::date IS NULL OR s.session_date <= %s::date) ORDER BY s.session_date DESC",
            (start, start, end, end),
        )

    def sentiment_day(self, day: date, strategy: str | None = None) -> dict[str, Any]:
        """One session: the DAY summary, every interval, every score, gate and headline."""
        scope = (day, strategy, strategy)
        return {
            "session_date": day.isoformat(),
            "slots": self._rows(
                "SELECT * FROM api.v_sentiment_snapshot WHERE session_date = %s "
                "ORDER BY slot_label = 'DAY', slot_label",
                (day,),
            ),
            "recommendations": self._rows(
                "SELECT * FROM api.v_sentiment_recommendation WHERE session_date = %s "
                "AND (%s::text IS NULL OR strategy_code = %s) ORDER BY slot_label, rank",
                scope,
            ),
            "gates": self._rows(
                "SELECT * FROM api.v_sentiment_gate WHERE session_date = %s "
                "AND (%s::text IS NULL OR strategy_code = %s) "
                "ORDER BY slot_label, strategy_code, gate",
                scope,
            ),
            "scores": self._rows(
                "SELECT session_date, slot_label, layer, component, value, score, state, source, "
                "detail FROM api.v_sentiment_score WHERE session_date = %s "
                "ORDER BY slot_label, layer, component",
                (day,),
            ),
            "headlines": self._rows(
                "SELECT * FROM api.v_sentiment_headline WHERE session_date = %s "
                "ORDER BY published_at DESC LIMIT 300",
                (day,),
            ),
            "events": self._rows(
                "SELECT * FROM api.v_macro_event WHERE session_date = %s ORDER BY event_ts", (day,)
            ),
        }

    def sentiment_latest(self) -> dict[str, Any]:
        """The newest interval written (intraday slot, or DAY when no interval yet)."""
        snap = self._one(
            "SELECT * FROM api.v_sentiment_snapshot ORDER BY session_date DESC, "
            "slot_label = 'DAY', slot_ts DESC LIMIT 1"
        )
        if snap is None:
            return {"snapshot": None, "recommendations": [], "gates": []}
        key = (snap["session_date"], snap["slot_label"])
        return {
            "snapshot": snap,
            "recommendations": self._rows(
                "SELECT * FROM api.v_sentiment_recommendation WHERE session_date = %s "
                "AND slot_label = %s ORDER BY rank",
                key,
            ),
            "gates": self._rows(
                "SELECT * FROM api.v_sentiment_gate WHERE session_date = %s AND slot_label = %s "
                "ORDER BY strategy_code, gate",
                key,
            ),
        }

    def sentiment_gates(
        self,
        start: date | None = None,
        end: date | None = None,
        strategy: str | None = None,
        gate: str | None = None,
        state: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM api.v_sentiment_gate WHERE "
            "(%s::date IS NULL OR session_date >= %s::date) "
            "AND (%s::date IS NULL OR session_date <= %s::date) "
            "AND (%s::text IS NULL OR strategy_code = %s) AND (%s::text IS NULL OR gate = %s) "
            "AND (%s::text IS NULL OR state = %s) "
            "ORDER BY session_date DESC, slot_label DESC, strategy_code, gate LIMIT 500",
            (start, start, end, end, strategy, strategy, gate, gate, state, state),
        )

    def sentiment_upcoming_events(self, day: date, days: int = 14) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM api.v_macro_event WHERE session_date BETWEEN %s AND %s::date + %s "
            "ORDER BY event_ts",
            (day, day, days),
        )

    def sentiment_series(
        self, layer: str, component: str, start: date | None = None, end: date | None = None
    ) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT session_date, value, score, state, source FROM api.v_sentiment_score "
            "WHERE slot_label = 'DAY' AND layer = %s AND component = %s "
            "AND (%s::date IS NULL OR session_date >= %s::date) "
            "AND (%s::date IS NULL OR session_date <= %s::date) ORDER BY session_date",
            (layer, component, start, start, end, end),
        )

    def sentiment_efficacy(self) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM api.v_sentiment_score WHERE slot_label = 'DAY' "
            "AND layer IN ('efficacy', 'drift') AND session_date = "
            "(SELECT max(session_date) FROM api.v_sentiment_score WHERE layer = 'efficacy') "
            "ORDER BY layer, component"
        )

    def sentiment_health(self) -> dict[str, Any]:
        return {
            "sources": self._rows("SELECT * FROM api.v_sentiment_health ORDER BY source"),
            "overrides": self._rows(
                "SELECT * FROM api.v_sentiment_override WHERE revoked_at IS NULL "
                "AND (valid_to IS NULL OR valid_to >= now()) ORDER BY valid_from DESC"
            ),
        }
