"""Feature store for the ML sleeves.

Two layers, both built so that nothing a model sees at time t was unknowable at time t:

  daily_features(...)      one row per trading day, everything measured AT THE CLOSE of that day.
                           A signal fired on day D+1 (at any time of day) may only use the row for day D —
                           `align_prior_close()` enforces that with a strict `<` on the date.
  intraday_features(...)   one row per intraday bar, everything measured at the CLOSE of that bar.
                           A signal fired at the close of bar t uses row t (the bar is complete);
                           the fill happens at the open of t+1, as in every other backtest in this repo.

The daily store accepts optional inputs and simply omits the columns it cannot build, so the same code
runs on the synthetic set (price only) and on the full set (price + VIX + breadth panel + GEX + macro
sentiment + per-name news sentiment).  Missing values are left as NaN — the GBDTs handle them natively.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# ─────────────────────────────────────────────────────────────────────────────
# Daily
# ─────────────────────────────────────────────────────────────────────────────
def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def daily_features(bars: pd.DataFrame, vix: pd.DataFrame | pd.Series | None = None,
                   panel: pd.DataFrame | None = None, gex: pd.DataFrame | None = None,
                   macro: dict[str, pd.Series] | None = None, sentiment: pd.DataFrame | None = None,
                   regime: pd.Series | None = None) -> pd.DataFrame:
    """bars: daily OHLCV of the traded instrument (index = date).
    vix: Series or DataFrame with a 'close'/'vix' column (VIX for index/stock sleeves, VXN for MNQ).
    panel: DataFrame of closes, columns = symbols (for breadth).
    gex: DataFrame from strategies/02 `daily_gex` (columns total_gex, gex_z, flip, ...), index = date.
    macro: {'fng': Series, 'pcr': Series, 'aaii_spread': Series} indexed by date (value known at that date's close).
    sentiment: DataFrame from ml/sentiment/finbert_score.py (index date; columns score, count, shock_z).
    regime: Series of integer regime states indexed by date (from ml/regime_hmm.py --export-state)."""
    b = bars.sort_index()
    c, h, l, v = b["close"], b["high"], b["low"], b["volume"].astype(float)
    r = np.log(c).diff()
    f = pd.DataFrame(index=b.index)
    f["ret_1"] = r
    f["ret_5"] = np.log(c / c.shift(5))
    f["ret_20"] = np.log(c / c.shift(20))
    f["rv_5"] = r.rolling(5).std() * np.sqrt(252)
    f["rv_20"] = r.rolling(20).std() * np.sqrt(252)
    f["rv_ratio"] = f["rv_5"] / f["rv_20"]
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    f["atr14_pct"] = tr.rolling(14).mean() / c * 100
    f["range_pct"] = (h - l) / c * 100
    # efficiency ratio (Kaufman): |net move| / sum |moves| over 20 days — 1 = clean trend, 0 = chop
    f["er_20"] = (c - c.shift(20)).abs() / (c.diff().abs().rolling(20).sum())
    f["dist_sma50"] = c / c.rolling(50).mean() - 1
    f["dist_sma200"] = c / c.rolling(200).mean() - 1
    f["sma50_slope"] = c.rolling(50).mean().pct_change(5)
    f["rsi_14"] = _rsi(c, 14)
    f["vol_z20"] = (v - v.rolling(20).mean()) / v.rolling(20).std()
    f["gap_pct"] = (b["open"] / c.shift(1) - 1) * 100
    f["close_pos"] = ((c - l) / (h - l).replace(0, np.nan)).clip(0, 1)   # where in the day's range it closed
    f["dow"] = b.index.dayofweek
    f["month_pos"] = _month_position(b.index)

    if vix is not None:
        vs = vix["close"] if isinstance(vix, pd.DataFrame) and "close" in vix else (
            vix["vix"] if isinstance(vix, pd.DataFrame) else vix)
        vs = vs.reindex(b.index).ffill()
        f["vix"] = vs
        f["vix_chg_5"] = vs.pct_change(5)
        f["vix_pct_252"] = vs.rolling(252, min_periods=60).rank(pct=True)
        f["vix_vs_rv"] = vs / 100 / f["rv_20"].replace(0, np.nan)        # implied / realised
        if isinstance(vix, pd.DataFrame) and "vix3m" in vix:
            f["vix_ratio"] = (vix["vix"] / vix["vix3m"]).reindex(b.index).ffill()
    if panel is not None and panel.shape[1] >= 5:
        p = panel.reindex(b.index).ffill()
        above50 = (p > p.rolling(50).mean()).mean(axis=1)
        f["pct_above_50"] = above50
        f["pct_above_200"] = (p > p.rolling(200).mean()).mean(axis=1)
        adv = (p.pct_change() > 0).sum(axis=1) - (p.pct_change() < 0).sum(axis=1)
        f["mcclellan"] = _ema(adv, 19) - _ema(adv, 39)
        f["breadth_thrust"] = above50.diff(10)
    if gex is not None and len(gex):
        g = gex.reindex(b.index).ffill()
        for col in ("gex_z", "total_gex"):
            if col in g:
                f[col] = g[col]
        if "total_gex" in g:
            f["gex_sign"] = np.sign(g["total_gex"])
        if "flip" in g:
            f["dist_flip"] = c / g["flip"] - 1
    if macro:
        for k, s in macro.items():
            s = pd.Series(s).sort_index().reindex(b.index).ffill()
            f[k] = s
            f[f"{k}_chg_5"] = s.diff(5)
    if sentiment is not None and len(sentiment):
        s = sentiment.reindex(b.index)
        for col in ("score", "count", "shock_z"):
            if col in s:
                f[f"sent_{col}"] = s[col]
        f["sent_score_5"] = s["score"].rolling(5, min_periods=1).mean() if "score" in s else np.nan
    if regime is not None:
        f["regime"] = pd.Series(regime).reindex(b.index).ffill()
    f.index.name = "date"
    return f


def _rsi(c: pd.Series, n: int) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _month_position(idx: pd.DatetimeIndex) -> pd.Series:
    """Trading-day position within the month, 0 = first session, 1 = last (known in advance)."""
    s = pd.Series(idx, index=idx)
    ym = s.dt.to_period("M")
    rank = s.groupby(ym).cumcount()
    n = s.groupby(ym).transform("count")
    return ((rank) / (n - 1).clip(lower=1)).astype(float)


def align_prior_close(features: pd.DataFrame, when: pd.DatetimeIndex | pd.Series) -> pd.DataFrame:
    """For each timestamp in `when`, return the feature row of the LAST day strictly BEFORE that
    timestamp's date.  Guarantees that an intraday signal on day D never sees day D's close."""
    fidx = pd.DatetimeIndex(features.index).tz_localize(None).normalize()
    w = pd.DatetimeIndex(pd.Series(when))
    if w.tz is not None:
        w = w.tz_localize(None)
    dates = w.normalize()
    pos = np.searchsorted(fidx.values, dates.values, side="left") - 1   # last index with fidx < date
    ok = pos >= 0
    out = pd.DataFrame(np.nan, index=range(len(w)), columns=features.columns)
    out.loc[ok, :] = features.iloc[pos[ok]].to_numpy()
    out.index = pd.Index(when)
    fd = pd.Series(pd.NaT, index=out.index, dtype="datetime64[ns]")
    fd.iloc[np.where(ok)[0]] = fidx[pos[ok]].values
    out["feat_date"] = fd
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Intraday (signal-time) features
# ─────────────────────────────────────────────────────────────────────────────
def intraday_features(bars: pd.DataFrame, session_open: str = "09:30", session_close: str = "16:00",
                      rv_window: int = 30, rvol_sessions: int = 20) -> pd.DataFrame:
    """bars: intraday OHLCV, tz-aware or ET-naive index.  Every value on row t is known at the close of bar t.
    Columns: rv_30 (annualised, bar returns), dist_vwap_atr, rvol, min_since_open, atr14, day_range_pos,
    session_ret, bars_in_session, hour, ret_1, ret_5, ret_15."""
    b = bars.sort_index()
    idx = b.index
    tz_naive = idx.tz_localize(None) if idx.tz is not None else idx
    day = tz_naive.normalize()
    minute = tz_naive.hour * 60 + tz_naive.minute
    o_h, o_m = map(int, session_open.split(":"))
    c_h, c_m = map(int, session_close.split(":"))
    open_min, close_min = o_h * 60 + o_m, c_h * 60 + c_m
    bars_per_day = pd.Series(1, index=idx).groupby(day).transform("count").to_numpy()
    step = max(1, int(round(np.median(np.diff(tz_naive.values).astype("timedelta64[m]").astype(float)))))
    per_year = 252 * max(1, (close_min - open_min) // step)

    c, h, l, v = b["close"], b["high"], b["low"], b["volume"].astype(float)
    r = np.log(c).diff()
    f = pd.DataFrame(index=idx)
    f["ret_1"] = r
    f["ret_5"] = np.log(c / c.shift(5))
    f["ret_15"] = np.log(c / c.shift(15))
    f["rv_30"] = r.rolling(rv_window).std() * np.sqrt(per_year)
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    f["atr14"] = atr
    f["atr14_pct"] = atr / c * 100
    # session-anchored VWAP and running range (reset at each calendar day of the index)
    tp = (h + l + c) / 3
    g = pd.Series(day, index=idx)
    cum_pv = (tp * v).groupby(g.values).cumsum()
    cum_v = v.groupby(g.values).cumsum()
    vwap = cum_pv / cum_v.replace(0, np.nan)
    f["vwap"] = vwap
    f["dist_vwap_atr"] = (c - vwap) / atr.replace(0, np.nan)
    day_hi = h.groupby(g.values).cummax()
    day_lo = l.groupby(g.values).cummin()
    f["day_range_pos"] = ((c - day_lo) / (day_hi - day_lo).replace(0, np.nan)).clip(0, 1)
    f["day_range_atr"] = (day_hi - day_lo) / atr.replace(0, np.nan)
    first_open = b["open"].groupby(g.values).transform("first")
    f["session_ret"] = (c / first_open - 1) * 100
    f["bars_in_session"] = pd.Series(1, index=idx).groupby(g.values).cumsum()
    f["min_since_open"] = (minute - open_min).astype(float)
    f["hour"] = tz_naive.hour + tz_naive.minute / 60.0
    f["in_rth"] = ((minute >= open_min) & (minute < close_min)).astype(int)
    # relative volume: this bar's volume vs the mean volume of the same minute-of-day over the last N sessions
    key = pd.Series(minute, index=idx)
    mean_by_minute = v.groupby(key.values).transform(lambda s: s.shift(1).rolling(rvol_sessions, min_periods=3).mean())
    f["rvol"] = v / mean_by_minute.replace(0, np.nan)
    f["vol_z"] = (v - v.rolling(60).mean()) / v.rolling(60).std().replace(0, np.nan)
    f.index.name = "ts"
    return f


def features_at(intraday: pd.DataFrame, when: pd.DatetimeIndex | pd.Series, strict_before: bool = False) -> pd.DataFrame:
    """Row of the intraday feature store for the last bar whose timestamp is <= each `when`
    (or strictly < when strict_before=True — use that when `when` is the FILL time, so the
    signal bar is the one before the fill)."""
    fi = intraday.index
    w = pd.DatetimeIndex(pd.Series(when))
    if fi.tz is not None and w.tz is None:
        w = w.tz_localize(fi.tz)
    elif fi.tz is None and w.tz is not None:
        w = w.tz_localize(None)
    side = "left" if strict_before else "right"
    pos = np.searchsorted(fi.values, w.values, side=side) - 1
    ok = pos >= 0
    out = pd.DataFrame(np.nan, index=range(len(w)), columns=intraday.columns)
    out.loc[ok, :] = intraday.iloc[pos[ok]].to_numpy()
    out.index = pd.Index(when)
    bt = pd.Series(pd.NaT, index=out.index, dtype=fi.dtype)
    bt.iloc[np.where(ok)[0]] = fi[pos[ok]]
    out["bar_ts"] = bt
    return out


def feature_columns(df: pd.DataFrame) -> list[str]:
    """Numeric columns only, minus bookkeeping."""
    drop = {"feat_date", "bar_ts", "vwap"}
    return [c for c in df.columns if c not in drop and pd.api.types.is_numeric_dtype(df[c])]
