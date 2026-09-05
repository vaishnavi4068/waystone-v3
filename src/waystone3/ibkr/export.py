"""Assemble a daily dump and publish it (GCS, or a local tree for --dry-run)."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from waystone3.ibkr.collect import collect
from waystone3.ibkr.ledger import ExecutionLedger
from waystone3.ibkr.models import (
    AccountSnapshot,
    DailyReport,
    Execution,
    Manifest,
    PositionSnapshot,
)
from waystone3.ibkr.paths import (
    account_key,
    executions_key,
    manifest_key,
    positions_key,
    success_key,
    summary_key,
)
from waystone3.ibkr.settings import IbkrSettings
from waystone3.ibkr.store import GcsStore, LocalFsStore, ReportStore
from waystone3.ibkr.summary import summarize
from waystone3.ibkr.timeutil import NY, today_ny


def assemble_report(
    day: date,
    executions: list[Execution],
    positions: list[PositionSnapshot],
    account: AccountSnapshot,
    settings: IbkrSettings,
    *,
    tws_connected: bool,
    generated_at: datetime | None = None,
) -> DailyReport:
    stamp = generated_at or datetime.now(NY)
    iso = day.isoformat()
    summary = summarize(iso, executions)
    manifest = Manifest(
        generated_at=stamp,
        date=iso,
        ib_host=settings.ib_host,
        ib_port=settings.ib_port,
        ib_client_id=settings.ib_client_id,
        fill_count=len(executions),
        tws_connected=tws_connected,
    )
    return DailyReport(
        date=iso,
        generated_at=stamp,
        published=False,
        executions=executions,
        positions=positions,
        account=account,
        summary=summary,
        manifest=manifest,
    )


def publish_report(store: ReportStore, report: DailyReport) -> str:
    """Write dump objects; ``_SUCCESS`` is last so readers treat the day as complete."""
    day = report.date
    exec_body = "".join(row.model_dump_json() + "\n" for row in report.executions)
    store.put(executions_key(day), exec_body.encode(), "application/x-ndjson")
    pos_json = json.dumps([p.model_dump(mode="json") for p in report.positions], indent=2)
    store.put(positions_key(day), (pos_json + "\n").encode(), "application/json")
    store.put(
        account_key(day),
        (json.dumps(report.account.model_dump(mode="json"), indent=2) + "\n").encode(),
        "application/json",
    )
    store.put(
        summary_key(day),
        (json.dumps(report.summary.model_dump(mode="json"), indent=2) + "\n").encode(),
        "application/json",
    )
    store.put(
        manifest_key(day),
        (json.dumps(report.manifest.model_dump(mode="json"), indent=2) + "\n").encode(),
        "application/json",
    )
    store.put(success_key(day), b"ok\n", "text/plain")
    report.published = True
    return success_key(day)


def export_day(
    settings: IbkrSettings | None = None,
    *,
    day: date | None = None,
    dry_run: bool = False,
    skip_collect: bool = False,
) -> DailyReport:
    cfg = settings or IbkrSettings()
    target = day or today_ny()
    ledger = ExecutionLedger(Path(cfg.ibkr_ledger_dir))
    tws_connected = False
    positions: list[PositionSnapshot] = ledger.load_positions(target)
    account = ledger.load_account(target) or AccountSnapshot()
    if not skip_collect:
        result = collect(cfg, day=target)
        tws_connected = result.tws_connected
        if target == today_ny():
            positions = result.positions
            account = result.account
    executions = ledger.load(target)
    report = assemble_report(
        target,
        executions,
        positions,
        account,
        cfg,
        tws_connected=tws_connected,
    )
    store = _target_store(cfg, dry_run=dry_run)
    publish_report(store, report)
    return report


def _target_store(settings: IbkrSettings, *, dry_run: bool) -> ReportStore:
    if dry_run:
        return LocalFsStore(Path(settings.ibkr_ledger_dir).resolve().parent / "dry-run")
    if settings.ibkr_reports_local_dir.strip():
        return LocalFsStore(Path(settings.ibkr_reports_local_dir))
    if not settings.ibkr_reports_bucket.strip():
        raise ValueError("set IBKR_REPORTS_BUCKET (or pass --dry-run / IBKR_REPORTS_LOCAL_DIR)")
    return GcsStore(settings.ibkr_reports_bucket)
