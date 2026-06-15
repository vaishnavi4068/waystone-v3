"""SQLite persistence for the shared workspace: team members + shared strategy + flags.

The Alpaca account itself is the source of truth for cash/positions/orders, so we only
persist what the broker doesn't hold: who the team members are (token → name), the shared
strategy config, and the kill-switch state. Put the DB on a persistent volume.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from waystone3.competition.models import StrategyConfig
from waystone3.competition.store import config_from_json, config_to_json


class WorkspaceStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS members "
            "(token TEXT PRIMARY KEY, name TEXT NOT NULL, ord INTEGER NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._conn.commit()

    # members
    def add_member(self, token: str, name: str, ord_: int) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO members (token, name, ord) VALUES (?, ?, ?)",
            (token, name, ord_),
        )
        self._conn.commit()

    def load_members(self) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT token, name FROM members ORDER BY ord"
        ).fetchall()
        return [(t, n) for t, n in rows]

    # meta (strategy + flags)
    def _set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()

    def _get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def save_strategy(self, config: StrategyConfig | None) -> None:
        self._set("strategy", config_to_json(config) if config else "")

    def load_strategy(self) -> StrategyConfig | None:
        raw = self._get("strategy")
        return config_from_json(raw) if raw else None

    def save_trading_enabled(self, enabled: bool) -> None:
        self._set("trading_enabled", "1" if enabled else "0")

    def load_trading_enabled(self) -> bool:
        raw = self._get("trading_enabled")
        return raw != "0"  # default enabled

    def close(self) -> None:
        self._conn.close()

    # exposed for callers that want the raw connection (unused by default)
    def raw(self) -> Any:
        return self._conn
