"""Turn duck-typed ib_insync fills / portfolio rows into dump models."""

from __future__ import annotations

from typing import Any

from waystone3.ibkr.classify import book_for
from waystone3.ibkr.models import Book, Execution, PositionSnapshot
from waystone3.ibkr.timeutil import parse_ib_time


def _str(value: object, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def _float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def _opt_strike(value: object) -> float | None:
    parsed = _float(value)
    if parsed is None or parsed == 0.0:
        return None
    return parsed


def _opt_right(value: object) -> str | None:
    text = _str(value).upper()
    return text if text in {"C", "P", "CALL", "PUT"} else None


def _expiry(contract: Any) -> str | None:
    raw = _str(getattr(contract, "lastTradeDateOrContractMonth", None))
    if not raw:
        raw = _str(getattr(contract, "expiry", None))
    return raw or None


def execution_from_fill(fill: Any, client_books: dict[int, Book] | None = None) -> Execution:
    contract = fill.contract
    execution = fill.execution
    commission = getattr(fill, "commissionReport", None)
    client_id = _int(getattr(execution, "clientId", None))
    sec_type = _str(getattr(contract, "secType", None), "STK")
    time = parse_ib_time(getattr(fill, "time", None) or getattr(execution, "time", None))
    comm = _float(getattr(commission, "commission", None)) if commission is not None else None
    pnl = _float(getattr(commission, "realizedPNL", None)) if commission is not None else None
    ccy = _str(getattr(commission, "currency", None)) if commission is not None else ""
    return Execution(
        exec_id=_str(getattr(execution, "execId", None)),
        perm_id=_int(getattr(execution, "permId", None)),
        order_id=_int(getattr(execution, "orderId", None)),
        time=time,
        account=_str(getattr(execution, "acctNumber", None)),
        sec_type=sec_type,
        symbol=_str(getattr(contract, "symbol", None)),
        local_symbol=_str(getattr(contract, "localSymbol", None))
        or _str(getattr(contract, "symbol", None)),
        con_id=_int(getattr(contract, "conId", None)),
        exchange=_str(getattr(execution, "exchange", None))
        or _str(getattr(contract, "exchange", None)),
        currency=_str(getattr(contract, "currency", None), "USD"),
        expiry=_expiry(contract),
        strike=_opt_strike(getattr(contract, "strike", None)),
        right=_opt_right(getattr(contract, "right", None)),
        multiplier=_str(getattr(contract, "multiplier", None)) or None,
        side=_str(getattr(execution, "side", None)),
        qty=float(_float(getattr(execution, "shares", None)) or 0.0),
        price=float(_float(getattr(execution, "price", None)) or 0.0),
        commission=comm,
        commission_currency=ccy or None,
        realized_pnl=pnl,
        cum_qty=_float(getattr(execution, "cumQty", None)),
        avg_price=_float(getattr(execution, "avgPrice", None)),
        client_id=client_id,
        book=book_for(sec_type, client_id, client_books),
    )


def position_from_item(item: Any, client_books: dict[int, Book] | None = None) -> PositionSnapshot:
    contract = item.contract
    sec_type = _str(getattr(contract, "secType", None), "STK")
    qty = float(_float(getattr(item, "position", 0)) or 0.0)
    avg = _float(getattr(item, "averageCost", None))
    if avg is None:
        avg = _float(getattr(item, "avgCost", None)) or 0.0
    market = _float(getattr(item, "marketPrice", None))
    return PositionSnapshot(
        account=_str(getattr(item, "account", None)),
        sec_type=sec_type,
        symbol=_str(getattr(contract, "symbol", None)),
        local_symbol=_str(getattr(contract, "localSymbol", None))
        or _str(getattr(contract, "symbol", None)),
        con_id=_int(getattr(contract, "conId", None)),
        exchange=_str(getattr(contract, "exchange", None)),
        currency=_str(getattr(contract, "currency", None), "USD"),
        expiry=_expiry(contract),
        strike=_opt_strike(getattr(contract, "strike", None)),
        right=_opt_right(getattr(contract, "right", None)),
        multiplier=_str(getattr(contract, "multiplier", None)) or None,
        qty=qty,
        avg_cost=float(avg or 0.0),
        market_price=market,
        market_value=_float(getattr(item, "marketValue", None)),
        unrealized_pnl=_float(getattr(item, "unrealizedPNL", None)),
        book=book_for(sec_type, None, client_books),
    )
