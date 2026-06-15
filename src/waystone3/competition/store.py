"""SQLite persistence for the competition.

A single table holds each player's identity, token, submitted strategy, cycle count, and a
serialized snapshot of their paper account. Stdlib ``sqlite3`` — no extra dependency. Put
the DB file on a persistent volume so a pod restart doesn't wipe the leaderboard.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any

from waystone3.competition.models import StrategyConfig
from waystone3.core.types import Timeframe


def config_to_json(config: StrategyConfig) -> str:
    return json.dumps(
        {
            "weights": config.weights,
            "watchlist": config.watchlist,
            "bullish_threshold": str(config.bullish_threshold),
            "bearish_threshold": str(config.bearish_threshold),
            "notional_per_trade": str(config.notional_per_trade),
            "timeframe": config.timeframe.value,
            "lookback": config.lookback,
        }
    )


def config_from_json(raw: str) -> StrategyConfig:
    data = json.loads(raw)
    return StrategyConfig(
        weights=data["weights"],
        watchlist=data["watchlist"],
        bullish_threshold=Decimal(data["bullish_threshold"]),
        bearish_threshold=Decimal(data["bearish_threshold"]),
        notional_per_trade=Decimal(data["notional_per_trade"]),
        timeframe=Timeframe(data["timeframe"]),
        lookback=int(data["lookback"]),
    )


class CompetitionStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS players (
                user_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                token TEXT NOT NULL,
                config_json TEXT,
                cycles_run INTEGER NOT NULL DEFAULT 0,
                broker_json TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def save_player(
        self,
        *,
        user_id: str,
        display_name: str,
        token: str,
        config: StrategyConfig | None,
        cycles_run: int,
        broker_state: dict[str, Any],
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO players (user_id, display_name, token, config_json, cycles_run, broker_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                display_name=excluded.display_name,
                token=excluded.token,
                config_json=excluded.config_json,
                cycles_run=excluded.cycles_run,
                broker_json=excluded.broker_json
            """,
            (
                user_id,
                display_name,
                token,
                config_to_json(config) if config else None,
                cycles_run,
                json.dumps(broker_state),
            ),
        )
        self._conn.commit()

    def load_players(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT user_id, display_name, token, config_json, cycles_run, broker_json "
            "FROM players ORDER BY user_id"
        ).fetchall()
        out: list[dict[str, Any]] = []
        for user_id, display_name, token, config_json, cycles_run, broker_json in rows:
            out.append(
                {
                    "user_id": user_id,
                    "display_name": display_name,
                    "token": token,
                    "config": config_from_json(config_json) if config_json else None,
                    "cycles_run": cycles_run,
                    "broker_state": json.loads(broker_json),
                }
            )
        return out

    def close(self) -> None:
        self._conn.close()
