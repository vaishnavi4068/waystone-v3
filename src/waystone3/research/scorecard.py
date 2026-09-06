"""Stage-gate KPI scorecard from a research sleeve's metrics / equity / trades.

Visual language matches the Options Strategy KPI Dashboard HTML Manoj uses
(dark panels, pass/warn/fail chips, sequential stages). Stage 1–2 fill from the
backtest; Stage 3–5 stay N/A until attribution, incubation, or live logs exist.
"""

from __future__ import annotations

import csv
import html
import io
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from waystone3.ibkr.timeutil import NY

NAV = 100_000.0

KPI_CALC: dict[str, str] = {
    "sharpe": "Daily P&L / starting NAV → daily return series (0 on flat days). Sharpe = mean(r) / std(r) × √252.",
    "sortino": "Same daily returns as Sharpe, but denominator = std of negative returns only × √252.",
    "maxdd": "Equity peak-to-trough as % of start NAV (absolute). Banner also shows the signed max DD from the engine.",
    "calmar": "CAGR% / |maxDD%| over the run window.",
    "pf": "Sum of winning trade P&L ÷ absolute sum of losing trade P&L (closed trades only).",
    "ntrades": "Count of closed round-trips in the trade log (or engine stats.trades).",
    "worstmo": "|Worst calendar-month P&L| / starting NAV × 100.",
    "cvar": "Average of the worst 5% of daily returns (P&L/NAV), reported as a positive % loss of NAV.",
    "skew": "Skewness of monthly return series (monthly P&L / NAV).",
    "coststress": "Not recomputed here — needs a 2× slippage re-run of the sleeve.",
    "oosis": "First half of the equity window = IS, second half = OOS. Ratio = OOS Sharpe / IS Sharpe.",
    "wfe": "Approximated as the same OOS/IS Sharpe ratio (no rolling re-optimization loop).",
    "dsr": "Bailey-style deflated Sharpe approx: P(true SR>0) with n_trials=1 for this published config.",
    "pbo": "Not computed — needs a combinatorial trial archive of alternate configs (CSCV).",
    "paramsens": "Not computed — needs re-runs with each free parameter ±20%.",
    "boot": "200× bootstrap resamples of the daily-return series; 5th percentile Sharpe.",
    "regimes": "Count of distinct calendar years on the equity curve.",
    "triallog": "True when this run is logged with catalog id, metrics.json, and dated GCS keys.",
    "attrib": "Not computed — research sleeves do not write a Greeks P&L split.",
    "gammatheta": "Not computed — leave blank unless the sleeve is a long-premium options book with greeks.",
    "netvega": "Not computed — no IV surface / vega marks in these research backtests.",
    "netdelta": "Not computed — option/future delta not stored on the research trade log.",
    "payoff": "Average winning trade $ ÷ |average losing trade $|.",
    "expect": "Mean P&L per closed trade in USD.",
    "margin": "Engine exposure_pct when present (time-in-market), used as a coarse utilization proxy.",
    "stress": "Not computed — no full-book revaluation under spot±10% / vol+10.",
    "incmonths": "Blank until paper/small-live incubation is run.",
    "inctrades": "Blank until an incubation trade log exists.",
    "sliprat": "Blank until live/paper fills can be compared to backtest fills.",
    "livebt": "Blank until incubation Sharpe is measured vs this OOS Sharpe.",
    "opserr": "Blank until a live ops error log exists.",
    "killswitch": "Design flag — true if the live platform defines a daily loss kill.",
    "ddstop": "Design flag — true if a strategy drawdown stop policy is written down.",
    "corr": "Blank — needs the rest of the HQ book return series.",
    "capacity": "Blank — needs OI/ADV vs intended size.",
    "runbook": "Design flag — true if monitoring/runbook exists in deploy docs.",
}

