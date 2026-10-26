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
    params: dict[str, Any] | None = None,
    store: ReportStore | None = None,
) -> dict[str, Any]:
    reports = store if store is not None else _store()
    if reports is None:
        raise ValueError("set IBKR_REPORTS_BUCKET or IBKR_REPORTS_LOCAL_DIR")
    if params:
        from waystone3.hq.strategy_runner import assert_no_threshold_changes

        assert_no_threshold_changes(params)
    row: dict[str, Any] = {
        "id": uuid.uuid4().hex[:12],
        "at": _now(),
        "text": text.strip(),
        "action": action.strip(),
        "acked": False,
        "source": source.strip() or "grok_bot",
    }
    if params:
        row["params"] = params
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


STRATEGY_INBOX_ACTIONS: frozenset[str] = frozenset(
    {
        "strategy-start",
        "strategy-halt",
        "strategy-resume",
        "strategy-configure",
        "strategy-execute",
        "strategy-context-brief",
        "strategy-context-apply",
        "sandbox-backtest-gcs",
        "sandbox-backtest-ibkr",
        "sandbox-diagnose-discrepancy",
        "sandbox-datasets",
        "sandbox-runs",
    }
)


def execute_strategy_instruction(
    item: dict[str, Any],
    *,
    runner: Any | None = None,
    store: ReportStore | None = None,
    spawn: bool = False,
) -> dict[str, Any]:
    """Execute a GrokBot inbox instruction targeting an HQ futures strategy or sandbox backtest.

    Thresholds are strictly immutable; any attempt to pass threshold keys in
    ``params`` raises ``ValueError``.
    """
    from waystone3.hq.backtest_sandbox import HqBacktestSandbox
    from waystone3.hq.context_lm import HqContextLanguageModel
    from waystone3.hq.strategy_runner import HqStrategyRunner, assert_no_threshold_changes

    rt = runner if runner is not None else HqStrategyRunner()
    action = str(item.get("action") or "").strip().lower()
    params = dict(item.get("params") or {})
    assert_no_threshold_changes(params)

    strategy = str(params.get("strategy") or item.get("strategy") or "").strip() or None
    if action == "sandbox-datasets":
        sbx = HqBacktestSandbox(runner=rt, store=store)
        return sbx.list_datasets()
    if action == "sandbox-runs":
        sbx = HqBacktestSandbox(runner=rt, store=store)
        run_id = params.get("run_id")
        if run_id:
            found = sbx.get_run(str(run_id))
            if found is None:
                raise ValueError(f"sandbox run {run_id!r} not found")
            return found
        return {"runs": sbx.list_runs(int(params.get("limit", 20)))}
    if action == "sandbox-backtest-gcs":
        if not strategy:
            raise ValueError("strategy parameter is required for sandbox-backtest-gcs")
        sbx = HqBacktestSandbox(runner=rt, store=store)
        return sbx.run_gcs_backtest(
            strategy,
            start=params.get("start"),
            end=params.get("end"),
            gcs_key=params.get("gcs_key"),
            contracts=params.get("contracts"),
            fng_prior_day=params.get("fng_prior_day"),
            live_parity=bool(params.get("live_parity", False)),
        )
    if action == "sandbox-backtest-ibkr":
        if not strategy:
            raise ValueError("strategy parameter is required for sandbox-backtest-ibkr")
        sbx = HqBacktestSandbox(runner=rt, store=store)
        return sbx.run_ibkr_backtest(
            strategy,
            days=int(params.get("days", 30)),
            bar_size=str(params.get("bar_size", "1 min")),
            use_rth=bool(params.get("use_rth", False)),
            account_id=params.get("account_id"),
            contracts=params.get("contracts"),
            fng_prior_day=params.get("fng_prior_day"),
            live_parity=bool(params.get("live_parity", False)),
        )
    if action == "sandbox-diagnose-discrepancy":
        if not strategy:
            raise ValueError("strategy parameter is required for sandbox-diagnose-discrepancy")
        sbx = HqBacktestSandbox(runner=rt, store=store)
        return sbx.diagnose_replay_discrepancy(
            strategy,
            start=params.get("start"),
            end=params.get("end"),
            gcs_key=params.get("gcs_key"),
        )
    if action == "strategy-context-brief":
        clm = HqContextLanguageModel(runner=rt)
        return clm.generate_brief(
            strategy,
            question=str(params.get("question") or item.get("text") or ""),
            use_llm=bool(params.get("use_llm", True)),
        )
    if action == "strategy-context-apply":
        clm = HqContextLanguageModel(runner=rt)
        return clm.apply_recommendation(
            strategy,
            actor=str(item.get("source") or "grok_bot"),
        )
    if not strategy:
        raise ValueError("strategy parameter is required for strategy-* inbox actions")

    if action == "strategy-configure":
        return rt.configure_account(
            strategy,
            account_id=params.get("account_id"),
            client_id=params.get("client_id"),
            ibkr_host=params.get("ibkr_host"),
            ibkr_port=params.get("ibkr_port"),
            contracts=params.get("contracts"),
            dry_run=params.get("dry_run"),
            hold_overnight=params.get("hold_overnight"),
            actor=str(item.get("source") or "grok_bot"),
        )
    if action == "strategy-start":
        return rt.start_strategy(
            strategy,
            account_id=params.get("account_id"),
            contracts=params.get("contracts"),
            dry_run=params.get("dry_run"),
            hold_overnight=params.get("hold_overnight"),
            spawn=bool(params.get("spawn", spawn)),
            actor=str(item.get("source") or "grok_bot"),
        )
    if action == "strategy-halt":
        return rt.halt_strategy(
            strategy,
            reason=str(params.get("reason") or item.get("text") or "halted by GrokBot"),
            stop_process=bool(params.get("stop_process", False)),
        )
    if action == "strategy-resume":
        return rt.resume_strategy(strategy)
    if action == "strategy-execute":
        trade_action = str(params.get("trade_action") or params.get("side") or "").strip()
        return rt.ibkr_execute_signal(
            strategy,
            trade_action,
            qty=params.get("qty") or params.get("contracts"),
            dry_run=params.get("dry_run"),
            reason=str(item.get("text") or "grok_bot_inbox"),
        )
    raise ValueError(f"unsupported strategy inbox action {action!r}")


def process_strategy_inbox(
    *,
    store: ReportStore | None = None,
    runner: Any | None = None,
    spawn: bool = False,
) -> list[dict[str, Any]]:
    """Process all pending ``strategy-*`` instructions in the GCS/local inbox,
    ack each handled item, and post status updates back to GrokBot."""
    reports = store if store is not None else _store()
    if reports is None:
        return []
    pending = list_inbox(pending_only=True, store=reports)
    results: list[dict[str, Any]] = []
    for row in pending:
        action = str(row.get("action") or "").strip().lower()
        if action not in STRATEGY_INBOX_ACTIONS:
            continue
        item_id = str(row.get("id") or "")
        try:
            out = execute_strategy_instruction(row, runner=runner, store=reports, spawn=spawn)
            ack_instruction(item_id, store=reports)
            post_status(
                "strategy_op",
                f"Executed {action} on {out.get('strategy_code', '')}",
                json.dumps(out, default=str)[:400],
                store=reports,
            )
            results.append({"id": item_id, "action": action, "ok": True, "result": out})
        except Exception as exc:
            results.append({"id": item_id, "action": action, "ok": False, "error": str(exc)})
    return results

