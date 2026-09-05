"""NQ / futures KPI scorecard from *futures_kpi_dashboard_NQ_FINAL_v5_1*.

Tiers 0–4, GREEN / AMBER / RED. COMPUTED rows use published futures fills.
MANUAL rows stay empty until trials / greeks / ops inputs are added.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from math import sqrt
from typing import Any

from waystone3.ibkr.kpis import (
    KpiSpec,
    KpiStatus,
    _iso_week,
    _max_dd_pct,
    _mean,
    _sharpe,
    _sortino,
    _std,
    evaluate,
)
from waystone3.ibkr.models import Book, DailyReport, Execution
from waystone3.ibkr.reader import list_published_days, load_report
from waystone3.ibkr.settings import IbkrSettings
from waystone3.ibkr.staged import (
    STAGED_FUTURES_MANUAL,
    STAGED_WEEK_END,
    apply_staged_manual,
    staged_meta,
)
from waystone3.ibkr.store import ReportStore

FUTURES_SPECS: tuple[KpiSpec, ...] = (
    KpiSpec(
        "trade_count",
        "No. of independent trades",
        "t0",
        "Tier 0 — Statistical validity",
        "COMPUTED",
        True,
        "ge",
        200.0,
        100.0,
        "Closed futures fills with realized P&L. Below ~100 every stat is noise.",
    ),
    KpiSpec(
        "n_trials",
        "No. of configs tested (trials)",
        "t0",
        "Tier 0 — Statistical validity",
        "MANUAL",
        True,
        "le",
        20.0,
        100.0,
        "How many parameter sets were tried. Feeds the deflated Sharpe (manual).",
    ),
    KpiSpec(
        "oos_is_sharpe",
        "OOS / IS Sharpe ratio",
        "t0",
        "Tier 0 — Statistical validity",
        "COMPUTED",
        True,
        "ge",
        0.7,
        0.5,
        "Last 30% weekly Sharpe ÷ first 70% weekly Sharpe.",
    ),
    KpiSpec(
        "deflated_sharpe",
        "Deflated Sharpe (PSR)",
        "t0",
        "Tier 0 — Statistical validity",
        "MANUAL",
        True,
        "ge",
        0.95,
        0.9,
        "P(true Sharpe > 0) after adjusting for trials, skew, kurtosis (manual).",
    ),
    KpiSpec(
        "wf_efficiency",
        "Walk-forward efficiency",
        "t0",
        "Tier 0 — Statistical validity",
        "COMPUTED",
        True,
        "ge",
        0.6,
        0.5,
        "Same time-split ratio as OOS/IS; sheet walk-forward proxy.",
    ),
    KpiSpec(
        "param_robustness",
        "Parameter robustness",
        "t0",
        "Tier 0 — Statistical validity",
        "MANUAL",
        True,
        "ge",
        0.8,
        0.6,
        "Plateau vs spike: neighbouring parameters also work (manual).",
    ),
    KpiSpec(
        "sharpe",
        "Sharpe ratio (ann., net)",
        "t1",
        "Tier 1 — Risk-adjusted performance",
        "COMPUTED",
        True,
        "ge",
        1.5,
        1.0,
        "Annualised excess return / vol from weekly futures P&L, net of commissions.",
    ),
    KpiSpec(
        "sortino",
        "Sortino ratio (ann.)",
        "t1",
        "Tier 1 — Risk-adjusted performance",
        "COMPUTED",
        False,
        "ge",
        2.0,
        1.3,
        "Annualized daily mean ÷ downside deviation.",
    ),
    KpiSpec(
        "calmar",
        "Calmar / MAR ratio",
        "t1",
        "Tier 1 — Risk-adjusted performance",
        "DERIVED",
        False,
        "ge",
        0.75,
        0.5,
        "Annualized return divided by absolute max drawdown.",
    ),
    KpiSpec(
        "information_ratio",
        "Information ratio (vs bmk)",
        "t1",
        "Tier 1 — Risk-adjusted performance",
        "MANUAL",
        False,
        "ge",
        0.75,
        0.5,
        "Active return / tracking error vs NQ buy-and-hold (manual).",
    ),
    KpiSpec(
        "max_dd",
        "Max drawdown",
        "t2",
        "Tier 2 — Risk & drawdown",
        "DERIVED",
        True,
        "le",
        0.15,
        0.25,
        "Largest peak-to-trough decline of cumulative futures P&L / NAV (fraction).",
    ),
    KpiSpec(
        "max_dd_months",
        "Max drawdown duration (mo)",
        "t2",
        "Tier 2 — Risk & drawdown",
        "DERIVED",
        False,
        "le",
        9.0,
        18.0,
        "Longest stretch underwater, in months (21 trading days / month).",
    ),
    KpiSpec(
        "ann_vol",
        "Annualised volatility",
        "t2",
        "Tier 2 — Risk & drawdown",
        "COMPUTED",
        False,
        "le",
        0.15,
        0.25,
        "Stdev of daily futures returns × sqrt(252).",
    ),
    KpiSpec(
        "cvar95",
        "CVaR 95% (per day)",
        "t2",
        "Tier 2 — Risk & drawdown",
        "DERIVED",
        False,
        "le",
        0.02,
        0.035,
        "Mean of the worst 5% of daily futures returns (fraction of NAV).",
    ),
    KpiSpec(
        "ulcer",
        "Ulcer index",
        "t2",
        "Tier 2 — Risk & drawdown",
        "DERIVED",
        False,
        "le",
        5.0,
        10.0,
        "RMS of drawdown depth over time.",
    ),
    KpiSpec(
        "tail_ratio",
        "Tail ratio",
        "t2",
        "Tier 2 — Risk & drawdown",
        "COMPUTED",
        False,
        "ge",
        1.2,
        1.0,
        "95th percentile daily return / abs(5th percentile).",
    ),
    KpiSpec(
        "profit_factor",
        "Profit factor",
        "t3",
        "Tier 3 — Trade quality & source of edge",
        "COMPUTED",
        False,
        "ge",
        1.5,
        1.2,
        "Sum of winning futures trades ÷ abs(sum of losers).",
    ),
    KpiSpec(
        "win_rate",
        "Win rate",
        "t3",
        "Tier 3 — Trade quality & source of edge",
        "COMPUTED",
        False,
        "ge",
        0.5,
        0.4,
        "Share of closed futures trades with positive realized P&L.",
    ),
    KpiSpec(
        "payoff_ratio",
        "Payoff ratio (avg W/avg L)",
        "t3",
        "Tier 3 — Trade quality & source of edge",
        "COMPUTED",
        False,
        "ge",
        1.5,
        1.0,
        "Average winning trade size vs average losing trade size.",
    ),
    KpiSpec(
        "expectancy_r",
        "Expectancy per trade (R)",
        "t3",
        "Tier 3 — Trade quality & source of edge",
        "DERIVED",
        False,
        "ge",
        0.15,
        0.05,
        "WinRate × payoff − LossRate × 1.",
    ),
    KpiSpec(
        "time_in_market",
        "Time in market",
        "t3",
        "Tier 3 — Trade quality & source of edge",
        "MANUAL",
        False,
        "le",
        0.6,
        0.85,
        "Fraction of session minutes deployed (needs hold times).",
    ),
    KpiSpec(
        "cost_drag",
        "Cost drag (% of gross P&L)",
        "t4",
        "Tier 4 — Cost & capacity robustness",
        "DERIVED",
        True,
        "le",
        0.3,
        0.5,
        "Commissions ÷ gross winning P&L (sheet: costs / gross).",
    ),
    KpiSpec(
        "be_cost_multiple",
        "Break-even cost multiple",
        "t4",
        "Tier 4 — Cost & capacity robustness",
        "DERIVED",
        False,
        "ge",
        3.0,
        2.0,
        "Gross P&L / commissions — how far costs can rise before net is 0.",
    ),
    KpiSpec(
        "slippage_realism",
        "Slippage realism",
        "t4",
        "Tier 4 — Cost & capacity robustness",
        "MANUAL",
        False,
        "ge",
        1.0,
        0.7,
        "Modeled slippage ÷ conservative independent tick estimate (manual).",
    ),
    KpiSpec(
        "margin_to_equity",
        "Margin-to-equity ratio",
        "t4",
        "Tier 4 — Cost & capacity robustness",
        "DERIVED",
        True,
        "le",
        0.35,
        0.5,
        "Highest maint. margin ÷ NLV across published snapshots.",
    ),
    KpiSpec(
        "capacity",
        "Capacity ($ notional)",
        "t4",
        "Tier 4 — Cost & capacity robustness",
        "MANUAL",
        False,
        "ge",
        25_000_000.0,
        5_000_000.0,
        "Notional before impact decays the edge (manual).",
    ),
    KpiSpec(
        "roll_cost_drag",
        "Roll cost drag",
        "t4",
        "Tier 4 — Cost & capacity robustness",
        "MANUAL",
        False,
        "le",
        0.1,
        0.2,
        "Cost of rolling contracts as a fraction of gross return (manual).",
    ),
)


def _sheet_status(status: KpiStatus) -> str:
    if status is KpiStatus.PASS:
        return "GREEN"
    if status is KpiStatus.WARN:
        return "AMBER"
    if status is KpiStatus.FAIL:
        return "RED"
    return "—"


def _future_fills(report: DailyReport) -> list[Execution]:
    return [e for e in report.executions if e.book is Book.FUTURES]


def _closed_pnls(fills: list[Execution]) -> list[float]:
    return [float(e.realized_pnl) for e in fills if e.realized_pnl is not None]


def _percentile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    ordered = sorted(xs)
    if len(ordered) == 1:
        return ordered[0]
    idx = q * (len(ordered) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    frac = idx - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def _cvar95_frac(daily_rets: list[float]) -> float | None:
    if len(daily_rets) < 5:
        return None
    ordered = sorted(daily_rets)
    tail_n = max(1, int(len(ordered) * 0.05))
    tail = ordered[:tail_n]
    mu = _mean(tail)
    return None if mu is None else abs(mu)


def _ulcer(daily_pnl: list[float], nav: float) -> float | None:
    if not daily_pnl or nav <= 0:
        return None
    equity = 0.0
    peak = 0.0
    squares = 0.0
    for pnl in daily_pnl:
        equity += pnl
        peak = max(peak, equity)
        dd_pct = (peak - equity) / nav * 100.0
        squares += dd_pct * dd_pct
    return sqrt(squares / len(daily_pnl))


def _dd_months(daily_pnl: list[float]) -> float | None:
    if not daily_pnl:
        return None
    equity = 0.0
    peak = 0.0
    run = 0
    longest = 0
    for pnl in daily_pnl:
        equity += pnl
        peak = max(peak, equity)
        if equity < peak:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    if longest == 0:
        return 0.0
    return longest / 21.0


def _tier_verdict(rows: list[dict[str, Any]]) -> str:
    measured = [r for r in rows if r["status"] != "—"]
    if not measured:
        return "—"
    if any(r["critical"] and r["status"] == "RED" for r in rows):
        return "RED"
    if any(r["status"] == "RED" for r in measured):
        return "RED"
    if any(r["status"] == "AMBER" for r in measured):
        return "AMBER"
    return "GREEN"


def _overall(stages: list[dict[str, Any]]) -> str:
    rows = [row for s in stages for row in s["kpis"]]
    if not any(r["status"] != "—" for r in rows):
        return "—"
    t0 = next((s for s in stages if s["id"] == "t0"), None)
    if t0 and any(r["status"] == "RED" for r in t0["kpis"]):
        return "REJECTED"
    if any(r["status"] == "RED" for r in rows):
        return "CONDITIONAL"
    if any(r["status"] == "AMBER" for r in rows):
        return "MARGINAL"
    return "PASS"


def compute_futures_kpis(
    store: ReportStore, settings: IbkrSettings | None = None
) -> dict[str, Any]:
    cfg = settings or IbkrSettings()
    nav = cfg.ibkr_kpi_nav if cfg.ibkr_kpi_nav > 0 else 100_000.0
    days = list_published_days(store)
    reports: list[DailyReport] = []
    for day in days:
        report = load_report(store, day)
        if report is not None:
            reports.append(report)

    daily_raw: list[tuple[date, float]] = []
    trades: list[float] = []
    commissions: list[float] = []
    margins: list[float] = []
    for report in reports:
        day = date.fromisoformat(report.date)
        fills = _future_fills(report)
        pnls = _closed_pnls(fills)
        daily_raw.append((day, sum(pnls)))
        trades.extend(pnls)
        commissions.extend(float(f.commission) for f in fills if f.commission is not None)
        if report.account.nlv > 0:
            margins.append(report.account.maint_margin / report.account.nlv)

    raw_pnls = [p for _, p in daily_raw]
    daily_rets = [p / nav for p in raw_pnls] if nav else []

    weekly: dict[tuple[int, int], float] = defaultdict(float)
    for day, pnl in daily_raw:
        weekly[_iso_week(day)] += pnl / nav
    week_rets = [weekly[k] for k in sorted(weekly)]

    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t < 0]
    profit_factor = sum(wins) / abs(sum(losses)) if wins and losses else None
    payoff = None
    avg_win = _mean(wins)
    avg_loss = _mean(losses)
    if avg_win is not None and avg_loss is not None and avg_loss != 0:
        payoff = avg_win / abs(avg_loss)
    win_rate = (len(wins) / len(trades)) if trades else None
    expectancy_r = None
    if win_rate is not None and payoff is not None:
        expectancy_r = win_rate * payoff - (1.0 - win_rate)

    span_days = 0
    if daily_raw:
        span_days = max(1, (daily_raw[-1][0] - daily_raw[0][0]).days)
    total_pnl = sum(raw_pnls)
    ann_return = (total_pnl / nav) * (365.0 / span_days) if span_days and nav else None
    max_dd_pct = _max_dd_pct(raw_pnls, nav)
    max_dd = (max_dd_pct / 100.0) if max_dd_pct is not None else None
    calmar = None
    if ann_return is not None and max_dd and max_dd > 0:
        calmar = ann_return / max_dd

    split = max(2, int(len(week_rets) * 0.7))
    is_sh = _sharpe(week_rets[:split], 52) if len(week_rets) >= 4 else None
    oos_sh = _sharpe(week_rets[split:], 52) if len(week_rets) >= 4 else None
    oos_is = (oos_sh / is_sh) if is_sh and oos_sh is not None and is_sh != 0 else None

    sd = _std(daily_rets)
    ann_vol = sd * sqrt(252) if sd is not None else None
    p95 = _percentile(daily_rets, 0.95)
    p05 = _percentile(daily_rets, 0.05)
    tail_ratio = None
    if p95 is not None and p05 is not None and p05 != 0:
        tail_ratio = p95 / abs(p05)

    gross = sum(wins) if wins else None
    cost = sum(commissions)
    cost_drag = (cost / gross) if gross and gross > 0 else None
    be_mult = (gross / cost) if gross and cost > 0 else None
    trials = cfg.ibkr_kpi_trials if cfg.ibkr_kpi_trials > 0 else None

    values: dict[str, float | None] = {
        "trade_count": float(len(trades)) if trades else None,
        "n_trials": trials,
        "oos_is_sharpe": oos_is,
        "deflated_sharpe": None,
        "wf_efficiency": oos_is,
        "param_robustness": None,
        "sharpe": _sharpe(week_rets, 52),
        "sortino": _sortino(daily_rets, 252),
        "calmar": calmar,
        "information_ratio": None,
        "max_dd": max_dd,
        "max_dd_months": _dd_months(raw_pnls),
        "ann_vol": ann_vol,
        "cvar95": _cvar95_frac(daily_rets),
        "ulcer": _ulcer(raw_pnls, nav),
        "tail_ratio": tail_ratio,
        "profit_factor": profit_factor,
        "win_rate": win_rate,
        "payoff_ratio": payoff,
        "expectancy_r": expectancy_r,
        "time_in_market": None,
        "cost_drag": cost_drag,
        "be_cost_multiple": be_mult,
        "slippage_realism": None,
        "margin_to_equity": max(margins) if margins else None,
        "capacity": None,
        "roll_cost_drag": None,
    }
    has_staged = any(report.manifest.staged for report in reports)
    if has_staged:
        values = apply_staged_manual(values, STAGED_FUTURES_MANUAL)

    by_tier: dict[str, list[dict[str, Any]]] = defaultdict(list)
    names: dict[str, str] = {}
    for spec in FUTURES_SPECS:
        names[spec.stage] = spec.stage_name
        value = values.get(spec.key)
        status = _sheet_status(evaluate(value, spec))
        by_tier[spec.stage].append(
            {
                "key": spec.key,
                "label": spec.label,
                "source": spec.source,
                "critical": spec.critical,
                "target": spec.pass_at,
                "min": spec.warn_at,
                "direction": spec.direction,
                "value": None if value is None else round(value, 4),
                "status": status,
                "definition": spec.definition,
                "unit": spec.unit,
            }
        )

    stages: list[dict[str, Any]] = []
    for sid in ("t0", "t1", "t2", "t3", "t4"):
        rows = by_tier[sid]
        filled = sum(1 for r in rows if r["status"] != "—")
        stages.append(
            {
                "id": sid,
                "name": names[sid],
                "verdict": _tier_verdict(rows),
                "filled": filled,
                "total": len(rows),
                "kpis": rows,
            }
        )

    week_series = [
        {"week": f"{year}-W{week:02d}", "return_pct": round(weekly[(year, week)] * 100.0, 4)}
        for (year, week) in sorted(weekly)
    ][-8:]

    as_of = STAGED_WEEK_END.isoformat() if has_staged else (days[-1].isoformat() if days else None)
    return {
        "as_of": as_of,
        "days": len(reports),
        "instrument": "NQ / ES futures",
        "assumptions": {
            "nav": nav,
            "contracts_per_trade": cfg.ibkr_kpi_contracts,
            "point_value": cfg.ibkr_kpi_point_value,
        },
        "overall": _overall(stages),
        "stages": stages,
        "weeks": week_series,
        "trade_count": len(trades),
        "span_days": span_days,
        **staged_meta(as_of, any_staged=has_staged),
    }
