"""Context Language Model (CLM) layer for HQ Futures Strategies (Claude Cowork & GrokBot).

Fuses five live context streams across ``waystone-v3`` and ``hq-strategy``:
  1. Intraday Engine Micro-State (Renko boxes, MCO/signal, VXN regime, 20d chop)
  2. Macro & Narrative Sentiment Gate (6 gates, F&G, VIX/VIX3M, COT, scored headlines)
  3. Live Execution & Microstructure Telemetry (slippage, fill latency, stop confirmation, sync)
  4. Workbook Stage-Gate KPIs (WEEK / MTD / ITD gates and red/amber/green counts)
  5. Agentic IBKR Paper Account State (account binding, contract size, realized loss vs cap)

Invariants enforced in code:
  - **Immutable Thresholds**: No strategy signal, gate, profit-take, or stop threshold can ever
    be modified by the Context Language Model, Claude Cowork, or GrokBot.
  - **Downward-Only Sizing**: Context can only preserve capital (``TRADE`` at <= 1.0x validated
    contracts, ``REDUCE`` to 1 contract, or ``STAND_DOWN`` / ``KILL_SWITCH``). It never
    inflates contract size above the strategy's validated default.
  - **Key-Optional LLM**: Uses structured Claude output (`claude-opus-4-8`) when configured
    (or via an injected ``complete_fn`` in tests) and always provides a deterministic,
    zero-hallucination quantitative synthesis even when no API key is set.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from waystone3.alerts.channels import GrokBotChannel
from waystone3.hq.reader import HqReader
from waystone3.hq.strategy_runner import (
    STRATEGY_SPECS,
    HqStrategyRunner,
    StrategySpec,
    assert_no_threshold_changes,
    resolve_strategy_code,
)
from waystone3.ibkr.timeutil import NY
from waystone3.llm import build_async_client

DEFAULT_CLM_MODEL = os.getenv("WAYSTONE_AGENT_MODEL", "claude-opus-4-8")

SyncCompleter = Callable[[str, str, dict[str, Any]], dict[str, Any]]
AsyncCompleter = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]

_CLM_SYSTEM_PROMPT = (
    "You are the Waystone HQ Context Language Model (CLM) risk supervisor and desk co-pilot "
    "operating for Claude Cowork and GrokBot. You supervise three validated CME futures "
    "strategies in hq-strategy: nq_v221 (NQ $20/pt), es_v221 (ES $50/pt), and r2_mnq "
    "(MNQ $2/pt STAYINMARKET).\n"
    "CRITICAL RULES:\n"
    "1. Strategy signal, gate, profit-take, and stop thresholds are 100% LOCKED. Never propose "
    "changing any threshold.\n"
    "2. Downward-only sizing: you may only recommend TRADE (up to default contracts), REDUCE "
    "(shrink contracts), or STAND_DOWN (engage KILL_SWITCH / 0 contracts).\n"
    "3. Ground every statement in the exact numbers from the provided JSON context."
)

_CLM_BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "regime_assessment": {"type": "string"},
        "portfolio_posture": {
            "type": "string",
            "enum": ["RISK_ON", "DEFENSIVE_MICRO", "SELECTIVE", "STAND_DOWN"],
        },
        "sleeve_verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "strategy_code": {"type": "string"},
                    "verdict": {
                        "type": "string",
                        "enum": ["TRADE", "REDUCE", "STAND_DOWN"],
                    },
                    "recommended_contracts": {"type": "integer"},
                    "rationale": {"type": "string"},
                },
                "required": [
                    "strategy_code",
                    "verdict",
                    "recommended_contracts",
                    "rationale",
                ],
                "additionalProperties": False,
            },
        },
        "execution_watch": {"type": "string"},
        "operator_actions": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [
        "headline",
        "regime_assessment",
        "portfolio_posture",
        "sleeve_verdicts",
        "execution_watch",
        "operator_actions",
    ],
    "additionalProperties": False,
}


def analyze_position_risk(
    spec: StrategySpec,
    engine_state: dict[str, Any] | None,
    db_summary: dict[str, Any],
) -> dict[str, Any]:
    """Compute exact point and dollar distance to every locked engine and broker exit."""
    thr = spec.thresholds
    open_trade = db_summary.get("open_trade")
    eng = engine_state or {}

    pos = eng.get("position")
    if not pos and open_trade:
        d = int(open_trade.get("direction") or 0)
        pos = "LONG" if d > 0 else ("SHORT" if d < 0 else None)

    entry_px = (
        float(open_trade["entry_px"])
        if open_trade and open_trade.get("entry_px") is not None
        else (float(eng["entry_price"]) if eng.get("entry_price") is not None else None)
    )
    qty = (
        int(open_trade["qty"])
        if open_trade and open_trade.get("qty") is not None
        else spec.default_contracts
    )

    if not pos or not entry_px:
        return {
            "in_position": False,
            "strategy_code": spec.code,
            "instrument_root": spec.instrument_root,
            "direction": None,
            "contracts": 0,
            "narrative": f"{spec.code} ({spec.instrument_root}) is currently FLAT.",
        }

    sign = 1.0 if pos == "LONG" else -1.0
    mult = spec.point_multiplier

    def _level_price(pct: float) -> float:
        return round(entry_px * (1.0 + sign * (pct / 100.0)), 2)

    def _usd_at_pct(pct: float) -> float:
        pts = entry_px * (pct / 100.0)
        return round(pts * mult * qty, 2)

    hard_stop_px = _level_price(thr.hard_stop_pct)
    broker_stop_px = (
        float(open_trade["stop_px"])
        if open_trade and open_trade.get("stop_px") is not None
        else _level_price(-thr.broker_stop_pct)
    )
    going_nowhere_px = _level_price(thr.going_nowhere_stop_pct)
    afterhours_stop_px = _level_price(thr.afterhours_stop_pct)
    pt_low_px = _level_price(thr.pt_level_low)
    pt_mid_px = _level_price(thr.pt_level_mid)
    pt_high_px = _level_price(thr.pt_level_high)

    gain_pct = eng.get("gain_pct")
    duration_bars = eng.get("trade_duration") or 0
    algo_status = eng.get("algo_status") or "going_no_where"
    stop_confirmed = (
        bool(open_trade.get("stop_confirmed")) if open_trade is not None else True
    )

    exit_ladder = {
        "broker_disaster_stop": {
            "pct": -thr.broker_stop_pct,
            "price": broker_stop_px,
            "est_pnl_usd": _usd_at_pct(-thr.broker_stop_pct),
            "stop_confirmed": stop_confirmed,
        },
        "engine_hard_stop_fts": {
            "pct": thr.hard_stop_pct,
            "price": hard_stop_px,
            "est_pnl_usd": _usd_at_pct(thr.hard_stop_pct),
        },
        "going_nowhere_stop": {
            "pct": thr.going_nowhere_stop_pct,
            "price": going_nowhere_px,
            "est_pnl_usd": _usd_at_pct(thr.going_nowhere_stop_pct),
            "active_now": algo_status == "going_no_where",
        },
        "afterhours_stop": {
            "pct": thr.afterhours_stop_pct,
            "price": afterhours_stop_px,
            "est_pnl_usd": _usd_at_pct(thr.afterhours_stop_pct),
        },
        "profit_take_low": {
            "pct": thr.pt_level_low,
            "price": pt_low_px,
            "est_pnl_usd": _usd_at_pct(thr.pt_level_low),
            "min_duration_bars": thr.pt_min_duration,
        },
        "profit_take_mid": {
            "pct": thr.pt_level_mid,
            "price": pt_mid_px,
            "est_pnl_usd": _usd_at_pct(thr.pt_level_mid),
            "min_duration_bars": thr.pt_min_duration,
        },
        "profit_take_high": {
            "pct": thr.pt_level_high,
            "price": pt_high_px,
            "est_pnl_usd": _usd_at_pct(thr.pt_level_high),
            "min_duration_bars": thr.pt_min_duration,
        },
    }

    gain_txt = f"{gain_pct:+.2f}%" if isinstance(gain_pct, (int, float)) else "n/a"
    stop_warn = "" if stop_confirmed else " [WARNING: IBKR GTC STOP UNCONFIRMED]"
    narrative = (
        f"{spec.code} holds {pos} {qty}x {spec.instrument_root} from {entry_px:.2f} "
        f"(gain={gain_txt}, duration={duration_bars} bars, regime={algo_status}). "
        f"Locked exits: going-nowhere ({thr.going_nowhere_stop_pct}%) @ {going_nowhere_px:.2f}, "
        f"engine hard stop 'fts' ({thr.hard_stop_pct}%) @ {hard_stop_px:.2f} "
        f"(${_usd_at_pct(thr.hard_stop_pct):,.0f}), "
        f"IBKR GTC stop (-{thr.broker_stop_pct}%) @ {broker_stop_px:.2f} "
        f"(${_usd_at_pct(-thr.broker_stop_pct):,.0f}); "
        f"profit-take tiers ({thr.pt_level_low}/{thr.pt_level_mid}/{thr.pt_level_high}%) "
        f"@ {pt_low_px:.2f} / {pt_mid_px:.2f} / {pt_high_px:.2f}.{stop_warn}"
    )

    return {
        "in_position": True,
        "strategy_code": spec.code,
        "instrument_root": spec.instrument_root,
        "direction": pos,
        "contracts": qty,
        "entry_price": entry_px,
        "gain_pct": gain_pct,
        "duration_bars": duration_bars,
        "algo_status": algo_status,
        "stop_confirmed": stop_confirmed,
        "exit_ladder": exit_ladder,
        "narrative": narrative,
    }


def diagnose_execution_drift(
    spec: StrategySpec,
    db_summary: dict[str, Any],
    recent_events: list[dict[str, Any]],
    sync_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Detect slippage spikes, loss-cap proximity, stop anomalies, and backtest sync drift."""
    thr = spec.thresholds
    realized = float(db_summary.get("today_realized_pnl") or 0.0)
    closed_today = int(db_summary.get("today_closed_trades") or 0)
    loss_limit = thr.daily_loss_limit_usd
    loss_used_pct = (
        round(max(0.0, -realized) / loss_limit * 100.0, 1) if loss_limit > 0 else 0.0
    )

    flags: list[str] = []
    severity = "OK"

    if realized <= -loss_limit:
        severity = "HALT"
        flags.append(
            f"Daily realized loss ${realized:,.2f} breached locked cap -${loss_limit:,.0f}"
        )
    elif loss_used_pct >= 70.0:
        severity = "CAUTION"
        flags.append(
            f"Daily realized loss ${realized:,.2f} is at {loss_used_pct:.0f}% "
            f"of -${loss_limit:,.0f} cap"
        )

    if closed_today >= thr.max_trades_per_day:
        severity = "HALT"
        flags.append(
            f"Trade count {closed_today} reached locked daily max {thr.max_trades_per_day}"
        )

    open_trade = db_summary.get("open_trade")
    if open_trade and open_trade.get("stop_confirmed") in (0, False):
        severity = "HALT"
        flags.append("Open trade has unconfirmed IBKR GTC stop — immediate flatten required")

    for ev in recent_events:
        kind = str(ev.get("event") or "")
        if kind in ("halt", "stop_filled"):
            if severity == "OK":
                severity = "CAUTION"
            flags.append(f"Recent engine event: {kind} ({ev.get('reason') or 'broker stop'})")
        elif kind == "ib_disconnect":
            if severity == "OK":
                severity = "CAUTION"
            flags.append("Recent IBKR Gateway disconnect recorded in telemetry")
        elif kind == "exit" and ev.get("slip_vs_quote_pt") is not None:
            try:
                slip = abs(float(ev["slip_vs_quote_pt"]))
                if slip >= 3.0:
                    if severity == "OK":
                        severity = "CAUTION"
                    flags.append(f"Elevated exit slippage ({slip:.2f} pts vs quote)")
            except (TypeError, ValueError):
                pass

    if sync_row:
        sync_status = str(sync_row.get("sync_status") or sync_row.get("status") or "")
        delta_usd = sync_row.get("net_pnl_delta")
        if sync_status.upper() == "FLAG":
            if severity == "OK":
                severity = "CAUTION"
            flags.append(
                f"Live vs backtest replay flagged (delta={delta_usd})"
            )

    summary = (
        f"{spec.code} execution {severity}: today P&L ${realized:,.2f} "
        f"({loss_used_pct:.0f}% of -${loss_limit:,.0f} loss cap, "
        f"{closed_today}/{thr.max_trades_per_day} trades)."
        + (" Flags: " + "; ".join(flags) if flags else " No microstructure anomalies.")
    )

    return {
        "strategy_code": spec.code,
        "severity": severity,
        "today_realized_pnl": realized,
        "loss_cap_usd": loss_limit,
        "loss_cap_used_pct": loss_used_pct,
        "today_closed_trades": closed_today,
        "max_trades_per_day": thr.max_trades_per_day,
        "flags": flags,
        "summary": summary,
    }


