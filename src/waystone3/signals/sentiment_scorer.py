"""Claude-based sentiment scorer (mirrors v2's proven prompt + tool-use shape).

One article in, a structured ``SentimentScore`` out. Uses Claude via forced tool-use with a
prompt-cached system prompt, so high-volume scoring is cheap. Defaults to Haiku — this is the
textbook fast/cheap classification path, scored per article at volume; pass ``model`` to use
a larger model. Key-optional and injectable (``score_fn``) so tests need no key or network.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from waystone3.llm import build_async_client

# Fast/cheap classification model — high volume, per-article. Override via `model=` or
# WAYSTONE_SCORER_MODEL (e.g. the Vertex Haiku model id).
DEFAULT_SCORER_MODEL = os.getenv("WAYSTONE_SCORER_MODEL", "claude-haiku-4-5")


class SentimentScore(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    sentiment: str  # bullish | bearish | neutral
    conviction: int = Field(ge=1, le=10)
    drivers: list[str]
    model: str
    scored_at: datetime


SYSTEM_PROMPT = (
    "You are a financial-markets analyst. Read a news headline (and brief summary if "
    "available) about a tradeable symbol and produce a structured sentiment assessment: "
    "direction (bullish/bearish/neutral), conviction 1-10 (1=vague, 10=major shock), and "
    "1-3 specific key drivers. Be disciplined: vague filler is conviction <= 3; if the news "
    "doesn't concretely concern the symbol's price drivers, return neutral with low "
    "conviction. Always use the score_article tool; never reply in prose."
)

SCORE_TOOL = {
    "name": "score_article",
    "description": "Submit your structured sentiment assessment.",
    "input_schema": {
        "type": "object",
        "properties": {
            "sentiment": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
            "conviction": {"type": "integer", "minimum": 1, "maximum": 10},
            "key_drivers": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 3,
            },
        },
        "required": ["sentiment", "conviction", "key_drivers"],
    },
}


class ClaudeSentimentScorer:
    def __init__(
        self,
        *,
        model: str = DEFAULT_SCORER_MODEL,
        score_fn: Any | None = None,
        enabled: bool | None = None,
    ) -> None:
        import os

        self.model = model
        self._score_fn = score_fn
        if enabled is None:
            enabled = score_fn is not None or bool(os.getenv("ANTHROPIC_API_KEY"))
        self.enabled = enabled

    async def score(self, title: str, summary: str | None, symbol: str) -> SentimentScore:
        if self._score_fn is not None:
            return await self._score_fn(title, summary, symbol)
        return await self._call_anthropic(title, summary, symbol)

    async def _call_anthropic(
        self, title: str, summary: str | None, symbol: str
    ) -> SentimentScore:
        client = build_async_client()
        user = f"Symbol: {symbol}\nTitle: {title}"
        if summary:
            user += f"\nSummary: {summary}"
        resp = await client.messages.create(
            model=self.model,
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[SCORE_TOOL],
            tool_choice={"type": "tool", "name": "score_article"},
            messages=[{"role": "user", "content": user}],
        )
        block = next((b for b in resp.content if b.type == "tool_use"), None)
        if block is None:
            raise RuntimeError("scorer: no tool_use block returned")
        data = block.input
        return SentimentScore(
            symbol=symbol,
            sentiment=data["sentiment"],
            conviction=int(data["conviction"]),
            drivers=list(data["key_drivers"]),
            model=self.model,
            scored_at=datetime.now(UTC),
        )