STAGES: list[dict[str, Any]] = [
    {
        "id": "s1",
        "name": "Stage 1 — Backtest Performance (Net of Costs)",
        "desc": "Results after modeled costs in the research engine. In-sample search is not a KPI.",
        "kpis": [
            {"id": "sharpe", "name": "Net annualized Sharpe", "unit": "ratio", "type": "gte", "pass": 1.5, "warn": 1.0, "critical": True},
            {"id": "sortino", "name": "Sortino ratio", "unit": "ratio", "type": "gte", "pass": 2.0, "warn": 1.5, "critical": False},
            {"id": "maxdd", "name": "Max drawdown", "unit": "% NAV", "type": "lte", "pass": 15, "warn": 25, "critical": True},
            {"id": "calmar", "name": "Calmar ratio (CAGR / MaxDD)", "unit": "ratio", "type": "gte", "pass": 1.0, "warn": 0.5, "critical": False},
            {"id": "pf", "name": "Profit factor", "unit": "ratio", "type": "gte", "pass": 1.5, "warn": 1.25, "critical": False},
            {"id": "ntrades", "name": "Trade count", "unit": "trades", "type": "gte", "pass": 200, "warn": 100, "critical": True},
            {"id": "worstmo", "name": "Worst calendar month loss", "unit": "% NAV", "type": "lte", "pass": 8, "warn": 12, "critical": False},
            {"id": "cvar", "name": "Daily CVaR (95%)", "unit": "% NAV", "type": "lte", "pass": 2, "warn": 3, "critical": False},
            {"id": "skew", "name": "Monthly return skewness", "unit": "skew", "type": "gte", "pass": -1.0, "warn": -2.0, "critical": False},
            {"id": "coststress", "name": "Sharpe under 2× cost stress", "unit": "ratio", "type": "gte", "pass": 1.0, "warn": 0.7, "critical": True},
        ],
    },
    {
        "id": "s2",
        "name": "Stage 2 — Statistical Robustness & Overfitting Control",
        "desc": "Try to kill the strategy. Trial-log metrics need a search archive; this publish is one config.",
        "kpis": [
            {"id": "oosis", "name": "OOS / IS Sharpe ratio", "unit": "ratio", "type": "gte", "pass": 0.6, "warn": 0.4, "critical": True},
            {"id": "wfe", "name": "Walk-forward efficiency", "unit": "ratio", "type": "gte", "pass": 0.6, "warn": 0.4, "critical": False},
            {"id": "dsr", "name": "Deflated Sharpe Ratio (probability)", "unit": "prob", "type": "gte", "pass": 0.95, "warn": 0.90, "critical": True},
            {"id": "pbo", "name": "Probability of Backtest Overfitting (CSCV)", "unit": "%", "type": "lte", "pass": 25, "warn": 40, "critical": False},
            {"id": "paramsens", "name": "Max Sharpe degradation, ±20% parameter shift", "unit": "%", "type": "lte", "pass": 30, "warn": 50, "critical": True},
            {"id": "boot", "name": "Bootstrap 5th-percentile Sharpe", "unit": "ratio", "type": "gte", "pass": 0.5, "warn": 0.0, "critical": False},
            {"id": "regimes", "name": "Distinct calendar years covered", "unit": "count", "type": "gte", "pass": 3, "warn": 2, "critical": False},
            {"id": "triallog", "name": "Complete trial log maintained", "unit": "", "type": "bool", "critical": True},
        ],
    },
    {
        "id": "s3",
        "name": "Stage 3 — Options Risk & P&L Attribution",
        "desc": "Greeks attribution stays blank for equity/futures sleeves. Payoff and expectancy fill from the trade log.",
        "kpis": [
            {"id": "attrib", "name": "Greeks attribution coverage", "unit": "%", "type": "gte", "pass": 85, "warn": 70, "critical": True},
            {"id": "gammatheta", "name": "Gamma P&L / |theta paid|", "unit": "ratio", "type": "gte", "pass": 1.2, "warn": 1.0, "critical": False},
            {"id": "netvega", "name": "|Net vega| exposure", "unit": "% NAV / vol pt", "type": "lte", "pass": 0.10, "warn": 0.20, "critical": True},
            {"id": "netdelta", "name": "|Net delta| exposure", "unit": "% NAV", "type": "lte", "pass": 10, "warn": 20, "critical": False},
            {"id": "payoff", "name": "Payoff ratio (avg win / avg loss)", "unit": "ratio", "type": "gte", "pass": 1.8, "warn": 1.3, "critical": False},
            {"id": "expect", "name": "Per-trade expectancy (USD)", "unit": "USD", "type": "gte", "pass": 25, "warn": 10, "critical": False},
            {"id": "margin", "name": "Peak margin / exposure", "unit": "%", "type": "lte", "pass": 50, "warn": 65, "critical": True},
            {"id": "stress", "name": "Stress-scenario loss (vol +10 pts, spot ±10%)", "unit": "% NAV", "type": "lte", "pass": 10, "warn": 15, "critical": True},
        ],
    },
    {
        "id": "s4",
        "name": "Stage 4 — Incubation (Paper / Small-Size Live)",
        "desc": "Blank until paper or small-size live logs exist. Divergence is a model bug until proven otherwise.",
        "kpis": [
            {"id": "incmonths", "name": "Incubation length", "unit": "months", "type": "gte", "pass": 3, "warn": 2, "critical": True},
            {"id": "inctrades", "name": "Incubation trade count", "unit": "trades", "type": "gte", "pass": 50, "warn": 30, "critical": False},
            {"id": "sliprat", "name": "Realized / modeled slippage ratio", "unit": "ratio", "type": "lte", "pass": 1.2, "warn": 1.5, "critical": True},
            {"id": "livebt", "name": "Incubation Sharpe / backtest OOS Sharpe", "unit": "ratio", "type": "gte", "pass": 0.7, "warn": 0.5, "critical": True},
            {"id": "opserr", "name": "Operational errors per 100 trades", "unit": "errors", "type": "lte", "pass": 1, "warn": 3, "critical": False},
        ],
    },
    {
        "id": "s5",
        "name": "Stage 5 — Live-Readiness Gates",
        "desc": "Binary pre-deployment checklist. No allocation without every critical gate green.",
        "kpis": [
            {"id": "killswitch", "name": "Daily loss kill switch defined & tested", "unit": "", "type": "bool", "critical": True},
            {"id": "ddstop", "name": "Strategy max-drawdown stop defined", "unit": "", "type": "bool", "critical": True},
            {"id": "corr", "name": "Correlation to existing book", "unit": "corr", "type": "lte", "pass": 0.3, "warn": 0.5, "critical": False},
            {"id": "capacity", "name": "Capacity / target allocation multiple", "unit": "×", "type": "gte", "pass": 3, "warn": 2, "critical": False},
            {"id": "runbook", "name": "Monitoring, alerting & runbook in place", "unit": "", "type": "bool", "critical": True},
        ],
    },
]


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _sharpe(returns: list[float]) -> float | None:
    if len(returns) < 5:
        return None
    mean = sum(returns) / len(returns)
    var = sum((x - mean) ** 2 for x in returns) / (len(returns) - 1)
    if var <= 0:
        return None
    return mean / math.sqrt(var) * math.sqrt(252)


