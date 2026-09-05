"""Onboard and list paper-trading algos. Registry lives on the report store."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from waystone3.ibkr.algo_paths import (
    REGISTRY_KEY,
    default_live_prefix,
    default_replay_prefix,
)
from waystone3.ibkr.models import Book
from waystone3.ibkr.store import ReportStore

_ID = re.compile(r"^[a-z][a-z0-9_]{1,47}$")

DEFAULT_ALGOS: tuple[dict[str, Any], ...] = (
    {
        "id": "s5_options",
        "name": "Strategy 5 options",
        "book": "options",
        "client_id": 42,
        "notes": "VWAP / Strategy 5 paper. Live IBKR log vs same-day replay.",
    },
    {
        "id": "nq_futures",
        "name": "NQ futures",
        "book": "futures",
        "client_id": 1,
        "notes": "NQ paper algo. Live IBKR log vs same-day replay.",
    },
    {
        "id": "es_futures",
        "name": "ES futures",
        "book": "futures",
        "client_id": 2,
        "notes": "ES paper algo. Live IBKR log vs same-day replay.",
    },
)


class AlgoConfig(BaseModel):
    id: str
    name: str
    book: Book = Book.OTHER
    live_prefix: str = ""
    replay_prefix: str = ""
    client_id: int | None = None
    enabled: bool = True
    notes: str = ""

    def resolved_live(self) -> str:
        return self.live_prefix.strip() or default_live_prefix(self.id)

    def resolved_replay(self) -> str:
        return self.replay_prefix.strip() or default_replay_prefix(self.id)


class AlgoRegistry(BaseModel):
    algos: list[AlgoConfig] = Field(default_factory=list)

    def get(self, algo_id: str) -> AlgoConfig | None:
        for row in self.algos:
            if row.id == algo_id:
                return row
        return None

    def upsert(self, algo: AlgoConfig) -> AlgoConfig:
        _validate_id(algo.id)
        if not algo.live_prefix.strip():
            algo.live_prefix = default_live_prefix(algo.id)
        if not algo.replay_prefix.strip():
            algo.replay_prefix = default_replay_prefix(algo.id)
        kept = [row for row in self.algos if row.id != algo.id]
        kept.append(algo)
        self.algos = sorted(kept, key=lambda row: row.id)
        return algo

    def remove(self, algo_id: str) -> bool:
        before = len(self.algos)
        self.algos = [row for row in self.algos if row.id != algo_id]
        return len(self.algos) < before


def _validate_id(algo_id: str) -> None:
    if not _ID.match(algo_id):
        raise ValueError("algo id must be lowercase letters, digits, underscore (start with a letter)")


def default_registry() -> AlgoRegistry:
    out = AlgoRegistry()
    for raw in DEFAULT_ALGOS:
        out.upsert(AlgoConfig.model_validate(raw))
    return out


def load_registry(store: ReportStore) -> AlgoRegistry:
    raw = store.get(REGISTRY_KEY)
    if not raw:
        return default_registry()
    payload = json.loads(raw)
    return AlgoRegistry.model_validate(payload)


def save_registry(store: ReportStore, registry: AlgoRegistry) -> None:
    body = registry.model_dump_json(indent=2) + "\n"
    store.put(REGISTRY_KEY, body.encode(), "application/json")


def ensure_registry(store: ReportStore) -> AlgoRegistry:
    existing = store.get(REGISTRY_KEY)
    registry = load_registry(store)
    if not existing:
        save_registry(store, registry)
    return registry
