#!/usr/bin/env python3
"""Combine sleeve equity curves into one research book (see ALLOCATOR_SPEC.md).

  python ml/allocator.py --book book.yaml
  python ml/allocator.py --book book.yaml --synthetic
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wsbt import metrics as M  # noqa: E402
from wsbt.report import RESULTS  # noqa: E402
from ml.kpi_export import compute_kpis  # noqa: E402


def _resolve(path: str | Path, base: Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else base / p


def load_book_config(path: Path) -> dict[str, Any]:
    """Parse the small book.yaml schema without a PyYAML dependency."""
    cfg: dict[str, Any] = {"sleeves": []}
    sleeve: dict[str, Any] | None = None
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith("  - id:"):
            if sleeve:
                cfg["sleeves"].append(sleeve)
            sleeve = {"id": line.split(":", 1)[1].strip()}
            continue
        if sleeve is not None and line.startswith("    "):
            key, val = line.strip().split(":", 1)
            val = val.strip()
            if key == "weight":
                sleeve[key] = float(val)
            else:
                sleeve[key] = val
            continue
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key, val = key.strip(), val.strip()
        if key == "sleeves":
            continue
        if key == "version":
            cfg[key] = int(val)
        elif key in {"nav", "warmup_days"}:
            cfg[key] = int(float(val))
        else:
            cfg[key] = val
    if sleeve:
        cfg["sleeves"].append(sleeve)
    if not cfg.get("name"):
        raise ValueError(f"book config missing name: {path}")
    return cfg


def load_sleeve_returns(equity_path: Path, nav: float) -> pd.Series:
    df = pd.read_csv(equity_path, parse_dates=["date"])
    if "date" not in df.columns:
        raise ValueError(f"{equity_path} missing date column")
    idx = pd.DatetimeIndex(df["date"]).normalize()
    if "daily_ret" in df.columns:
        ret = pd.Series(df["daily_ret"].astype(float).to_numpy(), index=idx, name=equity_path.parent.name)
    elif "daily_pnl" in df.columns:
        ret = pd.Series(df["daily_pnl"].astype(float).to_numpy() / nav, index=idx, name=equity_path.parent.name)
    else:
        eq = df["equity"].astype(float)
        ret = eq.pct_change().fillna(0.0)
        ret = pd.Series(ret.to_numpy(), index=idx, name=equity_path.parent.name)
    return ret.sort_index()


def align_sleeve_returns(series: dict[str, pd.Series]) -> pd.DataFrame:
    if not series:
        raise ValueError("no sleeve return series")
    frame = pd.DataFrame(series).fillna(0.0)
    frame.index = pd.DatetimeIndex(frame.index).normalize()
    return frame.sort_index()


def combine_returns(frame: pd.DataFrame, weights: dict[str, float], method: str) -> pd.Series:
    if method == "sleeve_mean":
        return frame.mean(axis=1)
    if method != "equal_weight":
        raise ValueError(f"unknown method: {method}")
    w = pd.Series({col: float(weights.get(col, 1.0)) for col in frame.columns}, dtype=float)
    total = w.sum()
    if total <= 0:
        raise ValueError("sleeve weights must sum to a positive number")
    w = w / total
    return (frame * w).sum(axis=1)


def synthetic_sleeves(n_days: int = 400, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2022-01-03", periods=n_days)
    out: dict[str, pd.Series] = {}
    for i, name in enumerate(("sleeve_a", "sleeve_b", "sleeve_c")):
        ret = rng.normal(0.0002 + 0.0001 * i, 0.008, size=n_days)
        out[name] = pd.Series(ret, index=idx)
    return pd.DataFrame(out)


def run(cfg: dict[str, Any], *, base: Path = ROOT, synthetic: bool = False) -> Path:
    nav = float(cfg.get("nav", 100_000))
    method = str(cfg.get("method", "equal_weight"))
    warmup = int(cfg.get("warmup_days", 0))
    name = str(cfg.get("name", "book"))
    sleeves = cfg.get("sleeves") or []

    if synthetic:
        frame = synthetic_sleeves()
        weights = {col: 1.0 for col in frame.columns}
        sleeve_ids = list(frame.columns)
    else:
        if not sleeves:
            raise ValueError("book.yaml must list at least one sleeve")
        series: dict[str, pd.Series] = {}
        weights: dict[str, float] = {}
        sleeve_ids: list[str] = []
        for row in sleeves:
            sid = str(row["id"])
            eq_path = _resolve(row["equity"], base)
            if not eq_path.exists():
                raise FileNotFoundError(f"missing sleeve equity: {eq_path}")
            series[sid] = load_sleeve_returns(eq_path, nav)
            weights[sid] = float(row.get("weight", 1.0))
            sleeve_ids.append(sid)
        frame = align_sleeve_returns(series)

    daily_ret = combine_returns(frame, weights, method)
    daily_pnl = daily_ret * nav
    equity = M.equity_from_returns(daily_ret, nav)

    if warmup > 0 and len(daily_ret) > warmup:
        stats_ret = daily_ret.iloc[warmup:]
        stats_pnl = daily_pnl.iloc[warmup:]
    else:
        stats_ret = daily_ret
        stats_pnl = daily_pnl

    stats = M.summary(stats_ret)
    eq_w = frame.mean(axis=1)
    baseline = M.summary(eq_w.iloc[warmup:] if warmup else eq_w)

    out_dir = RESULTS / name
    out_dir.mkdir(parents=True, exist_ok=True)
    eq_out = pd.DataFrame({"equity": equity, "daily_ret": daily_ret, "daily_pnl": daily_pnl})
    eq_out.index.name = "date"
    eq_out.to_csv(out_dir / "equity.csv")
    frame.to_csv(out_dir / "sleeve_returns.csv")

    extra = {
        "method": method,
        "sleeves": sleeve_ids,
        "n_sleeves": len(sleeve_ids),
        "weights": {k: round(v, 6) for k, v in weights.items()},
        "overlap_start": str(frame.index.min().date()) if len(frame) else None,
        "overlap_end": str(frame.index.max().date()) if len(frame) else None,
        "n_days": int(len(daily_ret)),
        "warmup_days": warmup,
        "sleeve_mean_baseline": {"sharpe": baseline.get("sharpe"), "max_drawdown_pct": baseline.get("max_drawdown_pct")},
        "synthetic": synthetic,
    }
    params = {"book": cfg.get("name"), "nav": nav, "method": method, "warmup_days": warmup, "n_sleeves": len(sleeve_ids)}
    (out_dir / "metrics.json").write_text(
        json.dumps({"strategy": name, "synthetic": synthetic, "params": params, "stats": stats, "extra": extra}, indent=1, default=str)
    )
    kpis = compute_kpis(pd.DataFrame(), stats_pnl, nav, name=name, family="book")
    (out_dir / "kpi.json").write_text(json.dumps(kpis, indent=1, default=str))

    print(M.format_report(name, stats, {"kpi": {k: kpis.get(k) for k in ("sharpe", "maxdd", "dsr", "boot")}}))
    print(f"  sleeves={sleeve_ids} method={method} days={len(daily_ret)}")
    print(f"  written -> {out_dir}/")
    return out_dir


def main() -> None:
    ap = argparse.ArgumentParser(description="Combine sleeve equity curves into one research book.")
    ap.add_argument("--book", default="book.yaml", help="YAML config path (relative to waystone_backtests/)")
    ap.add_argument("--synthetic", action="store_true", help="Use fake sleeve returns for tests")
    args = ap.parse_args()

    book_path = _resolve(args.book, ROOT)
    cfg = load_book_config(book_path) if not args.synthetic else {
        "version": 1,
        "name": "book_synthetic",
        "nav": 100_000,
        "method": "equal_weight",
        "warmup_days": 20,
    }
    run(cfg, synthetic=args.synthetic)


if __name__ == "__main__":
    main()