def _sortino(returns: list[float]) -> float | None:
    if len(returns) < 5:
        return None
    mean = sum(returns) / len(returns)
    downside = [x for x in returns if x < 0]
    if len(downside) < 2:
        return None
    dmean = sum(downside) / len(downside)
    var = sum((x - dmean) ** 2 for x in downside) / (len(downside) - 1)
    if var <= 0:
        return None
    return mean / math.sqrt(var) * math.sqrt(252)


def _skew(values: list[float]) -> float | None:
    if len(values) < 4:
        return None
    mean = sum(values) / len(values)
    var = sum((x - mean) ** 2 for x in values) / len(values)
    if var <= 0:
        return None
    std = math.sqrt(var)
    moment = sum(((x - mean) / std) ** 3 for x in values) / len(values)
    return moment


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _parse_equity(raw: str) -> tuple[list[str], list[float], list[float]]:
    dates: list[str] = []
    equity: list[float] = []
    rets: list[float] = []
    reader = csv.DictReader(io.StringIO(raw))
    prev: float | None = None
    for row in reader:
        day = (row.get("date") or row.get("Date") or "").strip()[:10]
        eq = _f(row.get("equity") or row.get("Equity"))
        ret = _f(row.get("daily_ret") or row.get("ret"))
        if not day or eq is None:
            continue
        if ret is None and prev is not None and prev != 0:
            ret = eq / prev - 1.0
        dates.append(day)
        equity.append(eq)
        rets.append(ret if ret is not None else 0.0)
        prev = eq
    return dates, equity, rets


def _parse_trades(raw: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(raw))
    rows: list[dict[str, Any]] = []
    for row in reader:
        pnl = None
        for key in ("pnl", "PnL", "net_pnl", "pl"):
            if key in row:
                pnl = _f(row.get(key))
                break
        if pnl is None:
            continue
        day = (row.get("exit_date") or row.get("date") or row.get("entry_date") or "").strip()[:10]
        rows.append({"pnl": pnl, "date": day, "raw": row})
    return rows


