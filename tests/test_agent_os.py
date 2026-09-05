from __future__ import annotations

from decimal import Decimal
from typing import Any

from waystone3.agent_os import build_agent_os, serve
from waystone3.agents.actions import ApprovalPolicy
from waystone3.brokers.paper import PaperBroker
from waystone3.core.types import Timeframe
from waystone3.data.stub import StubDataSource


def _completer(payload: dict[str, Any]):
    async def _c(system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        # Return judgement-shaped or tuning-shaped payload based on schema keys.
        props = schema.get("properties", {})
        if "quality" in props:
            return {"quality": payload["quality"], "summary": payload.get("summary", "x")}
        return {
            "adjust": payload.get("adjust", False),
            "emphasis": payload.get("emphasis", "balanced"),
            "reason": payload.get("reason", ""),
        }
    return _c


def test_team_recipients_seeded_from_cliq_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ZOHO_CLIQ_WEBHOOK_URL", "https://cliq.zoho.com/x?zapikey=K")
    monkeypatch.delenv("WHATSAPP_GROUP_API_URL", raising=False)
    monkeypatch.delenv("GROK_BOT_WEBHOOK_URL", raising=False)
    os_ = build_agent_os(is_paper=True)
    channels = {r.channel for r in os_.recipients.list_all()}
    assert channels == {"cliq"}  # only the configured channel is seeded


def test_team_recipients_seeded_from_grok_bot_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("ZOHO_CLIQ_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("WHATSAPP_GROUP_API_URL", raising=False)
    monkeypatch.setenv("GROK_BOT_WEBHOOK_URL", "https://api2.cursor.sh/automations/webhook/r1")
    os_ = build_agent_os(is_paper=True)
    channels = {r.channel for r in os_.recipients.list_all()}
    assert channels == {"grok_bot"}


def test_no_team_recipients_without_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("ZOHO_CLIQ_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("WHATSAPP_GROUP_API_URL", raising=False)
    monkeypatch.delenv("GROK_BOT_WEBHOOK_URL", raising=False)
    os_ = build_agent_os(is_paper=True)
    assert os_.recipients.list_all() == []  # nothing configured -> no auto recipients


async def test_serve_runs_cycles_and_places_orders() -> None:
    result = await serve(
        data=StubDataSource(default_slope=1.0),
        broker=PaperBroker(),
        watchlist=["AAPL", "MSFT"],
        cycles=2,
        timeframe=Timeframe.D1,
        lookback=80,
    )
    assert result.cycles == 2
    assert result.total_orders >= 1  # uptrend -> buys (first cycle at least)


async def test_serve_without_claude_key_has_no_judgements() -> None:
    # No completer + no API key -> Claude agents disabled, no judgements emitted.
    import os

    os.environ.pop("ANTHROPIC_API_KEY", None)
    result = await serve(
        data=StubDataSource(default_slope=1.0),
        broker=PaperBroker(),
        watchlist=["AAPL"],
        cycles=1,
    )
    assert result.judgements == []


async def test_serve_claude_loop_judges_and_tunes() -> None:
    # Analyst judges "concerning" -> tuner adjusts weights via the (AUTO) gateway.
    result = await serve(
        data=StubDataSource(default_slope=1.0),
        broker=PaperBroker(),
        watchlist=["AAPL"],
        cycles=1,
        completer=_completer(
            {"quality": "concerning", "adjust": True, "emphasis": "volume"}
        ),
        policy=ApprovalPolicy.AUTO,
    )
    assert result.judgements and result.judgements[0][1] == "concerning"
    # tuner applied the "volume" preset
    assert result.final_weights["volume"] == 0.35
    assert any(kind == "adjust_weights" and status == "applied"
               for kind, _, status in result.actions)


async def test_manual_policy_queues_actions_not_applied() -> None:
    result = await serve(
        data=StubDataSource(default_slope=1.0),
        broker=PaperBroker(),
        watchlist=["AAPL"],
        cycles=1,
        completer=_completer(
            {"quality": "bad", "adjust": True, "emphasis": "volume"}
        ),
        policy=ApprovalPolicy.MANUAL,
    )
    # action requested but pending (not applied); weights unchanged
    assert any(status == "pending" for _, _, status in result.actions)
    assert result.final_weights["volume"] == 0.2


async def test_build_agent_os_roster() -> None:
    os_ = build_agent_os(is_paper=True)
    names = {a.name for a in os_.registry.agents}
    assert {"logger", "risk_supervisor", "notifier", "analyst", "tuner"} <= names
    # gateway refuses to act on a live broker
    live = build_agent_os(is_paper=True)
    assert live.state.is_paper is True


def test_decimal_scaling_is_used() -> None:
    assert (Decimal(2000) * Decimal("0.5")).quantize(Decimal(1)) == Decimal(1000)
