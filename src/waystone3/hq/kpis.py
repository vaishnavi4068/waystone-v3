"""Port of ES_Futures_KPI_Dashboard.xlsx: Daily Calc, scorecard, returns and rolling sync.

Formulas follow the workbook cell-for-cell (tab "9. Daily Calc" and "2. KPI Dashboard"),
including its text outcomes ("No drawdown", "No losses", "Gross ≤ 0", "n/a").
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from waystone3.hq.calendar import month_start, trading_days, week_end

CRITICAL_ORDER = (
    "fut_trade_count",
    "fut_sharpe",
    "fut_calmar",
    "fut_max_dd",
    "fut_dd_duration_mo",
    "fut_ann_vol",
    "fut_cvar95",
    "fut_ulcer",
    "fut_profit_factor",
    "fut_win_rate",
    "fut_time_in_market",
    "fut_cost_drag",
    "fut_slippage_realism",
)
_GREEN_TEXT = {"No drawdown", "No losses"}
_RED_TEXT = {"Gross ≤ 0"}


@dataclass(frozen=True)
class Settings:
    starting_capital: float
    session_minutes: int
    ann_days: int = 252
    min_days_ratio: int = 5
    min_days_calmar: int = 21
    risk_free_rate: float = 0.0
    model_slip_rt_per_contract: float = 0.0


@dataclass(frozen=True)
class ClosedTrade:
    session_date: date
    net_pnl: float
    gross_pnl: float
    commission: float
    slippage_cost: float
    pnl_at_signal: float
    hold_min: float
    contracts: float


@dataclass(frozen=True)
class KpiDef:
    code: str
    direction: str | None
    green_at: float | None
    amber_at: float | None
    is_critical: bool


@dataclass
class DailyRow:
    session_date: date
    week_end: date
    month_start: date
    trades: int
    gross_pnl: float
    commission: float
    slippage_cost: float
    net_pnl: float
    hold_min_total: float
    equity_start: float
    equity_end: float
    daily_return: float
    peak_itd: float
    dd_itd: float
    uw_days_itd: int
    peak_week: float
    dd_week: float
    uw_days_week: int
    peak_month: float
    dd_month: float
    uw_days_month: int


def daily_calc(
    trades: Iterable[ClosedTrade],
    settings: Settings,
    start: date,
    end: date,
    holidays: Collection[date],
) -> list[DailyRow]:
    by_day: dict[date, list[ClosedTrade]] = {}
    for trade in trades:
        by_day.setdefault(trade.session_date, []).append(trade)
    rows: list[DailyRow] = []
    capital = settings.starting_capital
    prev: DailyRow | None = None
    for day in trading_days(start, end, holidays):
        todays = by_day.get(day, [])
        net = sum(t.net_pnl for t in todays)
        equity_start = prev.equity_end if prev else capital
        equity_end = equity_start + net
        wk, mo = week_end(day), month_start(day)
        peak_itd = max(prev.peak_itd if prev else capital, equity_end)
        same_week = prev is not None and prev.week_end == wk
        same_month = prev is not None and prev.month_start == mo
        peak_week = (
            max(prev.peak_week, equity_end) if same_week and prev else max(equity_start, equity_end)
        )
        peak_month = (
            max(prev.peak_month, equity_end)
            if same_month and prev
            else max(equity_start, equity_end)
        )
        dd_itd = 1 - equity_end / peak_itd
        dd_week = 1 - equity_end / peak_week
        dd_month = 1 - equity_end / peak_month
        row = DailyRow(
            session_date=day,
            week_end=wk,
            month_start=mo,
            trades=len(todays),
            gross_pnl=sum(t.gross_pnl for t in todays),
            commission=sum(t.commission for t in todays),
            slippage_cost=sum(t.slippage_cost for t in todays),
            net_pnl=net,
            hold_min_total=sum(t.hold_min for t in todays),
            equity_start=equity_start,
            equity_end=equity_end,
            daily_return=net / equity_start if equity_start else 0.0,
            peak_itd=peak_itd,
            dd_itd=dd_itd,
            uw_days_itd=((prev.uw_days_itd if prev else 0) + 1) if dd_itd > 0 else 0,
            peak_week=peak_week,
            dd_week=dd_week,
            uw_days_week=((prev.uw_days_week if same_week and prev else 0) + 1)
            if dd_week > 0
            else 0,
            peak_month=peak_month,
            dd_month=dd_month,
            uw_days_month=(
                ((prev.uw_days_month if same_month and prev else 0) + 1) if dd_month > 0 else 0
            ),
        )
        rows.append(row)
        prev = row
    return rows


def _percentile_inc(values: list[float], q: float) -> float:
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def status_for(value: float | str | None, kpi: KpiDef) -> str:
    if isinstance(value, str):
        if value in _GREEN_TEXT:
            return "GREEN"
        if value in _RED_TEXT:
            return "RED"
        return "NA"
    if value is None or kpi.green_at is None or kpi.amber_at is None:
        return "NA"
    if kpi.direction == "Higher":
        return "GREEN" if value >= kpi.green_at else "AMBER" if value >= kpi.amber_at else "RED"
    return "GREEN" if value <= kpi.green_at else "AMBER" if value <= kpi.amber_at else "RED"


@dataclass
class WindowResult:
    window: str
    start: date
    end: date
    trading_days: int
    trades: int
    net_pnl: float
    return_pct: float | None
    equity_end: float | None
    values: dict[str, float | str | None]


def window_kpis(
    window: str,
    rows: list[DailyRow],
    trades: list[ClosedTrade],
    settings: Settings,
    start: date,
    end: date,
) -> WindowResult:
    days = [r for r in rows if start <= r.session_date <= end]
    in_window = [t for t in trades if start <= t.session_date <= end]
    n = len(days)
    count = len(in_window)
    values: dict[str, float | str | None] = {}
    net = sum(r.net_pnl for r in days)
    start_equity = days[0].equity_start if days else None
    ret = net / start_equity if start_equity else None
    returns = [r.daily_return for r in days]
    dd_attr, uw_attr = {
        "WEEK": ("dd_week", "uw_days_week"),
        "MTD": ("dd_month", "uw_days_month"),
        "ITD": ("dd_itd", "uw_days_itd"),
    }[window]
    dds = [float(getattr(r, dd_attr)) for r in days]
    ann = settings.ann_days

    values["fut_trade_count"] = count
    if n:
        sd = statistics.stdev(returns) if n > 1 else 0.0
        if n < settings.min_days_ratio or sd == 0:
            values["fut_sharpe"] = "n/a"
            values["fut_ann_vol"] = "n/a" if n < settings.min_days_ratio else 0.0
        else:
            mean = statistics.fmean(returns) - settings.risk_free_rate / ann
            values["fut_sharpe"] = mean / sd * math.sqrt(ann)
            values["fut_ann_vol"] = sd * math.sqrt(ann)
        max_dd = max(dds)
        values["fut_max_dd"] = max_dd
        values["fut_dd_duration_mo"] = max(int(getattr(r, uw_attr)) for r in days) / (ann / 12)
        if n < settings.min_days_ratio:
            values["fut_cvar95"] = "n/a"
        else:
            cutoff = _percentile_inc(returns, 0.05)
            tail = [r for r in returns if r <= cutoff]
            values["fut_cvar95"] = max(0.0, -statistics.fmean(tail))
        values["fut_ulcer"] = math.sqrt(statistics.fmean(d * d for d in dds)) * 100
        if n < settings.min_days_calmar:
            values["fut_calmar"] = "n/a"
        elif max_dd == 0:
            values["fut_calmar"] = "No drawdown"
        elif ret is not None and 1 + ret > 0:
            values["fut_calmar"] = ((1 + ret) ** (ann / n) - 1) / max_dd
        else:
            values["fut_calmar"] = "n/a"
        values["fut_time_in_market"] = sum(t.hold_min for t in in_window) / (
            n * settings.session_minutes
        )
    if count:
        wins = sum(t.net_pnl for t in in_window if t.net_pnl > 0)
        losses = -sum(t.net_pnl for t in in_window if t.net_pnl < 0)
        if losses == 0:
            values["fut_profit_factor"] = "No losses" if wins > 0 else "n/a"
        else:
            values["fut_profit_factor"] = wins / losses
        values["fut_win_rate"] = sum(1 for t in in_window if t.net_pnl > 0) / count
        pre_cost = sum(t.pnl_at_signal for t in in_window)
        slip = sum(t.slippage_cost for t in in_window)
        comm = sum(t.commission for t in in_window)
        values["fut_cost_drag"] = "Gross ≤ 0" if pre_cost <= 0 else (slip + comm) / pre_cost
        contracts = sum(t.contracts for t in in_window)
        per_contract = slip / contracts if contracts else 0.0
        values["fut_slippage_realism"] = (
            "No slippage recorded"
            if per_contract <= 0
            else settings.model_slip_rt_per_contract / per_contract
        )
    values["hdr_trading_days"] = n
    values["hdr_trades"] = count
    values["hdr_net_pnl"] = net
    values["hdr_return"] = ret
    values["hdr_equity_end"] = days[-1].equity_end if days else None
    return WindowResult(
        window, start, end, n, count, net, ret, days[-1].equity_end if days else None, values
    )


def window_bounds(as_of: date, paper_start: date) -> dict[str, tuple[date, date]]:
    friday = week_end(as_of)
    return {
        "WEEK": (friday - timedelta(days=4), min(as_of, friday)),
        "MTD": (month_start(as_of), as_of),
        "ITD": (paper_start, as_of),
    }


@dataclass
class ScoredWindow:
    result: WindowResult
    statuses: dict[str, str]
    red: int
    amber: int
    green: int
    gate: str | None


def score_window(result: WindowResult, defs: dict[str, KpiDef]) -> ScoredWindow:
    statuses: dict[str, str] = {}
    for code, value in result.values.items():
        kpi = defs.get(code)
        if kpi is None or code.startswith("hdr_") or (code == "fut_trade_count" and result.window != "ITD"):
            statuses[code] = "INFO"
        else:
            statuses[code] = status_for(value, kpi)
    scored = [statuses[c] for c in CRITICAL_ORDER if c in statuses]
    red, amber, green = (scored.count(s) for s in ("RED", "AMBER", "GREEN"))
    gate: str | None = None
    if result.window == "ITD":
        if result.trades == 0:
            gate = "Log paper trades to evaluate"
        elif statuses.get("fut_trade_count") == "RED":
            gate = (
                f"INSUFFICIENT SAMPLE — {result.trades:,} trades; keep paper trading before any "
                "go/no-go call (Tier 0)"
            )
        elif (reds := sum(1 for c in CRITICAL_ORDER[1:] if statuses.get(c) == "RED")) > 0:
            gate = f"RED FLAGS — {reds} KPI(s) in the reject zone"
        elif amber > 0:
            gate = f"PASSING WITH WATCH ITEMS — {amber} amber"
        else:
            gate = "ALL GREEN"
    return ScoredWindow(result, statuses, red, amber, green, gate)


@dataclass
class PeriodRow:
    key: date
    number: int
    period_start: date
    period_end: date
    trading_days: int
    trades: int
    net_pnl: float
    start_equity: float
    return_pct: float | None
    max_dd: float | None
    win_rate: float | None
    cum_pnl: float
    account_value: float
    cum_return: float
    peak_account: float
    dd_from_peak: float
    sharpe: float | None = None
    profit_factor: float | None = None


def period_returns(
    rows: list[DailyRow], trades: list[ClosedTrade], settings: Settings, by: str
) -> list[PeriodRow]:
    """Workbook tabs 5/6/7: one row per week (Mon–Fri) or calendar month reached."""
    groups: dict[date, list[DailyRow]] = {}
    for row in rows:
        groups.setdefault(row.week_end if by == "week" else row.month_start, []).append(row)
    out: list[PeriodRow] = []
    capital = settings.starting_capital
    cum = 0.0
    peak = capital
    for number, (key, days) in enumerate(sorted(groups.items()), start=1):
        lo, hi = days[0].session_date, days[-1].session_date
        period_trades = [t for t in trades if lo <= t.session_date <= hi]
        net = sum(d.net_pnl for d in days)
        start_equity = days[0].equity_start
        cum += net
        account = capital + cum
        peak = max(peak, account)
        dd_attr = "dd_week" if by == "week" else "dd_month"
        wins = sum(1 for t in period_trades if t.net_pnl > 0)
        row = PeriodRow(
            key=key,
            number=number,
            period_start=key - timedelta(days=4) if by == "week" else key,
            period_end=key,
            trading_days=len(days),
            trades=len(period_trades),
            net_pnl=net,
            start_equity=start_equity,
            return_pct=net / start_equity if start_equity else None,
            max_dd=max(getattr(d, dd_attr) for d in days),
            win_rate=wins / len(period_trades) if period_trades else None,
            cum_pnl=cum,
            account_value=account,
            cum_return=cum / capital,
            peak_account=peak,
            dd_from_peak=1 - account / peak,
        )
        if by == "month":
            rets = [d.daily_return for d in days]
            if len(rets) >= settings.min_days_ratio and statistics.stdev(rets) > 0:
                row.sharpe = (
                    statistics.fmean(rets) / statistics.stdev(rets) * math.sqrt(settings.ann_days)
                )
            losses = -sum(t.net_pnl for t in period_trades if t.net_pnl < 0)
            gains = sum(t.net_pnl for t in period_trades if t.net_pnl > 0)
            row.profit_factor = gains / losses if losses else None
        out.append(row)
    return out


@dataclass(frozen=True)
class SyncPoint:
    session_date: date
    live_trades: int
    live_net_pnl: float
    bt_net_pnl: float | None
    pnl_delta_pct: float | None
    sync_status: str


@dataclass
class RollingRow:
    as_of: date
    days_logged: int
    all_time_trades: int
    all_time_live_pnl: float
    all_time_bt_pnl: float | None
    avg_pnl_delta_pct: float | None
    all_time_flags: int
    flag_rate: float | None
    last7_live_pnl: float
    last7_flags: int


def sync_rolling(points: list[SyncPoint]) -> list[RollingRow]:
    """Daily Sync Log "ROLLING KPIs" block, evaluated as of every logged day."""
    ordered = sorted(points, key=lambda p: p.session_date)
    out: list[RollingRow] = []
    for idx, point in enumerate(ordered):
        upto = ordered[: idx + 1]
        last7 = [p for p in upto if p.session_date >= point.session_date - timedelta(days=7)]
        bt = [p.bt_net_pnl for p in upto if p.bt_net_pnl is not None]
        pcts = [p.pnl_delta_pct for p in upto if p.pnl_delta_pct is not None]
        flags = sum(1 for p in upto if p.sync_status == "FLAG")
        rated = sum(1 for p in upto if p.sync_status != "N/A")
        out.append(
            RollingRow(
                as_of=point.session_date,
                days_logged=len(upto),
                all_time_trades=sum(p.live_trades for p in upto),
                all_time_live_pnl=sum(p.live_net_pnl for p in upto),
                all_time_bt_pnl=sum(bt) if bt else None,
                avg_pnl_delta_pct=statistics.fmean(pcts) if pcts else None,
                all_time_flags=flags,
                flag_rate=flags / rated if rated else None,
                last7_live_pnl=sum(p.live_net_pnl for p in last7),
                last7_flags=sum(1 for p in last7 if p.sync_status == "FLAG"),
            )
        )
    return out
