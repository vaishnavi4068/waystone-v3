"""Polygon.io news source — the same subscription that serves bars.

Fetches recent articles per ticker, dedupes across tickers, and maps to the shared
``Article`` model. HTTP layer is injectable for keyless, network-free tests.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, cast

from waystone3.news.base import Article

FetchFn = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class PolygonNewsSource:
    name = "polygon-news"
    BASE = "https://api.polygon.io"

    def __init__(
        self, api_key: str | None = None, fetch_fn: FetchFn | None = None, limit: int = 20
    ) -> None:
        import os

        self.api_key = api_key or os.getenv("POLYGON_API_KEY", "")
        self._fetch_fn = fetch_fn
        self.limit = limit

    async def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._fetch_fn is not None:
            return await self._fetch_fn(path, params)
        if not self.api_key:
            raise RuntimeError("POLYGON_API_KEY is not set")
        import httpx

        async with httpx.AsyncClient(base_url=self.BASE, timeout=30.0) as client:
            resp = await client.get(path, params={**params, "apiKey": self.api_key})
            resp.raise_for_status()
            return cast("dict[str, Any]", resp.json())

    async def fetch(self, symbols: list[str]) -> list[Article]:
        articles: list[Article] = []
        seen: set[str] = set()
        for symbol in symbols:
            data = await self._request(
                "/v2/reference/news",
                {"ticker": symbol.upper(), "limit": self.limit, "order": "desc"},
            )
            for row in data.get("results", []):
                article = _article_from(row)
                if article.content_hash not in seen:
                    seen.add(article.content_hash)
                    articles.append(article)
        return articles


def _article_from(row: dict[str, Any]) -> Article:
    publisher = row.get("publisher") or {}
    return Article(
        source=str(publisher.get("name", "polygon")),
        url=str(row.get("article_url", "")),
        title=str(row.get("title", "")),
        summary=row.get("description"),
        symbols=tuple(t.upper() for t in row.get("tickers", [])),
        published_at=datetime.fromisoformat(row["published_utc"].replace("Z", "+00:00")),
    )
