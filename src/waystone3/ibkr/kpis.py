"""Options scorecard KPIs from the Strategy 5 weekly paper dashboard.

Thresholds match *Weekly Paper Dashboard.xlsx* (Dashboard PAPER sheet).
COMPUTED / DERIVED values come from published option fills. MANUAL rows stay
empty until IBKR greeks or an ops log is added.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from math import sqrt
from typing import Any, Literal

from waystone3.ibkr.models import Book, DailyReport, Execution
from waystone3.ibkr.reader import list_published_days, load_report
from waystone3.ibkr.settings import IbkrSettings
from waystone3.ibkr.store import ReportStore

Direction = Literal["ge", "le"]


class KpiStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    EMPTY = "—"


@dataclass(frozen=True)
class KpiSpec:
    key: str
    label: str
    stage: str
    stage_name: str
    source: str
    critical: bool
    direction: Direction
    pass_at: float
    warn_at: float
    definition: str
    unit: str = ""


DEFAULT_NAV = 100_000.0
DEFAULT_CONTRACTS = 1.0
DEFAULT_MULTIPLIER = 100.0
DEFAULT_SLIPPAGE = 0.02

SPECS: tuple[KpiSpec, ...] = (
    KpiSpec(
        "weekly_sharpe",
        "Net weekly Sharpe",
        "s1",
        "Stage 1 — Backtest Performance",
        "COMPUTED",
        True,
        "ge",
        1.5,
        1.0,
        "Mean weekly options return / weekly vol, annualized * sqrt(52).",
    ),
    KpiSpec(
        "sortino",
        "Sortino ratio",
        "s1",
        "Stage 1 — Backtest Performance",
        "COMPUTED",
        False,
        "ge",
        2.0,
        1.5,
        "Annualized daily mean ÷ downside deviation (returns below 0).",
    ),
    KpiSpec(
        "max_dd",
        "Max drawdown (% NAV)",
        "s1",
        "Stage 1 — Backtest Performance",
        "DERIVED",
        True,
        "le",
        15.0,
        25.0,
        "Largest peak-to-trough decline of cumulative options P&L as % of assumed NAV.",
    ),
    KpiSpec(
        "calmar",
        "Calmar (ann. return / MaxDD)",
        "s1",
        "Stage 1 — Backtest Performance",
        "DERIVED",
        False,
        "ge",
        1.0,
        0.5,
        "Annualized return divided by absolute max drawdown.",
    ),
    KpiSpec(
        "profit_factor",
        "Profit factor",
        "s1",
        "Stage 1 — Backtest Performance",
        "COMPUTED",
        False,
        "ge",
        1.5,
        1.25,
        "Sum of winning option trades ÷ abs(sum of losing option trades).",
    ),
    KpiSpec(
        "trade_count",
        "Trade count",
        "s1",
        "Stage 1 — Backtest Performance",
        "COMPUTED",
        True,
        "ge",
        200.0,
        100.0,
        "Closed option trades (fills with a realized P&L).",
    ),
    KpiSpec(
        "worst_month",
        "Worst month loss (% NAV)",
        "s1",
        "Stage 1 — Backtest Performance",
        "DERIVED",
        False,
        "le",
        8.0,
        12.0,
        "Most negative calendar-month options P&L as % of assumed NAV.",
    ),
    KpiSpec(
        "cvar95",
        "Daily CVaR 95% (% NAV)",
        "s1",
        "Stage 1 — Backtest Performance",
        "DERIVED",
        False,
        "le",
        2.0,
        3.0,
        "Mean of the worst 5% of daily options returns, as % of NAV.",
    ),
    KpiSpec(
        "monthly_skew",
        "Monthly return skewness",
        "s1",
        "Stage 1 — Backtest Performance",
        "COMPUTED",
        False,
        "ge",
        -1.0,
        -2.0,
        "Skewness of calendar-month options returns.",
    ),
    KpiSpec(
        "sharpe_cost_stress",
        "Sharpe under cost stress (live)",
        "s1",
        "Stage 1 — Backtest Performance",
        "DERIVED",
        True,
        "ge",
        1.0,
        0.7,
        "Weekly Sharpe after subtracting assumed round-trip slippage (% of premium).",
    ),
    KpiSpec(
        "ann_return",
        "Annual return (%)",
        "s1",
        "Stage 1 — Backtest Performance",
        "DERIVED",
        True,
        "ge",
        120.0,
        100.0,
        "Annualized gross options return vs assumed NAV (sheet target 120%).",
    ),
    KpiSpec(
        "oos_is_sharpe",
        "OOS/IS Sharpe (time-split proxy)",
        "s2",
        "Stage 2 — Robustness & Overfitting",
        "COMPUTED",
        True,
        "ge",
        0.6,
        0.4,
        "Last 30% weekly Sharpe ÷ first 70% weekly Sharpe.",
    ),
    KpiSpec(
        "wf_efficiency",
        "Walk-forward efficiency",
        "s2",
        "Stage 2 — Robustness & Overfitting",
        "COMPUTED",
        False,
        "ge",
        0.6,
        0.4,
        "Same time-split ratio; retained as the sheet's walk-forward proxy.",
    ),
    KpiSpec(
        "pbo",
        "Prob. of Backtest Overfitting (%)",
        "s2",
        "Stage 2 — Robustness & Overfitting",
        "MANUAL",
        False,
        "le",
        25.0,
        40.0,
        "Estimated chance the strategy is overfit (manual).",
    ),
    KpiSpec(
        "bootstrap_p5_sharpe",
        "Bootstrap 5th-pct Sharpe",
        "s2",
        "Stage 2 — Robustness & Overfitting",
        "COMPUTED",
        False,
        "ge",
        0.5,
        0.0,
        "5th percentile of Sharpe from resampling daily P&L.",
    ),
    KpiSpec(
        "years_covered",
        "Distinct years covered",
        "s2",
        "Stage 2 — Robustness & Overfitting",
        "COMPUTED",
        False,
        "ge",
        3.0,
        2.0,
        "Unique calendar years present in published option dumps.",
    ),
    KpiSpec(
        "trial_log",
        "Complete trial log maintained",
        "s2",
        "Stage 2 — Robustness & Overfitting",
        "MANUAL",
        True,
        "ge",
        1.0,
        1.0,
        "Yes/No — is a complete trial log maintained (manual).",
    ),
    KpiSpec(
        "net_vega",
        "|Net vega| (% NAV / vol pt)",
        "s3",
        "Stage 3 — Options Risk & Attribution",
        "MANUAL",
        True,
        "le",
        0.10,
        0.20,
        "P&L per 1-point implied-vol move as % of NAV (needs IBKR greeks).",
    ),
    KpiSpec(
        "net_delta",
        "|Net delta| (% NAV)",
        "s3",
        "Stage 3 — Options Risk & Attribution",
        "MANUAL",
        False,
        "le",
        10.0,
        20.0,
        "Net directional exposure to the underlying (needs IBKR greeks).",
    ),
    KpiSpec(
        "payoff_ratio",
        "Payoff ratio (avg win/avg loss)",
        "s3",
        "Stage 3 — Options Risk & Attribution",
        "COMPUTED",
        False,
        "ge",
        1.8,
        1.3,
        "Average winning trade size vs average losing trade size.",
    ),
    KpiSpec(
        "expectancy_bps",
        "Per-trade expectancy (bps NAV)",
        "s3",
        "Stage 3 — Options Risk & Attribution",
        "DERIVED",
        False,
        "ge",
        20.0,
        10.0,
        "Average realized P&L per closed option trade, as basis points of NAV.",
    ),
    KpiSpec(
        "peak_margin",
        "Peak margin utilization (%)",
        "s3",
        "Stage 3 — Options Risk & Attribution",
        "DERIVED",
        True,
        "le",
        50.0,
        65.0,
        "Highest maint. margin ÷ NLV across published snapshots.",
    ),
    KpiSpec(
        "capital_util",
        "Capital utilization (%)",
        "s3",
        "Stage 3 — Options Risk & Attribution",
        "DERIVED",
        False,
        "ge",
        50.0,
        40.0,
        "Average options notional ÷ assumed NAV across published days.",
    ),
    KpiSpec(
        "incubation_months",
        "Incubation length (months)",
        "s4",
        "Stage 4 — Incubation",
        "MANUAL",
        True,
        "ge",
        3.0,
        2.0,
        "Does it hold up in a live/paper trial before real capital?",
    ),
    KpiSpec(
        "incubation_trades",
        "Incubation trade count",
        "s4",
        "Stage 4 — Incubation",
        "MANUAL",
        False,
        "ge",
        50.0,
        30.0,
        "How many trades happened during incubation.",
    ),
    KpiSpec(
        "slippage_ratio_inc",
        "Realized/modeled slippage ratio",
        "s4",
        "Stage 4 — Incubation",
        "MANUAL",
        True,
        "le",
        1.2,
        1.5,
        "Actual slippage vs what the model assumed.",
    ),
    KpiSpec(
        "incubation_sharpe_ratio",
        "Incubation Sharpe / backtest Sharpe",
        "s4",
        "Stage 4 — Incubation",
        "MANUAL",
        True,
        "ge",
        0.7,
        0.5,
        "Live Sharpe ÷ backtest Sharpe.",
    ),
    KpiSpec(
        "ops_errors",
        "Ops errors per 100 trades",
        "s4",
        "Stage 4 — Incubation",
        "MANUAL",
        False,
        "le",
        1.0,
        3.0,
        "Fat-fingers, missed fills, system glitches per 100 trades.",
    ),
    KpiSpec(
        "kill_switch",
        "Daily loss kill switch tested",
        "s5",
        "Stage 5 — Live-Readiness",
        "MANUAL",
        True,
        "ge",
        1.0,
        1.0,
        "Circuit-breaker actually fires in practice (Yes=1).",
    ),
    KpiSpec(
        "dd_stop",
        "Max-drawdown stop defined",
        "s5",
        "Stage 5 — Live-Readiness",
        "MANUAL",
        True,
        "ge",
        1.0,
        1.0,
        "Written rule for de-allocating if losses hit a threshold (Yes=1).",
    ),
    KpiSpec(
        "missed_fills",
        "Missed fills (count)",
        "exec",
        "Weekly Execution & Slippage Report",
        "MANUAL",
        False,
        "le",
        0.0,
        2.0,
        "Intended orders that did not receive a fill.",
    ),
    KpiSpec(
        "missed_fills_pct",
        "Missed fills (% of orders)",
        "exec",
        "Weekly Execution & Slippage Report",
        "MANUAL",
        False,
        "le",
        5.0,
        10.0,
        "Missed fills divided by total intended orders for the week.",
    ),
    KpiSpec(
        "avg_slippage_pct",
        "Average realized slippage (% of premium)",
        "exec",
        "Weekly Execution & Slippage Report",
        "MANUAL",
        False,
        "le",
        2.0,
        4.0,
        "Average execution slippage as a percentage of option premium.",
    ),
    KpiSpec(
        "slippage_ratio",
        "Realized/modeled slippage ratio",
        "exec",
        "Weekly Execution & Slippage Report",
        "MANUAL",
        False,
        "le",
        1.2,
        1.5,
        "Actual slippage divided by the slippage assumed in the model.",
    ),
)


def evaluate(value: float | None, spec: KpiSpec) -> KpiStatus:
    if value is None:
        return KpiStatus.EMPTY
    if spec.direction == "ge":
        if value >= spec.pass_at:
            return KpiStatus.PASS
        if value >= spec.warn_at:
            return KpiStatus.WARN
        return KpiStatus.FAIL
    if value <= spec.pass_at:
        return KpiStatus.PASS
    if value <= spec.warn_at:
        return KpiStatus.WARN
    return KpiStatus.FAIL


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _std(xs: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    mu = sum(xs) / len(xs)
    var = sum((x - mu) ** 2 for x in xs) / (len(xs) - 1)
    return sqrt(var) if var > 0 else 0.0


def _skew(xs: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    mu = sum(xs) / len(xs)
    sd = _std(xs)
    if sd is None or sd == 0:
        return None
    n = len(xs)
    return sum(((x - mu) / sd) ** 3 for x in xs) * n / ((n - 1) * (n - 2))


def _sharpe(xs: list[float], periods: float) -> float | None:
    mu = _mean(xs)
    sd = _std(xs)
    if mu is None or sd is None or sd == 0:
        return None
    return mu / sd * sqrt(periods)


def _sortino(xs: list[float], periods: float) -> float | None:
    mu = _mean(xs)
    downs = [x for x in xs if x < 0]
    if mu is None or len(downs) < 2:
        return None
    dd = _std(downs)
    if dd is None or dd == 0:
        return None
    return mu / dd * sqrt(periods)


def _max_dd_pct(daily_pnl: list[float], nav: float) -> float | None:
    if not daily_pnl or nav <= 0:
        return None
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for pnl in daily_pnl:
        equity += pnl
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return abs(worst) / nav * 100.0


def _cvar95_pct(daily_rets: list[float]) -> float | None:
    if len(daily_rets) < 5:
        return None
    ordered = sorted(daily_rets)
    tail_n = max(1, int(len(ordered) * 0.05))
    tail = ordered[:tail_n]
    mu = _mean(tail)
    return None if mu is None else abs(mu) * 100.0


def _iso_week(day: date) -> tuple[int, int]:
    iso = day.isocalendar()
    return iso.year, iso.week


def _option_fills(report: DailyReport) -> list[Execution]:
    return [e for e in report.executions if e.book is Book.OPTIONS]


def _closed_pnls(fills: list[Execution]) -> list[float]:
    return [float(e.realized_pnl) for e in fills if e.realized_pnl is not None]


def _premium(fill: Execution, default_mult: float) -> float:
    try:
        mult = float(fill.multiplier) if fill.multiplier else default_mult
    except ValueError:
        mult = default_mult
    return abs(fill.qty * fill.price * mult)


def _bootstrap_p5(daily: list[float], periods: float, rounds: int = 400) -> float | None:
    if len(daily) < 5:
        return None
    seed = 1_234_567
    n = len(daily)
    scores: list[float] = []
    for _ in range(rounds):
        sample: list[float] = []
        for _ in range(n):
            seed = (1_103_515_245 * seed + 12_345) % 2**31
            sample.append(daily[seed % n])
        score = _sharpe(sample, periods)
        if score is not None:
            scores.append(score)
    if not scores:
        return None
    scores.sort()
    return scores[max(0, int(0.05 * (len(scores) - 1)))]


def _stage_verdict(rows: list[dict[str, Any]]) -> str:
    computed = [r for r in rows if r["status"] != KpiStatus.EMPTY.value]
    if not computed:
        return KpiStatus.EMPTY.value
    if any(r["critical"] and r["status"] == KpiStatus.FAIL.value for r in rows):
        return KpiStatus.FAIL.value
    if any(r["status"] in {KpiStatus.FAIL.value, KpiStatus.WARN.value} for r in computed):
        return KpiStatus.WARN.value
    return KpiStatus.PASS.value


def prior_weekdays(end: date, count: int) -> list[date]:
    out: list[date] = []
    cursor = end - timedelta(days=1)
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(out))


def compute_options_kpis(
    store: ReportStore, settings: IbkrSettings | None = None
) -> dict[str, Any]:
    cfg = settings or IbkrSettings()
    nav = cfg.ibkr_kpi_nav if cfg.ibkr_kpi_nav > 0 else DEFAULT_NAV
    slippage = cfg.ibkr_kpi_slippage
    default_mult = cfg.ibkr_kpi_multiplier
    days = list_published_days(store)
    reports: list[DailyReport] = []
    for day in days:
        report = load_report(store, day)
        if report is not None:
            reports.append(report)

    daily_raw: list[tuple[date, float]] = []
    daily_cost: list[tuple[date, float]] = []
    daily_notional: list[float] = []
    margins: list[float] = []
    trades: list[float] = []
    for report in reports:
        day = date.fromisoformat(report.date)
        fills = _option_fills(report)
        pnls = _closed_pnls(fills)
        raw = sum(pnls)
        prem = sum(_premium(f, default_mult) for f in fills)
        cost = raw - prem * slippage
        daily_raw.append((day, raw))
        daily_cost.append((day, cost))
        if prem:
            daily_notional.append(prem)
        trades.extend(pnls)
        if report.account.nlv > 0:
            margins.append(report.account.maint_margin / report.account.nlv * 100.0)

    raw_pnls = [p for _, p in daily_raw]
    daily_rets = [p / nav for p in raw_pnls]

    weekly: dict[tuple[int, int], float] = defaultdict(float)
    weekly_cost: dict[tuple[int, int], float] = defaultdict(float)
    for (day, pnl), (_, cpnl) in zip(daily_raw, daily_cost, strict=True):
        key = _iso_week(day)
        weekly[key] += pnl / nav
        weekly_cost[key] += cpnl / nav
    week_rets = [weekly[k] for k in sorted(weekly)]
    week_cost_rets = [weekly_cost[k] for k in sorted(weekly_cost)]

    monthly: dict[tuple[int, int], float] = defaultdict(float)
    for day, pnl in daily_raw:
        monthly[(day.year, day.month)] += pnl / nav
    month_rets = [monthly[k] for k in sorted(monthly)]

    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t < 0]
    profit_factor: float | None = None
    if wins and losses:
        profit_factor = sum(wins) / abs(sum(losses))
    payoff = None
    if wins and losses:
        avg_loss = _mean(losses)
        avg_win = _mean(wins)
        if avg_win is not None and avg_loss is not None and avg_loss != 0:
            payoff = avg_win / abs(avg_loss)
    avg_trade = _mean(trades)
    expectancy = avg_trade / nav * 10_000 if avg_trade is not None else None

    span_days = 0
    if daily_raw:
        span_days = max(1, (daily_raw[-1][0] - daily_raw[0][0]).days)
    total_pnl = sum(raw_pnls)
    ann_return = (total_pnl / nav) * (365.0 / span_days) * 100.0 if span_days and nav else None

    max_dd = _max_dd_pct(raw_pnls, nav)
    ann_frac = (ann_return / 100.0) if ann_return is not None else None
    calmar = None
    if ann_frac is not None and max_dd and max_dd > 0:
        calmar = ann_frac / (max_dd / 100.0)
    worst_month = (abs(min(month_rets)) * 100.0) if month_rets else None

    split = max(2, int(len(week_rets) * 0.7))
    is_sh = _sharpe(week_rets[:split], 52) if len(week_rets) >= 4 else None
    oos_sh = _sharpe(week_rets[split:], 52) if len(week_rets) >= 4 else None
    oos_is = (oos_sh / is_sh) if is_sh and oos_sh is not None and is_sh != 0 else None

    years = {d.year for d, _ in daily_raw}

    values: dict[str, float | None] = {
        "weekly_sharpe": _sharpe(week_rets, 52),
        "sortino": _sortino(daily_rets, 252),
        "max_dd": max_dd,
        "calmar": calmar,
        "profit_factor": profit_factor,
        "trade_count": float(len(trades)) if trades else None,
        "worst_month": worst_month,
        "cvar95": _cvar95_pct(daily_rets),
        "monthly_skew": _skew(month_rets),
        "sharpe_cost_stress": _sharpe(week_cost_rets, 52),
        "ann_return": ann_return,
        "oos_is_sharpe": oos_is,
        "wf_efficiency": oos_is,
        "pbo": None,
        "bootstrap_p5_sharpe": _bootstrap_p5(daily_rets, 252),
        "years_covered": float(len(years)) if years else None,
        "trial_log": None,
        "net_vega": None,
        "net_delta": None,
        "payoff_ratio": payoff,
        "expectancy_bps": expectancy,
        "peak_margin": max(margins) if margins else None,
        "capital_util": (
            avg_notional / nav * 100.0 if (avg_notional := _mean(daily_notional)) is not None else None
        ),
        "incubation_months": None,
        "incubation_trades": None,
        "slippage_ratio_inc": None,
        "incubation_sharpe_ratio": None,
        "ops_errors": None,
        "kill_switch": None,
        "dd_stop": None,
        "missed_fills": None,
        "missed_fills_pct": None,
        "avg_slippage_pct": None,
        "slippage_ratio": None,
    }

    by_stage: dict[str, list[dict[str, Any]]] = defaultdict(list)
    names: dict[str, str] = {}
    for spec in SPECS:
        names[spec.stage] = spec.stage_name
        value = values.get(spec.key)
        status = evaluate(value, spec)
        by_stage[spec.stage].append(
            {
                "key": spec.key,
                "label": spec.label,
                "source": spec.source,
                "critical": spec.critical,
                "target": spec.pass_at,
                "min": spec.warn_at,
                "direction": spec.direction,
                "value": None if value is None else round(value, 4),
                "status": status.value,
                "definition": spec.definition,
                "unit": spec.unit,
            }
        )

    stages: list[dict[str, Any]] = []
    for sid in ("s1", "s2", "s3", "s4", "s5", "exec"):
        rows = by_stage[sid]
        filled = sum(1 for r in rows if r["status"] != KpiStatus.EMPTY.value)
        stages.append(
            {
                "id": sid,
                "name": names[sid],
                "verdict": _stage_verdict(rows),
                "filled": filled,
                "total": len(rows),
                "kpis": rows,
            }
        )

    score_stages = [s for s in stages if s["id"] != "exec"]
    overall = KpiStatus.EMPTY.value
    if any(s["verdict"] == KpiStatus.FAIL.value for s in score_stages):
        overall = KpiStatus.FAIL.value
    elif any(s["verdict"] == KpiStatus.WARN.value for s in score_stages):
        overall = KpiStatus.WARN.value
    elif any(s["verdict"] == KpiStatus.PASS.value for s in score_stages):
        overall = KpiStatus.PASS.value

    week_series = [
        {"week": f"{year}-W{week:02d}", "return_pct": round(weekly[(year, week)] * 100.0, 4)}
        for (year, week) in sorted(weekly)
    ][-8:]

    as_of = days[-1].isoformat() if days else None
    return {
        "as_of": as_of,
        "days": len(reports),
        "assumptions": {
            "nav": nav,
            "contracts_per_trade": cfg.ibkr_kpi_contracts,
            "option_multiplier": default_mult,
            "round_trip_slippage": slippage,
        },
        "overall": overall,
        "stages": stages,
        "weeks": week_series,
        "trade_count": len(trades),
        "span_days": span_days,
    }
