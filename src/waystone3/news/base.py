"""News article model and source protocol (mirrors the proven v2 shape)."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class Article(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str
    url: str
    title: str
    summary: str | None
    symbols: tuple[str, ...]
    published_at: datetime

    @property
    def content_hash(self) -> str:
        """Stable dedup key: source + title + timestamp (URLs carry tracking params)."""
        key = f"{self.source}|{self.title}|{self.published_at.isoformat()}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()


@runtime_checkable
class NewsSource(Protocol):
    name: str

    async def fetch(self, symbols: list[str]) -> list[Article]: ...
