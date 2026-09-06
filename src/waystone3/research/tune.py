"""Parameter tuning for research sleeves, with the audit trail the stage gates ask for.

The tuner never edits a strategy's rules. It re-runs ``backtest.py`` over the grid
declared in ``catalog.json`` (``scripts[].grid``), keeps a cumulative trial log, and
picks the configuration from the *plateau* (own in-sample Sharpe blended with its
grid neighbours) rather than the single best point. It then re-runs the pick with
2× costs and ±20% parameter shifts, runs an anchored walk-forward, and computes
CSCV probability-of-backtest-overfitting and the deflated Sharpe over the whole
trial log. Everything lands in ``results/<variant>/tuning.json`` and
``trials.csv`` so the scorecard can fill Stage 1-2 honestly.
"""

from __future__ import annotations

import csv
import itertools
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from waystone3.ibkr.timeutil import NY
from waystone3.research.catalog import get_strategy, load_catalog
from waystone3.research.paths import toolkit_root
from waystone3.research.window import MAX_YEARS, MIN_YEARS, resolve_window

TRADING_DAYS = 252
EULER_GAMMA = 0.5772156649015329
IS_FRAC = 0.6
DEFAULT_CONSTRAINTS = {"min_trades": 200, "max_dd_pct": 15.0}


# ── small stats ──────────────────────────────────────────────────────────────
def sharpe(r: pd.Series | np.ndarray) -> float:
    arr = np.asarray(r, dtype=float)
    arr = arr[~np.isnan(arr)]
    if len(arr) < 5 or arr.std(ddof=1) == 0:
        return 0.0
    return float(arr.mean() / arr.std(ddof=1) * math.sqrt(TRADING_DAYS))


def _norm_ppf(p: float) -> float:
    from scipy.stats import norm

    return float(norm.ppf(p))


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def deflated_sharpe(r: pd.Series, trial_sr_pp: list[float]) -> dict[str, Any]:
    """Bailey & López de Prado DSR: P(true SR > 0) after N trials with the observed SR dispersion."""
    arr = np.asarray(r, dtype=float)
    n = len(arr)
    sd = arr.std(ddof=1) if n > 1 else 0.0
    sr = float(arr.mean() / sd) if sd > 0 else 0.0
    trials = [x for x in trial_sr_pp if x == x]
    n_trials = max(1, len(trials))
    var = float(np.var(trials, ddof=1)) if len(trials) > 1 else 0.0
    if n_trials <= 1 or var <= 0:
        sr0 = 0.0
    else:
        s = math.sqrt(var)
        sr0 = s * ((1 - EULER_GAMMA) * _norm_ppf(1 - 1.0 / n_trials) + EULER_GAMMA * _norm_ppf(1 - 1.0 / (n_trials * math.e)))
    skew = float(pd.Series(arr).skew()) if n > 2 else 0.0
    kurt = float(pd.Series(arr).kurt() + 3) if n > 3 else 3.0
    denom = math.sqrt(max(1e-12, 1 - skew * sr + (kurt - 1) / 4.0 * sr**2))
    z = (sr - sr0) * math.sqrt(max(1, n - 1)) / denom
    return {
        "sr_obs_per_period": round(sr, 5),
        "sr0_expected_max_of_trials": round(sr0, 5),
        "n_trials": n_trials,
        "trial_sr_var": var,
        "probability": round(_norm_cdf(z), 4),
    }


