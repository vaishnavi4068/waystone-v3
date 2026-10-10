"""Pure feature math over plain series (``{date: value}``), so every rule is unit-testable.

Thesis references: F&G replica (§9.2, E4), R2 one-way day and R3 vol spike (§9.1),
COT / VIX term structure positioning (E3), recency-weighted aggregation and dispersion
(§10), PSI drift alarm at 0.2 (§11). The chop ratio mirrors the V221 engine's 20-day
efficiency gate on intraday bars.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

Series = Mapping[date, float]

FNG_FEAR = 30.0
FNG_GREED = 70.0
CHOP_THRESHOLD = 0.03
ONE_WAY_BODY = 0.7
VOL_SPIKE_MULT = 1.10
PSI_ALERT = 0.2


def _keys(s: Series) -> list[date]:
    return sorted(s)


def upto(s: Series, d: date, n: int, *, strict: bool = False) -> list[float]:
    """The last ``n`` values on or before ``d`` (before, when ``strict``), oldest first."""
    keys = _keys(s)
    i = bisect.bisect_left(keys, d) if strict else bisect.bisect_right(keys, d)
    return [s[k] for k in keys[max(0, i - n) : i]]


def last(s: Series, d: date, *, strict: bool = False, max_age_days: int = 7) -> float | None:
    keys = _keys(s)
    i = (bisect.bisect_left(keys, d) if strict else bisect.bisect_right(keys, d)) - 1
    if i < 0 or (d - keys[i]).days > max_age_days:
        return None
    return s[keys[i]]


def pct_rank(history: Sequence[float], x: float) -> float:
    """Share of ``history`` at or below ``x``, 0..100."""
    if not history:
        return 50.0
    below = sum(1 for h in history if h < x)
    equal = sum(1 for h in history if h == x)
    return round(100.0 * (below + 0.5 * equal) / len(history), 2)


@dataclass
class Market:
    """Daily history the features read; any series may be empty when a source is down."""

    spx: dict[date, float] = field(default_factory=dict)
    ndx: dict[date, float] = field(default_factory=dict)
    hy_oas: dict[date, float] = field(default_factory=dict)
    ust10y: dict[date, float] = field(default_factory=dict)
    vix: dict[date, float] = field(default_factory=dict)
    vix3m: dict[date, float] = field(default_factory=dict)
    vxn: dict[date, float] = field(default_factory=dict)
    vix9d: dict[date, float] = field(default_factory=dict)
    pcr_total: dict[date, float] = field(default_factory=dict)
    pcr_equity: dict[date, float] = field(default_factory=dict)
    rsp: dict[date, float] = field(default_factory=dict)
    spy: dict[date, float] = field(default_factory=dict)
    fng_cnn: dict[date, float] = field(default_factory=dict)
    ohlc: dict[str, dict[date, tuple[float, float, float, float]]] = field(default_factory=dict)


# ----------------------------------------------------------------- F&G replica (E4)
def _momentum(m: Market, d: date) -> float | None:
    xs = upto(m.spx, d, 125)
    if len(xs) < 100 or last(m.spx, d) is None:
        return None
    return xs[-1] / (sum(xs) / len(xs)) - 1.0


def _breadth(m: Market, d: date) -> float | None:
    rsp, spy = upto(m.rsp, d, 21), upto(m.spy, d, 21)
    if len(rsp) < 21 or len(spy) < 21 or last(m.rsp, d) is None:
        return None
    return (rsp[-1] / spy[-1]) / (rsp[0] / spy[0]) - 1.0


def _put_call(m: Market, d: date) -> float | None:
    xs = upto(m.pcr_total, d, 5)
    if len(xs) < 3 or last(m.pcr_total, d) is None:
        return None
    return -(sum(xs) / len(xs))


def _volatility(m: Market, d: date) -> float | None:
    xs = upto(m.vix, d, 50)
    if len(xs) < 40 or last(m.vix, d) is None:
        return None
    return -(xs[-1] / (sum(xs) / len(xs)))


def _junk(m: Market, d: date) -> float | None:
    v = last(m.hy_oas, d)
    return None if v is None else -v


def _safe_haven(m: Market, d: date) -> float | None:
    spx, y = upto(m.spx, d, 21), upto(m.ust10y, d, 21)
    if len(spx) < 21 or len(y) < 21 or last(m.spx, d) is None:
        return None
    bond_ret = -7.0 * (y[-1] - y[0]) / 100.0
    return (spx[-1] / spx[0] - 1.0) - bond_ret


FNG_COMPONENTS = {
    "momentum": _momentum,
    "breadth": _breadth,
    "put_call": _put_call,
    "volatility": _volatility,
    "junk_bond_demand": _junk,
    "safe_haven_demand": _safe_haven,
}


@dataclass(frozen=True)
class Component:
    name: str
    raw: float
    score: float


def fng_replica(m: Market, d: date, lookback: int = 252) -> tuple[float | None, list[Component]]:
    """Each component's percentile vs its own trailing year (0 fear .. 100 greed);
    the composite needs at least four of the six, else None (fail closed)."""
    dates = [k for k in _keys(m.vix or m.spx) if k <= d][-lookback:]
    parts: list[Component] = []
    for name, fn in FNG_COMPONENTS.items():
        raw = fn(m, d)
        if raw is None:
            continue
        hist = [v for v in (fn(m, k) for k in dates) if v is not None]
        parts.append(Component(name, raw, pct_rank(hist, raw)))
    if len(parts) < 4:
        return None, parts
    return round(sum(p.score for p in parts) / len(parts), 2), parts


def fng_state(v: float | None) -> str:
    if v is None:
        return "unknown"
    if v <= 25:
        return "extreme fear"
    if v <= 45:
        return "fear"
    if v < 55:
        return "neutral"
    if v < 75:
        return "greed"
    return "extreme greed"


# ----------------------------------------------------------------- vol / regime (R2, R3)
@dataclass(frozen=True)
class VolState:
    vix: float | None
    vix3m: float | None
    vxn: float | None
    vix_5d_mean: float | None
    term_ratio: float | None
    spike: bool | None

    @property
    def backwardation(self) -> bool:
        return self.term_ratio is not None and self.term_ratio > 1.0

    @property
    def label(self) -> str:
        if self.vix is None:
            return "unknown"
        if self.spike:
            return "spike"
        if self.backwardation:
            return "stressed"
        if self.term_ratio is not None and self.term_ratio < 0.9:
            return "calm"
        return "normal"


def vol_state(m: Market, d: date, vix_now: float | None = None) -> VolState:
    vix = vix_now if vix_now is not None else last(m.vix, d, max_age_days=4)
    vix3m = last(m.vix3m, d, max_age_days=4)
    vxn = last(m.vxn, d, max_age_days=4)
    prior = upto(m.vix, d, 5, strict=True)
    mean5 = sum(prior) / len(prior) if len(prior) == 5 else None
    ratio = vix / vix3m if vix is not None and vix3m else None
    spike = (
        None
        if vix is None or mean5 is None or vix3m is None
        else vix >= VOL_SPIKE_MULT * mean5 and vix > vix3m
    )
    return VolState(vix, vix3m, vxn, mean5, ratio, spike)


@dataclass(frozen=True)
class OneWay:
    body_ratio: float
    direction: str
    one_way: bool


def one_way(o: float, h: float, low: float, c: float) -> OneWay | None:
    rng = h - low
    if rng <= 0:
        return None
    ratio = abs(c - o) / rng
    return OneWay(round(ratio, 4), "up" if c >= o else "down", ratio >= ONE_WAY_BODY)


Bar = tuple[datetime, float, float]  # (ts in ET, open, close)


def chop_ratio(bars: Sequence[Bar], d: date, window: int = 20) -> float | None:
    """V221's 20-day efficiency on intraday bars: |first open of the last day before ``d``
    − first open ``window`` days earlier| / the summed bar-to-bar path over those days."""
    days: dict[date, list[Bar]] = {}
    for b in bars:
        if b[0].date() < d:
            days.setdefault(b[0].date(), []).append(b)
    keys = sorted(days)[-window:]
    if len(keys) < window:
        return None
    path = 0.0
    prev: float | None = None
    for k in keys:
        for _, _, close in sorted(days[k]):
            if prev is not None:
                path += abs(close - prev)
            prev = close
    first = sorted(days[keys[0]])[0][1]
    lastday = sorted(days[keys[-1]])[0][1]
    return None if path <= 0 else round(abs(lastday - first) / path, 5)


# ----------------------------------------------------------------- positioning (E3)
@dataclass(frozen=True)
class CotRow:
    report_date: date
    open_interest: float
    lev_long: float
    lev_short: float
    am_long: float
    am_short: float


@dataclass(frozen=True)
class Positioning:
    report_date: date
    lev_net_pct: float
    lev_z: float | None
    am_net_pct: float
    am_z: float | None

    @property
    def crowding(self) -> str:
        if self.lev_z is None:
            return "unknown"
        if self.lev_z <= -2:
            return "crowded short"
        if self.lev_z >= 2:
            return "crowded long"
        return "neutral"


def _z(xs: Sequence[float], x: float) -> float | None:
    if len(xs) < 20:
        return None
    mu = sum(xs) / len(xs)
    sd = math.sqrt(sum((v - mu) ** 2 for v in xs) / (len(xs) - 1))
    return None if sd == 0 else round((x - mu) / sd, 3)


def positioning(rows: Sequence[CotRow], d: date, weeks: int = 156) -> Positioning | None:
    """Latest COT report published by ``d`` (Tuesday data, released Friday)."""
    usable = sorted(
        (r for r in rows if r.report_date + timedelta(days=3) <= d and r.open_interest > 0),
        key=lambda r: r.report_date,
    )[-weeks:]
    if not usable:
        return None
    lev = [(r.lev_long - r.lev_short) / r.open_interest for r in usable]
    am = [(r.am_long - r.am_short) / r.open_interest for r in usable]
    return Positioning(
        usable[-1].report_date,
        round(lev[-1], 5),
        _z(lev[:-1], lev[-1]),
        round(am[-1], 5),
        _z(am[:-1], am[-1]),
    )


# ----------------------------------------------------------------- narrative (§10)
@dataclass(frozen=True)
class Item:
    ts: datetime
    score: float
    tier: float
    novelty: float
    kill: bool


@dataclass(frozen=True)
class Narrative:
    score: float | None
    dispersion: float | None
    n: int
    effective_n: float
    kill_hits: int


def aggregate(
    items: Sequence[Item], at: datetime, tau_hours: float = 5.0, lookback_hours: float = 24.0
) -> Narrative:
    """Σwᵢsᵢ/Σwᵢ with wᵢ = e^(−Δt/τ) × tier × novelty, plus weighted dispersion."""
    used = [it for it in items if timedelta(0) <= at - it.ts <= timedelta(hours=lookback_hours)]
    ws = [
        math.exp(-((at - it.ts).total_seconds() / 3600.0) / tau_hours) * it.tier * it.novelty
        for it in used
    ]
    kills = sum(1 for it in used if it.kill)
    total = sum(ws)
    if total <= 0:
        return Narrative(None, None, len(used), 0.0, kills)
    mean = sum(w * it.score for w, it in zip(ws, used, strict=True)) / total
    var = sum(w * (it.score - mean) ** 2 for w, it in zip(ws, used, strict=True)) / total
    eff = total**2 / sum(w * w for w in ws)
    return Narrative(round(mean, 4), round(math.sqrt(var), 4), len(used), round(eff, 2), kills)


def psi(expected: Sequence[float], actual: Sequence[float], bins: int = 10) -> float | None:
    """Population stability index of ``actual`` against ``expected`` on expected's deciles."""
    if len(expected) < 20 or len(actual) < 5:
        return None
    srt = sorted(expected)
    edges = [srt[int(len(srt) * i / bins)] for i in range(1, bins)]

    def shares(xs: Sequence[float]) -> list[float]:
        counts = [0] * bins
        for x in xs:
            counts[bisect.bisect_right(edges, x)] += 1
        return [max(c / len(xs), 1e-4) for c in counts]

    e, a = shares(expected), shares(actual)
    return round(sum((ai - ei) * math.log(ai / ei) for ai, ei in zip(a, e, strict=True)), 4)