def _compute_sleeve_verdict(
    spec: StrategySpec,
    status_item: dict[str, Any],
    drift: dict[str, Any],
    sentiment_rec: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compute downward-only contextual recommendation for one strategy sleeve.

    Never exceeds ``spec.default_contracts`` and never alters ``spec.thresholds``.
    """
    max_contracts = spec.default_contracts
    reasons: list[str] = []
    verdict = "TRADE"
    rec_contracts = max_contracts

    if status_item.get("kill_switch_active"):
        verdict = "STAND_DOWN"
        rec_contracts = 0
        reasons.append("KILL_SWITCH is currently active")

    if drift["severity"] == "HALT":
        verdict = "STAND_DOWN"
        rec_contracts = 0
        reasons.extend(drift["flags"])
    elif drift["severity"] == "CAUTION" and verdict != "STAND_DOWN":
        verdict = "REDUCE"
        rec_contracts = max(1, max_contracts // 2) if max_contracts > 1 else 1
        reasons.extend(drift["flags"])

    eng = status_item.get("engine_state") or {}
    last_gate = eng.get("last_gate")
    if isinstance(last_gate, dict) and last_gate.get("blocked"):
        verdict = "STAND_DOWN"
        rec_contracts = 0
        reasons.append(
            f"Engine FnG/chop gate blocked "
            f"(FnG={last_gate.get('fng')}, chop={last_gate.get('chop')})"
        )

    if sentiment_rec:
        s_verdict = str(sentiment_rec.get("verdict") or "TRADE").upper()
        s_mult = float(sentiment_rec.get("size_mult") or 1.0)
        if s_verdict == "STAND_DOWN":
            verdict = "STAND_DOWN"
            rec_contracts = 0
            reason_txt = sentiment_rec.get("reason") or "macro/kill gate"
            reasons.append(f"HQ sentiment gate verdict is STAND_DOWN ({reason_txt})")
        elif (s_verdict == "REDUCE" or s_mult < 1.0) and verdict != "STAND_DOWN":
            verdict = "REDUCE"
            scaled = max(1, round(max_contracts * min(1.0, s_mult)))
            rec_contracts = min(rec_contracts, scaled)
            reasons.append(
                f"HQ sentiment gate recommends REDUCE ({s_mult:g}x size)"
            )

    if not reasons:
        reasons.append(
            f"All gates open; locked thresholds verified "
            f"({spec.instrument_root} ${spec.point_multiplier:g}/pt)"
        )

    return {
        "strategy_code": spec.code,
        "instrument_root": spec.instrument_root,
        "point_multiplier": spec.point_multiplier,
        "verdict": verdict,
        "recommended_contracts": min(rec_contracts, max_contracts),
        "max_validated_contracts": max_contracts,
        "account_id": status_item["account_binding"]["account_id"],
        "rationale": "; ".join(reasons),
        "thresholds_locked": True,
    }


class HqContextLanguageModel:
    """Context Language Model supervisor for HQ futures strategies (`nq_v221`, `es_v221`, `r2_mnq`).

    Provides:
      - ``assemble_context``: full cross-repo snapshot (engine state, sentiment, KPIs, drift)
      - ``position_xray``: exact distance-to-stop and exit ladder for open positions
      - ``sleeve_allocation``: downward-only sizing and de-correlated Nasdaq/S&P routing
      - ``generate_brief``: Claude/Vertex structured synthesis with deterministic fallback
      - ``apply_recommendation``: guardrailed execution (can only halt, resume, or shrink size)
      - ``notify_grokbot``: pushes contextual brief + suggested actions to GrokBot webhook
    """

    def __init__(
        self,
        runner: HqStrategyRunner | None = None,
        reader: HqReader | None = None,
        *,
        model: str = DEFAULT_CLM_MODEL,
        complete_fn: SyncCompleter | AsyncCompleter | None = None,
    ) -> None:
        self.runner = runner if runner is not None else HqStrategyRunner()
        self.reader = reader
        self.model = model
        self._complete_fn = complete_fn

    @property
    def llm_configured(self) -> bool:
        return (
            self._complete_fn is not None
            or bool(os.getenv("ANTHROPIC_API_KEY"))
            or os.getenv("WAYSTONE_LLM_PROVIDER", "").lower() == "vertex"
        )

    def _safe_reader_call(self, fn: Callable[[], Any], default: Any = None) -> Any:
        if self.reader is None:
            return default
        try:
            return fn()
        except Exception:
            return default

    def assemble_context(self, strategy: str | None = None) -> dict[str, Any]:
        """Fuse runtime state, position X-ray, execution drift, sentiment gates, and KPIs."""
        codes = [resolve_strategy_code(strategy)] if strategy else list(STRATEGY_SPECS)

        sentiment_now = self._safe_reader_call(
            lambda: self.reader.sentiment_latest() if self.reader else None,
            default=None,
        )
        sentiment_by_code: dict[str, dict[str, Any]] = {}
        if isinstance(sentiment_now, dict):
            for rec in sentiment_now.get("recommendations") or []:
                if isinstance(rec, dict) and rec.get("strategy_code"):
                    sentiment_by_code[str(rec["strategy_code"])] = rec

        sync_rows = self._safe_reader_call(
            lambda: self.reader.daily_sync() if self.reader else [],
            default=[],
        )
        latest_sync_by_code: dict[str, dict[str, Any]] = {}
        for row in sync_rows or []:
            if isinstance(row, dict):
                code = str(row.get("strategy_code") or "")
                if code and code not in latest_sync_by_code:
                    latest_sync_by_code[code] = row

        sleeves: list[dict[str, Any]] = []
        for code in codes:
            spec = STRATEGY_SPECS[code]
            st = self.runner.status(code)
            pos_risk = analyze_position_risk(spec, st.get("engine_state"), st.get("db") or {})
            drift = diagnose_execution_drift(
                spec,
                st.get("db") or {},
                st.get("recent_events") or [],
                latest_sync_by_code.get(code),
            )
            verdict = _compute_sleeve_verdict(
                spec, st, drift, sentiment_by_code.get(code)
            )
            sleeves.append(
                {
                    "strategy_code": code,
                    "name": spec.name,
                    "instrument_root": spec.instrument_root,
                    "point_multiplier": spec.point_multiplier,
                    "account_binding": st["account_binding"],
                    "kill_switch_active": st["kill_switch_active"],
                    "process": st["process"],
                    "thresholds_locked": True,
                    "thresholds": st["thresholds"],
                    "engine_state": st["engine_state"],
                    "position_xray": pos_risk,
                    "execution_drift": drift,
                    "contextual_verdict": verdict,
                }
            )

        # Cross-sleeve Nasdaq beta check: route Nasdaq to R2 MNQ ($2/pt) when NQ is defensive
        by_code = {s["strategy_code"]: s for s in sleeves}
        if "nq_v221" in by_code and "r2_mnq" in by_code:
            nq_v = by_code["nq_v221"]["contextual_verdict"]
            r2_v = by_code["r2_mnq"]["contextual_verdict"]
            if (
                nq_v["verdict"] in ("REDUCE", "STAND_DOWN")
                and r2_v["verdict"] == "TRADE"
            ):
                r2_v["rationale"] += (
                    " | Preferred Nasdaq-100 sleeve ($2/pt MNQ) while "
                    "full-size NQ ($20/pt) is defensive"
                )

        return {
            "generated_at": datetime.now(NY).isoformat(),
            "thresholds_locked": True,
            "llm_configured": self.llm_configured,
            "sentiment_gate": sentiment_now,
            "sleeves": sleeves,
        }

    def position_xray(self, strategy: str | None = None) -> dict[str, Any]:
        """Return position distance-to-stop X-ray for one or all strategies."""
        ctx = self.assemble_context(strategy)
        xrays: list[dict[str, Any]] = [dict(s["position_xray"]) for s in ctx["sleeves"]]
        if strategy:
            return xrays[0]
        return {
            "generated_at": ctx["generated_at"],
            "thresholds_locked": True,
            "positions": xrays,
        }

    def _deterministic_brief(
        self, ctx: dict[str, Any], operator_question: str = ""
    ) -> dict[str, Any]:
        sleeves = ctx["sleeves"]
        verdicts = [
            {
                "strategy_code": s["strategy_code"],
                "verdict": s["contextual_verdict"]["verdict"],
                "recommended_contracts": s["contextual_verdict"]["recommended_contracts"],
                "rationale": s["contextual_verdict"]["rationale"],
            }
            for s in sleeves
        ]
        v_map = {v["strategy_code"]: v["verdict"] for v in verdicts}
        if all(v == "STAND_DOWN" for v in v_map.values()):
            posture = "STAND_DOWN"
        elif v_map.get("nq_v221") in ("STAND_DOWN", "REDUCE") and v_map.get("r2_mnq") == "TRADE":
            posture = "DEFENSIVE_MICRO"
        elif any(v != "TRADE" for v in v_map.values()):
            posture = "SELECTIVE"
        else:
            posture = "RISK_ON"

        active_positions = [
            s["position_xray"]["narrative"]
            for s in sleeves
            if s["position_xray"].get("in_position")
        ]
        drift_summaries = [s["execution_drift"]["summary"] for s in sleeves]

        sent = ctx.get("sentiment_gate")
        sent_summary = (
            str(sent.get("summary") or sent.get("headline") or "Sentiment gate loaded.")
            if isinstance(sent, dict)
            else (
                "HQ sentiment DB not connected; relying on engine FnG/chop gate "
                "and execution telemetry."
            )
        )

        actions: list[str] = []
        for s in sleeves:
            code = s["strategy_code"]
            cv = s["contextual_verdict"]
            if cv["verdict"] == "STAND_DOWN" and not s["kill_switch_active"]:
                actions.append(
                    f"Halt {code} (engage KILL_SWITCH): {cv['rationale']}"
                )
            elif (
                cv["verdict"] == "REDUCE"
                and s["account_binding"]["contracts"] > cv["recommended_contracts"]
            ):
                actions.append(
                    f"Reduce {code} contracts from {s['account_binding']['contracts']} "
                    f"to {cv['recommended_contracts']} on {s['account_binding']['account_id']}"
                )
            elif cv["verdict"] == "TRADE" and s["kill_switch_active"]:
                actions.append(f"Clear KILL_SWITCH on {code} when ready to resume")
        if not actions:
            actions.append(
                "Maintain current account bindings and locked V221/R2 thresholds."
            )

        headline = (
            f"Posture {posture}: "
            + ", ".join(
                f"{v['strategy_code']}={v['verdict']}({v['recommended_contracts']}c)"
                for v in verdicts
            )
        )
        if operator_question:
            headline = f"[Query: {operator_question[:60]}] {headline}"

        return {
            "headline": headline,
            "regime_assessment": sent_summary,
            "portfolio_posture": posture,
            "sleeve_verdicts": verdicts,
            "position_narratives": active_positions
            or ["All HQ futures sleeves are currently flat."],
            "execution_watch": " | ".join(drift_summaries),
            "operator_actions": actions,
            "source": "deterministic_clm",
            "thresholds_locked": True,
        }

    async def _call_llm(
        self, ctx: dict[str, Any], operator_question: str, fallback: dict[str, Any]
    ) -> dict[str, Any]:
        user_prompt = (
            f"Operator question / focus: {operator_question or 'Full desk & risk briefing'}\n\n"
            f"Deterministic baseline verdict:\n{json.dumps(fallback, default=str)}\n\n"
            f"Live fused HQ Strategy Context:\n{json.dumps(ctx, default=str)}"
        )
        if self._complete_fn is not None:
            res = self._complete_fn(_CLM_SYSTEM_PROMPT, user_prompt, _CLM_BRIEF_SCHEMA)
            out = await res if asyncio.iscoroutine(res) else res
        else:
            client = build_async_client()
            resp = await client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=_CLM_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
                output_config={
                    "format": {"type": "json_schema", "schema": _CLM_BRIEF_SCHEMA}
                },
            )
            text = next((b.text for b in resp.content if b.type == "text"), None)
            if not text:
                return fallback
            out = json.loads(text)

        if not isinstance(out, dict):
            return fallback

        # Enforce downward-only contract cap on LLM output so it can never inflate size
        sanitized_verdicts: list[dict[str, Any]] = []
        for item in out.get("sleeve_verdicts") or fallback["sleeve_verdicts"]:
            code = str(item.get("strategy_code") or "")
            if code in STRATEGY_SPECS:
                max_c = STRATEGY_SPECS[code].default_contracts
                rec_c = max(0, min(int(item.get("recommended_contracts", max_c)), max_c))
                if item.get("verdict") == "STAND_DOWN":
                    rec_c = 0
                sanitized_verdicts.append(
                    {
                        "strategy_code": code,
                        "verdict": item.get("verdict", "TRADE"),
                        "recommended_contracts": rec_c,
                        "rationale": str(item.get("rationale") or ""),
                    }
                )
        return {
            **fallback,
            **out,
            "sleeve_verdicts": sanitized_verdicts or fallback["sleeve_verdicts"],
            "source": f"claude_clm ({self.model})",
            "thresholds_locked": True,
        }

    def generate_brief(
        self,
        strategy: str | None = None,
        *,
        question: str = "",
        use_llm: bool = True,
    ) -> dict[str, Any]:
        """Generate a Context Language Model brief for Claude Cowork or GrokBot."""
        ctx = self.assemble_context(strategy)
        baseline = self._deterministic_brief(ctx, operator_question=question)
        if not use_llm or not self.llm_configured:
            return {**baseline, "context": ctx}
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop and loop.is_running():
                # Inside an async FastAPI/MCP event loop, run in a worker thread if needed
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    synthesized = pool.submit(
                        lambda: asyncio.run(self._call_llm(ctx, question, baseline))
                    ).result(timeout=25)
            else:
                synthesized = asyncio.run(self._call_llm(ctx, question, baseline))
            return {**synthesized, "context": ctx}
        except Exception as exc:
            return {
                **baseline,
                "llm_fallback_reason": str(exc),
                "context": ctx,
            }

    def apply_recommendation(
        self,
        strategy: str | None = None,
        *,
        actor: str = "context_lm",
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply the Context LM's downward-only risk verdict (STAND_DOWN -> halt,
        REDUCE -> shrink contracts on bound paper account). Never alters thresholds.
        """
        assert_no_threshold_changes(overrides)
        ctx = self.assemble_context(strategy)
        applied: list[dict[str, Any]] = []
        for sleeve in ctx["sleeves"]:
            code = sleeve["strategy_code"]
            spec = STRATEGY_SPECS[code]
            cv = sleeve["contextual_verdict"]
            verdict = cv["verdict"]
            rec_contracts = min(int(cv["recommended_contracts"]), spec.default_contracts)

            if verdict == "STAND_DOWN":
                halt_res = self.runner.halt_strategy(
                    code, reason=f"CLM STAND_DOWN ({actor}): {cv['rationale']}"
                )
                applied.append(
                    {
                        "strategy_code": code,
                        "action_taken": "halt",
                        "verdict": verdict,
                        "detail": halt_res,
                    }
                )
            elif verdict == "REDUCE":
                cfg_res = self.runner.configure_account(
                    code,
                    contracts=max(1, rec_contracts),
                    actor=actor,
                )
                applied.append(
                    {
                        "strategy_code": code,
                        "action_taken": "reduce_contracts",
                        "verdict": verdict,
                        "contracts": max(1, rec_contracts),
                        "detail": cfg_res,
                    }
                )
            else:
                applied.append(
                    {
                        "strategy_code": code,
                        "action_taken": "none",
                        "verdict": verdict,
                        "contracts": sleeve["account_binding"]["contracts"],
                    }
                )
        return {
            "applied_by": actor,
            "thresholds_locked": True,
            "results": applied,
        }

    def notify_grokbot(
        self,
        strategy: str | None = None,
        *,
        question: str = "",
        channel: GrokBotChannel | None = None,
    ) -> dict[str, Any]:
        """Generate a Context LM brief and push it to the GrokBot webhook routine."""
        brief = self.generate_brief(strategy, question=question)
        payload: dict[str, Any] = {
            "event": "waystone.strategy.context_brief",
            "headline": brief["headline"],
            "portfolio_posture": brief["portfolio_posture"],
            "regime_assessment": brief["regime_assessment"],
            "sleeve_verdicts": brief["sleeve_verdicts"],
            "position_narratives": brief.get("position_narratives", []),
            "execution_watch": brief["execution_watch"],
            "operator_actions": brief["operator_actions"],
            "thresholds_locked": True,
            "at": datetime.now(NY).isoformat(),
            "source": brief["source"],
        }
        ch = channel if channel is not None else GrokBotChannel()
        delivered = ch.post_sync(payload)
        return {"delivered": delivered, "payload": payload, "brief": brief}