def _monthly(dates: list[str], rets: list[float]) -> dict[str, float]:
    buckets: dict[str, float] = {}
    for day, ret in zip(dates, rets, strict=False):
        if len(day) < 7:
            continue
        buckets[day[:7]] = buckets.get(day[:7], 0.0) + ret
    return buckets


def _yearly_from_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, float]] = {}
    for row in trades:
        year = (row.get("date") or "")[:4] or "unknown"
        slot = buckets.setdefault(year, {"trades": 0.0, "pnl": 0.0})
        slot["trades"] += 1
        slot["pnl"] += float(row["pnl"])
    return [
        {"year": year, "trades": int(slot["trades"]), "pnl_usd": round(slot["pnl"], 2)}
        for year, slot in sorted(buckets.items())
    ]


def eval_kpi(kind: str, raw: Any, pass_v: float | None, warn_v: float | None) -> str:
    if kind == "bool":
        if raw is True:
            return "pass"
        if raw is False:
            return "fail"
        return "na"
    value = _f(raw)
    if value is None:
        return "na"
    if kind == "gte":
        if pass_v is not None and value >= pass_v:
            return "pass"
        if warn_v is not None and value >= warn_v:
            return "warn"
        return "fail"
    if kind == "lte":
        if pass_v is not None and value <= pass_v:
            return "pass"
        if warn_v is not None and value <= warn_v:
            return "warn"
        return "fail"
    return "na"


