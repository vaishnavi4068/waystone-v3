"""Strategy test assembly line.

The line watches continuous IBKR paper trading, compares it with the same-day
backtest, and opens a bug when they disagree. Each bug carries a suggested fix.
A human approves or rejects it. Approval can also run a historical backtest for
that session. Safe fixes update comparison policy inside waystone only. Locked
strategy thresholds are never changed, and the paper line keeps running.
"""

from __future__ import annotations

import copy
import json
import threading
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast

from waystone3.hq.strategy_runner import HqStrategyRunner, assert_no_threshold_changes
from waystone3.ibkr.store import ReportStore, build_report_store_from_env
from waystone3.ibkr.timeutil import NY

FACTORY_STATE_KEY = "factory/v1/line.json"

_FALLBACK: dict[str, Any] = {}
_FALLBACK_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(NY).isoformat()


def _empty_state() -> dict[str, Any]:
    return {"tickets": [], "policies": {}, "cycles": []}


def _jsonable(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _notes_text(row: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("notes", "notes_auto", "notes_manual"):
        value = row.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, list):
            parts.append("; ".join(str(item) for item in value))
        else:
            parts.append(str(value))
    return " | ".join(parts)


def _session_date(row: dict[str, Any]) -> str:
    raw = row.get("session_date")
    if raw is None:
        return ""
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()
    return str(raw)[:10]


def _evidence(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "sync_status",
        "live_trades",
        "bt_trades",
        "live_net_pnl",
        "bt_net_pnl",
        "pnl_delta",
        "pnl_delta_pct",
        "exit_reason_match",
        "loss_cap_hit",
        "paper_status",
        "backtest_status",
    )
    out = {key: _jsonable(row.get(key)) for key in keys if row.get(key) not in (None, "")}
    notes = _notes_text(row)
    if notes:
        out["notes"] = notes
    return out


def classify_sync_row(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn one daily-sync row into zero or more bug drafts. OK rows raise nothing."""
    status = str(row.get("sync_status") or "").strip().upper()
    if status in {"", "OK"}:
        return []
    code = str(row.get("strategy_code") or "").strip()
    session = _session_date(row)
    if not code or not session:
        return []
    notes = _notes_text(row)
    lowered = notes.lower()
    evidence = _evidence(row)

    def draft(
        bucket: str,
        title: str,
        suggested_fix: str,
        *,
        safe_to_apply: bool,
        action: str,
    ) -> dict[str, Any]:
        return {
            "fingerprint": f"{code}|{session}|{bucket}",
            "strategy_code": code,
            "session_date": session,
            "bucket": bucket,
            "title": title,
            "detail": notes or f"Daily sync is {status}.",
            "suggested_fix": suggested_fix,
            "safe_to_apply": safe_to_apply,
            "action": action,
            "sync_status": status,
            "evidence": evidence,
        }

    if status == "N/A" or "no backtest replay" in lowered:
        return [
            draft(
                "missing_replay",
                "Daily backtest missing",
                "Publish the same-day replay, or compare this session in live-parity "
                "mode on the bound paper account. Thresholds stay locked. Approving "
                "records that policy; it does not edit the engine.",
                safe_to_apply=True,
                action="prefer_live_parity",
            )
        ]

    bugs: list[dict[str, Any]] = []
    if "fill price shifted" in lowered or "entry fill price" in lowered:
        bugs.append(
            draft(
                "fill_exit_drift",
                "Fill price shifted the percent exit",
                "Paper and the backtest took the same signal and exit reason. The live "
                "fill moved the percent exit onto another bar. Compare this session with "
                "live-parity replay. Do not change profit-take, stop, or signal thresholds.",
                safe_to_apply=True,
                action="prefer_live_parity",
            )
        )
    if "trade count" in lowered:
        bugs.append(
            draft(
                "trade_count",
                "Trade count does not match",
                "Live blocks new entries after the daily loss cap and can flatten at "
                "15:55 ET. The reference backtest does not. Re-compare in live-parity "
                "mode before treating this as a signal bug. Thresholds stay locked.",
                safe_to_apply=True,
                action="prefer_live_parity",
            )
        )
    if "exit reason differs" in lowered:
        bugs.append(
            draft(
                "exit_reason",
                "Exit reason does not match",
                "A matched trade exited for a different reason. Inspect the broker stop, "
                "the engine hard stop, and profit-take on that bar. Do not retune those "
                "levels. Approving records the proposal and continues paper trading.",
                safe_to_apply=False,
                action="proposal_only",
            )
        )
    if "p&l delta" in lowered and not bugs:
        bugs.append(
            draft(
                "pnl_delta",
                "P&L delta past tolerance",
                "P&L differs and the trade count and exit reason do not explain it. Check "
                "commission (NQ/ES about $4.48 round turn at IBKR versus $4.50 in the "
                "engine; MNQ $1.22) and the MNQ multiplier ($2 per point). Thresholds stay locked.",
                safe_to_apply=True,
                action="note_commission_check",
            )
        )
    if not bugs:
        bugs.append(
            draft(
                "unclassified",
                "Unclassified daily-sync flag",
                "Daily sync is FLAG and the note did not match a known mismatch. Review "
                "the comparison. No threshold change is applied by this line.",
                safe_to_apply=False,
                action="proposal_only",
            )
        )
    return bugs


class HqStrategyFactory:
    """Persists assembly-line tickets and applies human decisions."""

    def __init__(
        self,
        runner: Any | None = None,
        reader: Any | None = None,
        sandbox: Any | None = None,
        store: ReportStore | None = None,
    ) -> None:
        self.runner = runner if runner is not None else HqStrategyRunner()
        self.reader = reader
        self.sandbox = sandbox
        if store is not None:
            self.store: ReportStore | None = store
        elif sandbox is not None and getattr(sandbox, "store", None) is not None:
            self.store = sandbox.store
        else:
            self.store = build_report_store_from_env()
        self._lock = threading.Lock()

    def scan(self, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Read daily sync rows and open a bug for each new mismatch."""
        if rows is None:
            if self.reader is None:
                source = "no_hq_database"
                loaded: list[dict[str, Any]] = []
            else:
                source = "hq_database"
                loaded = list(self.reader.daily_sync())
        else:
            source = "provided_rows"
            loaded = list(rows)

        created: list[str] = []
        seen: list[str] = []
        with self._lock:
            state = self._load()
            by_fp = {ticket["fingerprint"]: ticket for ticket in state["tickets"]}
            for row in loaded:
                code = str(row.get("strategy_code") or "").strip()
                session = _session_date(row)
                if code and session:
                    seen.append(f"{code}|{session}")
                for draft in classify_sync_row(row):
                    current = by_fp.get(draft["fingerprint"])
                    if current is not None:
                        if current["status"] == "pending_approval":
                            current["detail"] = draft["detail"]
                            current["suggested_fix"] = draft["suggested_fix"]
                            current["evidence"] = draft["evidence"]
                            current["updated_at"] = _now()
                        continue
                    ticket = {
                        **draft,
                        "ticket_id": f"fac_{uuid.uuid4().hex[:10]}",
                        "status": "pending_approval",
                        "station": "approval",
                        "created_at": _now(),
                        "updated_at": _now(),
                        "approved_by": None,
                        "approval_note": "",
                        "run_historical_backtest": False,
                        "historical_backtest": None,
                        "implementation": None,
                        "thresholds_locked": True,
                    }
                    state["tickets"].append(ticket)
                    by_fp[ticket["fingerprint"]] = ticket
                    created.append(ticket["ticket_id"])
            state["cycles"].append(
                {
                    "cycle_id": f"cyc_{uuid.uuid4().hex[:8]}",
                    "scanned_at": _now(),
                    "rows_seen": len(loaded),
                    "bugs_raised": len(created),
                    "source": source,
                    "strategy_sessions": sorted(set(seen)),
                }
            )
            state["cycles"] = state["cycles"][-40:]
            self._save(state)
        report = self.pipeline()
        report["raised"] = created
        report["source"] = source
        return report

    def pipeline(self) -> dict[str, Any]:
        """Current station board for the HQ dashboard."""
        with self._lock:
            state = self._load()
        paper = self._paper()
        tickets = sorted(state["tickets"], key=lambda item: item["created_at"], reverse=True)
        pending = [item for item in tickets if item["status"] == "pending_approval"]
        implemented = [item for item in tickets if item["status"] == "implemented"]
        continued = [item for item in tickets if item["status"] == "continued"]
        missing = [item for item in pending if item["bucket"] == "missing_replay"]
        cycles = list(state["cycles"])
        latest = cycles[-1] if cycles else None
        seen = set(latest["strategy_sessions"]) if latest else set()
        checked = bool(
            latest
            and latest.get("source") != "no_hq_database"
            and int(latest.get("rows_seen") or 0) > 0
        )
        strategies = self._strategy_board(paper, tickets, seen)
        armed = sum(1 for row in paper["strategies"] if row["paper"] == "armed")
        halted = sum(1 for row in paper["strategies"] if row["paper"] == "halted")
        if not paper["strategies"]:
            paper_state, paper_detail = "idle", "No strategy runtime is loaded."
        elif halted and not armed:
            paper_state = "halted"
            paper_detail = "Kill switch is on. Paper trading is stopped."
        elif halted:
            paper_state = "mixed"
            paper_detail = f"{armed} armed, {halted} halted. Thresholds stay locked."
        else:
            paper_state = "armed"
            paper_detail = f"{armed} strategies armed for continuous paper trading."

        if missing:
            daily_state = "gap"
            daily_detail = f"{len(missing)} session(s) have no daily backtest yet."
        elif checked:
            daily_state = "clear"
            daily_detail = "Same-day replay is present on the last scan."
        elif latest and latest.get("source") == "no_hq_database":
            daily_state = "idle"
            daily_detail = "No HQ database, so this pass could not check the daily replay."
        else:
            daily_state = "idle"
            daily_detail = "Scan has not run. Daily replay is checked on the next pass."

        if pending:
            compare_state = "flag"
            bug_state = "waiting"
        elif checked:
            compare_state = "clear"
            bug_state = "clear"
        else:
            compare_state = "idle"
            bug_state = "idle"
        compare_detail = (
            f"{latest['rows_seen']} sessions scanned, {latest['bugs_raised']} new bugs."
            if latest
            else "Waiting for the first compare."
        )
        return {
            "name": "Strategy test assembly line",
            "thresholds_locked": True,
            "stations": [
                {
                    "id": "paper",
                    "label": "Paper trade",
                    "state": paper_state,
                    "detail": paper_detail,
                },
                {
                    "id": "daily_backtest",
                    "label": "Daily backtest",
                    "state": daily_state,
                    "detail": daily_detail,
                },
                {
                    "id": "compare",
                    "label": "Compare",
                    "state": compare_state,
                    "detail": compare_detail,
                },
                {
                    "id": "bugs",
                    "label": "Raise bugs",
                    "state": bug_state,
                    "detail": f"{len(pending)} open mismatch bug(s), each with a suggested fix.",
                },
                {
                    "id": "approval",
                    "label": "Human approval",
                    "state": "waiting" if pending else "clear" if checked else "idle",
                    "detail": (
                        "Approve, reject, or run a historical backtest before the line continues."
                        if pending
                        else "Nothing is waiting on a person."
                    ),
                },
                {
                    "id": "implement",
                    "label": "Implement and continue",
                    "state": "done" if implemented or continued else "idle",
                    "detail": (
                        f"{len(implemented)} safe fixes applied, {len(continued)} proposals "
                        "recorded. Locked thresholds were not changed. Paper keeps running."
                    ),
                },
            ],
            "strategies": strategies,
            "policies": state["policies"],
            "tickets": tickets,
            "cycles": cycles[-8:],
        }

    def tickets(
        self, status: str | None = None, strategy: str | None = None
    ) -> list[dict[str, Any]]:
        report = self.pipeline()
        rows: list[dict[str, Any]] = [
            row for row in report["tickets"] if isinstance(row, dict)
        ]
        if status:
            rows = [row for row in rows if row["status"] == status]
        if strategy:
            rows = [row for row in rows if row["strategy_code"] == strategy]
        return rows

    def approve(
        self,
        ticket_id: str,
        *,
        actor: str = "operator",
        note: str = "",
        run_historical_backtest: bool = False,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Human gate. Optionally run a historical backtest, then implement or continue."""
        assert_no_threshold_changes(overrides)
        self._require_pending(ticket_id)
        historical: dict[str, Any] | None = None
        if run_historical_backtest:
            historical = self._historical_backtest(ticket_id)
            if not historical["ok"]:
                with self._lock:
                    state = self._load()
                    ticket = self._find(state, ticket_id)
                    if ticket["status"] != "pending_approval":
                        raise ValueError(f"ticket {ticket_id} is {ticket['status']}")
                    ticket["historical_backtest"] = historical
                    ticket["run_historical_backtest"] = True
                    ticket["updated_at"] = _now()
                    self._save(state)
                    return copy.deepcopy(ticket)
        with self._lock:
            state = self._load()
            ticket = self._find(state, ticket_id)
            if ticket["status"] != "pending_approval":
                raise ValueError(f"ticket {ticket_id} is {ticket['status']}")
            if historical is not None:
                ticket["historical_backtest"] = historical
                ticket["run_historical_backtest"] = True
            self._apply(state, ticket, actor=actor, note=note)
            self._save(state)
            return copy.deepcopy(ticket)

    def reject(
        self,
        ticket_id: str,
        *,
        actor: str = "operator",
        note: str = "",
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert_no_threshold_changes(overrides)
        with self._lock:
            state = self._load()
            ticket = self._find(state, ticket_id)
            if ticket["status"] != "pending_approval":
                raise ValueError(f"ticket {ticket_id} is {ticket['status']}")
            ticket["status"] = "rejected"
            ticket["station"] = "rejected"
            ticket["approved_by"] = actor
            ticket["approval_note"] = note
            ticket["updated_at"] = _now()
            ticket["implementation"] = {
                "applied": False,
                "engine_changed": False,
                "thresholds_changed": False,
                "thresholds_locked": True,
                "blocked_reason": "Rejected by a person. Paper trading was not changed.",
            }
            self._save(state)
            return copy.deepcopy(ticket)

    def _require_pending(self, ticket_id: str) -> None:
        with self._lock:
            state = self._load()
            ticket = self._find(state, ticket_id)
            if ticket["status"] != "pending_approval":
                raise ValueError(f"ticket {ticket_id} is {ticket['status']}")

    def _historical_backtest(self, ticket_id: str) -> dict[str, Any]:
        with self._lock:
            ticket = copy.deepcopy(self._find(self._load(), ticket_id))
        if self.sandbox is None:
            return {"ok": False, "error": "backtest sandbox is not configured", "live_parity": True}
        try:
            run = self.sandbox.run_gcs_backtest(
                ticket["strategy_code"],
                start=ticket["session_date"],
                end=ticket["session_date"],
                live_parity=True,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc), "live_parity": True}
        metrics = run.get("metrics") or {}
        return {
            "ok": True,
            "run_id": run.get("run_id"),
            "live_parity": True,
            "thresholds_locked": True,
            "net_pnl_usd": metrics.get("net_pnl_usd"),
        }

    def _apply(
        self, state: dict[str, Any], ticket: dict[str, Any], *, actor: str, note: str
    ) -> None:
        ticket["approved_by"] = actor
        ticket["approval_note"] = note
        ticket["updated_at"] = _now()
        code = ticket["strategy_code"]
        policy = state["policies"].setdefault(code, {})
        if ticket["safe_to_apply"] and ticket["action"] == "prefer_live_parity":
            policy["comparison_mode"] = "live_parity"
            policy["updated_at"] = ticket["updated_at"]
            policy["updated_by"] = actor
            ticket["status"] = "implemented"
            ticket["station"] = "continue"
            ticket["implementation"] = {
                "applied": True,
                "action": "prefer_live_parity",
                "engine_changed": False,
                "thresholds_changed": False,
                "thresholds_locked": True,
                "follow_up": (
                    "Later compares for this strategy should use live-parity replay. "
                    "The engine and its thresholds were not edited."
                ),
            }
            return
        if ticket["safe_to_apply"] and ticket["action"] == "note_commission_check":
            policy["commission_check"] = "noted"
            policy["updated_at"] = ticket["updated_at"]
            policy["updated_by"] = actor
            ticket["status"] = "implemented"
            ticket["station"] = "continue"
            ticket["implementation"] = {
                "applied": True,
                "action": "note_commission_check",
                "engine_changed": False,
                "thresholds_changed": False,
                "thresholds_locked": True,
                "follow_up": (
                    "Commission and multiplier were flagged for the operator. No threshold changed."
                ),
            }
            return
        ticket["status"] = "continued"
        ticket["station"] = "continue"
        ticket["implementation"] = {
            "applied": False,
            "action": "proposal_only",
            "engine_changed": False,
            "thresholds_changed": False,
            "thresholds_locked": True,
            "blocked_reason": (
                "This fix would require an engine or threshold change. Locked thresholds "
                "were not modified. The proposal is recorded and paper trading continues."
            ),
        }

    def _paper(self) -> dict[str, Any]:
        try:
            raw = self.runner.status()
        except Exception as exc:
            return {"state": "unknown", "detail": str(exc), "strategies": []}
        strategies = raw.get("strategies") if isinstance(raw, dict) else None
        items: list[Any]
        if isinstance(strategies, list):
            items = strategies
        elif isinstance(raw, dict):
            items = [raw]
        else:
            items = []
        rows: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict) or "strategy_code" not in item:
                continue
            binding = item.get("account_binding") or {}
            process = item.get("process") or {}
            rows.append(
                {
                    "strategy_code": item["strategy_code"],
                    "account_id": binding.get("account_id"),
                    "paper": "halted" if item.get("kill_switch_active") else "armed",
                    "running": bool(process.get("running")),
                    "thresholds_locked": True,
                }
            )
        return {"strategies": rows}

    def _strategy_board(
        self,
        paper: dict[str, Any],
        tickets: list[dict[str, Any]],
        seen: set[str],
    ) -> list[dict[str, Any]]:
        board: dict[str, dict[str, Any]] = {}
        for row in paper["strategies"]:
            board[row["strategy_code"]] = {
                "strategy_code": row["strategy_code"],
                "account_id": row.get("account_id"),
                "paper": row["paper"],
                "process_running": row["running"],
                "station": "paper",
                "open_bugs": 0,
                "last_ticket_status": None,
            }
        for ticket in reversed(tickets):
            slot = board.setdefault(
                ticket["strategy_code"],
                {
                    "strategy_code": ticket["strategy_code"],
                    "account_id": None,
                    "paper": "unknown",
                    "process_running": False,
                    "station": "paper",
                    "open_bugs": 0,
                    "last_ticket_status": None,
                },
            )
            slot["last_ticket_status"] = ticket["status"]
            if ticket["status"] == "pending_approval":
                slot["open_bugs"] += 1
        for code, slot in board.items():
            on_last_scan = any(item.startswith(f"{code}|") for item in seen)
            if slot["open_bugs"]:
                slot["station"] = "approval"
            elif on_last_scan or slot["last_ticket_status"] in {"implemented", "continued"}:
                slot["station"] = "continue"
            else:
                slot["station"] = "paper"
        return list(board.values())

    def _find(self, state: dict[str, Any], ticket_id: str) -> dict[str, Any]:
        tickets = state.get("tickets")
        if isinstance(tickets, list):
            for ticket in tickets:
                if isinstance(ticket, dict) and ticket.get("ticket_id") == ticket_id:
                    return cast(dict[str, Any], ticket)
        raise ValueError(f"ticket {ticket_id} not found")

    def _load(self) -> dict[str, Any]:
        if self.store is None:
            with _FALLBACK_LOCK:
                raw = _FALLBACK.get("state")
            if not isinstance(raw, dict):
                return _empty_state()
            return copy.deepcopy(raw)
        blob = self.store.get(FACTORY_STATE_KEY)
        if not blob:
            return _empty_state()
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            return _empty_state()
        if not isinstance(parsed, dict):
            return _empty_state()
        parsed.setdefault("tickets", [])
        parsed.setdefault("policies", {})
        parsed.setdefault("cycles", [])
        return parsed

    def _save(self, state: dict[str, Any]) -> None:
        if self.store is None:
            with _FALLBACK_LOCK:
                _FALLBACK["state"] = copy.deepcopy(state)
            return
        payload = json.dumps(state, default=str, indent=2).encode()
        self.store.put(FACTORY_STATE_KEY, payload, "application/json")
