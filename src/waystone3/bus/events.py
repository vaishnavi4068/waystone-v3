"""Typed events published on the bus.

Events carry primitive/summary data only (never the live ``CycleReport`` object) so the
bus never imports the runner — keeping the dependency arrow one-way: runner -> bus.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class Event:
    """Base type. Subscribing to ``Event`` receives every event."""


@dataclass(frozen=True)
class DecisionMade(Event):
    symbol: str
    score: Decimal
    action: str  # "buy" | "sell" | "hold"
    drivers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OrderFilled(Event):
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    order_id: str


@dataclass(frozen=True)
class OrderBlocked(Event):
    symbol: str
    side: str
    qty: Decimal
    reason: str


@dataclass(frozen=True)
class StrongSignal(Event):
    symbol: str
    score: Decimal
    drivers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CycleCompleted(Event):
    cycle: int
    num_orders: int
    decisions: list[tuple[str, Decimal, str]]  # (symbol, score, action)


@dataclass(frozen=True)
class CycleJudged(Event):
    cycle: int
    quality: str  # e.g. "good" | "concerning"
    summary: str
    by: str  # agent name


@dataclass(frozen=True)
class AgentAction(Event):
    agent: str
    kind: str  # "adjust_weights" | "gate_trading" | "trigger_retune" | ...
    detail: str
    approved: bool


@dataclass(frozen=True)
class AlertRaised(Event):
    severity: str  # "info" | "warn" | "critical"
    role: str  # "trader" | "engineer" | "ops"
    title: str
    body: str


@dataclass(frozen=True)
class AgentError(Event):
    agent: str
    event: str  # repr of the event being handled
    error: str
