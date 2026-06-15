"""The competition engine.

Each user registers, gets a token + a private paper account, submits a strategy config, and
runs cycles against a shared data feed. Standings rank everyone by paper-account return.
Backtesting their config (before or during play) reuses the same `run_backtest` as the rest
of the platform. Everything is in-memory; the MCP layer (P4) adds auth + remote access.
"""

from __future__ import annotations

import itertools
import secrets
from datetime import datetime
from decimal import Decimal

from waystone3.brokers.paper import PaperBroker
from waystone3.competition.models import Entry, Standing, StrategyConfig
from waystone3.competition.store import CompetitionStore
from waystone3.core.types import Timeframe
from waystone3.data.base import MarketDataSource
from waystone3.decision.engine import DecisionConfig, DecisionEngine
from waystone3.risk.guard import RiskGuard
from waystone3.runner.backtest import BacktestMetrics, run_backtest
from waystone3.runner.cycle import CycleReport, run_cycle
from waystone3.signals.base import SignalContributor
from waystone3.signals.registry import build_contributor


class Competition:
    def __init__(
        self,
        data: MarketDataSource,
        *,
        initial_cash: Decimal = Decimal(100_000),
        max_players: int = 5,
        store: CompetitionStore | None = None,
    ) -> None:
        self.data = data
        self.initial_cash = initial_cash
        self.max_players = max_players
        self.store = store
        self._entries: dict[str, Entry] = {}
        self._by_token: dict[str, str] = {}
        self._ids = itertools.count(1)
        if store is not None:
            self._load()

    def _load(self) -> None:
        assert self.store is not None
        max_n = 0
        for row in self.store.load_players():
            entry = Entry(
                user_id=row["user_id"],
                display_name=row["display_name"],
                token=row["token"],
                broker=PaperBroker.from_state(row["broker_state"]),
                guard=RiskGuard(is_paper=True),
                config=row["config"],
                cycles_run=row["cycles_run"],
            )
            self._entries[entry.user_id] = entry
            self._by_token[entry.token] = entry.user_id
            max_n = max(max_n, int(entry.user_id.lstrip("u") or 0))
        self._ids = itertools.count(max_n + 1)

    def _persist(self, entry: Entry) -> None:
        if self.store is not None:
            self.store.save_player(
                user_id=entry.user_id,
                display_name=entry.display_name,
                token=entry.token,
                config=entry.config,
                cycles_run=entry.cycles_run,
                broker_state=entry.broker.export_state(),
            )

    # ── membership ────────────────────────────────────────────────────────────
    def register(self, display_name: str) -> Entry:
        if len(self._entries) >= self.max_players:
            raise ValueError(f"competition is full ({self.max_players} players)")
        user_id = f"u{next(self._ids)}"
        token = secrets.token_hex(16)
        entry = Entry(
            user_id=user_id,
            display_name=display_name,
            token=token,
            broker=PaperBroker(initial_cash=self.initial_cash),
            guard=RiskGuard(is_paper=True),
        )
        self._entries[user_id] = entry
        self._by_token[token] = user_id
        self._persist(entry)
        return entry

    def authenticate(self, token: str) -> Entry | None:
        user_id = self._by_token.get(token)
        return self._entries.get(user_id) if user_id else None

    def get(self, user_id: str) -> Entry | None:
        return self._entries.get(user_id)

    def all_entries(self) -> list[Entry]:
        return list(self._entries.values())

    async def refresh_marks(self) -> None:
        """Mark every player's open positions to the latest price (for read views)."""
        for entry in self._entries.values():
            await self._mark_to_market(entry)

    def submit_strategy(self, user_id: str, config: StrategyConfig) -> None:
        config.validate()
        entry = self._require(user_id)
        entry.config = config
        self._persist(entry)

    # ── play ──────────────────────────────────────────────────────────────────
    def _contributors(self, config: StrategyConfig) -> list[SignalContributor]:
        return [
            build_contributor(name)
            for name, weight in config.weights.items()
            if weight > 0
        ]

    def _engine(self, config: StrategyConfig) -> DecisionEngine:
        return DecisionEngine(
            DecisionConfig(
                bullish_threshold=config.bullish_threshold,
                bearish_threshold=config.bearish_threshold,
                notional_per_trade=config.notional_per_trade,
            )
        )

    async def run_cycle(self, user_id: str) -> CycleReport:
        entry = self._require(user_id)
        config = self._require_strategy(entry)
        report = await run_cycle(
            data=self.data,
            broker=entry.broker,
            guard=entry.guard,
            contributors=self._contributors(config),
            weights=config.weights,
            engine=self._engine(config),
            watchlist=config.watchlist,
            timeframe=config.timeframe,
            bars_lookback=config.lookback,
        )
        entry.cycles_run += 1
        self._persist(entry)
        return report

    async def run_backtest(
        self, user_id: str, start: datetime, end: datetime
    ) -> BacktestMetrics:
        entry = self._require(user_id)
        config = self._require_strategy(entry)
        result = await run_backtest(
            data=self.data,
            symbols=config.watchlist,
            timeframe=config.timeframe,
            start=start,
            end=end,
            lookback=config.lookback,
            initial_cash=self.initial_cash,
            contributors=self._contributors(config),
            weights=config.weights,
            decision=self._engine(config).config,
        )
        return result.metrics

    async def standings(self) -> list[Standing]:
        rows: list[Standing] = []
        for entry in self._entries.values():
            await self._mark_to_market(entry)
            acct = await entry.broker.get_account()
            ret = (acct.equity - self.initial_cash) / self.initial_cash
            rows.append(
                Standing(
                    rank=0,
                    display_name=entry.display_name,
                    equity=acct.equity,
                    return_pct=ret,
                    cycles_run=entry.cycles_run,
                    has_strategy=entry.config is not None,
                )
            )
        rows.sort(key=lambda s: s.return_pct, reverse=True)
        return [
            Standing(
                rank=i + 1,
                display_name=s.display_name,
                equity=s.equity,
                return_pct=s.return_pct,
                cycles_run=s.cycles_run,
                has_strategy=s.has_strategy,
            )
            for i, s in enumerate(rows)
        ]

    async def _mark_to_market(self, entry: Entry) -> None:
        tf = entry.config.timeframe if entry.config else Timeframe.D1
        for position in await entry.broker.get_positions():
            bars = await self.data.get_bars(position.symbol, tf, 1)
            if bars:
                entry.broker.set_mark(position.symbol, bars[-1].close)

    # ── helpers ─────────────────────────────────────────────────────────────
    def _require(self, user_id: str) -> Entry:
        entry = self._entries.get(user_id)
        if entry is None:
            raise KeyError(f"unknown user {user_id!r}")
        return entry

    @staticmethod
    def _require_strategy(entry: Entry) -> StrategyConfig:
        if entry.config is None:
            raise ValueError(f"{entry.display_name} has not submitted a strategy")
        return entry.config
