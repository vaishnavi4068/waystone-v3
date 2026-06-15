"""ClaudeAgent base — an agent backed by a Claude model.

Key-optional: without an ``ANTHROPIC_API_KEY`` (and without an injected completer) the
agent disables itself and the bus simply never routes events to it — the rest of the
control plane runs unaffected. Structured output is requested via ``output_config.format``
so the model returns schema-valid JSON. Tests inject ``complete_fn`` to avoid any network
or key requirement.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from waystone3.llm import build_async_client

log = structlog.get_logger()

# Most capable Claude model by default; override per-agent or via env (e.g. a Vertex model id).
DEFAULT_MODEL = os.getenv("WAYSTONE_AGENT_MODEL", "claude-opus-4-8")

Completer = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]


class ClaudeAgent:
    """Mixin/base providing a structured Claude completion to agent subclasses."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        complete_fn: Completer | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.model = model
        self._complete_fn = complete_fn
        if enabled is None:
            enabled = complete_fn is not None or bool(os.getenv("ANTHROPIC_API_KEY"))
        self.enabled = enabled

    async def complete(
        self, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        if self._complete_fn is not None:
            return await self._complete_fn(system, user, schema)
        return await self._call_anthropic(system, user, schema)

    async def _call_anthropic(
        self, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        client = build_async_client()
        resp = await client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError("Claude refused the request")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            raise RuntimeError("Claude returned no text block")
        return json.loads(text)
