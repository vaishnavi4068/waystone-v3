"""Shared research ops log: GCS status/inbox + optional Grok Bot webhook."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Any

from waystone3.alerts.channels import GrokBotChannel
from waystone3.ibkr.store import ReportStore, build_report_store_from_env
from waystone3.ibkr.timeutil import NY
from waystone3.research.paths import OPS_INBOX_KEY, OPS_OUTBOX_KEY, OPS_STATUS_KEY


def _store() -> ReportStore | None:
    return build_report_store_from_env()


def _now() -> str:
    return datetime.now(NY).isoformat()


def _append_jsonl(store: ReportStore, key: str, row: dict[str, Any]) -> None:
    raw = store.get(key) or b""
    line = json.dumps(row, default=str) + "\n"
    store.put(key, raw + line.encode(), "application/x-ndjson")


def post_status(
    phase: str,
    title: str,
    body: str = "",
    *,
    approval: str | None = None,
    extra: dict[str, Any] | None = None,
    store: ReportStore | None = None,
) -> dict[str, Any]:
    """Write status to GCS and wake Grok Bot if the webhook env is set."""
    payload: dict[str, Any] = {
        "event": "waystone.research.status",
        "phase": phase,
        "title": title,
        "body": body,
        "approval": approval,
        "at": _now(),
        "source": "waystone3",
        **(extra or {}),
    }
    reports = store if store is not None else _store()
    if reports is not None:
        reports.put(OPS_STATUS_KEY, json.dumps(payload, indent=2).encode(), "application/json")
        _append_jsonl(reports, OPS_OUTBOX_KEY, payload)
    GrokBotChannel().post_sync(payload)
    return payload


def read_status(store: ReportStore | None = None) -> dict[str, Any] | None:
    reports = store if store is not None else _store()
    if reports is None:
        return None
    raw = reports.get(OPS_STATUS_KEY)
    if raw is None:
        return None
    data = json.loads(raw.decode())
    return data if isinstance(data, dict) else None


def add_instruction(
    text: str,
    *,
    action: str = "",
    source: str = "grok_bot",
    store: ReportStore | None = None,
) -> dict[str, Any]:
    reports = store if store is not None else _store()
    if reports is None:
        raise ValueError("set IBKR_REPORTS_BUCKET or IBKR_REPORTS_LOCAL_DIR")
    row = {
        "id": uuid.uuid4().hex[:12],
        "at": _now(),
        "text": text.strip(),
        "action": action.strip(),
        "acked": False,
        "source": source.strip() or "grok_bot",
    }
    _append_jsonl(reports, OPS_INBOX_KEY, row)
    return row


def _inbox_rows(store: ReportStore) -> list[dict[str, Any]]:
    raw = store.get(OPS_INBOX_KEY)
    if not raw:
        return []
    out: list[dict[str, Any]] = []
    for line in raw.decode().splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def list_inbox(
    *, pending_only: bool = True, store: ReportStore | None = None
) -> list[dict[str, Any]]:
    reports = store if store is not None else _store()
    if reports is None:
        return []
    rows = _inbox_rows(reports)
    if pending_only:
        return [row for row in rows if not row.get("acked")]
    return rows


def ack_instruction(item_id: str, store: ReportStore | None = None) -> bool:
    reports = store if store is not None else _store()
    if reports is None:
        return False
    rows = _inbox_rows(reports)
    found = False
    for row in rows:
        if row.get("id") == item_id:
            row["acked"] = True
            row["acked_at"] = _now()
            found = True
    if found:
        blob = "".join(json.dumps(row, default=str) + "\n" for row in rows)
        reports.put(OPS_INBOX_KEY, blob.encode(), "application/x-ndjson")
    return found


def inbox_token() -> str:
    return (
        os.getenv("GROK_BOT_INBOX_TOKEN")
        or os.getenv("GROK_BOT_WEBHOOK_KEY")
        or os.getenv("GROK_BOT_SENDER_KEY")
        or ""
    ).strip()