def pbo_cscv(matrix: np.ndarray, blocks: int = 8) -> dict[str, Any]:
    """Combinatorially symmetric cross-validation (Bailey, Borwein, López de Prado, Zhu 2014).

    matrix: T×N daily returns, one column per trial. Split T into ``blocks`` chunks; every
    half/half combination is an IS/OOS pair. Pick the IS-best column, look up its OOS rank.
    PBO = share of combinations where the IS-best is below the OOS median."""
    t, n = matrix.shape
    if n < 2 or t < blocks * 20:
        return {"pbo_pct": None, "combos": 0, "blocks": blocks}
    chunks = np.array_split(np.arange(t), blocks)
    logits: list[float] = []
    for pick in itertools.combinations(range(blocks), blocks // 2):
        is_idx = np.concatenate([chunks[k] for k in pick])
        oos_idx = np.concatenate([chunks[k] for k in range(blocks) if k not in pick])
        is_sr = np.array([sharpe(matrix[is_idx, j]) for j in range(n)])
        oos_sr = np.array([sharpe(matrix[oos_idx, j]) for j in range(n)])
        best = int(np.argmax(is_sr))
        rank = (oos_sr < oos_sr[best]).sum() + 0.5 * ((oos_sr == oos_sr[best]).sum() - 1)
        omega = (rank + 0.5) / n
        omega = min(max(omega, 1e-6), 1 - 1e-6)
        logits.append(math.log(omega / (1 - omega)))
    arr = np.array(logits)
    return {"pbo_pct": round(100 * float((arr <= 0).mean()), 1), "combos": len(arr), "blocks": blocks}


# ── trials ───────────────────────────────────────────────────────────────────
@dataclass
class Trial:
    params: dict[str, Any]
    args: list[str]
    cost_mult: float
    purpose: str
    ok: bool = False
    folder: str = ""
    daily: pd.Series | None = None
    stats: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def key(self) -> str:
        return json.dumps(self.params, sort_keys=True, default=str)

    def row(self, is_frac: float = IS_FRAC) -> dict[str, Any]:
        r = self.daily if self.daily is not None else pd.Series(dtype=float)
        cut = int(len(r) * is_frac)
        return {
            "ts": datetime.now(NY).isoformat(timespec="seconds"),
            "purpose": self.purpose,
            "cost_mult": self.cost_mult,
            "params": self.key,
            "ok": self.ok,
            "sharpe": self.stats.get("sharpe"),
            "cagr_pct": self.stats.get("cagr_pct"),
            "max_drawdown_pct": self.stats.get("max_drawdown_pct"),
            "trades": self.stats.get("trades"),
            "profit_factor": self.stats.get("profit_factor"),
            "is_sharpe": round(sharpe(r.iloc[:cut]), 3) if len(r) else None,
            "oos_sharpe": round(sharpe(r.iloc[cut:]), 3) if len(r) else None,
            "error": self.error[-300:],
        }


def _flag_args(base: list[str], overrides: dict[str, Any]) -> list[str]:
    """Replace/insert CLI flags in ``base``. bool True -> bare flag, False/None -> flag removed,
    list -> flag followed by each element."""
    out: list[str] = []
    skip = set(overrides)
    i = 0
    while i < len(base):
        tok = base[i]
        if tok in skip:
            i += 1
            while i < len(base) and not base[i].startswith("--"):
                i += 1
            continue
        out.append(tok)
        i += 1
    for flag, value in overrides.items():
        if value is None or value is False:
            continue
        if value is True:
            out.append(flag)
        elif isinstance(value, (list, tuple)):
            out.extend([flag, *[str(v) for v in value]])
        else:
            out.append(flag)
            out.append(str(value))
    return out


def grid_points(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    keys = list(grid)
    return [dict(zip(keys, combo, strict=True)) for combo in itertools.product(*[grid[k] for k in keys])]


def _neighbours(point: dict[str, Any], grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key, values in grid.items():
        try:
            pos = values.index(point[key])
        except ValueError:
            continue
        for step in (-1, 1):
            j = pos + step
            if 0 <= j < len(values):
                nb = dict(point)
                nb[key] = values[j]
                out.append(nb)
    return out


def _shift(value: Any, pct: float) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        shifted = int(round(value * (1 + pct)))
        if shifted == value:
            shifted = value + (1 if pct > 0 else -1)
        return shifted if shifted > 0 else None
    return round(value * (1 + pct), 6)


class Tuner:
    def __init__(
        self,
        strategy_id: str,
        *,
        script_index: int = 0,
        workers: int | None = None,
        years: float = MAX_YEARS,
        min_years: float = MIN_YEARS,
        log=print,
    ) -> None:
        row = get_strategy(strategy_id)
        if row is None:
            raise ValueError(f"unknown strategy {strategy_id}")
        self.row = row
        self.sid = strategy_id
        self.script_index = script_index
        self.spec = (row.get("scripts") or [{}])[script_index]
        self.grid: dict[str, list[Any]] = dict(self.spec.get("grid") or {})
        self.constraints = {**DEFAULT_CONSTRAINTS, **(self.spec.get("constraints") or {})}
        self.root = toolkit_root()
        self.script = self.root / row["folder"] / "backtest.py"
        self.data_dir = Path(os.environ.get("WSBT_DATA_DIR", self.root / "data"))
        self.results_dir = Path(os.environ.get("WSBT_RESULTS_DIR", self.root / "results"))
        self.workers = max(1, workers or min(8, (os.cpu_count() or 2)))
        self.log = log
        self.window = resolve_window(
            list(row.get("daily_symbols") or []),
            self.data_dir,
            roots=list(row.get("intraday_roots") or []),
            min_years=min_years,
            max_years=years,
        )
        text = self.script.read_text()
        self.window_args: list[str] = []
        if self.window is not None:
            if "--start" in text:
                self.window_args += ["--start", self.window.start]
            if "--end" in text:
                self.window_args += ["--end", self.window.end]
        self.supports_cost_mult = "--cost-mult" in text
        self.base_args = [str(a) for a in (self.spec.get("args") or [])]

    # ── running ──────────────────────────────────────────────────────────
    def _run(self, trial: Trial) -> Trial:
        tmp = Path(tempfile.mkdtemp(prefix=f"wsbt_{self.sid}_"))
        env = os.environ.copy()
        env["WSBT_DATA_DIR"] = str(self.data_dir)
        env["WSBT_RESULTS_DIR"] = str(tmp)
        args = [sys.executable, str(self.script), *trial.args, *self.window_args]
        if trial.cost_mult != 1.0 and self.supports_cost_mult:
            args += ["--cost-mult", str(trial.cost_mult)]
        try:
            proc = subprocess.run(args, cwd=self.root, env=env, capture_output=True, text=True, check=False, timeout=1800)
            if proc.returncode != 0:
                trial.error = (proc.stdout + proc.stderr)[-1500:]
                return trial
            folders = [p for p in tmp.iterdir() if p.is_dir() and (p / "metrics.json").is_file()]
            if not folders:
                trial.error = "no metrics.json written"
                return trial
            folder = sorted(folders)[0]
            trial.folder = folder.name
            metrics = json.loads((folder / "metrics.json").read_text())
            trial.stats = metrics.get("stats") or {}
            eq = pd.read_csv(folder / "equity.csv", parse_dates=["date"]).set_index("date")
            trial.daily = eq["daily_ret"].astype(float)
            trial.ok = True
        except Exception as exc:  # noqa: BLE001
            trial.error = str(exc)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return trial

    def run_many(self, trials: list[Trial]) -> list[Trial]:
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return list(pool.map(self._run, trials))

    def make_trial(self, point: dict[str, Any], purpose: str, cost_mult: float = 1.0) -> Trial:
        return Trial(params=point, args=_flag_args(self.base_args, point), cost_mult=cost_mult, purpose=purpose)

    # ── selection ────────────────────────────────────────────────────────
    def _feasible(self, t: Trial) -> bool:
        if not t.ok or t.daily is None:
            return False
        trades = t.stats.get("trades") or 0
        dd = abs(float(t.stats.get("max_drawdown_pct") or 0.0))
        return trades >= self.constraints["min_trades"] and dd <= self.constraints["max_dd_pct"]

    def select(self, trials: list[Trial]) -> tuple[Trial, dict[str, Any]]:
        ok = [t for t in trials if t.ok and t.daily is not None]
        if not ok:
            raise RuntimeError("every trial failed: " + (trials[0].error if trials else ""))
        pool = [t for t in ok if self._feasible(t)]
        relaxed = False
        if not pool:
            relaxed = True
            pool = ok
        by_key = {t.key: t for t in ok}
        cut_frac = IS_FRAC

        def is_sr(t: Trial) -> float:
            r = t.daily
            return sharpe(r.iloc[: int(len(r) * cut_frac)])

        scored = []
        for t in pool:
            own = is_sr(t)
            nbs = [by_key[json.dumps(nb, sort_keys=True, default=str)] for nb in _neighbours(t.params, self.grid)
                   if json.dumps(nb, sort_keys=True, default=str) in by_key]
            nb_sr = [is_sr(nb) for nb in nbs]
            plateau = 0.5 * own + 0.5 * (float(np.mean(nb_sr)) if nb_sr else own)
            scored.append((plateau, own, t, nb_sr))
        scored.sort(key=lambda x: x[0], reverse=True)
        plateau, own, best, nb_sr = scored[0]
        info = {
            "rule": "max( 0.5*IS Sharpe + 0.5*mean IS Sharpe of grid neighbours ) over feasible trials",
            "is_frac": cut_frac,
            "feasible_trials": len([t for t in ok if self._feasible(t)]),
            "constraints": self.constraints,
            "constraints_relaxed": relaxed,
            "plateau_score": round(plateau, 3),
            "is_sharpe": round(own, 3),
            "neighbour_is_sharpe": [round(x, 3) for x in nb_sr],
            "top5": [
                {"params": t.params, "plateau": round(p, 3), "is_sharpe": round(o, 3),
                 "oos_sharpe": round(sharpe(t.daily.iloc[int(len(t.daily) * cut_frac):]), 3),
                 "sharpe": t.stats.get("sharpe"), "trades": t.stats.get("trades"), "maxdd": t.stats.get("max_drawdown_pct")}
                for p, o, t, _ in scored[:5]
            ],
        }
        return best, info

    # ── robustness ───────────────────────────────────────────────────────
    @staticmethod
    def walk_forward(trials: list[Trial], folds: int = 4) -> dict[str, Any]:
        """Anchored walk-forward: pick the IS-best config on everything before fold k, trade fold k."""
        ok = [t for t in trials if t.ok and t.daily is not None]
        if len(ok) < 2:
            return {"wfe": None, "folds": folds}
        panel = pd.concat({t.key: t.daily for t in ok}, axis=1).fillna(0.0).sort_index()
        n = len(panel)
        edges = [int(n * k / folds) for k in range(folds + 1)]
        oos_parts, is_srs, picks = [], [], []
        for k in range(1, folds):
            train = panel.iloc[: edges[k]]
            test = panel.iloc[edges[k] : edges[k + 1]]
            srs = train.apply(sharpe)
            pick = str(srs.idxmax())
            is_srs.append(float(srs.max()))
            oos_parts.append(test[pick])
            picks.append({"fold": k, "test_start": str(test.index[0].date()), "test_end": str(test.index[-1].date()),
                          "params": json.loads(pick), "is_sharpe": round(float(srs.max()), 3),
                          "oos_sharpe": round(sharpe(test[pick]), 3)})
        stitched = pd.concat(oos_parts)
        oos = sharpe(stitched)
        is_mean = float(np.mean(is_srs)) if is_srs else 0.0
        wfe = (oos / is_mean) if is_mean > 0 else None
        return {"wfe": None if wfe is None else round(min(wfe, 5.0), 3), "oos_sharpe": round(oos, 3),
                "is_sharpe_mean": round(is_mean, 3), "folds": folds, "picks": picks}

    def sensitivity(self, best: Trial) -> tuple[list[Trial], dict[str, Any]]:
        base_sr = float(best.stats.get("sharpe") or 0.0)
        trials: list[Trial] = []
        for key, value in best.params.items():
            for pct in (-0.2, 0.2):
                shifted = _shift(value, pct)
                if shifted is None or shifted == value:
                    continue
                point = dict(best.params)
                point[key] = shifted
                trials.append(self.make_trial(point, f"sens{pct:+.0%}"))
        done = self.run_many(trials) if trials else []
        rows, worst = [], 0.0
        for t in done:
            sr = float(t.stats.get("sharpe") or 0.0) if t.ok else float("nan")
            degr = (base_sr - sr) / base_sr * 100 if base_sr > 0 and sr == sr else None
            if degr is not None:
                worst = max(worst, degr)
            rows.append({"purpose": t.purpose, "params": t.params, "sharpe": None if sr != sr else round(sr, 3),
                         "degradation_pct": None if degr is None else round(degr, 1), "ok": t.ok})
        return done, {"base_sharpe": base_sr, "max_degradation_pct": round(worst, 1) if rows else None, "shifts": rows}

    # ── orchestration ────────────────────────────────────────────────────
    def tune(self, *, extra_points: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if not self.grid:
            raise ValueError(f"{self.sid}: scripts[{self.script_index}] has no 'grid' in catalog.json")
        points = grid_points(self.grid) + list(extra_points or [])
        self.log(f"{self.sid}: {len(points)} grid trials × {self.workers} workers  window={self.window_args or 'full'}")
        grid_trials = self.run_many([self.make_trial(p, "grid") for p in points])
        failed = [t for t in grid_trials if not t.ok]
        if failed:
            self.log(f"  {len(failed)} trial(s) failed, e.g. {failed[0].error[-400:]}")
        best, sel = self.select(grid_trials)
        self.log(f"  pick {best.params}  sharpe={best.stats.get('sharpe')} trades={best.stats.get('trades')} "
                 f"maxDD={best.stats.get('max_drawdown_pct')}")
        stress = self.run_many([self.make_trial(best.params, "cost2x", cost_mult=2.0)])[0] if self.supports_cost_mult else None
        sens_trials, sens = self.sensitivity(best)
        wf = self.walk_forward(grid_trials)
        ok = [t for t in grid_trials if t.ok and t.daily is not None]
        panel = pd.concat({t.key: t.daily for t in ok}, axis=1).fillna(0.0).sort_index()
        pbo = pbo_cscv(panel.to_numpy(), blocks=8 if len(panel) < 2000 else 10)

        folder = self.results_dir / best.folder
        folder.mkdir(parents=True, exist_ok=True)
        all_trials = grid_trials + ([stress] if stress else []) + sens_trials
        self._append_log(folder / "trials.csv", all_trials)
        logged = self._read_log(folder / "trials.csv")
        distinct = {r["params"]: r for r in logged if r.get("purpose") == "grid" and r.get("ok") in ("True", True)}
        sr_pp = [float(r["sharpe"]) / math.sqrt(TRADING_DAYS) for r in distinct.values() if r.get("sharpe") not in (None, "", "None")]
        dsr = deflated_sharpe(best.daily, sr_pp)

        r = best.daily
        cut = int(len(r) * IS_FRAC)
        out = {
            "strategy_id": self.sid,
            "variant_folder": best.folder,
            "generated_at": datetime.now(NY).isoformat(timespec="seconds"),
            "window": self.window_args,
            "grid": self.grid,
            "base_args": self.base_args,
            "chosen": {"params": best.params, "args": best.args, "stats": best.stats,
                       "is_sharpe": round(sharpe(r.iloc[:cut]), 3), "oos_sharpe": round(sharpe(r.iloc[cut:]), 3)},
            "selection": sel,
            "n_grid_trials": len(points),
            "n_trials_logged": len(distinct),
            "cost_stress": None if stress is None else {
                "cost_mult": 2.0, "ok": stress.ok, "sharpe": stress.stats.get("sharpe"),
                "cagr_pct": stress.stats.get("cagr_pct"), "net_pnl": stress.stats.get("net_pnl"), "error": stress.error[-300:]},
            "sensitivity": sens,
            "walk_forward": wf,
            "pbo": pbo,
            "dsr": dsr,
        }
        (folder / "tuning.json").write_text(json.dumps(out, indent=1, default=str))
        self.log(f"  cost2x sharpe={out['cost_stress'] and out['cost_stress']['sharpe']}  sens={sens['max_degradation_pct']}%  "
                 f"WFE={wf.get('wfe')}  PBO={pbo.get('pbo_pct')}%  DSR={dsr['probability']} (n={dsr['n_trials']})")
        self.log(f"  written -> {folder / 'tuning.json'}, trials.csv")
        return out

    # ── trial log ────────────────────────────────────────────────────────
    @staticmethod
    def _append_log(path: Path, trials: list[Trial]) -> None:
        rows = [t.row() for t in trials]
        if not rows:
            return
        new = not path.is_file()
        with path.open("a", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            if new:
                writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def _read_log(path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        with path.open() as handle:
            return list(csv.DictReader(handle))

    # ── apply ────────────────────────────────────────────────────────────
    def apply(self, chosen_args: list[str]) -> Path:
        """Write the chosen args back into catalog.json for this script entry."""
        path = self.root / "catalog.json"
        cat = load_catalog()
        for row in cat["strategies"]:
            if row["id"] == self.sid:
                scripts = row.setdefault("scripts", [{}])
                scripts[self.script_index]["args"] = chosen_args
        path.write_text(json.dumps(cat, indent=2) + "\n")
        return path


def load_tuning(folder: Path) -> dict[str, Any] | None:
    path = folder / "tuning.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