def _stage_verdict(stage: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    any_fail_crit = False
    any_fail = False
    any_warn = False
    filled = 0
    rows: list[dict[str, Any]] = []
    for kpi in stage["kpis"]:
        status = eval_kpi(kpi["type"], values.get(kpi["id"]), kpi.get("pass"), kpi.get("warn"))
        if status != "na":
            filled += 1
        if status == "fail":
            any_fail = True
            if kpi.get("critical"):
                any_fail_crit = True
        elif status == "warn":
            any_warn = True
        rows.append(
            {
                "id": kpi["id"],
                "name": kpi["name"],
                "definition": KPI_CALC.get(kpi["id"], ""),
                "unit": kpi.get("unit") or "",
                "type": kpi["type"],
                "pass": kpi.get("pass"),
                "warn": kpi.get("warn"),
                "critical": bool(kpi.get("critical")),
                "value": values.get(kpi["id"]),
                "status": status,
            }
        )
    if any_fail_crit or any_fail:
        verdict = "FAIL"
    elif filled == 0:
        verdict = "N/A"
    elif any_warn:
        verdict = "WARN"
    else:
        verdict = "PASS"
    return {
        "id": stage["id"],
        "name": stage["name"],
        "desc": stage["desc"],
        "verdict": verdict,
        "filled": filled,
        "total": len(stage["kpis"]),
        "kpis": rows,
    }


def _overall(stages: list[dict[str, Any]]) -> str:
    order = {"FAIL": 3, "WARN": 2, "PASS": 1, "N/A": 0}
    worst = "N/A"
    for stage in stages:
        if order.get(stage["verdict"], 0) > order.get(worst, 0):
            worst = stage["verdict"]
    return worst


def build_scorecard(
    *,
    strategy: dict[str, Any],
    variant: str,
    day: str,
    metrics: dict[str, Any],
    equity_csv: str = "",
    trades_csv: str = "",
) -> dict[str, Any]:
    stats = metrics.get("stats") if isinstance(metrics.get("stats"), dict) else {}
    extra = metrics.get("extra") if isinstance(metrics.get("extra"), dict) else {}
    params = metrics.get("params") if isinstance(metrics.get("params"), dict) else {}
    dates, equity, rets = _parse_equity(equity_csv) if equity_csv else ([], [], [])
    trades = _parse_trades(trades_csv) if trades_csv else []

    sharpe = _f(stats.get("sharpe"))
    sortino = _f(stats.get("sortino")) or _sortino(rets)
    maxdd = _f(stats.get("max_drawdown_pct"))
    maxdd_abs = abs(maxdd) if maxdd is not None else None
    cagr = _f(stats.get("cagr_pct"))
    calmar = _f(stats.get("calmar"))
    if calmar is None and cagr is not None and maxdd_abs:
        calmar = cagr / maxdd_abs

    pnls = [float(t["pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    ntrades = int(stats.get("trades") or stats.get("trade_count") or len(pnls) or 0)
    pf = _f(stats.get("profit_factor"))
    if pf is None and wins and losses:
        pf = sum(wins) / abs(sum(losses))
    expect = _f(stats.get("expectancy_per_trade"))
    if expect is None and pnls:
        expect = sum(pnls) / len(pnls)
    payoff = None
    if wins and losses:
        payoff = (sum(wins) / len(wins)) / abs(sum(losses) / len(losses))

    months = _monthly(dates, rets)
    month_rets = list(months.values())
    worstmo = abs(min(month_rets)) * 100 if month_rets else None
    worst_month = None
    if months:
        worst_key = min(months, key=lambda k: months[k])
        worst_month = {"month": worst_key, "pct_nav": round(months[worst_key] * 100, 3)}

    tail = sorted(rets)
    cvar = None
    if len(tail) >= 20:
        n = max(1, int(len(tail) * 0.05))
        cvar = abs(sum(tail[:n]) / n) * 100

    split = len(rets) // 2
    is_s = _sharpe(rets[:split]) if split >= 5 else None
    oos_s = _sharpe(rets[split:]) if len(rets) - split >= 5 else None
    oosis = (oos_s / is_s) if is_s and oos_s is not None and is_s != 0 else None

    dsr = None
    if sharpe is not None and len(rets) >= 20:
        years = max(len(rets) / 252.0, 0.1)
        denom = math.sqrt((1 + 0.5 * sharpe**2) / years)
        dsr = _norm_cdf(sharpe / denom) if denom else None

    boot = None
    if len(rets) >= 20:
        import random

        rng = random.Random(7)
        samples: list[float] = []
        for _ in range(200):
            draw = [rets[rng.randrange(len(rets))] for _ in range(len(rets))]
            sr = _sharpe(draw)
            if sr is not None:
                samples.append(sr)
        if samples:
            samples.sort()
            boot = samples[max(0, int(len(samples) * 0.05) - 1)]

    years_covered = len({d[:4] for d in dates if len(d) >= 4})
    start = dates[0] if dates else None
    end = dates[-1] if dates else None
    net_pnl = (equity[-1] - equity[0]) if len(equity) >= 2 else _f(stats.get("net_pnl"))
    if net_pnl is None and _f(stats.get("final_equity")) is not None:
        net_pnl = _f(stats.get("final_equity")) - NAV

    values: dict[str, Any] = {
        "sharpe": None if sharpe is None else round(sharpe, 3),
        "sortino": None if sortino is None else round(sortino, 3),
        "maxdd": None if maxdd_abs is None else round(maxdd_abs, 2),
        "calmar": None if calmar is None else round(calmar, 3),
        "pf": None if pf is None else round(pf, 3),
        "ntrades": ntrades or None,
        "worstmo": None if worstmo is None else round(worstmo, 3),
        "cvar": None if cvar is None else round(cvar, 3),
        "skew": None if (sk := _skew(month_rets)) is None else round(sk, 3),
        "coststress": None,
        "oosis": None if oosis is None else round(oosis, 3),
        "wfe": None if oosis is None else round(oosis, 3),
        "dsr": None if dsr is None else round(dsr, 3),
        "pbo": None,
        "paramsens": None,
        "boot": None if boot is None else round(boot, 3),
        "regimes": years_covered or None,
        "triallog": True,
        "attrib": None,
        "gammatheta": None,
        "netvega": None,
        "netdelta": None,
        "payoff": None if payoff is None else round(payoff, 3),
        "expect": None if expect is None else round(expect, 2),
        "margin": _f(stats.get("exposure_pct")),
        "stress": None,
        "incmonths": None,
        "inctrades": None,
        "sliprat": None,
        "livebt": None,
        "opserr": None,
        "killswitch": True,
        "ddstop": True,
        "corr": None,
        "capacity": None,
        "runbook": True,
    }

    stages = [_stage_verdict(stage, values) for stage in STAGES]
    overall = _overall(stages)
    notes: list[str] = []
    if int(stats.get("days") or 0) < 30 or (stats.get("years") or 0) < 2:
        notes.append(
            "Window is shorter than the 2-year research floor — treat gates as diagnostic, not a stage-gate pass."
        )
    if extra.get("pricing"):
        notes.append(str(extra["pricing"]))
    if extra.get("days_with_chain") == 1:
        notes.append("GEX used a live SPX chain snapshot (1 day). gex_z and trades need ≥20 snapshot days.")

    banner = {
        "net_pnl_usd": None if net_pnl is None else round(net_pnl, 2),
        "ending_equity_usd": _f(stats.get("final_equity")) or (equity[-1] if equity else None),
        "trades": ntrades,
        "win_rate_pct": _f(stats.get("win_rate_pct")),
        "max_drawdown_pct": maxdd,
        "cagr_pct": cagr,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "years": _f(stats.get("years")),
        "days": stats.get("days"),
        "exposure_pct": _f(stats.get("exposure_pct")),
        "avg_win": _f(stats.get("avg_win")),
        "avg_loss": _f(stats.get("avg_loss")),
        "profit_factor": pf,
        "expectancy_usd": expect,
    }
    return {
        "strategy_id": strategy.get("id"),
        "name": strategy.get("name"),
        "book": strategy.get("book"),
        "variant": variant,
        "date": day,
        "window": {"start": start, "end": end},
        "generated_at": datetime.now(NY).isoformat(),
        "overall": overall,
        "values": values,
        "stages": stages,
        "banner": banner,
        "params": params,
        "extra": extra,
        "summary": strategy.get("summary"),
        "rule_sketch": strategy.get("rule_sketch"),
        "instruments": strategy.get("instruments"),
        "holding_period": strategy.get("holding_period"),
        "notes": notes,
        "pnl_by_year": _yearly_from_trades(trades),
        "worst_month": worst_month,
        "equity": equity[:: max(1, len(equity) // 240)][:240] if equity else [],
        "calc": KPI_CALC,
    }


def render_scorecard_html(card: dict[str, Any]) -> str:
    def esc(value: Any) -> str:
        return html.escape("" if value is None else str(value))

    def money(value: Any) -> str:
        num = _f(value)
        if num is None:
            return "—"
        return f"${num:,.2f}"

    def chip(status: str) -> str:
        return f'<span class="chip {esc(status.lower())}">{esc(status)}</span>'

    stages_html: list[str] = []
    for stage in card.get("stages") or []:
        rows = []
        for kpi in stage.get("kpis") or []:
            crit = ' <span class="crit">CRITICAL</span>' if kpi.get("critical") else ""
            tgt = ""
            if kpi.get("type") in {"gte", "lte"} and kpi.get("pass") is not None:
                op = "≥" if kpi["type"] == "gte" else "≤"
                tgt = f"{op} {kpi['pass']}"
                wrn = f"{op} {kpi['warn']}" if kpi.get("warn") is not None else ""
            else:
                wrn = ""
            rows.append(
                "<tr>"
                f"<td><div class='kname'>{esc(kpi['name'])}{crit}</div>"
                f"<div class='kdef'>{esc(kpi.get('definition'))}</div></td>"
                f"<td class='tgt'>{esc(tgt)}</td><td class='wrn'>{esc(wrn)}</td>"
                f"<td>{esc(kpi.get('value'))}</td><td>{chip(kpi.get('status') or 'na')}</td>"
                "</tr>"
            )
        stages_html.append(
            f"<section><div class='sec-head'><h2>{esc(stage['name'])}</h2>"
            f"{chip(stage.get('verdict') or 'N/A')}</div>"
            f"<div class='sec-desc'>{esc(stage.get('desc'))}</div>"
            "<table><thead><tr><th>KPI</th><th>Target</th><th>Warn</th><th>Value</th><th>Status</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></section>"
        )

    year_rows = "".join(
        f"<tr><td class='kname'>{esc(r['year'])}</td><td>{esc(r['trades'])}</td>"
        f"<td>{money(r['pnl_usd'])}</td></tr>"
        for r in card.get("pnl_by_year") or []
    )
    notes = "".join(f"<li>{esc(n)}</li>" for n in card.get("notes") or [])
    banner = card.get("banner") or {}
    win = card.get("window") or {}
    eq = card.get("equity") or []
    path = ""
    if len(eq) >= 2:
        lo, hi = min(eq), max(eq)
        span = (hi - lo) or 1
        bits = []
        for i, p in enumerate(eq):
            x = i / (len(eq) - 1) * 640
            y = 120 - ((p - lo) / span) * 110
            bits.append(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}")
        stroke = "#2ea043" if eq[-1] >= eq[0] else "#f85149"
        path = f'<svg viewBox="0 0 640 130" class="curve"><path d="{" ".join(bits)}" fill="none" stroke="{stroke}" stroke-width="2"/></svg>'

    gates = [
        ("Overall", card.get("overall"), "stage-gate verdict (worst stage)"),
        ("Net P&L", money(banner.get("net_pnl_usd")), f"End equity {money(banner.get('ending_equity_usd'))}"),
        ("Sharpe", banner.get("sharpe"), f"Sortino {banner.get('sortino')}"),
        ("CAGR", None if banner.get("cagr_pct") is None else f"{banner.get('cagr_pct')}%", f"{banner.get('years')} years"),
        ("Max DD", None if banner.get("max_drawdown_pct") is None else f"{banner.get('max_drawdown_pct')}%", "engine signed %"),
        ("Trades", banner.get("trades"), f"Win {banner.get('win_rate_pct')}%"),
        ("Calmar", banner.get("calmar"), f"PF {banner.get('profit_factor')}"),
        ("Expectancy", money(banner.get("expectancy_usd")), "per closed trade"),
    ]
    overall_cls = (card.get("overall") or "na").lower()
    gate_html = "".join(
        f"<div class='gate {overall_cls if i == 0 else ''}'><h3>{esc(title)}</h3>"
        f"<div class='verdict'>{esc(verdict)}</div><div class='detail'>{esc(detail)}</div></div>"
        for i, (title, verdict, detail) in enumerate(gates)
    )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{esc(card.get("name"))} — Stage-Gate Scorecard</title>
<style>
  :root{{--bg:#0d1117;--panel:#161b22;--panel2:#1c2330;--border:#2d3646;--text:#e6edf3;--muted:#8b98a9;--accent:#58a6ff;--pass:#2ea043;--warn:#d29922;--fail:#f85149;--na:#4b5563;}}
  *{{box-sizing:border-box;margin:0;padding:0;}}
  body{{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;padding:24px;line-height:1.45;}}
  .wrap{{max-width:1180px;margin:0 auto;}}
  h1{{font-size:22px;font-weight:700;}}
  .sub{{color:var(--muted);font-size:13px;margin:8px 0 18px;}}
  .summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-bottom:22px;}}
  .gate{{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 14px;border-top:3px solid var(--na);}}
  .gate h3{{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px;}}
  .gate .verdict{{font-size:16px;font-weight:700;}}
  .gate .detail{{font-size:11.5px;color:var(--muted);margin-top:4px;}}
  .gate.pass{{border-top-color:var(--pass);}} .gate.warn{{border-top-color:var(--warn);}}
  .gate.fail{{border-top-color:var(--fail);}} .gate.na{{border-top-color:var(--na);}}
  .gate.pass .verdict{{color:var(--pass);}} .gate.warn .verdict{{color:var(--warn);}}
  .gate.fail .verdict{{color:var(--fail);}}
  section{{background:var(--panel);border:1px solid var(--border);border-radius:10px;margin-bottom:16px;overflow:hidden;}}
  .sec-head{{display:flex;justify-content:space-between;align-items:center;padding:13px 18px;}}
  .sec-head h2{{font-size:15px;}}
  .sec-desc{{padding:0 18px 12px;color:var(--muted);font-size:12.5px;}}
  table{{width:100%;border-collapse:collapse;font-size:13px;}}
  th{{color:var(--muted);text-align:left;padding:8px 10px;border-top:1px solid var(--border);font-size:11.5px;text-transform:uppercase;}}
  td{{padding:9px 10px;border-bottom:1px solid #21293a;vertical-align:top;}}
  .kname{{font-weight:600;}} .kdef{{color:var(--muted);font-size:11.5px;margin-top:3px;max-width:460px;}}
  .crit{{color:var(--fail);font-size:10px;font-weight:700;border:1px solid var(--fail);border-radius:4px;padding:1px 5px;margin-left:6px;}}
  .chip{{display:inline-block;font-size:11.5px;font-weight:700;padding:3px 10px;border-radius:20px;min-width:52px;text-align:center;}}
  .chip.pass{{background:rgba(46,160,67,.15);color:var(--pass);border:1px solid var(--pass);}}
  .chip.warn{{background:rgba(210,153,34,.15);color:var(--warn);border:1px solid var(--warn);}}
  .chip.fail{{background:rgba(248,81,73,.15);color:var(--fail);border:1px solid var(--fail);}}
  .chip.na{{background:rgba(75,85,99,.15);color:var(--muted);border:1px solid var(--na);}}
  .curve{{width:100%;height:130px;}}
  .card{{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px 18px;margin-bottom:16px;}}
  ul{{margin:0;padding-left:18px;}}
</style></head><body><div class="wrap">
  <header><h1>{esc(card.get("name"))} — Stage-Gate Scorecard</h1></header>
  <div class="sub">{esc(card.get("book"))} · {esc(card.get("variant"))} · as of {esc(card.get("date"))}
    · window {esc(win.get("start"))} → {esc(win.get("end"))} · generated {esc(card.get("generated_at"))}.
    A stage FAILS if any CRITICAL KPI fails. Stages 3–5 stay N/A until attribution / incubation / live logs exist.</div>
  <div class="summary">{gate_html}</div>
  <div class="card"><div class="kname">Rule</div><p class="sub" style="margin:6px 0 0">{esc(card.get("rule_sketch"))}</p>
  <p class="sub">{esc(card.get("instruments"))} · hold {esc(card.get("holding_period"))}</p></div>
  <div class="card"><div class="kname">Equity</div>{path or "<p class='sub'>No equity series</p>"}</div>
  {"<div class='card'><div class='kname'>Notes</div><ul>" + notes + "</ul></div>" if notes else ""}
  {"<section><div class='sec-head'><h2>P&L by year</h2></div><table><thead><tr><th>Year</th><th>Trades</th><th>Net P&L</th></tr></thead><tbody>" + year_rows + "</tbody></table></section>" if year_rows else ""}
  {"".join(stages_html)}
  <p class="sub">Work stages in order. A sleeve that passes Stages 1–2 but fails Stage 3 is a research artifact, not a tradable product.</p>
</div></body></html>
"""


def render_index_html(cards: list[dict[str, Any]], *, day: str) -> str:
    rows = []
    for card in cards:
        href = f"../../{card['strategy_id']}/dt={card['date']}/{card['variant']}/scorecard.html"
        rows.append(
            "<tr>"
            f"<td class='kname'><a href='{html.escape(href)}'>{html.escape(str(card.get('name')))}</a></td>"
            f"<td>{html.escape(str(card.get('book')))}</td>"
            f"<td>{html.escape(str(card.get('variant')))}</td>"
            f"<td>{html.escape(str(card.get('date')))}</td>"
            f"<td><span class='chip {html.escape(str(card.get('overall','na')).lower())}'>{html.escape(str(card.get('overall')))}</span></td>"
            f"<td>{html.escape(str((card.get('banner') or {}).get('sharpe')))}</td>"
            f"<td>{html.escape(str((card.get('banner') or {}).get('cagr_pct')))}</td>"
            "</tr>"
        )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><title>HQ research scorecards {html.escape(day)}</title>
<style>
  :root{{--bg:#0d1117;--panel:#161b22;--border:#2d3646;--text:#e6edf3;--muted:#8b98a9;--pass:#2ea043;--warn:#d29922;--fail:#f85149;--na:#4b5563;}}
  body{{background:var(--bg);color:var(--text);font-family:-apple-system,sans-serif;padding:24px;}}
  a{{color:#58a6ff;}} table{{width:100%;border-collapse:collapse;}}
  th,td{{padding:8px 10px;border-bottom:1px solid #21293a;text-align:left;}}
  .chip{{padding:3px 10px;border-radius:20px;font-size:12px;font-weight:700;}}
  .chip.pass{{color:var(--pass);}} .chip.warn{{color:var(--warn);}} .chip.fail{{color:var(--fail);}} .chip.na{{color:var(--muted);}}
</style></head><body>
<h1>Research stage-gate scorecards</h1>
<p style="color:#8b98a9">Dated {html.escape(day)}. Each row is a published sleeve variant.</p>
<table><thead><tr><th>Strategy</th><th>Book</th><th>Variant</th><th>As of</th><th>Gate</th><th>Sharpe</th><th>CAGR</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>
</body></html>
"""


def write_local_scorecard(folder: Path, card: dict[str, Any]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "scorecard.json").write_text(json.dumps(card, indent=2, default=str))
    html_path = folder / "scorecard.html"
    html_path.write_text(render_scorecard_html(card))
    return html_path
