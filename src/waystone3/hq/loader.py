"""GCS -> Postgres loader for Waystone HQ (``waystone3 load-logs``).

Each run:
  1. lists the strategy prefixes in the bucket and skips objects whose GCS generation
     is already loaded,
  2. stores every new file version in ``raw`` (one transaction per file, so a bad file
     never blocks the rest),
  3. parses paper logs and backtest replays into ``core``,
  4. recomputes comparison, daily sync, day status, daily P&L and every KPI table for the
     strategies that changed (see ``recompute.py``).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from waystone3.hq.backtest_daily import FILE_NAME as BACKTEST_NAME
from waystone3.hq.backtest_daily import BacktestDay, parse_backtest_daily
from waystone3.hq.calendar import NY, session_date_for
from waystone3.hq.db import Conn, Cursor, Row, copy_rows, upsert
from waystone3.hq.db import connect as connect
from waystone3.hq.recompute import recompute
from waystone3.hq.refdata import RefData, Strategy, dec, load_ref
from waystone3.hq.sources import ObjectInfo, ObjectSource
from waystone3.hq.v221_log import PaperDay, Trade, parse_paper_log

JOBS = ("paper", "backtest", "backfill", "recompute")
_PAPER_LOG = re.compile(r"(?:^|/)(\d{4}-\d{2}-\d{2})\.log$")
_ANY_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_PAPER_TABLES = ("signal_event", "paper_fill", "ops_event", "account_snapshot", "paper_trade")


@dataclass
class RunReport:
    run_id: int
    job: str
    status: str = "RUNNING"
    files_seen: int = 0
    files_loaded: int = 0
    failures: list[str] = field(default_factory=list)
    recomputed: list[str] = field(default_factory=list)


def classify_paper(name: str) -> tuple[str, date | None]:
    base = name.rsplit("/", 1)[-1]
    found = _ANY_DATE.search(base)
    day = date.fromisoformat(found.group(1)) if found else None
    if _PAPER_LOG.search(name):
        return "paper_log", day
    if base.endswith("_info.txt"):
        return "paper_info", day
    if base.startswith("events_") and base.endswith(".jsonl"):
        return "paper_events", day
    return "other", day


class Loader:
    def __init__(
        self,
        conn: Conn,
        source: ObjectSource,
        *,
        now: datetime | None = None,
        triggered_by: str | None = None,
        app_version: str = "waystone3",
    ) -> None:
        self.conn = conn
        self.source = source
        self.now = (now or datetime.now(UTC)).astimezone(NY)
        self.triggered_by = triggered_by
        self.app_version = app_version
        self.ref = RefData()

    # ------------------------------------------------------------------ run
    def run(self, job: str, codes: Iterable[str] | None = None) -> RunReport:
        if job not in JOBS:
            raise ValueError(f"job must be one of {JOBS}")
        self.ref = load_ref(self.conn)
        with self.conn.cursor() as cur:
            row = upsert(
                cur,
                "ops.load_run",
                {"job": job, "triggered_by": self.triggered_by, "app_version": self.app_version},
                returning="run_id",
            )
        assert row is not None
        self.conn.commit()
        report = RunReport(run_id=row["run_id"], job=job)
        wanted = set(codes or [])
        selected = [s for s in self.ref.strategies.values() if not wanted or s.code in wanted]
        touched: set[int] = set()
        try:
            if job in ("paper", "backfill"):
                for strategy in selected:
                    for obj in self.source.list(strategy.paper_prefix):
                        if self._ingest(report, strategy, obj, *classify_paper(obj.name)):
                            touched.add(strategy.strategy_id)
            if job in ("backtest", "backfill"):
                touched |= self._ingest_backtests(report, selected)
            # Backtest day status is time-based (PENDING turns MISSING after the
            # replay deadline), so the backtest job rescores even with no new files.
            if job in ("backtest", "recompute"):
                touched = {s.strategy_id for s in selected}
            for strategy_id in sorted(touched):
                strategy = self.ref.strategies[strategy_id]
                if strategy.asset_class == "future":
                    if job in ("backtest", "backfill", "recompute"):
                        self._reparse_backtests(strategy)
                    recompute(self.conn, self.ref, strategy, self.now)
                    self.conn.commit()
                    report.recomputed.append(strategy.code)
        except Exception as exc:
            self.conn.rollback()
            report.status = "FAILED"
            self._finish(report, f"{type(exc).__name__}: {exc}")
            raise
        if report.failures:
            report.status = "PARTIAL"
        elif report.files_loaded == 0 and job != "recompute":
            report.status = "NOOP"
        else:
            report.status = "OK"
        self._finish(report, "; ".join(report.failures) or None)
        return report

    def _reparse_backtests(self, strategy: Strategy) -> None:
        """Re-parse every current replay file from its stored lines in ``raw.source_line``.

        Replay files are tiny, and the parser improves over time; unchanged files are
        never re-read from the bucket, so this is how a parser fix reaches old days.
        The VM folder is synced recursively, so a session can have more than one replay
        file (a rerun, an archive copy, an aborted stub). Exactly one is used per session:
        the one with the most recognisable content, then the most recently updated.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT file_id, session_date, gcs_uri, gcs_updated_at FROM raw.source_file "
                "WHERE strategy_id = %s AND source_kind = 'backtest_daily' AND is_current "
                "AND session_date IS NOT NULL ORDER BY session_date, file_id",
                (strategy.strategy_id,),
            )
            by_day: dict[date, list[tuple[Row, str]]] = {}
            for f in cur.fetchall():
                cur.execute(
                    "SELECT content FROM raw.source_line WHERE file_id = %s ORDER BY line_no",
                    (f["file_id"],),
                )
                text = "\n".join(r["content"] for r in cur.fetchall())
                by_day.setdefault(f["session_date"], []).append((f, text))
            for day, candidates in by_day.items():
                ranked = sorted(
                    candidates,
                    key=lambda c: (
                        *_replay_quality(parse_backtest_daily(c[1], day)),
                        c[0]["gcs_updated_at"] or datetime.min.replace(tzinfo=UTC),
                        c[0]["file_id"],
                    ),
                    reverse=True,
                )
                (chosen, text), others = ranked[0], ranked[1:]
                status, message = _write_backtest(
                    cur,
                    self.ref,
                    strategy,
                    chosen["file_id"],
                    text,
                    day,
                    reparse=True,
                    replay_file=chosen["gcs_uri"],
                    replay_candidates=len(candidates),
                )
                cur.execute(
                    "UPDATE raw.source_file SET parse_status = %s, parse_message = %s "
                    "WHERE file_id = %s",
                    (status, message, chosen["file_id"]),
                )
                for other, _ in others:
                    cur.execute(
                        "UPDATE raw.source_file SET parse_status = 'SKIPPED', parse_message = %s "
                        "WHERE file_id = %s",
                        (
                            f"another replay is used for this session: {chosen['gcs_uri']}",
                            other["file_id"],
                        ),
                    )

    def _finish(self, report: RunReport, error: str | None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ops.load_run
                SET finished_at = now(), status = %s, files_seen = %s, files_loaded = %s, error = %s
                WHERE run_id = %s
                """,
                (
                    report.status,
                    report.files_seen,
                    report.files_loaded,
                    error[:4000] if error else None,
                    report.run_id,
                ),
            )
        self.conn.commit()

    def _ingest_backtests(self, report: RunReport, selected: list[Strategy]) -> set[int]:
        by_prefix: dict[str, Strategy] = {}
        for s in selected:
            if s.backtest_file_prefix and s.backtest_prefix:
                by_prefix[s.backtest_file_prefix] = s
        # The R2 replay names its files R2_MNQ_<date>: prefix plus instrument root.
        for s in selected:
            if s.backtest_file_prefix and s.backtest_prefix and s.instrument_root:
                by_prefix.setdefault(f"{s.backtest_file_prefix}{s.instrument_root}_", s)
        touched: set[int] = set()
        for prefix in sorted({s.backtest_prefix for s in by_prefix.values() if s.backtest_prefix}):
            for obj in self.source.list(prefix):
                base = obj.name.rsplit("/", 1)[-1]
                match = BACKTEST_NAME.match(base)
                if match and (strategy := by_prefix.get(match["prefix"])):
                    day = date.fromisoformat(match["d"])
                    if self._ingest(report, strategy, obj, "backtest_daily", day):
                        touched.add(strategy.strategy_id)
                elif "comparison" in base.lower():
                    # Legacy VM comparison reports are kept in raw for audit; the loader
                    # computes its own comparison from the paper and replay trades.
                    owner = next((s for p, s in by_prefix.items() if base.startswith(p)), None)
                    found = _ANY_DATE.search(base)
                    day_or_none = date.fromisoformat(found.group(1)) if found else None
                    self._ingest(report, owner, obj, "comparison", day_or_none)
        return touched

    # ------------------------------------------------------------------ raw layer
    def _ingest(
        self,
        report: RunReport,
        strategy: Strategy | None,
        obj: ObjectInfo,
        kind: str,
        session_date: date | None,
    ) -> bool:
        """Returns True when a new paper-log or replay version was parsed into core."""
        report.files_seen += 1
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT file_id, gcs_generation, sha256 FROM raw.source_file "
                "WHERE gcs_uri = %s AND is_current",
                (obj.uri,),
            )
            current = cur.fetchone()
        if current and current["gcs_generation"] == obj.generation:
            return False
        try:
            data = self.source.read(obj)
            sha = hashlib.sha256(data).hexdigest()
            if current and current["sha256"] == sha:
                with self.conn.cursor() as cur:
                    cur.execute(
                        "UPDATE raw.source_file SET gcs_generation = %s, gcs_updated_at = %s "
                        "WHERE file_id = %s",
                        (obj.generation, obj.updated, current["file_id"]),
                    )
                self.conn.commit()
                return False
            text = data.decode("utf-8", errors="replace").replace("\x00", "")
            with self.conn.cursor() as cur:
                file_id = self._register(cur, strategy, kind, obj, sha, len(data), session_date)
                status, message = self._parse(cur, strategy, kind, file_id, obj, text, session_date)
                cur.execute(
                    "UPDATE raw.source_file SET parse_status = %s, parse_message = %s "
                    "WHERE file_id = %s",
                    (status, message, file_id),
                )
                upsert(
                    cur,
                    "ops.load_file",
                    {
                        "run_id": report.run_id,
                        "file_id": file_id,
                        "action": "REPLACED" if current else "INSERTED",
                        "message": message,
                    },
                )
            self.conn.commit()
        except Exception as exc:
            self.conn.rollback()
            message = f"{type(exc).__name__}: {exc}"
            report.failures.append(f"{obj.name}: {message}")
            self._record_failure(report, strategy, kind, obj, session_date, message)
            return False
        report.files_loaded += 1
        return status in ("PARSED", "PARTIAL") and kind in ("paper_log", "backtest_daily")

    def _record_failure(
        self,
        report: RunReport,
        strategy: Strategy | None,
        kind: str,
        obj: ObjectInfo,
        session_date: date | None,
        message: str,
    ) -> None:
        try:
            data = self.source.read(obj)
            sha = hashlib.sha256(data).hexdigest()
            with self.conn.cursor() as cur:
                file_id = self._register(cur, strategy, kind, obj, sha, len(data), session_date)
                cur.execute(
                    "UPDATE raw.source_file SET parse_status = 'FAILED', parse_message = %s "
                    "WHERE file_id = %s",
                    (message[:2000], file_id),
                )
                upsert(
                    cur,
                    "ops.load_file",
                    {
                        "run_id": report.run_id,
                        "file_id": file_id,
                        "action": "FAILED",
                        "message": message[:2000],
                    },
                    ("run_id", "file_id"),
                    update=False,
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()

    def _register(
        self,
        cur: Cursor,
        strategy: Strategy | None,
        kind: str,
        obj: ObjectInfo,
        sha: str,
        size: int,
        session_date: date | None,
    ) -> int:
        cur.execute(
            "UPDATE raw.source_file SET is_current = false "
            "WHERE gcs_uri = %s AND is_current AND sha256 <> %s",
            (obj.uri, sha),
        )
        cur.execute(
            """
            INSERT INTO raw.source_file (strategy_id, source_kind, gcs_uri, gcs_generation, sha256,
                                         size_bytes, gcs_updated_at, session_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (gcs_uri, sha256) DO UPDATE SET
                is_current = true, gcs_generation = EXCLUDED.gcs_generation,
                gcs_updated_at = EXCLUDED.gcs_updated_at, parse_status = 'PENDING',
                parse_message = NULL, loaded_at = now()
            RETURNING file_id
            """,
            (
                strategy.strategy_id if strategy else None,
                kind,
                obj.uri,
                obj.generation,
                sha,
                size,
                obj.updated,
                session_date,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        file_id: int = row["file_id"]
        # Only the current version keeps its text; older versions keep their metadata.
        cur.execute(
            "DELETE FROM raw.source_line WHERE file_id IN "
            "(SELECT file_id FROM raw.source_file WHERE gcs_uri = %s AND NOT is_current)",
            (obj.uri,),
        )
        cur.execute("DELETE FROM raw.source_line WHERE file_id = %s", (file_id,))
        cur.execute("DELETE FROM raw.source_event WHERE file_id = %s", (file_id,))
        return file_id

    def _parse(
        self,
        cur: Cursor,
        strategy: Strategy | None,
        kind: str,
        file_id: int,
        obj: ObjectInfo,
        text: str,
        session_date: date | None,
    ) -> tuple[str, str | None]:
        if kind == "paper_events":
            return _store_events(cur, file_id, text)
        if kind == "paper_log" and strategy and strategy.asset_class == "future" and session_date:
            day = parse_paper_log(text, session_date)
            copy_rows(
                cur,
                "raw.source_line",
                ("file_id", "line_no", "line_ts", "content"),
                ((file_id, ln.line_no, ln.ts, ln.raw) for ln in day.lines),
            )
            return PaperWriter(cur, self.ref, strategy, file_id, self.now).write(obj.uri, day)
        copy_rows(
            cur,
            "raw.source_line",
            ("file_id", "line_no", "line_ts", "content"),
            ((file_id, i, None, line) for i, line in enumerate(text.splitlines(), start=1)),
        )
        if kind == "backtest_daily" and strategy and session_date:
            return _write_backtest(cur, self.ref, strategy, file_id, text, session_date)
        return "SKIPPED", None


def _store_events(cur: Cursor, file_id: int, text: str) -> tuple[str, str | None]:
    bad = 0
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        event_ts: datetime | None = None
        event_type: str | None = None
        if isinstance(payload, dict):
            raw_ts = payload.get("ts") or payload.get("time") or payload.get("timestamp")
            if isinstance(raw_ts, str):
                try:
                    event_ts = datetime.fromisoformat(raw_ts)
                    event_ts = event_ts if event_ts.tzinfo else event_ts.replace(tzinfo=NY)
                except ValueError:
                    event_ts = None
            kind = payload.get("type") or payload.get("event")
            event_type = None if kind is None else str(kind)
        upsert(
            cur,
            "raw.source_event",
            {
                "file_id": file_id,
                "line_no": line_no,
                "event_ts": event_ts,
                "event_type": event_type,
                "payload": Jsonb(payload),
            },
        )
    return ("PARTIAL", f"{bad} non-JSON lines") if bad else ("PARSED", None)


def instrument_id(
    cur: Cursor, ref: RefData, symbol: str | None, strategy: Strategy, point_value: Decimal
) -> int | None:
    if not symbol:
        return None
    if symbol not in ref.instruments:
        row = upsert(
            cur,
            "ref.instrument",
            {
                "symbol": symbol,
                "root": strategy.instrument_root,
                "asset_class": strategy.asset_class,
                "multiplier": point_value,
            },
            ("symbol",),
            returning="instrument_id",
        )
        assert row is not None
        ref.instruments[symbol] = row["instrument_id"]
    return ref.instruments[symbol]


class PaperWriter:
    """Writes one parsed paper log into core (replacing rows from earlier versions)."""

    def __init__(
        self, cur: Cursor, ref: RefData, strategy: Strategy, file_id: int, now: datetime
    ) -> None:
        self.cur = cur
        self.ref = ref
        self.strategy = strategy
        self.sid = strategy.strategy_id
        self.file_id = file_id
        self.now = now

    def write(self, uri: str, day: PaperDay) -> tuple[str, str | None]:
        cur, sid = self.cur, self.sid
        settings = self.ref.settings_for(sid, day.file_date)
        pv = dec(settings["point_value"]) or Decimal(1)
        roll = int(settings["session_roll_hour"])
        cur.execute("SELECT file_id FROM raw.source_file WHERE gcs_uri = %s", (uri,))
        versions = [r["file_id"] for r in cur.fetchall()]
        for table in _PAPER_TABLES:
            cur.execute(
                f"DELETE FROM core.{table} WHERE strategy_id = %s AND file_id = ANY(%s)",
                (sid, versions),
            )
        inst = instrument_id(cur, self.ref, day.symbol, self.strategy, pv)
        self._session(day, inst)
        base = {"strategy_id": sid, "session_date": day.file_date, "file_id": self.file_id}
        for snap in day.snapshots:
            snapshot = {
                **base,
                "snapshot_ts": snap.ts,
                "broker_account": snap.account,
                "nlv": snap.nlv,
                "cash": snap.cash,
                "excess_liquidity": snap.excess_liquidity,
                "init_margin": snap.init_margin,
                "maint_margin": snap.maint_margin,
                "line_no": snap.line_no,
            }
            snap_key = ("strategy_id", "snapshot_ts", "broker_account")
            upsert(cur, "core.account_snapshot", snapshot, snap_key, update=False)
        for sig in day.signals:
            signal = {
                **base,
                "signal_bar_ts": sig.bar_ts,
                "side": sig.side,
                "signal_px": sig.px,
                "outcome": sig.outcome,
                "block_reason": sig.reason,
                "line_no": sig.line_no,
            }
            upsert(cur, "core.signal_event", signal, ("strategy_id", "signal_bar_ts", "side"))
        for fill in day.fills:
            row = {
                **base,
                "session_date": session_date_for(fill.ts, roll, self.ref.holidays),
                "fill_ts": fill.ts,
                "instrument_id": inst,
                "order_ref": fill.order_ref,
                "action": fill.action,
                "quantity": fill.quantity,
                "price": fill.price,
                "commission": fill.commission,
                "leg_role": fill.role,
                "line_no": fill.line_no,
            }
            fill_key = ("strategy_id", "fill_ts", "action", "price", "quantity")
            upsert(cur, "core.paper_fill", row, fill_key, update=False)
        for event in day.events:
            row = {
                **base,
                "event_ts": event.ts,
                "category": event.category,
                "code": event.code,
                "severity": event.severity,
                "message": event.message,
                "line_no": event.line_no,
            }
            upsert(cur, "core.ops_event", row, ("file_id", "line_no"), update=False)
        skipped = 0
        for trade in day.trades:
            if trade.entry_ts is None or trade.entry_px is None:
                skipped += 1
                continue
            row = self._trade_row(day, trade, settings, inst)
            upsert(cur, "core.paper_trade", row, ("strategy_id", "entry_ts", "direction"))

        checks: dict[str, object] = {**day.checks(), "trades_skipped": skipped}
        cur.execute(
            """
            INSERT INTO ops.day_status (strategy_id, session_date, paper_status, paper_loaded_at,
                                        checks)
            VALUES (%s, %s, %s, now(), jsonb_build_object('paper', %s::jsonb))
            ON CONFLICT (strategy_id, session_date) DO UPDATE SET
                paper_status = EXCLUDED.paper_status, paper_loaded_at = now(),
                checks = COALESCE(ops.day_status.checks, '{}'::jsonb) || EXCLUDED.checks
            """,
            (sid, day.file_date, self._paper_status(day), Jsonb(checks)),
        )
        partial = not day.consistent or skipped > 0
        return ("PARTIAL" if partial else "PARSED"), json.dumps(checks)

    def _session(self, day: PaperDay, inst: int | None) -> None:
        summary = day.summary
        wins = sum(1 for t in day.trades if t.closed and (t.net_pnl or 0) > 0)
        row = {
            "strategy_id": self.sid,
            "session_date": day.file_date,
            "file_id": self.file_id,
            "params_fp": day.params_fp,
            "broker_account": day.broker_account,
            "broker_client_id": day.broker_client_id,
            "instrument_id": inst,
            "first_line_at": day.first_ts,
            "last_line_at": day.last_ts,
            "summary_present": summary is not None,
            "triggers_reported": summary.triggers if summary else None,
            "entries_reported": summary.entries if summary else None,
            "wins_reported": summary.wins if summary and summary.wins is not None else wins,
            "win_rate_reported": summary.win_rate if summary else None,
            "net_pnl_reported": summary.net_pnl if summary else None,
            "nlv_start": day.nlv_start,
            "nlv_end": day.nlv_end,
            "gate_blocks": day.gate_blocks,
            "loss_cap_blocks": day.loss_cap_blocks,
            "loss_cap_hit": day.loss_cap_hit,
        }
        upsert(self.cur, "core.paper_session", row, ("strategy_id", "session_date"))

    def _paper_status(self, day: PaperDay) -> str:
        if not day.consistent:
            return "PARTIAL"
        if day.summary is not None:
            return "FINAL"
        if day.file_date < self.now.date():
            return "PRELIMINARY"
        return "INTRADAY"

    def _trade_row(
        self, day: PaperDay, trade: Trade, settings: Row, inst: int | None
    ) -> dict[str, Any]:
        assert trade.entry_ts is not None and trade.entry_px is not None
        pv = dec(settings["point_value"]) or Decimal(1)
        sign = Decimal(1) if trade.direction == "LONG" else Decimal(-1)
        contracts = trade.contracts or Decimal(settings["default_contracts"])
        entry_slip = trade.entry_slip_pts
        if entry_slip is None and trade.entry_signal_px is not None:
            entry_slip = (trade.entry_px - trade.entry_signal_px) * sign
        exit_slip = trade.exit_slip_pts
        if exit_slip is None and trade.exit_signal_px is not None and trade.exit_px is not None:
            exit_slip = (trade.exit_signal_px - trade.exit_px) * sign
        row: dict[str, Any] = {
            "strategy_id": self.sid,
            "session_date": day.file_date,
            "trade_no": trade.trade_no,
            "instrument_id": instrument_id(self.cur, self.ref, trade.symbol, self.strategy, pv)
            or inst,
            "direction": trade.direction,
            "contracts": contracts,
            "signal_bar_ts": trade.signal_bar_ts,
            "entry_ts": trade.entry_ts,
            "exit_ts": None,
            "entry_px": trade.entry_px,
            "exit_px": None,
            "entry_signal_px": trade.entry_signal_px,
            "exit_signal_px": trade.exit_signal_px,
            "entry_slip_pts": entry_slip,
            "exit_slip_pts": exit_slip,
            "fill_latency_s": (
                Decimal(str((trade.entry_ts - trade.signal_ts).total_seconds()))
                if trade.signal_ts
                else None
            ),
            "exit_reason": trade.exit_reason,
            "points": None,
            "gross_pnl": None,
            "commission": trade.commission,
            "slippage_cost": None,
            "pnl_at_signal": None,
            "net_pnl": None,
            "hold_min": None,
            "mae_pts": trade.mae_pts,
            "mfe_pts": trade.mfe_pts,
            "params_fp": day.params_fp,
            "file_id": self.file_id,
            "line_no": trade.line_no,
        }
        if not trade.closed or trade.exit_ts is None:
            return row
        points = trade.points
        if points is None and trade.exit_px is not None:
            points = (trade.exit_px - trade.entry_px) * sign
        gross = trade.gross_pnl
        if gross is None and points is not None:
            gross = points * pv * contracts
        commission = trade.commission
        if commission is None:
            commission = (dec(settings["commission_rt_per_contract"]) or Decimal(0)) * contracts
        net = trade.net_pnl
        if net is None and gross is not None:
            net = gross - commission
        slippage = ((entry_slip or Decimal(0)) + (exit_slip or Decimal(0))) * pv * contracts
        hold = trade.hold_min
        if hold is None:
            minutes = (trade.exit_ts - trade.entry_ts).total_seconds() / 60
            hold = Decimal(str(minutes)).quantize(Decimal("0.01"))
        roll = int(settings["session_roll_hour"])
        row.update(
            session_date=session_date_for(trade.exit_ts, roll, self.ref.holidays),
            exit_ts=trade.exit_ts,
            exit_px=trade.exit_px,
            points=points,
            gross_pnl=gross,
            commission=commission,
            slippage_cost=slippage,
            pnl_at_signal=None if gross is None else gross + slippage,
            net_pnl=net,
            hold_min=hold,
        )
        return row


def _backtest_rows(parsed: BacktestDay, pv: Decimal, settings: Row) -> list[dict[str, Any]]:
    comm_rt = dec(settings["commission_rt_per_contract"]) or Decimal(0)
    rows: list[dict[str, Any]] = []
    for t in parsed.trades:
        inferred = t.contracts is None
        contracts = (
            t.contracts if t.contracts is not None else Decimal(settings["default_contracts"])
        )
        sign = Decimal(1) if t.direction == "LONG" else Decimal(-1)
        points = t.points
        if points is None and t.entry_px is not None and t.exit_px is not None:
            points = (t.exit_px - t.entry_px) * sign
        gross = t.gross_pnl
        if gross is None and points is not None:
            gross = points * pv * contracts
        commission = t.commission
        net, derived = t.net_pnl, False
        if net is None and gross is not None:
            commission = commission if commission is not None else comm_rt * contracts
            net, derived = gross - commission, True
        hold = t.hold_min
        if hold is None and t.exit_ts is not None:
            hold = Decimal(str((t.exit_ts - t.entry_ts).total_seconds() / 60))
        rows.append(
            {
                "trade_seq": t.seq,
                "direction": t.direction,
                "entry_ts": t.entry_ts,
                "exit_ts": t.exit_ts,
                "entry_px": t.entry_px,
                "exit_px": t.exit_px,
                "points": points,
                "contracts": contracts,
                "contracts_inferred": inferred,
                "gross_pnl": gross,
                "commission": commission,
                "net_pnl": net,
                "net_pnl_derived": derived,
                "exit_reason": t.exit_reason,
                "hold_min": hold,
            }
        )
    return rows


def _replay_quality(parsed: BacktestDay) -> tuple[bool, bool, int]:
    has_totals = parsed.trades_reported is not None or parsed.total_net_reported is not None
    return has_totals, bool(parsed.trades), -len(parsed.unparsed)


def _write_backtest(
    cur: Cursor,
    ref: RefData,
    strategy: Strategy,
    file_id: int,
    text: str,
    session_date: date,
    *,
    reparse: bool = False,
    replay_file: str | None = None,
    replay_candidates: int | None = None,
) -> tuple[str, str | None]:
    sid = strategy.strategy_id
    settings = ref.settings_for(sid, session_date)
    parsed = parse_backtest_daily(text, session_date)
    pv = parsed.point_value or dec(settings["point_value"]) or Decimal(1)
    rows = _backtest_rows(parsed, pv, settings)
    net_parsed = sum((r["net_pnl"] or Decimal(0) for r in rows), Decimal(0))
    reported = parsed.total_net_reported
    net_match = None if reported is None or not rows else abs(net_parsed - reported) <= 1
    expected = settings["expected_bar_count"]
    if expected and parsed.bar_count is not None and parsed.bar_count < expected:
        status = "DATA_INCOMPLETE"
    elif (
        (parsed.trades_reported not in (None, len(parsed.trades)))
        or (not parsed.trades and parsed.unparsed)
        or (not parsed.trades and parsed.trades_reported is None)
        or net_match is False
    ):
        status = "INCOMPLETE"
    else:
        status = "COMPLETE"
    run_values: dict[str, Any] = {
        "strategy_id": sid,
        "session_date": session_date,
        "file_id": file_id,
        "config_label": parsed.config_label,
        "params_fp": parsed.params_fp,
        "bar_file": parsed.bar_file,
        "bar_count": parsed.bar_count,
        "vol_index": parsed.vol_index,
        "sentiment_source": parsed.sentiment_source,
        "point_value": pv,
        "flatten_time": parsed.flatten_time,
        "daily_loss_cap": parsed.daily_loss_cap,
        "trades_reported": parsed.trades_reported,
        "total_net_reported": parsed.total_net_reported,
        "maxdd_reported": parsed.maxdd_reported,
        "status": status,
    }
    if not reparse:
        run_values["loaded_at"] = datetime.now(UTC)
    run = upsert(
        cur, "core.backtest_run", run_values, ("strategy_id", "session_date"), returning="run_id"
    )
    assert run is not None
    run_id = run["run_id"]
    cur.execute("DELETE FROM core.backtest_trade WHERE run_id = %s", (run_id,))
    for row in rows:
        upsert(
            cur,
            "core.backtest_trade",
            {"run_id": run_id, "strategy_id": sid, "session_date": session_date, **row},
        )
    checks = {
        "trades_parsed": len(parsed.trades),
        "trades_reported": parsed.trades_reported,
        "net_parsed": float(net_parsed) if rows else None,
        "net_reported": None if reported is None else float(reported),
        "net_match": net_match,
        "bar_count": parsed.bar_count,
        "expected_bar_count": expected,
        "unparsed_lines": len(parsed.unparsed),
        "run_status": status,
    }
    if replay_file is not None:
        checks["replay_file"] = replay_file
        checks["replay_candidates"] = replay_candidates
    cur.execute(
        """
        INSERT INTO ops.day_status (strategy_id, session_date, backtest_loaded_at, checks)
        VALUES (%s, %s, now(), jsonb_build_object('backtest', %s::jsonb))
        ON CONFLICT (strategy_id, session_date) DO UPDATE SET
            backtest_loaded_at = CASE WHEN %s THEN ops.day_status.backtest_loaded_at
                                      ELSE now() END,
            checks = COALESCE(ops.day_status.checks, '{}'::jsonb) || EXCLUDED.checks
        """,
        (sid, session_date, Jsonb(checks), reparse),
    )
    parse_status = "PARSED" if status == "COMPLETE" and not parsed.unparsed else "PARTIAL"
    return parse_status, json.dumps(checks)
