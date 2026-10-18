"""Backtest Sandbox for Claude Cowork (MCP) and GrokBot.

Allows operators to run isolated, read-only sandbox backtests for HQ futures strategies
(`nq_v221`, `es_v221`, `r2_mnq`) and research catalog sleeves against two explicit data
sources:
  1. **GCS Historical Data** (`source="gcs"`):
     Reads continuous 1-minute futures bars (`NSDQ250/FUTURES_1MIN/<ROOT>/...`),
     daily OHLC (`NSDQ250/<SYMBOL>_daily_ohlc_*.csv`), or cached local `wsbt` mirrors from
     `gs://$IBKR_REPORTS_BUCKET` (via `ReportStore` or GCS client) over a requested date
     window (`start` .. `end`).
  2. **IBKR Daily / Historical Data through the Bound Paper Account** (`source="ibkr"`):
     Connects to IBKR Gateway using the strategy's **exact bound paper account**
     (`AccountBinding`: `account_id`, `client_id`, `ibkr_host`, `ibkr_port`) via
     `ibkr_futures.py` (or runs `v221_futures_live.py --reconcile <days>`), pulls historical
     bars from that same IBKR paper session, and replays the locked `V221Engine` / `R2Engine`.

Invariants:
  - **Immutable Thresholds**: All signal, gate, profit-take, and stop thresholds are 100%
    locked (`assert_no_threshold_changes`).
  - **Sandbox Isolation**: Sandbox runs never place orders, never mutate live `engine_*.json`
    state, and store their run artifacts under `sandbox/v1/runs/<run_id>.json` in the report
    store (or local sandbox directory).
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import math
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from waystone3.hq.strategy_runner import (
    STRATEGY_SPECS,
    HqStrategyRunner,
    StrategySpec,
    assert_no_threshold_changes,
    resolve_strategy_code,
    validate_paper_account,
)
from waystone3.ibkr.store import ReportStore, build_report_store_from_env
from waystone3.ibkr.timeutil import NY
from waystone3.research.nsdq250 import futures_continuous_key
from waystone3.research.paths import NSDQ250_PREFIX, toolkit_root

SANDBOX_PREFIX = "sandbox/v1/runs"
SANDBOX_INDEX_KEY = "sandbox/v1/index.json"


@dataclass
class SandboxMetrics:
    strategy_code: str
    instrument_root: str
    data_source: str  # "gcs" | "ibkr"
    account_id: str
    client_id: int
    contracts: int
    bars_count: int
    sessions_count: int
    start_date: str | None
    end_date: str | None
    trades_count: int
    trades_per_session: float
    win_rate_pct: float
    gross_pnl_usd: float
    commissions_usd: float
    net_pnl_usd: float
    initial_capital_usd: float
    final_equity_usd: float
    total_return_pct: float
    max_drawdown_pct: float
    sharpe: float | None
    exit_reasons: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_bar_timestamp(raw_ts: str) -> datetime:
    cleaned = raw_ts.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        # Handle "YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DD"
        if len(cleaned) == 10:
            dt = datetime.strptime(cleaned, "%Y-%m-%d").replace(hour=9, minute=30)
        else:
            dt = datetime.strptime(cleaned[:19], "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=NY) if dt.tzinfo is None else dt.astimezone(NY)


def parse_csv_bars(
    csv_text: str,
    *,
    start: str | None = None,
    end: str | None = None,
    default_vxn: float = 20.0,
) -> list[dict[str, Any]]:
    """Parse GCS or local CSV bars (1-min or daily) into engine bar dicts."""
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        return []
    fields = {name.lower().strip(): name for name in reader.fieldnames}
    ts_col = (
        fields.get("datetime_et")
        or fields.get("ts")
        or fields.get("timestamp")
        or fields.get("datetime")
        or fields.get("date")
    )
    if ts_col is None:
        raise ValueError("CSV missing timestamp column (datetime_et, ts, timestamp, or date)")
    for req in ("open", "high", "low", "close"):
        if req not in fields:
            raise ValueError(f"CSV missing required column {req!r}")
    vol_col = fields.get("volume")
    vxn_col = fields.get("vol_price") or fields.get("vxn")

    start_d = date.fromisoformat(start[:10]) if start else None
    end_d = date.fromisoformat(end[:10]) if end else None

    bars: list[dict[str, Any]] = []
    for row in reader:
        raw_ts = row.get(ts_col, "")
        if not raw_ts:
            continue
        dt = _parse_bar_timestamp(raw_ts)
        d = dt.date()
        if start_d and d < start_d:
            continue
        if end_d and d > end_d:
            continue
        bars.append(
            {
                "datetime_et": dt,
                "open": float(row[fields["open"]]),
                "high": float(row[fields["high"]]),
                "low": float(row[fields["low"]]),
                "close": float(row[fields["close"]]),
                "volume": float(row[vol_col]) if vol_col and row.get(vol_col) else 1000.0,
                "vol_price": (
                    float(row[vxn_col])
                    if vxn_col and row.get(vxn_col) not in (None, "")
                    else default_vxn
                ),
            }
        )
    bars.sort(key=lambda b: b["datetime_et"])
    return bars


def compute_sandbox_metrics(
    spec: StrategySpec,
    *,
    data_source: str,
    account_id: str,
    client_id: int,
    contracts: int,
    bars: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    initial_capital: float = 100_000.0,
) -> tuple[SandboxMetrics, list[dict[str, Any]]]:
    """Compute equity curve, drawdown, Sharpe, win rate, and trade P&L with locked specs."""
    mult = spec.point_multiplier
    comm_per_side = spec.commission_per_side
    comm_round_turn = comm_per_side * 2.0 if spec.engine_kind == "r2_mnq" else comm_per_side

    sessions = sorted(
        {
            (
                b["datetime_et"].date().isoformat()
                if isinstance(b["datetime_et"], datetime)
                else _parse_bar_timestamp(str(b["datetime_et"])).date().isoformat()
            )
            for b in bars
        }
    )
    start_date = sessions[0] if sessions else None
    end_date = sessions[-1] if sessions else None

    equity = initial_capital
    peak = initial_capital
    max_dd_pct = 0.0
    gross_total = 0.0
    comm_total = 0.0
    wins = 0
    daily_pnl: dict[str, float] = {}
    enriched_trades: list[dict[str, Any]] = []

    for idx, t in enumerate(trades, start=1):
        pts = float(t.get("pts") or 0.0)
        gross = round(pts * mult * contracts, 2)
        comm = round(comm_round_turn * contracts, 2)
        net = round(gross - comm, 2)
        gross_total += gross
        comm_total += comm
        if net > 0:
            wins += 1
        equity = round(equity + net, 2)
        if equity > peak:
            peak = equity
        dd = ((peak - equity) / peak * 100.0) if peak > 0 else 0.0
        if dd > max_dd_pct:
            max_dd_pct = dd
        exit_time = str(t.get("exit_time") or "")
        day_str = exit_time[:10] if len(exit_time) >= 10 else (end_date or "unknown")
        daily_pnl[day_str] = daily_pnl.get(day_str, 0.0) + net
        enriched_trades.append(
            {
                "seq": idx,
                "direction": t.get("direction"),
                "contracts": contracts,
                "entry_time": t.get("entry_time"),
                "entry_price": t.get("entry_price"),
                "exit_time": exit_time,
                "exit_price": t.get("exit_price"),
                "points": round(pts, 4),
                "gross_pnl_usd": gross,
                "commission_usd": comm,
                "net_pnl_usd": net,
                "equity_after_usd": equity,
                "reason": t.get("reason"),
            }
        )

    n_trades = len(enriched_trades)
    n_sessions = len(sessions)
    win_rate = round((wins / n_trades) * 100.0, 2) if n_trades > 0 else 0.0
    net_total = round(gross_total - comm_total, 2)
    total_return_pct = round((net_total / initial_capital) * 100.0, 2)

    sharpe: float | None = None
    pnl_vals = list(daily_pnl.values())
    if len(pnl_vals) > 1:
        mean_d = sum(pnl_vals) / len(pnl_vals)
        var_d = sum((x - mean_d) ** 2 for x in pnl_vals) / len(pnl_vals)
        std_d = math.sqrt(var_d)
        if std_d > 0:
            sharpe = round((mean_d / std_d) * math.sqrt(252.0), 2)

    exit_counts = dict(Counter(str(t.get("reason") or "unknown") for t in enriched_trades))

    metrics = SandboxMetrics(
        strategy_code=spec.code,
        instrument_root=spec.instrument_root,
        data_source=data_source,
        account_id=account_id,
        client_id=client_id,
        contracts=contracts,
        bars_count=len(bars),
        sessions_count=n_sessions,
        start_date=start_date,
        end_date=end_date,
        trades_count=n_trades,
        trades_per_session=round(n_trades / max(1, n_sessions), 2),
        win_rate_pct=win_rate,
        gross_pnl_usd=round(gross_total, 2),
        commissions_usd=round(comm_total, 2),
        net_pnl_usd=net_total,
        initial_capital_usd=initial_capital,
        final_equity_usd=round(initial_capital + net_total, 2),
        total_return_pct=total_return_pct,
        max_drawdown_pct=round(max_dd_pct, 2),
        sharpe=sharpe,
        exit_reasons=exit_counts,
    )
    return metrics, enriched_trades


class HqBacktestSandbox:
    """Isolated Backtest Sandbox for Claude Cowork (MCP) and GrokBot.

    Supports:
      1. ``run_gcs_backtest``: Replays locked HQ strategy engines (`nq_v221`, `es_v221`,
         `r2_mnq`) over historical data in GCS (`gs://$IBKR_REPORTS_BUCKET/NSDQ250/...`)
         or local `wsbt` data mirrors.
      2. ``run_ibkr_backtest``: Connects to IBKR Gateway using the strategy's **same bound
         paper account** (`account_id` + `client_id`), fetches daily/intraday historical
         bars from IBKR for that contract, and runs the sandbox backtest replay.
    """

    def __init__(
        self,
        runner: HqStrategyRunner | None = None,
        store: ReportStore | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self.runner = runner if runner is not None else HqStrategyRunner()
        self.store = store if store is not None else build_report_store_from_env()
        self.data_dir = data_dir or (toolkit_root() / "data")
        self._memory_runs: list[dict[str, Any]] = []

    # ── Data Discovery & Loading (GCS Bucket + Local Mirror) ─────────────────
    def list_datasets(self) -> dict[str, Any]:
        """List historical datasets available in the GCS bucket / local mirror and
        show the bound IBKR paper account for each strategy."""
        gcs_keys: list[str] = []
        if self.store is not None:
            with contextlib.suppress(Exception):
                gcs_keys = sorted(self.store.list_keys(NSDQ250_PREFIX))[:100]

        local_intraday: list[str] = []
        intraday_dir = self.data_dir / "intraday"
        if intraday_dir.is_dir():
            local_intraday = sorted(p.name for p in intraday_dir.glob("*.csv"))

        local_daily: list[str] = []
        daily_dir = self.data_dir / "daily"
        if daily_dir.is_dir():
            local_daily = sorted(p.name for p in daily_dir.glob("*.csv"))

        accounts = {
            code: self.runner.binding(code).to_dict() for code in STRATEGY_SPECS
        }
        return {
            "gcs_prefix": NSDQ250_PREFIX,
            "gcs_objects": gcs_keys,
            "local_intraday_csvs": local_intraday,
            "local_daily_csvs": local_daily,
            "bound_ibkr_accounts": accounts,
            "thresholds_locked": True,
        }

    def _load_gcs_bars(
        self,
        spec: StrategySpec,
        *,
        gcs_key: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        root = spec.instrument_root.upper()
        candidate_keys = (
            [gcs_key]
            if gcs_key
            else [
                futures_continuous_key(root),
                f"{NSDQ250_PREFIX}/FUTURES_1MIN/{root}_1min.csv",
                f"{NSDQ250_PREFIX}/{root}_1min.csv",
            ]
        )

        # 1. Check ReportStore (GCS bucket or LocalFsStore mirror)
        if self.store is not None:
            for key in candidate_keys:
                if not key:
                    continue
                raw_bytes = self.store.get(key)
                if raw_bytes:
                    bars = parse_csv_bars(raw_bytes.decode(), start=start, end=end)
                    return bars, f"store://{key}"
            # Also check any matching daily OHLC file in store under NSDQ250/
            for key in self.store.list_keys(f"{NSDQ250_PREFIX}/{root}"):
                if key.endswith(".csv"):
                    raw_bytes = self.store.get(key)
                    if raw_bytes:
                        bars = parse_csv_bars(raw_bytes.decode(), start=start, end=end)
                        return bars, f"store://{key}"

        # 2. Check local wsbt data/intraday or data/daily mirror
        for local_candidate in (
            self.data_dir / "intraday" / f"{root}_1min.csv",
            self.data_dir / "daily" / f"{root}.csv",
        ):
            if local_candidate.is_file():
                bars = parse_csv_bars(local_candidate.read_text(), start=start, end=end)
                return bars, f"file://{local_candidate}"

        # 3. Direct GCS download via nsdq250 helper if bucket credentials are active
        try:
            from waystone3.research.nsdq250 import sync_intraday_root

            synced = sync_intraday_root(root, self.data_dir)
            if synced is not None and synced.is_file():
                bars = parse_csv_bars(synced.read_text(), start=start, end=end)
                return bars, f"gs://{futures_continuous_key(root)}"
        except Exception:
            pass

        raise FileNotFoundError(
            f"No historical CSV found for {root} in GCS store ({candidate_keys[0]}) "
            f"or local data dir ({self.data_dir})"
        )

    # ── Persistence of Sandbox Runs ──────────────────────────────────────────
    def _record_run(self, record: dict[str, Any]) -> dict[str, Any]:
        self._memory_runs.insert(0, record)
        del self._memory_runs[50:]
        if self.store is not None:
            run_id = str(record["run_id"])
            self.store.put(
                f"{SANDBOX_PREFIX}/{run_id}.json",
                (json.dumps(record, indent=2, default=str) + "\n").encode(),
                "application/json",
            )
            summary_row = {
                "run_id": run_id,
                "created_at": record["created_at"],
                "strategy_code": record["strategy_code"],
                "data_source": record["data_source"],
                "account_id": record["account_id"],
                "metrics": record["metrics"],
            }
            existing: list[dict[str, Any]] = []
            raw_idx = self.store.get(SANDBOX_INDEX_KEY)
            if raw_idx:
                with contextlib.suppress(Exception):
                    loaded = json.loads(raw_idx.decode())
                    if isinstance(loaded, list):
                        existing = loaded
            existing = [r for r in existing if r.get("run_id") != run_id]
            existing.insert(0, summary_row)
            del existing[100:]
            self.store.put(
                SANDBOX_INDEX_KEY,
                (json.dumps(existing, indent=2, default=str) + "\n").encode(),
                "application/json",
            )
        return record

    def list_runs(self, limit: int = 25) -> list[dict[str, Any]]:
        """List recent sandbox backtest runs."""
        if self.store is not None:
            raw_idx = self.store.get(SANDBOX_INDEX_KEY)
            if raw_idx:
                with contextlib.suppress(Exception):
                    loaded = json.loads(raw_idx.decode())
                    if isinstance(loaded, list):
                        return loaded[:limit]
        return [
            {
                "run_id": r["run_id"],
                "created_at": r["created_at"],
                "strategy_code": r["strategy_code"],
                "data_source": r["data_source"],
                "account_id": r["account_id"],
                "metrics": r["metrics"],
            }
            for r in self._memory_runs[:limit]
        ]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Load full details (including trade list) of one sandbox run."""
        if self.store is not None:
            raw = self.store.get(f"{SANDBOX_PREFIX}/{run_id}.json")
            if raw:
                with contextlib.suppress(Exception):
                    data = json.loads(raw.decode())
                    if isinstance(data, dict):
                        return data
        for r in self._memory_runs:
            if r.get("run_id") == run_id:
                return r
        return None

    # ── 1. Sandbox Backtest on GCS Historical Data ───────────────────────────
    def run_gcs_backtest(
        self,
        strategy: str,
        *,
        start: str | None = None,
        end: str | None = None,
        gcs_key: str | None = None,
        contracts: int | None = None,
        fng_prior_day: float | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run a sandbox backtest of an HQ strategy against historical data in GCS.

        Uses the strategy's bound paper account metadata (`account_id`, `client_id`) and
        strictly enforces locked strategy thresholds.
        """
        assert_no_threshold_changes(overrides)
        code = resolve_strategy_code(strategy)
        spec = STRATEGY_SPECS[code]
        bind = self.runner.binding(code)
        qty = int(contracts) if contracts is not None else bind.contracts
        if qty < 1 or qty > 10:
            raise ValueError("contracts must be between 1 and 10")

        bars, dataset_uri = self._load_gcs_bars(
            spec, gcs_key=gcs_key, start=start, end=end
        )
        if not bars:
            raise ValueError(
                f"No bars matched window start={start!r} end={end!r} in {dataset_uri}"
            )

        engine = self.runner.load_engine_instance(code, use_saved_state=False)
        for b in bars:
            dt = b["datetime_et"]
            fng_lookup = None
            all_dates = None
            if fng_prior_day is not None:
                from datetime import timedelta

                pd_date = dt.date() - timedelta(days=1)
                fng_lookup = {pd_date: float(fng_prior_day)}
                all_dates = [pd_date]
            engine.on_bar(
                dt=dt,
                open_=b["open"],
                high=b["high"],
                low=b["low"],
                close=b["close"],
                volume=b["volume"],
                vol_price=b["vol_price"],
                fng_lookup=fng_lookup,
                all_fng_dates=all_dates,
                backtest_fill=True,
            )

        raw_trades = list(getattr(engine, "trades", []))
        metrics, enriched_trades = compute_sandbox_metrics(
            spec,
            data_source="gcs",
            account_id=bind.account_id,
            client_id=bind.client_id,
            contracts=qty,
            bars=bars,
            trades=raw_trades,
        )

        run_id = f"sbx_gcs_{code}_{uuid.uuid4().hex[:8]}"
        record = {
            "run_id": run_id,
            "created_at": datetime.now(NY).isoformat(),
            "sandbox": True,
            "strategy_code": code,
            "instrument_root": spec.instrument_root,
            "data_source": "gcs",
            "dataset_uri": dataset_uri,
            "account_id": bind.account_id,
            "client_id": bind.client_id,
            "thresholds_locked": True,
            "thresholds": spec.thresholds.to_dict(),
            "metrics": metrics.to_dict(),
            "engine_snapshot": engine.snapshot(),
            "trades": enriched_trades[-200:],
        }
        return self._record_run(record)

    # ── 2. Sandbox Backtest on IBKR Daily / Historical Data (Same Account) ───
    def run_ibkr_backtest(
        self,
        strategy: str,
        *,
        days: int = 30,
        bar_size: str = "1 min",
        use_rth: bool = False,
        account_id: str | None = None,
        contracts: int | None = None,
        fng_prior_day: float | None = None,
        bars_override: list[dict[str, Any]] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run a sandbox backtest using IBKR historical data fetched through the strategy's
        bound IBKR paper account (`IBKR_ACCOUNT_ID`, `IBKR_CLIENT_ID`).

        Connects to the same IBKR Gateway paper account that trades the strategy, verifies
        the `D...` paper account, pulls `days` of bars (`1 min` or `1 day`) for the front
        futures contract, and replays the locked `V221Engine` or `R2Engine`.
        """
        assert_no_threshold_changes(overrides)
        if days < 1 or days > 365:
            raise ValueError("days must be between 1 and 365")

        code = resolve_strategy_code(strategy)
        spec = STRATEGY_SPECS[code]
        bind = self.runner.binding(code)
        target_account = (
            validate_paper_account(account_id)
            if account_id is not None
            else validate_paper_account(bind.account_id)
        )
        qty = int(contracts) if contracts is not None else bind.contracts
        if qty < 1 or qty > 10:
            raise ValueError("contracts must be between 1 and 10")

        contract_symbol = spec.instrument_root
        if bars_override is not None:
            bars = list(bars_override)
            dataset_uri = (
                f"ibkr://{target_account}@{bind.ibkr_host}:{bind.ibkr_port}/"
                f"{contract_symbol} ({days}D {bar_size})"
            )
        else:
            _, bk = self.runner._load_ibkr_module(code)
            bk.ACCOUNT_ID = target_account
            try:
                bk.ensure_connected()
                verified_acct = bk.verify_account()
                contract, _dte = bk.front_future(spec.instrument_root)
                contract_symbol = getattr(contract, "localSymbol", spec.instrument_root)
                df = bk.historical_bars(
                    contract,
                    duration=f"{days} D",
                    bar_size=bar_size,
                    use_rth=use_rth,
                )
                if df is None or df.empty:
                    raise RuntimeError(
                        f"IBKR returned no bars for {contract_symbol} on account {verified_acct}"
                    )
                bars = []
                for ts, row in df.iterrows():
                    dt = (
                        ts.to_pydatetime()
                        if hasattr(ts, "to_pydatetime")
                        else _parse_bar_timestamp(str(ts))
                    )
                    dt = dt.replace(tzinfo=NY) if dt.tzinfo is None else dt.astimezone(NY)
                    bars.append(
                        {
                            "datetime_et": dt,
                            "open": float(row["open"]),
                            "high": float(row["high"]),
                            "low": float(row["low"]),
                            "close": float(row["close"]),
                            "volume": float(row.get("volume", 1000.0)),
                            "vol_price": 20.0,
                        }
                    )
                dataset_uri = (
                    f"ibkr://{verified_acct}@{bind.ibkr_host}:{bind.ibkr_port}/"
                    f"{contract_symbol} ({days}D {bar_size})"
                )
            finally:
                with contextlib.suppress(Exception):
                    bk.disconnect()

        if not bars:
            raise ValueError(f"No IBKR bars available to replay for {code}")

        engine = self.runner.load_engine_instance(code, use_saved_state=False)
        for b in bars:
            dt = b["datetime_et"]
            if isinstance(dt, str):
                dt = _parse_bar_timestamp(dt)
            fng_lookup = None
            all_dates = None
            if fng_prior_day is not None:
                from datetime import timedelta

                pd_date = dt.date() - timedelta(days=1)
                fng_lookup = {pd_date: float(fng_prior_day)}
                all_dates = [pd_date]
            engine.on_bar(
                dt=dt,
                open_=float(b["open"]),
                high=float(b["high"]),
                low=float(b["low"]),
                close=float(b["close"]),
                volume=float(b.get("volume", 1000.0)),
                vol_price=float(b.get("vol_price", 20.0)),
                fng_lookup=fng_lookup,
                all_fng_dates=all_dates,
                backtest_fill=True,
            )

        raw_trades = list(getattr(engine, "trades", []))
        metrics, enriched_trades = compute_sandbox_metrics(
            spec,
            data_source="ibkr",
            account_id=target_account,
            client_id=bind.client_id,
            contracts=qty,
            bars=bars,
            trades=raw_trades,
        )

        run_id = f"sbx_ibkr_{code}_{uuid.uuid4().hex[:8]}"
        record = {
            "run_id": run_id,
            "created_at": datetime.now(NY).isoformat(),
            "sandbox": True,
            "strategy_code": code,
            "instrument_root": spec.instrument_root,
            "contract": contract_symbol,
            "data_source": "ibkr",
            "dataset_uri": dataset_uri,
            "account_id": target_account,
            "client_id": bind.client_id,
            "days_requested": days,
            "bar_size": bar_size,
            "use_rth": use_rth,
            "thresholds_locked": True,
            "thresholds": spec.thresholds.to_dict(),
            "metrics": metrics.to_dict(),
            "engine_snapshot": engine.snapshot(),
            "trades": enriched_trades[-200:],
        }
        return self._record_run(record)
