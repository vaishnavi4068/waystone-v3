from __future__ import annotations

import pytest

from waystone3.brokers.paper import PaperBroker
from waystone3.workspace.runtime import build_broker_from_env


def test_no_creds_no_choice_uses_paper_sim(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("WAYSTONE_BROKER", raising=False)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    assert isinstance(build_broker_from_env(), PaperBroker)


def test_explicit_paper_overrides_present_creds(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Creds present but operator pins the sim -> must not touch Alpaca.
    monkeypatch.setenv("WAYSTONE_BROKER", "paper")
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    assert isinstance(build_broker_from_env(), PaperBroker)


def test_invalid_broker_value_raises(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("WAYSTONE_BROKER", "etrade")
    with pytest.raises(ValueError, match="WAYSTONE_BROKER"):
        build_broker_from_env()
