"""Daily live-paper vs replay-backtest comparison for one algo."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from waystone3.ibkr.algo_paths import (
    ALGOS_PREFIX,
    blotter_executions_key,
    blotter_success_key,
    day_from_algo_success,
)
from waystone3.ibkr.algo_registry import AlgoConfig, AlgoRegistry
from waystone3.ibkr.models import Execution
from waystone3.ibkr.reader import load_report
from waystone3.ibkr.store import ReportStore
from waystone3.ibkr.summary import summarize
from waystone3.ibkr.views import execution_dict


def _side(raw: str) -> str:
    token = raw.strip().upper()
    if token in {"BOT", "BUY"}:
        return "buy"
    if token in {"SLD", "SELL"}:
        return "sell"
    return raw.lower()


def _match_key(fill: Execution) -> tuple[Any, ...]:
    return (
        fill.book.value,
        fill.symbol.upper(),
        _side(fill.side),
        round(abs(fill.qty), 4),
        fill.expiry or "",
        fill.strike if fill.strike is not None else "",
        (fill.right or "").upper(),
    )


def load_blotter_fills(store: ReportStore, prefix: str, day: date) -> list[Execution] | None:
    if not store.exists(blotter_success_key(prefix, day)):
        return None
    raw = store.get(blotter_executions_key(prefix, day)) or b""
    return [Execution.model_validate_json(line) for line in raw.splitlines() if line.strip()]


def load_live_fills(
    store: ReportStore, algo: AlgoConfig, day: date
) -> tuple[list[Execution], str]:
    fills = load_blotter_fills(store, algo.resolved_live(), day)
    if fills is not None:
        return fills, "algo_live"
    report = load_report(store, day)
    if report is None:
        return [], "missing"
    out = [e for e in report.executions if e.book is algo.book]
    if algo.client_id is not None:
        tagged = [e for e in out if e.client_id == algo.client_id]
        if tagged:
            return tagged, "ibkr_dump"
    return out, "ibkr_dump" if out else "missing"


def _stats(fills: list[Execution], day: date) -> dict[str, Any]:
    summary = summarize(day.isoformat(), fills)
    return summary.totals.model_dump(mode="json")


def _pair(live: list[Execution], replay: list[Execution]) -> tuple[list[tuple[Execution, Execution]], list[Execution], list[Execution]]:
    unused_replay = list(replay)
    matched: list[tuple[Execution, Execution]] = []
    live_only: list[Execution] = []
    for left in live:
        key = _match_key(left)
        candidates = [row for row in unused_replay if _match_key(row) == key]
        if not candidates:
            live_only.append(left)
            continue
        best = min(candidates, key=lambda row: abs(row.price - left.price))
        unused_replay.remove(best)
        matched.append((left, best))
    return matched, live_only, unused_replay


def compare_algo_day(store: ReportStore, algo: AlgoConfig, day: date) -> dict[str, Any]:
    live, live_source = load_live_fills(store, algo, day)
    replay = load_blotter_fills(store, algo.resolved_replay(), day)
    replay_source = "algo_replay" if replay is not None else "missing"
    replay_fills = replay or []
    matched, live_only, replay_only = _pair(live, replay_fills)

    slips: list[float] = []
    pnl_gaps: list[float] = []
    rows: list[dict[str, Any]] = []
    for left, right in matched:
        slip = left.price - right.price
        slips.append(slip)
        live_pnl = left.realized_pnl
        replay_pnl = right.realized_pnl
        gap = None
        if live_pnl is not None and replay_pnl is not None:
            gap = live_pnl - replay_pnl
            pnl_gaps.append(gap)
        rows.append(
            {
                "status": "matched",
                "symbol": left.symbol,
                "local_symbol": left.local_symbol,
                "side": _side(left.side),
                "qty": left.qty,
                "book": left.book.value,
                "live_price": left.price,
                "replay_price": right.price,
                "price_delta": round(slip, 6),
                "live_pnl": live_pnl,
                "replay_pnl": replay_pnl,
                "pnl_delta": gap,
                "live_time": left.time.isoformat(),
                "replay_time": right.time.isoformat(),
            }
        )
    for left in live_only:
        rows.append(
            {
                "status": "live_only",
                "symbol": left.symbol,
                "local_symbol": left.local_symbol,
                "side": _side(left.side),
                "qty": left.qty,
                "book": left.book.value,
                "live_price": left.price,
                "replay_price": None,
                "price_delta": None,
                "live_pnl": left.realized_pnl,
                "replay_pnl": None,
                "pnl_delta": None,
                "live_time": left.time.isoformat(),
                "replay_time": None,
            }
        )
    for right in replay_only:
        rows.append(
            {
                "status": "replay_only",
                "symbol": right.symbol,
                "local_symbol": right.local_symbol,
                "side": _side(right.side),
                "qty": right.qty,
                "book": right.book.value,
                "live_price": None,
                "replay_price": right.price,
                "price_delta": None,
                "live_pnl": None,
                "replay_pnl": right.realized_pnl,
                "pnl_delta": None,
                "live_time": None,
                "replay_time": right.time.isoformat(),
            }
        )

    live_stats = _stats(live, day)
    replay_stats = _stats(replay_fills, day)
    return {
        "algo": algo.model_dump(mode="json"),
        "date": day.isoformat(),
        "live_source": live_source,
        "replay_source": replay_source,
        "live": live_stats,
        "replay": replay_stats,
        "deltas": {
            "fills": live_stats["fills"] - replay_stats["fills"],
            "qty": live_stats["qty"] - replay_stats["qty"],
            "notional": live_stats["notional"] - replay_stats["notional"],
            "commission": live_stats["commission"] - replay_stats["commission"],
            "realized_pnl": live_stats["realized_pnl"] - replay_stats["realized_pnl"],
        },
        "matched": len(matched),
        "live_only": len(live_only),
        "replay_only": len(replay_only),
        "avg_price_delta": round(sum(slips) / len(slips), 6) if slips else None,
        "avg_pnl_delta": round(sum(pnl_gaps) / len(pnl_gaps), 4) if pnl_gaps else None,
        "rows": rows,
        "live_fills": [execution_dict(e) for e in live],
        "replay_fills": [execution_dict(e) for e in replay_fills],
    }


def list_compare_days(store: ReportStore, registry: AlgoRegistry) -> list[str]:
    found: set[date] = set()
    prefixes = {f"{ALGOS_PREFIX}/"}
    for algo in registry.algos:
        prefixes.add(algo.resolved_live().rstrip("/") + "/")
        prefixes.add(algo.resolved_replay().rstrip("/") + "/")
    for prefix in prefixes:
        for key in store.list_keys(prefix):
            if not key.endswith("/_SUCCESS"):
                continue
            parsed = day_from_algo_success(key)
            if parsed is not None:
                found.add(parsed[2])
                continue
            marker = "/dt="
            if marker in key:
                tail = key.rsplit(marker, 1)[-1][:10]
                try:
                    found.add(date.fromisoformat(tail))
                except ValueError:
                    continue
    from waystone3.ibkr.reader import list_published_days

    found.update(list_published_days(store))
    return [d.isoformat() for d in sorted(found)]


def publish_blotter(
    store: ReportStore,
    prefix: str,
    day: date,
    fills: list[Execution],
    generated_at: datetime | None = None,
) -> None:
    del generated_at
    from waystone3.ibkr.algo_paths import blotter_summary_key

    exec_body = "".join(row.model_dump_json() + "\n" for row in fills)
    store.put(blotter_executions_key(prefix, day), exec_body.encode(), "application/x-ndjson")
    summary = summarize(day.isoformat(), fills)
    store.put(
        blotter_summary_key(prefix, day),
        (summary.model_dump_json(indent=2) + "\n").encode(),
        "application/json",
    )
    store.put(blotter_success_key(prefix, day), b"ok\n", "text/plain")
