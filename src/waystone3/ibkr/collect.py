"""Pull TWS session fills/positions/account and merge into the local ledger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from waystone3.ibkr.classify import parse_client_books
from waystone3.ibkr.client import IbkrClient
from waystone3.ibkr.ledger import ExecutionLedger
from waystone3.ibkr.models import AccountSnapshot, Execution, PositionSnapshot
from waystone3.ibkr.settings import IbkrSettings
from waystone3.ibkr.timeutil import ny_date, today_ny


@dataclass
class CollectResult:
    day: date
    executions: list[Execution]
    positions: list[PositionSnapshot]
    account: AccountSnapshot
    new_fills: int
    tws_connected: bool


def collect(
    settings: IbkrSettings | None = None,
    *,
    day: date | None = None,
    client: IbkrClient | None = None,
) -> CollectResult:
    """Connect to TWS, merge fills into the ledger, snapshot positions/account."""
    cfg = settings or IbkrSettings()
    target = day or today_ny()
    books = parse_client_books(cfg.ibkr_client_books)
    ledger = ExecutionLedger(Path(cfg.ibkr_ledger_dir))
    owned = client is None
    session = client or IbkrClient(cfg)
    if owned:
        session.connect()
    try:
        fills = session.fetch_fills(books)
        positions = session.fetch_positions(books)
        account = session.fetch_account()
    finally:
        if owned:
            session.disconnect()

    new_fills = 0
    grouped: dict[date, list[Execution]] = {}
    for fill in fills:
        grouped.setdefault(ny_date(fill.time), []).append(fill)
    for fill_day, group in grouped.items():
        new_fills += ledger.merge(fill_day, group)
    ledger.save_snapshot(target, positions, account)
    return CollectResult(
        day=target,
        executions=ledger.load(target),
        positions=positions,
        account=account,
        new_fills=new_fills,
        tws_connected=True,
    )
