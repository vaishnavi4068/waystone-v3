"""The shared team trading workspace.

One broker (Alpaca paper in prod), one shared strategy, many authorized operators. Any
member can view the account, edit the shared strategy, run a cycle (which submits real
orders to the shared account), or hit the kill-switch. A single-cycle lock prevents two
operators from trading at once. The account/positions/orders are read live from the broker;
only the member roster, the shared strategy, and the kill-switch are persisted.
"""

from __future__ import annotations

import asyncio
import itertools
import secrets
from dataclasses import dataclass

from waystone3.brokers.base import Broker
from waystone3.competition.models import StrategyConfig
from waystone3.data.base import MarketDataSource
from waystone3.decision.engine import DecisionConfig, DecisionEngine
from waystone3.risk.guard import RiskGuard, RiskLimits
from waystone3.runner.cycle import CycleReport, run_cycle
from waystone3.signals.base import SignalContributor
from waystone3.signals.registry import build_contributor
from waystone3.workspace.store import WorkspaceStore


@dataclass(frozen=True)
class Member:
    name: str
    token: str


@dataclass
class AuditEntry:
    seq: int
    actor: str
    action: str
    detail: str


class WorkspaceError(Exception):
    pass


class TradingWorkspace:
    def __init__(
        self,
        data: MarketDataSource,
        broker: Broker,
        *,
        store: WorkspaceStore | None = None,
        max_members: int = 5,
        risk_limits: RiskLimits | None = None,
    ) -> None:
        self.data = data
        self.broker = broker
        self.store = store
        self.max_members = max_members
        self.risk_limits = risk_limits or RiskLimits(require_paper=True)
        self._members: dict[str, str] = {}  # token -> name
        self.strategy: StrategyConfig | None = None
        self.trading_enabled = True
        self.audit: list[AuditEntry] = []
        self._lock = asyncio.Lock()
        self._seq = itertools.count(1)
        self._ord = itertools.count(1)
        if store is not None:
            self._load()

    def _load(self) -> None:
        assert self.store is not None
        members = self.store.load_members()
        for token, name in members:
            self._members[token] = name
        self._ord = itertools.count(len(members) + 1)
        self.strategy = self.store.load_strategy()
        self.trading_enabled = self.store.load_trading_enabled()

    # ── membership ────────────────────────────────────────────────────────────
    def register_member(self, name: str) -> Member:
        if len(self._members) >= self.max_members:
            raise WorkspaceError(f"team is full ({self.max_members} members)")
        token = secrets.token_hex(16)
        self._members[token] = name
        if self.store is not None:
            self.store.add_member(token, name, next(self._ord))
        return Member(name=name, token=token)

    def authenticate(self, token: str) -> str | None:
        return self._members.get(token)

    def members(self) -> list[str]:
        return list(self._members.values())

    # ── strategy + flags ───────────────────────────────────────────────────────
    def set_strategy(self, config: StrategyConfig, actor: str) -> None:
        config.validate()
        self.strategy = config
        if self.store is not None:
            self.store.save_strategy(config)
        self._record(actor, "set_strategy", ", ".join(config.watchlist))

    def set_trading(self, enabled: bool, actor: str) -> None:
        self.trading_enabled = enabled
        if self.store is not None:
            self.store.save_trading_enabled(enabled)
        self._record(actor, "resume" if enabled else "halt", "")

    # ── execution ───────────────────────────────────────────────────────────────
    async def run_cycle(self, actor: str) -> CycleReport:
        if self.strategy is None:
            raise WorkspaceError("no shared strategy set")
        async with self._lock:  # one cycle at a time on the shared account
            cfg = self.strategy
            contributors: list[SignalContributor] = [
                build_contributor(name) for name, w in cfg.weights.items() if w > 0
            ]
            engine = DecisionEngine(
                DecisionConfig(
                    bullish_threshold=cfg.bullish_threshold,
                    bearish_threshold=cfg.bearish_threshold,
                    notional_per_trade=cfg.notional_per_trade,
                )
            )
            guard = RiskGuard(limits=self.risk_limits, is_paper=self.broker.is_paper)
            report = await run_cycle(
                data=self.data,
                broker=self.broker,
                guard=guard,
                contributors=contributors,
                weights=cfg.weights,
                engine=engine,
                watchlist=cfg.watchlist,
                timeframe=cfg.timeframe,
                bars_lookback=cfg.lookback,
                trading_enabled=self.trading_enabled,
            )
            self._record(actor, "run_cycle", f"{len(report.orders)} order(s) submitted")
            return report

    def _record(self, actor: str, action: str, detail: str) -> None:
        self.audit.append(AuditEntry(next(self._seq), actor, action, detail))
