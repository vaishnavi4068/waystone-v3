"""HQ Strategy Runner & IBKR Execution Controller for agentic trading accounts.

Bridges ``waystone-v3`` (HQ MCP, REST API, and GrokBot webhook/inbox) with the
validated futures strategies in ``hq-strategy`` (``NQ_FUTURE``, ``ES_ALGO``,
``R2_MNQ_ALGO``) and their IBKR paper execution layer.

Invariant:
  Strategy signal & exit thresholds are IMMUTABLE and read verbatim from the
  validated engines in ``hq-strategy`` (``v221_engine.py`` and
  ``v221_engine_r2_mnq.py``). Neither MCP callers nor GrokBot can alter
  ``FAST``, ``SLOW``, ``SIG``, ``BRICK_MIN``, ``BRICK_MAX``, ``VOL_WINDOW``,
  ``FNG_THRESHOLD``, ``CHOP_THRESHOLD``, ``HARD_STOP_PCT``, ``PT_LEVEL_*``,
  ``CORR_*``, or ``STOP_PCT``.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import signal
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from waystone3.ibkr.timeutil import NY

LOCKED_THRESHOLD_KEYS: frozenset[str] = frozenset(
    {
        "fast",
        "slow",
        "sig",
        "ema_fast",
        "ema_slow",
        "ema_sig",
        "brick_min",
        "brick_max",
        "vol_window",
        "fng_threshold",
        "chop_threshold",
        "rolling_window_days",
        "hard_stop_pct",
        "pt_level_low",
        "pt_level_mid",
        "pt_level_high",
        "pt_min_duration",
        "going_nowhere_stop_pct",
        "afterhours_stop_pct",
        "corr_neg_threshold",
        "corr_pos_threshold",
        "stop_pct",
        "daily_loss_limit",
        "max_trades_per_day",
        "min_excess_liq",
        "bullish_threshold",
        "bearish_threshold",
    }
)


@dataclass(frozen=True)
class LockedThresholds:
    """Immutable validated engine & risk thresholds for a strategy."""

    fast_ema: int
    slow_ema: int
    signal_ema: int
    brick_min: float
    brick_max: float
    vol_window: int
    vol_spike_office_pct: float
    vol_spike_afterhours_pct: float
    fng_threshold: float
    chop_threshold: float
    rolling_window_days: int
    hard_stop_pct: float
    going_nowhere_stop_pct: float
    afterhours_stop_pct: float
    pt_level_low: float
    pt_level_mid: float
    pt_level_high: float
    pt_min_duration: int
    corr_neg_threshold: float
    corr_pos_threshold: float
    broker_stop_pct: float
    daily_loss_limit_usd: float
    max_trades_per_day: int
    min_excess_liq_usd: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StrategySpec:
    """Static definition mapping an HQ strategy code to its ``hq-strategy`` folder."""

    code: str
    name: str
    rel_dir: str
    live_script: str
    engine_kind: str  # "v221" | "r2_mnq"
    instrument_root: str
    point_multiplier: float
    commission_per_side: float
    default_contracts: int
    default_client_id: int
    default_account_id: str
    default_hold_overnight: bool
    default_flatten_at: str
    default_no_new_entries_after: str
    thresholds: LockedThresholds


V221_BASE_THRESHOLDS_NQ = LockedThresholds(
    fast_ema=45,
    slow_ema=60,
    signal_ema=20,
    brick_min=0.0,
    brick_max=10.0,
    vol_window=20,
    vol_spike_office_pct=10.0,
    vol_spike_afterhours_pct=25.0,
    fng_threshold=30.0,
    chop_threshold=0.005,
    rolling_window_days=20,
    hard_stop_pct=-0.8,
    going_nowhere_stop_pct=-0.5,
    afterhours_stop_pct=-0.4,
    pt_level_low=2.0,
    pt_level_mid=3.0,
    pt_level_high=4.0,
    pt_min_duration=3,
    corr_neg_threshold=-0.40,
    corr_pos_threshold=0.70,
    broker_stop_pct=1.0,
    daily_loss_limit_usd=2500.0,
    max_trades_per_day=12,
    min_excess_liq_usd=15000.0,
)

V221_BASE_THRESHOLDS_ES = LockedThresholds(
    fast_ema=45,
    slow_ema=60,
    signal_ema=20,
    brick_min=0.0,
    brick_max=10.0,
    vol_window=20,
    vol_spike_office_pct=10.0,
    vol_spike_afterhours_pct=25.0,
    fng_threshold=30.0,
    chop_threshold=0.005,
    rolling_window_days=20,
    hard_stop_pct=-0.8,
    going_nowhere_stop_pct=-0.5,
    afterhours_stop_pct=-0.4,
    pt_level_low=2.0,
    pt_level_mid=3.0,
    pt_level_high=4.0,
    pt_min_duration=3,
    corr_neg_threshold=-0.40,
    corr_pos_threshold=0.70,
    broker_stop_pct=0.7,
    daily_loss_limit_usd=2500.0,
    max_trades_per_day=12,
    min_excess_liq_usd=15000.0,
)

R2_MNQ_THRESHOLDS = LockedThresholds(
    fast_ema=8,
    slow_ema=20,
    signal_ema=10,
    brick_min=0.0,
    brick_max=6.5,
    vol_window=20,
    vol_spike_office_pct=10.0,
    vol_spike_afterhours_pct=25.0,
    fng_threshold=30.0,
    chop_threshold=0.06,
    rolling_window_days=20,
    hard_stop_pct=-0.6,
    going_nowhere_stop_pct=-0.5,
    afterhours_stop_pct=-0.2,
    pt_level_low=0.5,
    pt_level_mid=1.0,
    pt_level_high=1.5,
    pt_min_duration=1,
    corr_neg_threshold=-0.20,
    corr_pos_threshold=0.50,
    broker_stop_pct=0.8,
    daily_loss_limit_usd=2500.0,
    max_trades_per_day=12,
    min_excess_liq_usd=15000.0,
)

STRATEGY_SPECS: dict[str, StrategySpec] = {
    "nq_v221": StrategySpec(
        code="nq_v221",
        name="V221 Hybrid NQ Futures (Variant C)",
        rel_dir="NQ_FUTURE",
        live_script="v221_futures_live.py",
        engine_kind="v221",
        instrument_root="NQ",
        point_multiplier=20.0,
        commission_per_side=4.50,
        default_contracts=2,
        default_client_id=77,
        default_account_id="DUR842609",
        default_hold_overnight=False,
        default_flatten_at="15:55",
        default_no_new_entries_after="15:55",
        thresholds=V221_BASE_THRESHOLDS_NQ,
    ),
    "es_v221": StrategySpec(
        code="es_v221",
        name="V221 Hybrid ES Futures (Variant C)",
        rel_dir="ES_ALGO",
        live_script="v221_futures_live.py",
        engine_kind="v221",
        instrument_root="ES",
        point_multiplier=50.0,
        commission_per_side=4.50,
        default_contracts=2,
        default_client_id=53,
        default_account_id="DUR842609",
        default_hold_overnight=False,
        default_flatten_at="15:55",
        default_no_new_entries_after="15:55",
        thresholds=V221_BASE_THRESHOLDS_ES,
    ),
    "r2_mnq": StrategySpec(
        code="r2_mnq",
        name="R2 STAYINMARKET MNQ Futures (DTV221)",
        rel_dir="R2_MNQ_ALGO/R2_MNQ",
        live_script="v221_futures_live_r2.py",
        engine_kind="r2_mnq",
        instrument_root="MNQ",
        point_multiplier=2.0,
        commission_per_side=1.25,
        default_contracts=1,
        default_client_id=88,
        default_account_id="DUR842609",
        default_hold_overnight=False,
        default_flatten_at="15:55",
        default_no_new_entries_after="15:55",
        thresholds=R2_MNQ_THRESHOLDS,
    ),
}

STRATEGY_ALIASES: dict[str, str] = {
    "nq": "nq_v221",
    "nq_future": "nq_v221",
    "nq_futures": "nq_v221",
    "nq_v221": "nq_v221",
    "es": "es_v221",
    "es_algo": "es_v221",
    "es_futures": "es_v221",
    "es_v221": "es_v221",
    "r2": "r2_mnq",
    "mnq": "r2_mnq",
    "r2_mnq": "r2_mnq",
    "r2_mnq_algo": "r2_mnq",
}


def resolve_strategy_code(strategy: str) -> str:
    key = strategy.strip().lower()
    if key in STRATEGY_ALIASES:
        return STRATEGY_ALIASES[key]
    valid = ", ".join(sorted(STRATEGY_SPECS))
    raise ValueError(f"unknown strategy {strategy!r}; expected one of {valid}")


def validate_paper_account(account_id: str) -> str:
    cleaned = account_id.strip().upper()
    if not cleaned:
        raise ValueError("account_id cannot be empty")
    if not cleaned.startswith("D"):
        raise ValueError(
            f"account_id={account_id!r} refused: agentic/MCP execution only permits IBKR "
            "PAPER accounts starting with 'D' (e.g. DUR842608, DUR842609)"
        )
    return cleaned


def assert_no_threshold_changes(overrides: dict[str, Any] | None) -> None:
    """Reject any attempt to override locked strategy or risk thresholds."""
    if not overrides:
        return
    bad = sorted(k for k in overrides if k.lower() in LOCKED_THRESHOLD_KEYS)
    if bad:
        raise ValueError(
            f"strategy thresholds are immutable and cannot be changed ({', '.join(bad)})"
        )


def default_hq_strategy_root() -> Path:
    env_root = os.getenv("HQ_STRATEGY_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()
    # Default sibling checkout in /agent/repos/hq-strategy or ../hq-strategy
    here = Path(__file__).resolve()
    for candidate in (
        Path("/agent/repos/hq-strategy"),
        here.parents[4] / "hq-strategy",
        here.parents[3] / "hq-strategy",
    ):
        if candidate.exists():
            return candidate
    return Path("/agent/repos/hq-strategy")


@dataclass
class AccountBinding:
    strategy_code: str
    account_id: str
    client_id: int
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 4002
    contracts: int = 1
    dry_run: bool = False
    hold_overnight: bool = False
    updated_at: str = field(default_factory=lambda: datetime.now(NY).isoformat())
    updated_by: str = "system"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HqStrategyRunner:
    """Manages HQ strategies (`nq_v221`, `es_v221`, `r2_mnq`), account bindings,
    process supervision, kill-switches, bar evaluation, and IBKR paper execution.
    """

    def __init__(
        self,
        repo_root: Path | None = None,
        state_file: Path | None = None,
    ) -> None:
        self.repo_root = (repo_root or default_hq_strategy_root()).resolve()
        parent_dir = self.repo_root.parent if self.repo_root.exists() else Path("/tmp")
        self.state_file = state_file or (
            self.repo_root / ".waystone_agentic_accounts.json"
            if os.access(parent_dir, os.W_OK)
            else Path("/tmp/waystone_agentic_accounts.json")
        )
        self._bindings: dict[str, AccountBinding] = self._load_bindings()

    # ── Account bindings (persisted) ─────────────────────────────────────────
    def _default_binding(self, spec: StrategySpec) -> AccountBinding:
        return AccountBinding(
            strategy_code=spec.code,
            account_id=os.getenv("IBKR_ACCOUNT_ID", spec.default_account_id),
            client_id=spec.default_client_id,
            ibkr_host=os.getenv("IBKR_HOST", "127.0.0.1"),
            ibkr_port=int(os.getenv("IBKR_PORT", "4002")),
            contracts=spec.default_contracts,
            dry_run=False,
            hold_overnight=spec.default_hold_overnight,
        )

    def _load_bindings(self) -> dict[str, AccountBinding]:
        out = {code: self._default_binding(spec) for code, spec in STRATEGY_SPECS.items()}
        if self.state_file.is_file():
            try:
                raw = json.loads(self.state_file.read_text())
                if isinstance(raw, dict):
                    for code, item in raw.items():
                        if code in STRATEGY_SPECS and isinstance(item, dict):
                            spec = STRATEGY_SPECS[code]
                            out[code] = AccountBinding(
                                strategy_code=code,
                                account_id=validate_paper_account(
                                    str(item.get("account_id") or spec.default_account_id)
                                ),
                                client_id=int(item.get("client_id") or spec.default_client_id),
                                ibkr_host=str(item.get("ibkr_host") or "127.0.0.1"),
                                ibkr_port=int(item.get("ibkr_port") or 4002),
                                contracts=max(
                                    1, int(item.get("contracts") or spec.default_contracts)
                                ),
                                dry_run=bool(item.get("dry_run", False)),
                                hold_overnight=bool(
                                    item.get("hold_overnight", spec.default_hold_overnight)
                                ),
                                updated_at=str(
                                    item.get("updated_at") or datetime.now(NY).isoformat()
                                ),
                                updated_by=str(item.get("updated_by") or "system"),
                            )
            except Exception:
                pass
        return out

    def _save_bindings(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {code: b.to_dict() for code, b in self._bindings.items()}
            self.state_file.write_text(json.dumps(payload, indent=2) + "\n")
        except OSError:
            pass

    def spec(self, strategy: str) -> StrategySpec:
        return STRATEGY_SPECS[resolve_strategy_code(strategy)]

    def strategy_dir(self, strategy: str) -> Path:
        spec = self.spec(strategy)
        return self.repo_root / spec.rel_dir

    def binding(self, strategy: str) -> AccountBinding:
        code = resolve_strategy_code(strategy)
        return self._bindings[code]

    def configure_account(
        self,
        strategy: str,
        *,
        account_id: str | None = None,
        client_id: int | None = None,
        ibkr_host: str | None = None,
        ibkr_port: int | None = None,
        contracts: int | None = None,
        dry_run: bool | None = None,
        hold_overnight: bool | None = None,
        actor: str = "grok_bot",
        extra_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Bind a strategy to an IBKR paper trading account and runtime sizing.

        Signal & exit thresholds are locked and cannot be modified.
        """
        assert_no_threshold_changes(extra_overrides)
        spec = self.spec(strategy)
        cur = self._bindings[spec.code]
        new_account = (
            validate_paper_account(account_id) if account_id is not None else cur.account_id
        )
        new_client_id = int(client_id) if client_id is not None else cur.client_id
        if new_client_id == 42:
            raise ValueError("client_id 42 is reserved for the options algo; use 77, 53, 88, etc.")
        for other_code, other_b in self._bindings.items():
            if other_code != spec.code and other_b.client_id == new_client_id:
                raise ValueError(
                    f"client_id {new_client_id} is already assigned to {other_code}"
                )
        new_contracts = int(contracts) if contracts is not None else cur.contracts
        if new_contracts < 1 or new_contracts > 10:
            raise ValueError("contracts must be between 1 and 10")

        updated = AccountBinding(
            strategy_code=spec.code,
            account_id=new_account,
            client_id=new_client_id,
            ibkr_host=(ibkr_host.strip() if ibkr_host else cur.ibkr_host),
            ibkr_port=int(ibkr_port) if ibkr_port is not None else cur.ibkr_port,
            contracts=new_contracts,
            dry_run=cur.dry_run if dry_run is None else bool(dry_run),
            hold_overnight=cur.hold_overnight if hold_overnight is None else bool(hold_overnight),
            updated_at=datetime.now(NY).isoformat(),
            updated_by=actor,
        )
        self._bindings[spec.code] = updated
        self._save_bindings()
        return {
            "strategy_code": spec.code,
            "binding": updated.to_dict(),
            "thresholds_locked": True,
            "thresholds": spec.thresholds.to_dict(),
        }

    # ── Inspection of local SQLite DB, state JSON, and kill switch ───────────
    def _pid_file(self, sdir: Path) -> Path:
        return sdir / "v221_logs" / "state" / "runner.pid"

    def _kill_file(self, sdir: Path) -> Path:
        return sdir / "KILL_SWITCH"

    def _process_status(self, sdir: Path) -> dict[str, Any]:
        pid_path = self._pid_file(sdir)
        if not pid_path.is_file():
            return {"running": False, "pid": None}
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            return {"running": True, "pid": pid}
        except (ValueError, ProcessLookupError):
            return {"running": False, "pid": None}
        except PermissionError:
            return {"running": True, "pid": int(pid_path.read_text().strip())}

    def _engine_snapshot(self, spec: StrategySpec, sdir: Path) -> dict[str, Any] | None:
        state_dir = sdir / "v221_logs" / "state"
        candidates = [
            state_dir / f"engine_{spec.instrument_root}_1m.json",
            state_dir / f"engine_{spec.instrument_root}_1m_rth.json",
        ]
        if state_dir.is_dir():
            candidates.extend(sorted(state_dir.glob("engine_*.json")))
        for path in candidates:
            if path.is_file():
                try:
                    raw = json.loads(path.read_text())
                    mco_list = raw.get("mco_list") or []
                    sig_list = raw.get("mco_sig_list") or []
                    renko_closes = raw.get("renko_closes") or []
                    gains = raw.get("unrealized_gain_list") or []
                    return {
                        "state_file": path.name,
                        "bars_seen": raw.get("bars_seen", 0),
                        "bricks_total": raw.get("bricks_total", 0),
                        "brick_size": raw.get("brick_size"),
                        "atr": raw.get("atr"),
                        "renko_close": renko_closes[-1] if renko_closes else None,
                        "up_box": raw.get("up_box"),
                        "down_box": raw.get("down_box"),
                        "mco": round(float(mco_list[-1]), 4) if mco_list else None,
                        "signal": round(float(sig_list[-1]), 4) if sig_list else None,
                        "algo_status": raw.get("algo_status"),
                        "vol_ma_status": raw.get("vol_ma_status"),
                        "correlation_status": raw.get("correlation_status"),
                        "chop": raw.get("cached_chop_ratio"),
                        "position": raw.get("position"),
                        "entry_price": raw.get("entry_price"),
                        "trade_duration": raw.get("trade_duration"),
                        "gain_pct": round(float(gains[-1]), 4) if gains else None,
                        "pending_signal": raw.get("pending_signal"),
                        "last_trigger": raw.get("last_trigger"),
                        "last_bar_ts": raw.get("last_bar_ts"),
                        "last_gate": raw.get("last_gate"),
                    }
                except Exception:
                    continue
        return None

    def _db_summary(self, sdir: Path) -> dict[str, Any]:
        db_path = sdir / "v221_logs" / "v221_futures.db"
        if not db_path.is_file():
            return {
                "db_exists": False,
                "open_trade": None,
                "recent_trades": [],
                "today_realized_pnl": 0.0,
            }
        try:
            con = sqlite3.connect(str(db_path), timeout=5)
            con.row_factory = sqlite3.Row
            open_row = con.execute(
                "SELECT id, trade_date, local_symbol, direction, qty, entry_ts, entry_px, "
                "stop_px, stop_order_id, oca_group, stop_confirmed "
                "FROM trades WHERE exit_ts IS NULL AND entry_px IS NOT NULL "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            recent = con.execute(
                "SELECT id, trade_date, local_symbol, direction, qty, entry_ts, entry_px, "
                "exit_ts, exit_px, exit_reason, points, gross_pnl, commission, net_pnl "
                "FROM trades ORDER BY id DESC LIMIT 5"
            ).fetchall()
            today_str = datetime.now(NY).date().isoformat()
            today_pnl = con.execute(
                "SELECT COALESCE(SUM(net_pnl), 0.0), COUNT(*) FROM trades "
                "WHERE trade_date = ? AND exit_ts IS NOT NULL",
                (today_str,),
            ).fetchone()
            con.close()
            return {
                "db_exists": True,
                "open_trade": dict(open_row) if open_row else None,
                "recent_trades": [dict(r) for r in recent],
                "today_realized_pnl": float(today_pnl[0]) if today_pnl else 0.0,
                "today_closed_trades": int(today_pnl[1]) if today_pnl else 0,
            }
        except Exception as exc:
            return {"db_exists": True, "error": str(exc), "open_trade": None, "recent_trades": []}

    def _latest_events(self, sdir: Path, limit: int = 10) -> list[dict[str, Any]]:
        log_dir = sdir / "v221_logs"
        if not log_dir.is_dir():
            return []
        files = sorted(log_dir.glob("events_*.jsonl"))
        if not files:
            return []
        lines = files[-1].read_text(errors="replace").splitlines()[-limit:]
        out: list[dict[str, Any]] = []
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def status(self, strategy: str | None = None) -> dict[str, Any]:
        """Return runtime status, locked thresholds, account binding, DB trades, and kill-switch."""
        codes = [resolve_strategy_code(strategy)] if strategy else list(STRATEGY_SPECS)
        items: list[dict[str, Any]] = []
        for code in codes:
            spec = STRATEGY_SPECS[code]
            sdir = self.strategy_dir(code)
            kill_file = self._kill_file(sdir)
            proc = self._process_status(sdir)
            bind = self._bindings[code]
            items.append(
                {
                    "strategy_code": code,
                    "name": spec.name,
                    "instrument_root": spec.instrument_root,
                    "point_multiplier": spec.point_multiplier,
                    "commission_per_side": spec.commission_per_side,
                    "strategy_dir": str(sdir),
                    "installed": (sdir / spec.live_script).is_file(),
                    "kill_switch_active": kill_file.exists(),
                    "process": proc,
                    "account_binding": bind.to_dict(),
                    "thresholds_locked": True,
                    "thresholds": spec.thresholds.to_dict(),
                    "engine_state": self._engine_snapshot(spec, sdir),
                    "db": self._db_summary(sdir),
                    "recent_events": self._latest_events(sdir, limit=5),
                }
            )
        if strategy:
            return items[0]
        return {
            "hq_strategy_root": str(self.repo_root),
            "thresholds_locked": True,
            "strategies": items,
        }

    # ── Process & Kill-Switch Lifecycle ──────────────────────────────────────
    def build_env(self, strategy: str) -> dict[str, str]:
        """Construct the environment variables for ``v221_futures_live.py``."""
        spec = self.spec(strategy)
        bind = self._bindings[spec.code]
        validate_paper_account(bind.account_id)
        env = dict(os.environ)
        env.update(
            {
                "IBKR_HOST": bind.ibkr_host,
                "IBKR_PORT": str(bind.ibkr_port),
                "IBKR_CLIENT_ID": str(bind.client_id),
                "IBKR_ACCOUNT_ID": bind.account_id,
                "V221_ROOT": spec.instrument_root,
                "V221_CONTRACTS": str(bind.contracts),
                "V221_BAR_MIN": "1",
                "V221_RTH_ONLY": "0",
                "V221_PARAMS_CONFIRMED": "1",
                "V221_HOLD_OVERNIGHT": "1" if bind.hold_overnight else "0",
                "V221_FLATTEN_AT": spec.default_flatten_at,
                "V221_NO_NEW_ENTRIES_AFTER": spec.default_no_new_entries_after,
                "V221_STOP_PCT": str(spec.thresholds.broker_stop_pct),
                "V221_DAILY_LOSS": str(int(spec.thresholds.daily_loss_limit_usd)),
                "V221_MAX_TRADES": str(spec.thresholds.max_trades_per_day),
                "V221_MIN_EXCESS_LIQ": str(int(spec.thresholds.min_excess_liq_usd)),
                "V221_DRY_RUN": "1" if bind.dry_run else "0",
            }
        )
        return env

    def halt_strategy(
        self,
        strategy: str,
        *,
        reason: str = "halted via agentic control",
        stop_process: bool = False,
    ) -> dict[str, Any]:
        """Engage the strategy's ``KILL_SWITCH`` so the next cycle flattens and halts."""
        spec = self.spec(strategy)
        sdir = self.strategy_dir(spec.code)
        sdir.mkdir(parents=True, exist_ok=True)
        kill_file = self._kill_file(sdir)
        payload = {
            "halted_at": datetime.now(NY).isoformat(),
            "reason": reason,
            "strategy_code": spec.code,
        }
        kill_file.write_text(json.dumps(payload) + "\n")
        proc = self._process_status(sdir)
        stopped_pid = None
        if stop_process and proc["running"] and proc["pid"]:
            try:
                os.kill(int(proc["pid"]), signal.SIGTERM)
                stopped_pid = proc["pid"]
            except OSError:
                pass
        return {
            "strategy_code": spec.code,
            "kill_switch_active": True,
            "kill_switch_path": str(kill_file),
            "reason": reason,
            "stopped_pid": stopped_pid,
            "process": self._process_status(sdir),
        }

    def resume_strategy(self, strategy: str) -> dict[str, Any]:
        """Clear the strategy's ``KILL_SWITCH`` file so it can resume taking entries."""
        spec = self.spec(strategy)
        sdir = self.strategy_dir(spec.code)
        kill_file = self._kill_file(sdir)
        was_active = kill_file.exists()
        if was_active:
            kill_file.unlink()
        return {
            "strategy_code": spec.code,
            "kill_switch_active": False,
            "was_active": was_active,
            "process": self._process_status(sdir),
        }

    def start_strategy(
        self,
        strategy: str,
        *,
        account_id: str | None = None,
        contracts: int | None = None,
        dry_run: bool | None = None,
        hold_overnight: bool | None = None,
        spawn: bool = True,
        actor: str = "grok_bot",
    ) -> dict[str, Any]:
        """Configure account/sizing, clear ``KILL_SWITCH``, and start the strategy daemon."""
        spec = self.spec(strategy)
        if any(v is not None for v in (account_id, contracts, dry_run, hold_overnight)):
            self.configure_account(
                spec.code,
                account_id=account_id,
                contracts=contracts,
                dry_run=dry_run,
                hold_overnight=hold_overnight,
                actor=actor,
            )
        sdir = self.strategy_dir(spec.code)
        script = sdir / spec.live_script
        if not script.is_file():
            raise FileNotFoundError(f"strategy script not found: {script}")

        # Clear kill switch before launch
        self.resume_strategy(spec.code)
        proc_info = self._process_status(sdir)
        if proc_info["running"]:
            return {
                "strategy_code": spec.code,
                "status": "already_running",
                "pid": proc_info["pid"],
                "account_binding": self._bindings[spec.code].to_dict(),
                "thresholds_locked": True,
            }

        env = self.build_env(spec.code)
        cmd = [sys.executable, "-u", str(script)]
        pid: int | None = None
        if spawn:
            state_dir = sdir / "v221_logs" / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            stdout_path = sdir / "v221_logs" / "daemon_stdout.log"
            with stdout_path.open("a", encoding="utf-8") as out_fh:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(sdir),
                    env=env,
                    stdout=out_fh,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                pid = proc.pid
            self._pid_file(sdir).write_text(f"{pid}\n")

        return {
            "strategy_code": spec.code,
            "status": "started" if spawn else "staged",
            "pid": pid,
            "command": cmd,
            "account_binding": self._bindings[spec.code].to_dict(),
            "thresholds_locked": True,
            "thresholds": spec.thresholds.to_dict(),
        }

    def run_selftest(self, strategy: str) -> dict[str, Any]:
        """Run the strategy's offline ``--selftest`` and verify locked thresholds."""
        spec = self.spec(strategy)
        sdir = self.strategy_dir(spec.code)
        script = sdir / spec.live_script
        if not script.is_file():
            raise FileNotFoundError(f"strategy script not found: {script}")
        env = self.build_env(spec.code)
        res = subprocess.run(
            [sys.executable, str(script), "--selftest"],
            cwd=str(sdir),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return {
            "strategy_code": spec.code,
            "ok": res.returncode == 0,
            "returncode": res.returncode,
            "output": (res.stdout + "\n" + res.stderr).strip()[-2000:],
            "thresholds": spec.thresholds.to_dict(),
        }

    # ── Pure In-Memory Engine Loader & Bar Evaluator (Zero Threshold Drift) ──
    def load_engine_instance(self, strategy: str, *, use_saved_state: bool = True) -> Any:
        """Load the exact ``V221Engine`` or ``R2Engine`` from ``hq-strategy``."""
        spec = self.spec(strategy)
        sdir = self.strategy_dir(spec.code)
        engine_py = sdir / "v221_engine.py"
        if not engine_py.is_file():
            raise FileNotFoundError(f"v221_engine.py not found in {sdir}")

        if spec.engine_kind == "r2_mnq":
            r2_py = sdir / "v221_engine_r2_mnq.py"
            mod_spec = importlib.util.spec_from_file_location(f"hq_r2_{spec.code}", r2_py)
            assert mod_spec and mod_spec.loader
            r2_mod = importlib.util.module_from_spec(mod_spec)
            sys.modules[mod_spec.name] = r2_mod
            mod_spec.loader.exec_module(r2_mod)
            r2_mod.check_engine_hash(str(engine_py))
            patched_path = Path("/tmp") / f"v221_engine_r2_patched_{os.getpid()}.py"
            eng_mod = r2_mod.build_r2_engine(
                engine_path=str(engine_py), write_to=str(patched_path)
            )
            eng_cls = r2_mod.make_r2_engine_class(eng_mod)
            flags = {"use_vxn_entries": False, "use_vxn_exits": True, "use_gate": True}
        else:
            mod_spec = importlib.util.spec_from_file_location(f"hq_v221_{spec.code}", engine_py)
            assert mod_spec and mod_spec.loader
            eng_mod = importlib.util.module_from_spec(mod_spec)
            sys.modules[mod_spec.name] = eng_mod
            mod_spec.loader.exec_module(eng_mod)
            eng_cls = eng_mod.V221Engine
            flags = {"use_vxn_entries": False, "use_vxn_exits": True, "use_gate": True}

        state_path = sdir / "v221_logs" / "state" / f"engine_{spec.instrument_root}_1m.json"
        if use_saved_state and state_path.is_file():
            return eng_cls.load(state_path, **flags)
        return eng_cls(**flags)

    def evaluate_bars(
        self,
        strategy: str,
        bars: list[dict[str, Any]],
        *,
        fng_prior_day: float | None = None,
        use_saved_state: bool = False,
        backtest_fill: bool = True,
    ) -> dict[str, Any]:
        """Run one or more bars through the strategy's exact engine with locked thresholds."""
        spec = self.spec(strategy)
        engine = self.load_engine_instance(spec.code, use_saved_state=use_saved_state)
        last_out: dict[str, Any] = {}
        signals: list[dict[str, Any]] = []
        for row in bars:
            raw_dt = row.get("datetime_et") or row.get("ts") or row.get("timestamp")
            if isinstance(raw_dt, str):
                dt = datetime.fromisoformat(raw_dt)
            elif isinstance(raw_dt, datetime):
                dt = raw_dt
            else:
                dt = datetime.now(NY)
            dt = dt.replace(tzinfo=NY) if dt.tzinfo is None else dt.astimezone(NY)

            fng_lookup = None
            all_dates = None
            if fng_prior_day is not None:
                from datetime import timedelta

                prior_d = dt.date() - timedelta(days=1)
                fng_lookup = {prior_d: float(fng_prior_day)}
                all_dates = [prior_d]

            out = engine.on_bar(
                dt=dt,
                open_=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume", 1000.0)),
                vol_price=float(row.get("vol_price", row.get("vxn", 20.0))),
                fng_lookup=fng_lookup,
                all_fng_dates=all_dates,
                backtest_fill=backtest_fill,
            )
            last_out = out
            if out.get("signal") or out.get("exit"):
                signals.append(
                    {
                        "timestamp": dt.isoformat(),
                        "signal": out.get("signal"),
                        "exit": out.get("exit"),
                        "brick": out.get("brick"),
                        "gate": out.get("gate"),
                        "gain": out.get("gain"),
                    }
                )

        return {
            "strategy_code": spec.code,
            "instrument_root": spec.instrument_root,
            "bars_evaluated": len(bars),
            "last_bar_result": last_out,
            "signals_emitted": signals,
            "engine_snapshot": engine.snapshot(),
            "trades_completed": list(getattr(engine, "trades", [])),
            "thresholds_locked": True,
            "thresholds": spec.thresholds.to_dict(),
        }

    # ── Direct IBKR Gateway Connection Helper (Paper-Only + Locked Stop%) ────
    def _load_ibkr_module(self, strategy: str) -> tuple[StrategySpec, Any]:
        spec = self.spec(strategy)
        bind = self._bindings[spec.code]
        validate_paper_account(bind.account_id)
        sdir = self.strategy_dir(spec.code)
        ib_py = sdir / "ibkr_futures.py"
        if not ib_py.is_file():
            raise FileNotFoundError(f"ibkr_futures.py not found in {sdir}")

        env = self.build_env(spec.code)
        for k, v in env.items():
            if k.startswith(("IBKR_", "V221_")):
                os.environ[k] = v

        mod_spec = importlib.util.spec_from_file_location(f"ibkr_fut_{spec.code}", ib_py)
        assert mod_spec and mod_spec.loader
        bk: Any = importlib.util.module_from_spec(mod_spec)
        sys.modules[mod_spec.name] = bk
        mod_spec.loader.exec_module(bk)
        bk.IBKR_HOST = bind.ibkr_host
        bk.IBKR_PORT = bind.ibkr_port
        bk.IBKR_CLIENT_ID = bind.client_id
        bk.ACCOUNT_ID = bind.account_id
        return spec, bk

    def ibkr_account_state(self, strategy: str = "nq_v221") -> dict[str, Any]:
        """Connect to IBKR Gateway for a strategy's bound paper account and return account,
        front contract, open positions, and resting V221 stop orders."""
        spec, bk = self._load_ibkr_module(strategy)
        bind = self._bindings[spec.code]
        try:
            bk.ensure_connected()
            verified = bk.verify_account()
            acct = bk.get_account()
            contract, dte = bk.front_future(spec.instrument_root)
            pos = bk.get_position(contract)
            all_pos = bk.all_positions()
            stops = bk.open_v221_orders()
            return {
                "connected": True,
                "strategy_code": spec.code,
                "account_id": verified,
                "client_id": bind.client_id,
                "front_contract": getattr(contract, "localSymbol", spec.instrument_root),
                "days_to_expiry": dte,
                "strategy_position": pos,
                "account": acct,
                "positions": all_pos,
                "resting_v221_orders": stops,
                "thresholds_locked": True,
            }
        except Exception as exc:
            return {
                "connected": False,
                "strategy_code": spec.code,
                "account_id": bind.account_id,
                "client_id": bind.client_id,
                "ibkr_host": bind.ibkr_host,
                "ibkr_port": bind.ibkr_port,
                "error": str(exc),
                "db": self._db_summary(self.strategy_dir(spec.code)),
            }
        finally:
            with contextlib.suppress(Exception):
                bk.disconnect()

    def ibkr_execute_signal(
        self,
        strategy: str,
        action: str,
        *,
        qty: int | None = None,
        dry_run: bool | None = None,
        reason: str = "grok_bot_mcp",
    ) -> dict[str, Any]:
        """Execute a bracket entry (``BUY`` / ``SELL``) or ``FLATTEN`` on the strategy's
        bound IBKR paper account.

        Safety invariants:
          1. Paper account only (``D...`` verified against Gateway managedAccounts).
          2. Protective GTC stop distance is ALWAYS locked to ``spec.thresholds.broker_stop_pct``
             (1.0% for NQ, 0.7% for ES, 0.8% for R2 MNQ) — callers cannot override the stop%.
          3. If ``stop_confirmed`` is False on a bracket fill, immediately flattens.
        """
        act = action.strip().upper()
        if act not in ("BUY", "SELL", "FLATTEN"):
            raise ValueError("action must be BUY, SELL, or FLATTEN")
        spec = self.spec(strategy)
        bind = self._bindings[spec.code]
        validate_paper_account(bind.account_id)
        contracts = int(qty) if qty is not None else bind.contracts
        if contracts < 1 or contracts > 10:
            raise ValueError("qty must be between 1 and 10")
        is_dry = bind.dry_run if dry_run is None else bool(dry_run)
        stop_pct = spec.thresholds.broker_stop_pct

        if is_dry:
            return {
                "ok": True,
                "dry_run": True,
                "strategy_code": spec.code,
                "account_id": bind.account_id,
                "instrument_root": spec.instrument_root,
                "action": act,
                "qty": contracts,
                "locked_stop_pct": stop_pct,
                "reason": reason,
            }

        _, bk = self._load_ibkr_module(spec.code)
        try:
            bk.ensure_connected()
            bk.verify_account()
            contract, dte = bk.front_future(spec.instrument_root)
            if act == "FLATTEN":
                res = bk.flatten(contract, root=spec.instrument_root, tag=f"MCP_{reason[:16]}")
                return {
                    "ok": True,
                    "dry_run": False,
                    "strategy_code": spec.code,
                    "account_id": bind.account_id,
                    "contract": contract.localSymbol,
                    "action": "FLATTEN",
                    "result": res,
                }

            quote = bk.get_quote(contract, wait_sec=2.0)
            ref_px = quote.get("last") or quote.get("mid") or quote.get("ask") or quote.get("bid")
            if not ref_px:
                bars = bk.historical_bars(contract, duration="1 D", bar_size="1 min")
                if not bars.empty:
                    ref_px = float(bars["close"].iloc[-1])
            if not ref_px:
                raise RuntimeError(f"cannot determine reference price for {contract.localSymbol}")

            raw_stop = (
                ref_px * (1.0 - stop_pct / 100.0)
                if act == "BUY"
                else ref_px * (1.0 + stop_pct / 100.0)
            )
            stop_px = bk.round_to_tick(raw_stop, spec.instrument_root)
            order_res = bk.place_bracket(
                contract,
                action=act,
                qty=contracts,
                stop_price=stop_px,
                root=spec.instrument_root,
            )
            if order_res.get("filled_qty", 0) > 0 and not order_res.get("stop_confirmed"):
                flatten_res = bk.flatten(
                    contract,
                    root=spec.instrument_root,
                    tag="STOP_UNCONFIRMED",
                    oca_group=order_res.get("oca_group"),
                )
                return {
                    "ok": False,
                    "strategy_code": spec.code,
                    "account_id": bind.account_id,
                    "contract": contract.localSymbol,
                    "error": (
                        "protective stop was not confirmed by IBKR; position flattened immediately"
                    ),
                    "order": order_res,
                    "flatten": flatten_res,
                }
            return {
                "ok": True,
                "dry_run": False,
                "strategy_code": spec.code,
                "account_id": bind.account_id,
                "contract": contract.localSymbol,
                "days_to_expiry": dte,
                "action": act,
                "qty": contracts,
                "locked_stop_pct": stop_pct,
                "stop_price": stop_px,
                "order": order_res,
            }
        finally:
            with contextlib.suppress(Exception):
                bk.disconnect()
