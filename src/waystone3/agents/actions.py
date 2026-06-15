"""The guardrails for *acting* agents.

Agents never touch the broker, weights, or trading switches directly. They submit an
:class:`ActionRequest` to the :class:`ActionGateway`, which enforces three protections
before anything changes:

1. **Paper-only** — the gateway refuses to apply any action when the runtime is not in
   paper mode, regardless of policy. Acting on real money is never automatic here.
2. **Approval policy** — ``AUTO`` applies immediately (fine for paper/demo), ``MANUAL``
   queues the action for a human to approve, ``DENY`` blocks all acting.
3. **Audit** — every request is recorded append-only (applied / pending / denied), and an
   ``AgentAction`` event is published so the rest of the system can react.

``RuntimeState`` is the only thing actions mutate; the serve loop reads it each cycle, so
an approved weight change or trading halt takes effect on the next pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from waystone3.bus.bus import EventBus
from waystone3.bus.events import AgentAction


@dataclass
class RuntimeState:
    weights: dict[str, float]
    trading_enabled: bool = True
    exposure_scale: Decimal = Decimal(1)
    is_paper: bool = True
    retune_requested: bool = False


class ApprovalPolicy(StrEnum):
    AUTO = "auto"  # apply immediately (paper/demo)
    MANUAL = "manual"  # queue for human approval
    DENY = "deny"  # block all acting


@dataclass(frozen=True)
class ActionRequest:
    reason: str


@dataclass(frozen=True)
class AdjustWeights(ActionRequest):
    changes: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class GateTrading(ActionRequest):
    enabled: bool = True


@dataclass(frozen=True)
class ScaleExposure(ActionRequest):
    scale: Decimal = Decimal(1)


@dataclass(frozen=True)
class TriggerRetune(ActionRequest):
    pass


@dataclass
class AuditEntry:
    id: int
    agent: str
    kind: str
    detail: str
    reason: str
    status: str  # "applied" | "pending" | "denied"


def _describe(action: ActionRequest) -> tuple[str, str]:
    match action:
        case AdjustWeights(changes=changes):
            return "adjust_weights", ", ".join(f"{k}={v}" for k, v in changes.items())
        case GateTrading(enabled=enabled):
            return "gate_trading", f"enabled={enabled}"
        case ScaleExposure(scale=scale):
            return "scale_exposure", f"scale={scale}"
        case TriggerRetune():
            return "trigger_retune", "requested"
        case _:
            return "unknown", repr(action)


@dataclass
class ActionGateway:
    state: RuntimeState
    policy: ApprovalPolicy = ApprovalPolicy.MANUAL
    bus: EventBus | None = None
    audit: list[AuditEntry] = field(default_factory=list)
    _pending: dict[int, ActionRequest] = field(default_factory=dict)
    _seq: int = 0

    async def request(self, agent: str, action: ActionRequest) -> AuditEntry:
        self._seq += 1
        kind, detail = _describe(action)

        if not self.state.is_paper:
            status = "denied"  # hard paper-only gate
        elif self.policy == ApprovalPolicy.DENY:
            status = "denied"
        elif self.policy == ApprovalPolicy.AUTO:
            self._apply(action)
            status = "applied"
        else:
            self._pending[self._seq] = action
            status = "pending"

        entry = AuditEntry(self._seq, agent, kind, detail, action.reason, status)
        self.audit.append(entry)
        if self.bus is not None:
            await self.bus.publish(
                AgentAction(agent=agent, kind=kind, detail=detail, approved=status == "applied")
            )
        return entry

    def approve(self, entry_id: int) -> bool:
        """Approve a pending action (human-in-the-loop for MANUAL policy)."""
        action = self._pending.pop(entry_id, None)
        if action is None or not self.state.is_paper:
            return False
        self._apply(action)
        for e in self.audit:
            if e.id == entry_id:
                e.status = "applied"
        return True

    @property
    def pending(self) -> list[AuditEntry]:
        return [e for e in self.audit if e.status == "pending"]

    def _apply(self, action: ActionRequest) -> None:
        match action:
            case AdjustWeights(changes=changes):
                for k, v in changes.items():
                    self.state.weights[k] = max(0.0, v)
            case GateTrading(enabled=enabled):
                self.state.trading_enabled = enabled
            case ScaleExposure(scale=scale):
                self.state.exposure_scale = max(Decimal(0), min(Decimal(1), scale))
            case TriggerRetune():
                self.state.retune_requested = True
