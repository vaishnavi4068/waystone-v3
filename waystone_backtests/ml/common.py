"""Data access shared by the ML scripts: real CSVs under data/ when they exist, synthetic otherwise.

Real-data file contracts (all produced by tools/ in this repo):
  data/intraday/<SYM>_1min.csv      ts,open,high,low,close,volume            fetch_polygon.py futures / ib_fetch_bars.py
  data/intraday/<SYM>_10min.csv     same (or resampled from 1min on the fly)
  data/daily/<SYM>.csv              date,open,high,low,close,adj_close,volume   fetch_polygon.py indices / fetch_yf.py
  data/macro/fng.csv                date,value                               ml/sentiment/fetch_free_sentiment.py fng
  data/macro/pcr.csv                date,value                               ... pcr
  data/macro/aaii.csv               date,bull,neutral,bear                   ... aaii
  data/sentiment/<SYM>_daily.csv    date,score,count,shock_z                 ml/sentiment/finbert_score.py
  data/regime/<name>_states.csv     date,state                               ml/regime_hmm.py --export-state
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wsbt import data as D  # noqa: E402

ET = "America/New_York"
DATA = D.DATA_DIR


# ─────────────────────────────────────────────────────────────────────────────
# Intraday
# ─────────────────────────────────────────────────────────────────────────────
def resample(bars: pd.DataFrame, rule: str = "10min") -> pd.DataFrame:
    o = bars.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["open"])
    return o[o["volume"] > 0]


def intraday(symbol: str, bar_min: int = 1, synthetic: bool = False, days: int = 120, seed: int = 5,
             rth_only: bool = False, s0: float = 23000.0, start: str = "2026-01-05") -> pd.DataFrame:
    if not synthetic:
        p1 = DATA / "intraday" / f"{D._safe_name(symbol)}_{bar_min}min.csv"
        if p1.exists():
            return D.load_intraday(symbol) if bar_min == 1 else _read_intraday(p1)
        p0 = DATA / "intraday" / f"{D._safe_name(symbol)}_1min.csv"
        if p0.exists():
            return resample(D.load_intraday(symbol), f"{bar_min}min")
        raise D.DataMissing(f"no intraday CSV for {symbol} (expected {p1} or {p0})")
    b = D.synthetic_intraday(days=days, start=start, s0=s0, seed=seed, rth_only=rth_only)
    return b if bar_min == 1 else resample(b, f"{bar_min}min")


def scale_vol(bars: pd.DataFrame, scale: float, tick: float = 0.25) -> pd.DataFrame:
    """Multiply the log-return path of a synthetic bar series by `scale` (keeps the OHLC shape)."""
    b = bars.copy()
    c = b["close"].to_numpy(dtype=float)
    r = np.diff(np.log(c), prepend=np.log(c[0]))
    ratio = np.exp(np.log(c[0]) + np.cumsum(r * scale)) / c
    for k in ("open", "high", "low", "close"):
        b[k] = np.round(b[k] * ratio / tick) * tick
    return b


def _read_intraday(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["ts"], index_col="ts")
    if df.index.tz is None:
        df.index = df.index.tz_localize(ET)
    else:
        df.index = df.index.tz_convert(ET)
    return df.sort_index()


def daily_from_intraday(bars: pd.DataFrame, session_shift_hours: int = 6) -> pd.DataFrame:
    """Session-daily bars.  Globex sessions start at 18:00 ET the previous evening, so timestamps are shifted
    by +6h before bucketing (an 18:00 D-1 bar lands on D; RTH-only bars are unaffected)."""
    idx = bars.index.tz_localize(None) if bars.index.tz is not None else bars.index
    b = bars.copy(); b.index = idx + pd.Timedelta(hours=session_shift_hours)
    d = b.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["open"])
    d.index.name = "date"
    return d


def synthetic_universe(symbols: list[str], days: int = 120, seed: int = 11, bar_min: int = 10,
                       start: str = "2026-01-05") -> dict[str, pd.DataFrame]:
    """RTH 1-min bars per name (different level, vol and seed), resampled to `bar_min`."""
    rng = np.random.default_rng(seed)
    out = {}
    for i, s in enumerate(symbols):
        s0 = float(rng.uniform(40, 400))
        b = D.synthetic_intraday(days=days, start=start, s0=s0, seed=seed * 100 + i, rth_only=True, tick=0.01)
        # give names different vol levels so the ATR% ranking has something to rank
        scale = float(rng.uniform(0.7, 2.2))
        c = b["close"].to_numpy()
        r = np.diff(np.log(c), prepend=np.log(c[0]))
        c2 = np.exp(np.log(c[0]) + np.cumsum(r * scale))
        ratio = c2 / c
        for k in ("open", "high", "low", "close"):
            b[k] = (b[k] * ratio).round(2)
        out[s] = resample(b, f"{bar_min}min") if bar_min > 1 else b
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Daily / macro
# ─────────────────────────────────────────────────────────────────────────────
def daily(symbol: str, synthetic: bool = False, n: int = 2500, seed: int = 1, start: str = "2016-01-04", **kw) -> pd.DataFrame:
    if not synthetic:
        return D.load_daily(symbol)
    return D.synthetic_daily(n=n, start=start, seed=seed, **kw)


def try_daily(symbol: str | None) -> pd.DataFrame | None:
    if not symbol:
        return None
    try:
        return D.load_daily(symbol)
    except Exception:
        return None


def vol_index(symbol: str | None, ref_daily: pd.DataFrame, synthetic: bool = False, seed: int = 3) -> pd.DataFrame:
    """VIX/VXN daily frame with columns vix, vix3m (synthetic) or close (real)."""
    if synthetic or not symbol:
        v = D.synthetic_vix(ref_daily, seed=seed)
        v["close"] = v["vix"]
        return v
    return D.load_daily(symbol)


def load_macro(synthetic: bool = False, index: pd.DatetimeIndex | None = None, seed: int = 7) -> dict[str, pd.Series]:
    out = {}
    if synthetic:
        if index is None:
            return out
        rng = np.random.default_rng(seed)
        x, vals = 50.0, np.empty(len(index))
        for i in range(len(index)):                       # mean-reverting around 50, occasionally into fear/greed extremes
            x = 50 + 0.96 * (x - 50) + rng.normal(0, 3)
            vals[i] = x
        fng = pd.Series(np.clip(vals, 3, 97), index=index).round(1)
        out["fng"] = fng
        out["pcr"] = pd.Series(np.clip(0.9 + 0.15 * rng.standard_normal(len(index)) - (fng - 50) / 200, 0.4, 1.8), index=index)
        out["aaii_spread"] = pd.Series(((fng - 50) / 2 + rng.normal(0, 8, len(index))).round(1), index=index)
        return out
    for name, col in (("fng", "value"), ("pcr", "value")):
        p = DATA / "macro" / f"{name}.csv"
        if p.exists():
            s = pd.read_csv(p, parse_dates=["date"]).set_index("date")[col].astype(float).sort_index()
            out[name] = s
    p = DATA / "macro" / "aaii.csv"
    if p.exists():
        a = pd.read_csv(p, parse_dates=["date"]).set_index("date").sort_index()
        out["aaii_spread"] = (a["bull"] - a["bear"]).astype(float)
    return out


def load_sentiment(symbol: str) -> pd.DataFrame | None:
    p = DATA / "sentiment" / f"{D._safe_name(symbol)}_daily.csv"
    if not p.exists():
        return None
    return pd.read_csv(p, parse_dates=["date"]).set_index("date").sort_index()


def load_regime(name: str) -> pd.Series | None:
    p = DATA / "regime" / f"{name}_states.csv"
    if not p.exists():
        return None
    return pd.read_csv(p, parse_dates=["date"]).set_index("date")["state"]


def synthetic_fng(index: pd.DatetimeIndex, seed: int = 7) -> pd.Series:
    return load_macro(True, index, seed)["fng"]
