"""Local JSONL ledger of fills, keyed by execId (TWS history is session-scoped)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from waystone3.ibkr.models import AccountSnapshot, Execution, PositionSnapshot

_EXEC = "executions.jsonl"
_POS = "positions.json"
_ACCT = "account.json"


class ExecutionLedger:
    def __init__(self, root: Path) -> None:
        self.root = root

    def day_dir(self, day: date) -> Path:
        return self.root / f"dt={day.isoformat()}"

    def load(self, day: date) -> list[Execution]:
        path = self.day_dir(day) / _EXEC
        if not path.is_file():
            return []
        out: list[Execution] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(Execution.model_validate_json(line))
        return out

    def merge(self, day: date, fills: Sequence[Execution]) -> int:
        existing = {row.exec_id: row for row in self.load(day)}
        added = 0
        for fill in fills:
            if not fill.exec_id:
                continue
            if fill.exec_id not in existing:
                added += 1
            existing[fill.exec_id] = fill
        self._write_executions(day, list(existing.values()))
        return added

    def save_snapshot(
        self,
        day: date,
        positions: Sequence[PositionSnapshot],
        account: AccountSnapshot,
    ) -> None:
        folder = self.day_dir(day)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / _POS).write_text(
            json.dumps([p.model_dump(mode="json") for p in positions], indent=2) + "\n",
            encoding="utf-8",
        )
        (folder / _ACCT).write_text(
            json.dumps(account.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )

    def load_positions(self, day: date) -> list[PositionSnapshot]:
        path = self.day_dir(day) / _POS
        if not path.is_file():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [PositionSnapshot.model_validate(row) for row in raw]

    def load_account(self, day: date) -> AccountSnapshot | None:
        path = self.day_dir(day) / _ACCT
        if not path.is_file():
            return None
        return AccountSnapshot.model_validate_json(path.read_text(encoding="utf-8"))

    def _write_executions(self, day: date, rows: Sequence[Execution]) -> None:
        folder = self.day_dir(day)
        folder.mkdir(parents=True, exist_ok=True)
        ordered = sorted(rows, key=lambda r: (r.time, r.exec_id))
        body = "".join(r.model_dump_json() + "\n" for r in ordered)
        (folder / _EXEC).write_text(body, encoding="utf-8")
