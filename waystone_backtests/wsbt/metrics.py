"""Performance metrics on a daily P&L / return series, plus the deflated Sharpe ratio."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy.stats import norm

TRADING_DAYS = 252
EULER_GAMMA = 0.5772156649015329


def equity_from_returns(daily_ret: pd.Series, start_equity: float = 100_000.0) -> pd.Series:
    return start_equity * (1.0 + daily_ret.fillna(0.0)).cumprod()


def drawdown(equity: pd.Series) -> pd.Series:
    peak = equity.cummax()
    return equity / peak - 1.0


def summary(daily_ret: pd.Series, trades: pd.DataFrame | None = None, start_equity: float = 100_000.0,
            periods: int = TRADING_DAYS) -> dict:
    """daily_ret: fractional return on the strategy's capital per trading day (0 when flat).
    trades: optional DataFrame with a 'pnl' column ($ net of costs) and optionally 'bars'/'days' held."""
    r = daily_ret.dropna().astype(float)
    n = len(r)
    if n == 0:
        return {"days": 0}
    eq = equity_from_returns(r, start_equity)
    years = n / periods
    total = float(eq.iloc[-1] / start_equity - 1.0)
    cagr = (1.0 + total) ** (1.0 / years) - 1.0 if years > 0 and total > -1 else float("nan")
    vol = float(r.std(ddof=1) * math.sqrt(periods)) if n > 1 else float("nan")
    sharpe = float(r.mean() / r.std(ddof=1) * math.sqrt(periods)) if n > 1 and r.std(ddof=1) > 0 else float("nan")
    downside = r[r < 0].std(ddof=1) if (r < 0).sum() > 1 else float("nan")
    sortino = float(r.mean() / downside * math.sqrt(periods)) if downside and downside > 0 else float("nan")
    dd = drawdown(eq)
    maxdd = float(dd.min())
    calmar = cagr / abs(maxdd) if maxdd < 0 else float("nan")
    exposure = float((r != 0).mean())
    out = {
        "days": n, "years": round(years, 2), "total_return_pct": round(100 * total, 2),
        "cagr_pct": round(100 * cagr, 2) if cagr == cagr else None,
        "ann_vol_pct": round(100 * vol, 2) if vol == vol else None,
        "sharpe": round(sharpe, 2) if sharpe == sharpe else None,
        "sortino": round(sortino, 2) if sortino == sortino else None,
        "max_drawdown_pct": round(100 * maxdd, 2), "calmar": round(calmar, 2) if calmar == calmar else None,
        "exposure_pct": round(100 * exposure, 1),
        "best_day_pct": round(100 * float(r.max()), 2), "worst_day_pct": round(100 * float(r.min()), 2),
        "skew": round(float(r.skew()), 2) if n > 2 else None, "kurtosis": round(float(r.kurt()), 2) if n > 3 else None,
        "final_equity": round(float(eq.iloc[-1]), 2),
    }
    if trades is not None and len(trades):
        pnl = trades["pnl"].astype(float)
        wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
        out.update({
            "trades": int(len(pnl)), "trades_per_year": round(len(pnl) / years, 1) if years > 0 else None,
            "win_rate_pct": round(100 * float((pnl > 0).mean()), 1),
            "avg_win": round(float(wins.mean()), 2) if len(wins) else None,
            "avg_loss": round(float(losses.mean()), 2) if len(losses) else None,
            "profit_factor": round(float(wins.sum() / -losses.sum()), 2) if len(losses) and losses.sum() < 0 else None,
            "expectancy_per_trade": round(float(pnl.mean()), 2),
            "net_pnl": round(float(pnl.sum()), 2),
        })
        for col in ("days_held", "bars_held"):
            if col in trades:
                out[f"avg_{col}"] = round(float(trades[col].mean()), 1)
    return out


def sharpe_per_period(daily_ret: pd.Series) -> float:
    r = daily_ret.dropna().astype(float)
    return float(r.mean() / r.std(ddof=1)) if len(r) > 1 and r.std(ddof=1) > 0 else 0.0


def deflated_sharpe(sr_obs: float, n_obs: int, n_trials: int, trial_sr_var: float,
                    skew: float = 0.0, kurt: float = 3.0) -> dict:
    """Bailey & López de Prado (2014) Deflated Sharpe Ratio.

    sr_obs        : NON-annualised Sharpe of the selected strategy (per period, e.g. per day)
    n_obs         : number of return observations
    n_trials      : number of strategy variants tried (grid size x re-runs)
    trial_sr_var  : variance of the per-period Sharpe ratios across the trials
    skew, kurt    : of the selected strategy's returns (kurt = 3 for normal)

    Returns SR0 (the Sharpe you'd expect from the best of N random trials) and the
    probability that the observed Sharpe beats it.  DSR < 0.95 = not significant."""
    if n_trials <= 1 or trial_sr_var <= 0:
        sr0 = 0.0
    else:
        sd = math.sqrt(trial_sr_var)
        sr0 = sd * ((1 - EULER_GAMMA) * norm.ppf(1 - 1.0 / n_trials) + EULER_GAMMA * norm.ppf(1 - 1.0 / (n_trials * math.e)))
    denom = math.sqrt(max(1e-12, 1 - skew * sr_obs + (kurt - 1) / 4.0 * sr_obs ** 2))
    z = (sr_obs - sr0) * math.sqrt(max(1, n_obs - 1)) / denom
    return {"sr_obs_per_period": round(float(sr_obs), 4), "sr0_expected_max_of_trials": round(float(sr0), 4),
            "n_trials": n_trials, "dsr_probability": round(float(norm.cdf(z)), 3)}


def format_report(title: str, stats: dict, extra: dict | None = None) -> str:
    lines = ["=" * 78, f"  {title}", "-" * 78]
    order = ["days", "years", "total_return_pct", "cagr_pct", "ann_vol_pct", "sharpe", "sortino", "max_drawdown_pct",
             "calmar", "exposure_pct", "trades", "trades_per_year", "win_rate_pct", "avg_win", "avg_loss",
             "profit_factor", "expectancy_per_trade", "net_pnl", "avg_days_held", "avg_bars_held",
             "best_day_pct", "worst_day_pct", "skew", "kurtosis", "final_equity"]
    for k in order:
        if k in stats and stats[k] is not None:
            lines.append(f"  {k:24s}: {stats[k]}")
    if extra:
        lines.append("-" * 78)
        for k, v in extra.items():
            lines.append(f"  {k:24s}: {v}")
    lines.append("=" * 78)
    return "\n".join(lines)
