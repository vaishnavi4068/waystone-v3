"""Read published daily dumps from a ReportStore."""

from __future__ import annotations

import json
from datetime import date, datetime

from waystone3.ibkr.models import (
    AccountSnapshot,
    DailyReport,
    DailySummary,
    Execution,
    Manifest,
    PositionSnapshot,
)
from waystone3.ibkr.paths import (
    PREFIX,
    account_key,
    day_from_success_key,
    executions_key,
    manifest_key,
    positions_key,
    success_key,
    summary_key,
)
from waystone3.ibkr.store import ReportStore
from waystone3.ibkr.timeutil import NY, today_ny


def is_published(store: ReportStore, day: date | str) -> bool:
    return store.exists(success_key(day))


def list_published_days(store: ReportStore) -> list[date]:
    days: list[date] = []
    for key in store.list_keys(f"{PREFIX}/"):
        parsed = day_from_success_key(key)
        if parsed is not None:
            days.append(parsed)
    return sorted(set(days))


def latest_published_day(store: ReportStore) -> date | None:
    days = list_published_days(store)
    return days[-1] if days else None


def load_report(store: ReportStore, day: date) -> DailyReport | None:
    if not is_published(store, day):
        return None
    exec_raw = store.get(executions_key(day)) or b""
    executions = [
        Execution.model_validate_json(line) for line in exec_raw.splitlines() if line.strip()
    ]
    pos_raw = store.get(positions_key(day)) or b"[]"
    positions = [PositionSnapshot.model_validate(row) for row in json.loads(pos_raw)]
    acct_raw = store.get(account_key(day)) or b"{}"
    account = AccountSnapshot.model_validate_json(acct_raw)
    summary_raw = store.get(summary_key(day))
    summary = (
        DailySummary.model_validate_json(summary_raw)
        if summary_raw
        else DailySummary(date=day.isoformat())
    )
    man_raw = store.get(manifest_key(day))
    if man_raw:
        manifest = Manifest.model_validate_json(man_raw)
        generated: datetime = manifest.generated_at
    else:
        generated = executions[0].time if executions else datetime.now(NY)
        manifest = Manifest(
            generated_at=generated,
            date=day.isoformat(),
            ib_host="",
            ib_port=0,
            ib_client_id=0,
            fill_count=len(executions),
            tws_connected=True,
        )
    return DailyReport(
        date=day.isoformat(),
        generated_at=generated,
        published=True,
        executions=executions,
        positions=positions,
        account=account,
        summary=summary,
        manifest=manifest,
    )


def load_latest(store: ReportStore) -> DailyReport | None:
    day = latest_published_day(store)
    if day is None:
        return None
    return load_report(store, day)


def today_is_published(store: ReportStore) -> bool:
    return is_published(store, today_ny())
