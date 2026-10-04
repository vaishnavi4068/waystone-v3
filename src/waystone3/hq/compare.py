"""Paper vs same-day backtest replay: trade matching and the Daily Sync Log row."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

MATCH_WINDOW = timedelta(minutes=10)
LOSS_CAP_BLOCKED = "LOSS_CAP_BLOCKED"


@dataclass
class TradeRef:
    key: int
    direction: str
    entry_ts: datetime
    exit_ts: datetime | None
    points: Decimal | None
    net_pnl: Decimal | None
    exit_reason: str | None
    gross_pnl: Decimal | None = None
    commission: Decimal | None = None


@dataclass
class Match:
    seq: int
    match_type: str
    paper: TradeRef | None
    backtest: TradeRef | None
    unmatched_reason: str | None = None

    @property
    def entry_gap_s(self) -> Decimal | None:
        if self.paper is None or self.backtest is None:
            return None
        return Decimal(str((self.paper.entry_ts - self.backtest.entry_ts).total_seconds()))

    @property
    def exit_gap_min(self) -> Decimal | None:
        if not (self.paper and self.backtest and self.paper.exit_ts and self.backtest.exit_ts):
            return None
        seconds = (self.paper.exit_ts - self.backtest.exit_ts).total_seconds()
        return (Decimal(str(seconds)) / 60).quantize(Decimal("0.01"))

    @property
    def points_gap(self) -> Decimal | None:
        if not (self.paper and self.backtest):
            return None
        if self.paper.points is None or self.backtest.points is None:
            return None
        return self.paper.points - self.backtest.points

    @property
    def pnl_delta(self) -> Decimal | None:
        if not (self.paper and self.backtest):
            return None
        if self.paper.net_pnl is None or self.backtest.net_pnl is None:
            return None
        return self.paper.net_pnl - self.backtest.net_pnl

    @property
    def pnl_delta_pct(self) -> Decimal | None:
        delta = self.pnl_delta
        if delta is None or self.backtest is None or not self.backtest.net_pnl:
            return None
        return (delta / abs(self.backtest.net_pnl)).quantize(Decimal("0.0001"))

    @property
    def exit_reason_match(self) -> bool | None:
        if not (
            self.paper and self.backtest and self.paper.exit_reason and self.backtest.exit_reason
        ):
            return None
        return self.paper.exit_reason == self.backtest.exit_reason


def match_trades(
    paper: list[TradeRef], backtest: list[TradeRef], loss_cap_at: datetime | None
) -> list[Match]:
    """Greedy nearest-entry matching on direction within MATCH_WINDOW.

    Backtest trades the live engine never took after its daily loss cap fired are tagged
    LOSS_CAP_BLOCKED: that difference is the live risk control working, not drift.
    """
    free = sorted(paper, key=lambda t: t.entry_ts)
    pairs: list[tuple[TradeRef | None, TradeRef | None, str | None]] = []
    for bt in sorted(backtest, key=lambda t: t.entry_ts):
        candidates = [
            p
            for p in free
            if p.direction == bt.direction and abs(p.entry_ts - bt.entry_ts) <= MATCH_WINDOW
        ]
        if candidates:
            best = min(candidates, key=lambda p: abs(p.entry_ts - bt.entry_ts))
            free.remove(best)
            pairs.append((best, bt, None))
        elif loss_cap_at is not None and bt.entry_ts >= loss_cap_at - MATCH_WINDOW:
            pairs.append((None, bt, LOSS_CAP_BLOCKED))
        else:
            pairs.append((None, bt, "NOT_TAKEN_LIVE"))
    pairs.extend((p, None, "NOT_IN_BACKTEST") for p in free)
    pairs.sort(key=lambda pair: (pair[0] or pair[1]).entry_ts)  # type: ignore[union-attr]
    out: list[Match] = []
    for seq, (p, b, reason) in enumerate(pairs, start=1):
        kind = "MATCHED" if p and b else "PAPER_ONLY" if p else "BACKTEST_ONLY"
        out.append(Match(seq, kind, p, b, reason))
    return out


def _sum(values: list[Decimal | None]) -> Decimal | None:
    present = [v for v in values if v is not None]
    return sum(present, Decimal(0)) if present else None


@dataclass
class SyncRow:
    live_trades: int
    live_win_rate: Decimal | None
    live_points: Decimal | None
    live_gross_pnl: Decimal | None
    live_commission: Decimal | None
    live_net_pnl: Decimal | None
    live_entry_ts: datetime | None
    live_exit_ts: datetime | None
    live_exit_reason: str | None
    bt_trades: int | None
    bt_points: Decimal | None
    bt_net_pnl: Decimal | None
    bt_entry_ts: datetime | None
    bt_exit_ts: datetime | None
    bt_exit_reason: str | None
    pnl_delta: Decimal | None
    pnl_delta_pct: Decimal | None
    exit_reason_match: str | None
    exit_time_gap_min: Decimal | None
    loss_cap_hit: str
    sync_status: str
    notes: list[str] = field(default_factory=list)


def daily_sync(
    paper: list[TradeRef],
    matches: list[Match] | None,
    *,
    loss_cap_hit: bool,
    pct_threshold: Decimal,
    usd_floor: Decimal,
) -> SyncRow:
    """One Daily Sync Log row. ``matches`` is None when no backtest replay exists."""
    paper = sorted(paper, key=lambda t: t.entry_ts)
    wins = sum(1 for t in paper if (t.net_pnl or 0) > 0)
    row = SyncRow(
        live_trades=len(paper),
        live_win_rate=(Decimal(wins) / len(paper)).quantize(Decimal("0.0001")) if paper else None,
        live_points=_sum([t.points for t in paper]),
        live_gross_pnl=_sum([t.gross_pnl for t in paper]),
        live_commission=_sum([t.commission for t in paper]),
        live_net_pnl=_sum([t.net_pnl for t in paper]) if paper else Decimal(0),
        live_entry_ts=paper[0].entry_ts if paper else None,
        live_exit_ts=paper[-1].exit_ts if paper else None,
        live_exit_reason=paper[-1].exit_reason if paper else None,
        bt_trades=None,
        bt_points=None,
        bt_net_pnl=None,
        bt_entry_ts=None,
        bt_exit_ts=None,
        bt_exit_reason=None,
        pnl_delta=None,
        pnl_delta_pct=None,
        exit_reason_match=None,
        exit_time_gap_min=None,
        loss_cap_hit="Y" if loss_cap_hit else "N",
        sync_status="N/A",
    )
    if matches is None:
        row.notes.append("no backtest replay for this day")
        return row

    excluded = [m for m in matches if m.unmatched_reason == LOSS_CAP_BLOCKED]
    compared = sorted(
        (m.backtest for m in matches if m.backtest and m not in excluded), key=lambda t: t.entry_ts
    )
    row.bt_trades = len(compared)
    row.bt_points = _sum([t.points for t in compared])
    row.bt_net_pnl = _sum([t.net_pnl for t in compared]) if compared else Decimal(0)
    if compared:
        row.bt_entry_ts, row.bt_exit_ts = compared[0].entry_ts, compared[-1].exit_ts
        row.bt_exit_reason = compared[-1].exit_reason
    if excluded:
        row.notes.append(
            f"{len(excluded)} backtest trade(s) excluded: live correctly blocked by the daily loss cap"
        )

    if row.live_net_pnl is not None and row.bt_net_pnl is not None:
        row.pnl_delta = row.live_net_pnl - row.bt_net_pnl
        if row.bt_net_pnl:
            row.pnl_delta_pct = (row.pnl_delta / abs(row.bt_net_pnl)).quantize(Decimal("0.0001"))
    if row.live_exit_ts and row.bt_exit_ts:
        gap = (row.live_exit_ts - row.bt_exit_ts).total_seconds()
        row.exit_time_gap_min = (Decimal(str(gap)) / 60).quantize(Decimal("0.01"))

    if not paper and not compared:
        row.sync_status = "OK"
        row.notes.append("no trades live or in the backtest")
        return row

    flags: list[str] = []
    pair_matches = [m.exit_reason_match for m in matches if m.match_type == "MATCHED"]
    if row.live_exit_reason and row.bt_exit_reason:
        agrees = row.live_exit_reason == row.bt_exit_reason and all(
            x is not False for x in pair_matches
        )
        row.exit_reason_match = "Y" if agrees else "N"
        if not agrees:
            flags.append("exit reason differs")
    if row.live_trades != row.bt_trades:
        flags.append(f"trade count {row.live_trades} live vs {row.bt_trades} backtest")
    if (
        row.pnl_delta_pct is not None
        and row.pnl_delta is not None
        and abs(row.pnl_delta_pct) > pct_threshold
        and abs(row.pnl_delta) > usd_floor
    ):
        flags.append(f"P&L delta {row.pnl_delta_pct:.1%} exceeds {pct_threshold:.0%}")
    row.sync_status = "FLAG" if flags else "OK"
    row.notes.extend(flags)
    return row
