"""Read-only ib_insync session. Never places orders."""

from __future__ import annotations

from typing import Any

from waystone3.ibkr.convert import execution_from_fill, position_from_item
from waystone3.ibkr.models import AccountSnapshot, Book, Execution, PositionSnapshot
from waystone3.ibkr.settings import IbkrSettings


def _tag_float(values: dict[str, str], *names: str) -> float:
    for name in names:
        raw = values.get(name)
        if raw is None or raw == "":
            continue
        try:
            return float(raw)
        except ValueError:
            continue
    return 0.0


class IbkrClient:
    def __init__(self, settings: IbkrSettings) -> None:
        self.settings = settings
        self._ib: Any | None = None

    def connect(self) -> None:
        from ib_insync import IB

        ib = IB()  # type: ignore[no-untyped-call]
        ib.connect(
            self.settings.ib_host,
            self.settings.ib_port,
            clientId=self.settings.ib_client_id,
            timeout=15,
        )
        self._ib = ib

    def disconnect(self) -> None:
        ib = self._ib
        self._ib = None
        if ib is not None:
            ib.disconnect()

    def _require(self) -> Any:
        if self._ib is None:
            raise RuntimeError("not connected to TWS/Gateway")
        return self._ib

    def fetch_fills(self, client_books: dict[int, Book] | None = None) -> list[Execution]:
        from ib_insync import ExecutionFilter

        ib = self._require()
        fills = ib.reqExecutions(ExecutionFilter())
        return [execution_from_fill(fill, client_books) for fill in fills]

    def fetch_positions(
        self, client_books: dict[int, Book] | None = None
    ) -> list[PositionSnapshot]:
        ib = self._require()
        ib.reqPositions()
        ib.sleep(0.4)
        items = list(ib.portfolio())
        if not items:
            items = list(ib.positions())
        return [position_from_item(item, client_books) for item in items]

    def fetch_account(self) -> AccountSnapshot:
        ib = self._require()
        rows = list(ib.accountSummary())
        if not rows:
            ib.reqAccountSummary()
            ib.sleep(0.6)
            rows = list(ib.accountSummary())
        values: dict[str, str] = {}
        account_id = ""
        currency = "USD"
        for row in rows:
            tag = str(getattr(row, "tag", ""))
            ccy = str(getattr(row, "currency", "") or "")
            if ccy and ccy not in {"USD", "BASE", ""}:
                continue
            values[tag] = str(getattr(row, "value", "") or "")
            if not account_id:
                account_id = str(getattr(row, "account", "") or "")
            if ccy:
                currency = "USD" if ccy == "BASE" else ccy
        if not account_id:
            account_id = values.get("AccountCode", "")
        return AccountSnapshot(
            account_id=account_id,
            nlv=_tag_float(values, "NetLiquidation"),
            cash=_tag_float(values, "TotalCashValue"),
            buying_power=_tag_float(values, "BuyingPower", "AvailableFunds", "ExcessLiquidity"),
            excess_liquidity=_tag_float(values, "ExcessLiquidity"),
            maint_margin=_tag_float(values, "MaintMarginReq"),
            currency=values.get("Currency") or currency,
        )
