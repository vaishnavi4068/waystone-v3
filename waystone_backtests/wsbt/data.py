"""Data access: CSV contracts, loaders, and synthetic generators.

CSV CONTRACTS (all dates ISO, all prices unadjusted unless noted)
-----------------------------------------------------------------
data/daily/<SYMBOL>.csv        date,open,high,low,close,adj_close,volume
                               (adj_close optional; SYMBOL as on Yahoo: SPY, ^VIX, ^GSPC, XLK ... -> file _VIX.csv;
                                or Polygon indices I:SPX, I:VIX, I:VIX3M -> file I_SPX.csv; futures MES, MNQ)
data/intraday/<SYMBOL>_1min.csv ts,open,high,low,close,volume[,bid_volume,ask_volume]
                               ts = ISO timestamp with offset, e.g. 2026-09-04T09:31:00-04:00
data/chains/<UNDERLYING>_chain.csv
                               date,spot,expiry,strike,right,oi[,iv][,gamma]
                               one row per (date, expiry, strike, right); right in {C,P}
data/earnings/<SYMBOL>.csv     date,time,eps_est,eps_act[,implied_move_pct]
                               time in {bmo,amc,unknown}; implied_move_pct as 5.2 for 5.2%
data/sp500.csv                 symbol[,weight]  — constituent list (the options bot already has one)
data/fomc_dates.csv            date  — FOMC decision (statement) days

tools/fetch_yf.py       fills data/daily and data/earnings from Yahoo Finance (stocks/ETFs).
tools/fetch_polygon.py  fills indices, futures, option chains/bars and implied moves from Polygon/Massive.
tools/ib_fetch_bars.py  fills data/intraday from IB Gateway (run on the VM) — fallback if no Polygon futures.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(__import__("os").environ.get("WSBT_DATA_DIR", ROOT / "data"))

DAILY_COLS = ["open", "high", "low", "close", "volume"]


class DataMissing(FileNotFoundError):
    pass


# ══════════════════════════════════════════════════════════════════════════════
# Loaders
# ══════════════════════════════════════════════════════════════════════════════
def _safe_name(symbol: str) -> str:
    """^GSPC -> _GSPC, I:SPX -> I_SPX, O:SPX... -> O_SPX..."""
    return symbol.replace("^", "_").replace("/", "_").replace(":", "_")


def daily_path(symbol: str) -> Path:
    return DATA_DIR / "daily" / f"{_safe_name(symbol)}.csv"


def load_daily(symbol: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
    p = daily_path(symbol)
    if not p.exists():
        raise DataMissing(f"{p} not found — run `python tools/fetch_yf.py --symbols {symbol}` "
                          f"(or drop a CSV with columns date,open,high,low,close,volume there)")
    df = pd.read_csv(p, parse_dates=["date"]).set_index("date").sort_index()
    df.columns = [c.lower() for c in df.columns]
    missing = [c for c in DAILY_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"{p}: missing columns {missing}")
    df = df[~df.index.duplicated(keep="last")]
    if start:
        df = df[df.index >= pd.Timestamp(start)]
    if end:
        df = df[df.index <= pd.Timestamp(end)]
    return df[DAILY_COLS + [c for c in ("adj_close",) if c in df.columns]].astype(float)


def load_many(symbols: list[str], start=None, end=None, strict: bool = False) -> dict[str, pd.DataFrame]:
    out = {}
    for s in symbols:
        try:
            out[s] = load_daily(s, start, end)
        except DataMissing:
            if strict:
                raise
    return out


def closes_panel(frames: dict[str, pd.DataFrame], field: str = "close") -> pd.DataFrame:
    return pd.DataFrame({k: v[field] for k, v in frames.items()}).sort_index()


def load_intraday(symbol: str, start=None, end=None) -> pd.DataFrame:
    p = DATA_DIR / "intraday" / f"{_safe_name(symbol)}_1min.csv"
    if not p.exists():
        raise DataMissing(f"{p} not found — run tools/ib_fetch_bars.py on the VM, or pass --synthetic")
    df = pd.read_csv(p)
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("America/New_York")
    df = df.set_index("ts").sort_index()
    if start:
        df = df[df.index >= pd.Timestamp(start, tz="America/New_York")]
    if end:
        df = df[df.index <= pd.Timestamp(end, tz="America/New_York")]
    return df


def load_chain_history(underlying: str) -> pd.DataFrame:
    p = DATA_DIR / "chains" / f"{_safe_name(underlying)}_chain.csv"
    if not p.exists():
        raise DataMissing(f"{p} not found — see strategies/02_gex_dealer_gamma/README.md for how to collect it")
    df = pd.read_csv(p, parse_dates=["date", "expiry"])
    df["right"] = df["right"].str.upper().str[0]
    return df


def load_earnings(symbol: str) -> pd.DataFrame:
    p = DATA_DIR / "earnings" / f"{_safe_name(symbol)}.csv"
    if not p.exists():
        raise DataMissing(f"{p} not found — run `python tools/fetch_yf.py --earnings --symbols {symbol}`")
    df = pd.read_csv(p, parse_dates=["date"]).sort_values("date")
    if "time" not in df:
        df["time"] = "unknown"
    df["time"] = df["time"].fillna("unknown").str.lower()
    return df


def load_symbol_list(name: str = "sp500.csv", max_symbols: int | None = None) -> list[str]:
    p = DATA_DIR / name
    if not p.exists():
        raise DataMissing(f"{p} not found — copy the options bot's sp500.csv here")
    df = pd.read_csv(p)
    col = "symbol" if "symbol" in df.columns else df.columns[0]
    syms = [str(s).strip().upper().replace(".", "-") for s in df[col] if str(s).strip()]
    return syms[:max_symbols] if max_symbols else syms


# ══════════════════════════════════════════════════════════════════════════════
# Synthetic data — shape-correct, statistically plain.  Mechanics only.
# ══════════════════════════════════════════════════════════════════════════════
def trading_days(start: str, n: int) -> pd.DatetimeIndex:
    return pd.bdate_range(start, periods=n, freq="C", holidays=[])


def synthetic_daily(n: int = 2500, start: str = "2016-01-04", s0: float = 300.0, ann_vol: float = 0.18,
                    ann_drift: float = 0.07, seed: int = 7, regime: bool = True) -> pd.DataFrame:
    """GBM with GARCH-ish vol clustering and optional trend/range regimes.  Returns OHLCV."""
    rng = np.random.default_rng(seed)
    idx = trading_days(start, n)
    dt = 1 / 252
    vol = np.empty(n)
    v = ann_vol
    rets = np.empty(n)
    trend = 0.0
    for i in range(n):
        if regime and i % 63 == 0:                       # quarterly regime draw
            trend = rng.choice([-0.25, 0.0, 0.0, 0.35]) * ann_vol   # negative / flat / positive drift
        v = 0.93 * v + 0.07 * ann_vol * rng.lognormal(0, 0.35)
        vol[i] = v
        rets[i] = (ann_drift + trend) * dt - 0.5 * v * v * dt + v * math.sqrt(dt) * rng.standard_normal()
    close = s0 * np.exp(np.cumsum(rets))
    prev = np.concatenate([[s0], close[:-1]])
    gap = rng.standard_normal(n) * vol * math.sqrt(dt) * 0.35
    open_ = prev * np.exp(gap)
    rng_pct = np.abs(rng.standard_normal(n)) * vol * math.sqrt(dt) * 1.2 + 0.002
    hi = np.maximum(open_, close) * (1 + rng_pct * rng.uniform(0.3, 1.0, n))
    lo = np.minimum(open_, close) * (1 - rng_pct * rng.uniform(0.3, 1.0, n))
    volu = rng.lognormal(16, 0.35, n) * (1 + 5 * np.abs(rets) / (vol * math.sqrt(dt)))
    df = pd.DataFrame({"open": open_, "high": hi, "low": lo, "close": close, "volume": volu.round()}, index=idx)
    df.index.name = "date"
    return df


def synthetic_panel(symbols: list[str], n: int = 2500, start: str = "2016-01-04", seed: int = 11,
                    common_beta: float = 0.7) -> dict[str, pd.DataFrame]:
    """Correlated set of daily series (one common factor + idiosyncratic), each with its own drift."""
    rng = np.random.default_rng(seed)
    market = synthetic_daily(n, start, seed=seed)
    mret = np.log(market["close"]).diff().fillna(0).to_numpy()
    out = {}
    for i, s in enumerate(symbols):
        r_id = rng.standard_normal(n) * 0.012
        # drift re-drawn every ~6 months so no symbol has a permanent edge (keeps momentum honest)
        drift = np.repeat(rng.normal(0.0, 0.0003, n // 126 + 1), 126)[:n]
        rets = common_beta * mret + r_id + drift
        base = synthetic_daily(n, start, s0=float(rng.uniform(30, 400)), seed=seed + 100 + i, regime=False)
        close = base["close"].iloc[0] * np.exp(np.cumsum(rets))
        scale = close / base["close"].to_numpy()
        df = base.copy()
        for c in ("open", "high", "low", "close"):
            df[c] = base[c].to_numpy() * scale
        out[s] = df
    return out


def synthetic_vix(spx: pd.DataFrame, seed: int = 3) -> pd.DataFrame:
    """VIX ~ realised vol + premium, VIX3M = VIX x term (usually contango, inverts in stress)."""
    rng = np.random.default_rng(seed)
    r = np.log(spx["close"]).diff().fillna(0)
    rv = r.rolling(21).std().bfill() * math.sqrt(252) * 100
    prem = 3.5 + 1.5 * rng.standard_normal(len(r)).cumsum() * 0.02
    vix = (rv * 1.1 + prem).clip(9, 90)
    shock = (r < -0.02).astype(float) * 8
    vix = (vix + shock).rolling(3).mean().bfill()
    term = 1.08 - 0.25 * (vix - vix.rolling(252, min_periods=20).mean().bfill()) / 20.0
    vix3m = vix * term.clip(0.85, 1.2)
    out = pd.DataFrame({"vix": vix.values, "vix3m": vix3m.values}, index=spx.index)
    return out


def synthetic_intraday(days: int = 40, start: str = "2026-07-01", s0: float = 23000.0, seed: int = 5,
                       rth_only: bool = False, tick: float = 0.25) -> pd.DataFrame:
    """1-minute futures-like bars with a U-shaped intraday vol/volume profile and a
    delta proxy baked into bid_volume/ask_volume.  24h Globex (18:00-17:00 ET) or RTH."""
    rng = np.random.default_rng(seed)
    frames = []
    px = s0
    day0 = pd.Timestamp(start)
    d = 0
    made = 0
    while made < days:
        day = day0 + pd.Timedelta(days=d)
        d += 1
        if day.weekday() >= 5:
            continue
        if rth_only:
            ts = pd.date_range(day.strftime("%Y-%m-%d") + " 09:30", periods=390, freq="1min", tz="America/New_York")
        else:
            ts = pd.date_range((day - pd.Timedelta(days=1)).strftime("%Y-%m-%d") + " 18:00", periods=23 * 60,
                               freq="1min", tz="America/New_York")
        n = len(ts)
        minute_of_day = ts.hour * 60 + ts.minute
        rth = (minute_of_day >= 570) & (minute_of_day < 960)
        prof = np.where(rth, 1.0 + 1.5 * np.exp(-((minute_of_day - 570) / 40.0)) + 0.8 * np.exp(-((960 - minute_of_day) / 40.0)), 0.35)
        sig = 0.00028 * prof
        drift_day = rng.normal(0, 0.0015)
        rets = drift_day / n + sig * rng.standard_normal(n)
        # intraday mean-reversion pull toward session VWAP-ish level to give the CVD strategy something to do
        closes = np.empty(n)
        p = px
        anchor = px
        for i in range(n):
            pull = -0.003 * (p / anchor - 1)
            p = p * math.exp(rets[i] + pull)
            closes[i] = p
        opens = np.concatenate([[px], closes[:-1]])
        rng_abs = np.abs(rng.standard_normal(n)) * sig * closes * 1.5 + tick
        highs = np.maximum(opens, closes) + rng_abs * rng.uniform(0.2, 1, n)
        lows = np.minimum(opens, closes) - rng_abs * rng.uniform(0.2, 1, n)
        vol = (rng.lognormal(5.2, 0.5, n) * prof).round()
        buy_frac = np.clip(0.5 + 0.35 * np.sign(closes - opens) * rng.uniform(0.2, 1, n) + 0.05 * rng.standard_normal(n), 0.05, 0.95)
        askv = (vol * buy_frac).round()
        bidv = vol - askv
        q = lambda a: np.round(a / tick) * tick
        frames.append(pd.DataFrame({"open": q(opens), "high": q(highs), "low": q(lows), "close": q(closes),
                                    "volume": vol, "bid_volume": bidv, "ask_volume": askv}, index=ts))
        px = closes[-1] * math.exp(rng.normal(0, 0.002))
        made += 1
    df = pd.concat(frames)
    df.index.name = "ts"
    return df


def synthetic_chain(spot: pd.Series, seed: int = 9, dtes=(7, 14, 30, 45), strike_step: float = 25.0,
                    width_pct: float = 0.12) -> pd.DataFrame:
    """Daily option-chain snapshots with OI clustered at round strikes and a put skew.
    Produces rows: date, spot, expiry, strike, right, oi, iv."""
    rng = np.random.default_rng(seed)
    rows = []
    for dt, s in spot.items():
        strikes = np.arange(math.floor(s * (1 - width_pct) / strike_step) * strike_step,
                            math.ceil(s * (1 + width_pct) / strike_step) * strike_step + strike_step, strike_step)
        for dte in dtes:
            exp = dt + pd.Timedelta(days=dte)
            for k in strikes:
                m = (k - s) / s
                round_bonus = 3.0 if k % 100 == 0 else (1.6 if k % 50 == 0 else 1.0)
                base_oi = 4000 * round_bonus * math.exp(-(m / 0.05) ** 2) * (1.4 if dte <= 14 else 1.0)
                iv_c = max(0.08, 0.18 + 0.35 * max(0, m) * 0.3 - 0.05 * min(0, m))
                iv_p = max(0.08, 0.18 - 0.9 * min(0, m) + 0.1 * max(0, m))
                rows.append((dt, s, exp, k, "C", int(base_oi * rng.uniform(0.6, 1.4) * (0.7 if m < 0 else 1.5)), round(iv_c, 4)))
                rows.append((dt, s, exp, k, "P", int(base_oi * rng.uniform(0.6, 1.4) * (1.1 if m < 0 else 0.5)), round(iv_p, 4)))
    return pd.DataFrame(rows, columns=["date", "spot", "expiry", "strike", "right", "oi", "iv"])


def synthetic_earnings(symbols: list[str], start: str, end: str, seed: int = 21) -> dict[str, pd.DataFrame]:
    """Quarterly earnings dates per symbol with EPS estimate/actual."""
    rng = np.random.default_rng(seed)
    out = {}
    for i, s in enumerate(symbols):
        first = pd.Timestamp(start) + pd.Timedelta(days=int(rng.integers(20, 80)))
        dates = pd.date_range(first, end, freq="91D")
        dates = [d + pd.offsets.BDay(0) for d in dates]
        est = rng.uniform(0.5, 3.0, len(dates))
        act = est * (1 + rng.normal(0.02, 0.12, len(dates)))
        out[s] = pd.DataFrame({"date": dates, "time": rng.choice(["bmo", "amc"], len(dates)),
                               "eps_est": est.round(2), "eps_act": act.round(2), "implied_move_pct": rng.uniform(3, 9, len(dates)).round(1)})
    return out
