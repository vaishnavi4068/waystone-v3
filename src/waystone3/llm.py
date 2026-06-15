"""LLM client factory — direct Anthropic API or Google Vertex AI.

Set ``WAYSTONE_LLM_PROVIDER=vertex`` to route every Claude call through Vertex AI. Vertex
authenticates with **GCP IAM via Application Default Credentials — not an API key**: on GKE,
grant the pod's service account ``roles/aiplatform.user`` through Workload Identity and no
secret is stored anywhere. The default provider is the first-party Anthropic API
(``ANTHROPIC_API_KEY``).

Model IDs differ between providers — override per role with ``WAYSTONE_AGENT_MODEL`` and
``WAYSTONE_SCORER_MODEL`` (use the exact IDs from your region's Vertex Model Garden).
"""

from __future__ import annotations

import os
from typing import Any

from anthropic import AsyncAnthropic


def build_async_client() -> Any:
    """Return an async Claude client for the configured provider."""
    if os.getenv("WAYSTONE_LLM_PROVIDER", "anthropic").lower() == "vertex":
        from anthropic import AsyncAnthropicVertex

        # Only pass project_id when set; otherwise the SDK infers it from ADC / env.
        kwargs: dict[str, Any] = {"region": os.getenv("VERTEX_REGION", "us-east5")}
        project = os.getenv("VERTEX_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
        if project:
            kwargs["project_id"] = project
        return AsyncAnthropicVertex(**kwargs)
    return AsyncAnthropic()
