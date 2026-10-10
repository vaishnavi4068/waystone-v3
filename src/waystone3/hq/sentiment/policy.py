"""Sentiment gate and strategy selector (thesis §5, §10, §11; policy ``POLICY_VERSION``).

Each gate is decided on its own and stored as its own audit row (``sentiment.gate_decision``)
with a state, confidence, evidence, the as-of time of its inputs and an expiry:

  data         HALT when a required input (macro calendar, VIX) is missing or older than its
               freshness limit. Fail closed, and no override can open it.
  event        E1: no new entries from 30 min before to 60 min after CPI / NFP / PCE / FOMC.
  kill         E2: macro kill switch. Only factual, market-relevant headlines count (kill term
               plus an index / macro / market entity; opinion, prediction and how-to framing and
               idioms like "price war" are excluded), tone must be negative. Asymmetric trust:
               one wire-tier source halts; lower tiers need two independent publishers on the
               same story (syndicated reprints count once). The halt holds
               for ``KILL_COOLDOWN`` after the last hit (hysteresis). Feeds down intraday:
               CAUTION, because a halt could be missed.
  engine       V221's own entry gate, mirrored: blocked when prior-day F&G ≤ 30 AND
               chop ≤ 0.03; a missing F&G does not block (as in the engine).
  vol          R3 vol spike (VIX ≥ 1.10× its prior 5-day mean and above VIX3M) or VIX3M
               backwardation: half size.
  positioning  E3: COT leveraged-fund crowding. Context only; never sizes up or sets direction.

Text can only halt or shrink; nothing here raises size above 1.0 (increases wait for Tier-0
validity: DSR and walk-forward). Rank is historical net P&L per session in the same regime
(F&G bucket × vol state), shrunk toward the strategy's all-regime mean.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from waystone3.hq.sentiment.features import (
    CHOP_THRESHOLD,
    FNG_FEAR,
    FNG_GREED,
    Narrative,
    OneWay,
    Positioning,
    VolState,
)
from waystone3.hq.sentiment.nlp import independent_sources

POLICY_VERSION = "sg-1.1"
EVENT_BEFORE = timedelta(minutes=30)
EVENT_AFTER = timedelta(minutes=60)
KILL_LOOKBACK = timedelta(hours=3)
KILL_COOLDOWN = timedelta(hours=3)
KILL_MIN_NEG = -0.30
KILL_WIRE_TIER = 1.0
KILL_CORROBORATION = 2
HEADLINES_MAX_AGE = timedelta(hours=2)
SHRINK_K = 5.0

OPEN, CAUTION, HALT, BLOCKED, UNKNOWN = "OPEN", "CAUTION", "HALT", "BLOCKED", "UNKNOWN"
GATES = ("data", "event", "kill", "engine", "vol", "positioning")
STOPS = (HALT, BLOCKED)


def policy_config() -> dict[str, Any]:
    return {
        "policy_version": POLICY_VERSION,
        "event_window_min": [
            -int(EVENT_BEFORE.total_seconds() // 60),
            int(EVENT_AFTER.total_seconds() // 60),
        ],
        "kill_lookback_h": KILL_LOOKBACK.total_seconds() / 3600,
        "kill_cooldown_h": KILL_COOLDOWN.total_seconds() / 3600,
        "kill_min_negative": KILL_MIN_NEG,
        "kill_corroboration": KILL_CORROBORATION,
        "kill_requires": "factual, market-relevant, same story",
        "fng_fear": FNG_FEAR,
        "chop_threshold": CHOP_THRESHOLD,
        "vol_spike_mult": 1.10,
        "headlines_max_age_h": HEADLINES_MAX_AGE.total_seconds() / 3600,
        "max_size": 1.0,
        "shrink_k": SHRINK_K,
    }


@dataclass(frozen=True)
class Event:
    kind: str
    title: str
    ts: datetime


@dataclass(frozen=True)
class KillHeadline:
    title: str
    terms: tuple[str, ...]
    ts: datetime
    tier: float
    score: float
    publisher: str


@dataclass(frozen=True)
class StrategyRef:
    code: str
    root: str


@dataclass(frozen=True)
class Override:
    gate: str
    action: str  # FORCE_HALT | FORCE_OPEN
    reason: str
    created_by: str
    strategy_code: str | None = None
    valid_to: datetime | None = None


@dataclass(frozen=True)
class Fit:
    regime: str
    n: int
    mean_regime: float | None
    n_all: int
    mean_all: float | None

    @property
    def score(self) -> float | None:
        if self.mean_all is None:
            return None
        if not self.n or self.mean_regime is None:
            return round(self.mean_all, 2)
        return round(
            (self.n * self.mean_regime + SHRINK_K * self.mean_all) / (self.n + SHRINK_K), 2
        )


@dataclass(frozen=True)
class Gate:
    name: str
    state: str
    reason: str = ""
    size_mult: float = 1.0
    confidence: float = 1.0
    evidence: tuple[str, ...] = ()
    as_of: datetime | None = None
    expires_at: datetime | None = None
    override: str | None = None

    @property
    def stops(self) -> bool:
        return self.state in STOPS

    @property
    def text(self) -> str:
        return f"{self.state}: {self.reason}" if self.reason else self.state


@dataclass
class SlotInputs:
    session_date: date
    slot_label: str
    at: datetime
    fng_prior: float | None
    vol: VolState
    events: Sequence[Event]
    calendar_ok: bool
    narrative: Narrative
    kills: Sequence[KillHeadline]
    headlines_as_of: datetime | None = None
    vix_as_of: date | None = None
    fng_as_of: date | None = None
    chop: dict[str, float | None] = field(default_factory=dict)
    positioning: dict[str, Positioning | None] = field(default_factory=dict)
    one_way_prior: dict[str, OneWay | None] = field(default_factory=dict)

    @property
    def intraday(self) -> bool:
        return self.slot_label != "DAY"

    def fingerprint(self) -> str:
        """Stable hash of every input the gates read, for the audit trail."""

        def enc(o: Any) -> Any:
            if isinstance(o, datetime | date):
                return o.isoformat()
            raise TypeError(type(o))

        raw = json.dumps(
            {
                "policy": POLICY_VERSION,
                "date": self.session_date,
                "slot": self.slot_label,
                "fng_prior": self.fng_prior,
                "vol": asdict(self.vol),
                "events": [asdict(e) for e in self.events],
                "calendar_ok": self.calendar_ok,
                "narrative": asdict(self.narrative),
                "kills": [asdict(k) for k in self.kills],
                "chop": self.chop,
                "positioning": {k: asdict(v) if v else None for k, v in self.positioning.items()},
            },
            default=enc,
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Recommendation:
    strategy_code: str
    verdict: str
    size_mult: float
    rank: int
    fit: Fit
    gates: tuple[Gate, ...]
    reasons: tuple[str, ...]

    def gate(self, name: str) -> Gate:
        return next(g for g in self.gates if g.name == name)


def regime_key(fng: float | None, vol: VolState) -> str:
    bucket = (
        "unknown"
        if fng is None
        else "fear"
        if fng <= FNG_FEAR
        else "greed"
        if fng >= FNG_GREED
        else "neutral"
    )
    v = vol.label
    return f"{bucket}/{'stressed' if v in ('spike', 'stressed') else v}"


def chop_root(root: str) -> str:
    return {"MNQ": "NQ", "MES": "ES", "M2K": "RTY"}.get(root, root)


def _at(d: date | None, s: SlotInputs) -> datetime | None:
    return None if d is None else datetime.combine(d, s.at.timetz())


# ------------------------------------------------------------------- gates
def gate_data(s: SlotInputs) -> Gate:
    missing = []
    if not s.calendar_ok:
        missing.append("macro calendar (not refreshed in 7 days)")
    if s.vol.vix is None:
        missing.append("VIX (none in the last 4 days)")
    if missing:
        return Gate(
            "data",
            HALT,
            "missing " + "; ".join(missing) + " — fail closed",
            0.0,
            1.0,
            tuple(missing),
            _at(s.vix_as_of, s),
        )
    return Gate("data", OPEN, as_of=_at(s.vix_as_of, s))


def gate_event(s: SlotInputs) -> Gate:
    evidence = tuple(f"{e.kind} {e.ts:%Y-%m-%d %H:%M} ET — {e.title}" for e in s.events)
    if s.intraday:
        live = [e for e in s.events if e.ts - EVENT_BEFORE <= s.at <= e.ts + EVENT_AFTER]
        if live:
            e = live[0]
            return Gate(
                "event",
                HALT,
                f"{e.kind} at {e.ts:%H:%M} ET; no new entries "
                f"{e.ts - EVENT_BEFORE:%H:%M}–{e.ts + EVENT_AFTER:%H:%M}",
                0.0,
                1.0,
                evidence,
                s.at,
                e.ts + EVENT_AFTER,
            )
        later = [e for e in s.events if e.ts > s.at]
        if later:
            e = later[0]
            return Gate(
                "event",
                CAUTION,
                f"{e.kind} at {e.ts:%H:%M} ET, blackout starts {e.ts - EVENT_BEFORE:%H:%M}",
                1.0,
                1.0,
                evidence,
                s.at,
                e.ts - EVENT_BEFORE,
            )
        return Gate("event", OPEN, as_of=s.at, evidence=evidence)
    if s.events:
        windows = ", ".join(
            f"{e.kind} {e.ts - EVENT_BEFORE:%H:%M}–{e.ts + EVENT_AFTER:%H:%M}" for e in s.events
        )
        return Gate(
            "event",
            CAUTION,
            f"blackout {windows} ET",
            0.75,
            1.0,
            evidence,
            s.at,
            max(e.ts for e in s.events) + EVENT_AFTER,
        )
    return Gate("event", OPEN, as_of=s.at)


def gate_kill(s: SlotInputs) -> Gate:
    window_start = s.at - (KILL_LOOKBACK if s.intraday else timedelta(hours=24))
    hits = sorted(
        (
            k
            for k in s.kills
            if window_start <= k.ts <= s.at and k.score <= KILL_MIN_NEG and k.tier > 0
        ),
        key=lambda k: k.ts,
    )
    wire = [k for k in hits if k.tier >= KILL_WIRE_TIER]
    sources = independent_sources([(k.title, k.publisher) for k in hits])
    evidence = tuple(
        f"{k.ts:%H:%M} [{k.publisher}, tier {k.tier:g}, tone {k.score:+.2f}] "
        f"{k.title[:120]} ({', '.join(k.terms)})"
        for k in hits[-5:]
    )
    if wire or sources >= KILL_CORROBORATION:
        trigger = wire[-1] if wire else hits[-1]
        basis = "wire-tier source" if wire else f"{sources} independent publishers, same story"
        conf = min(1.0, 0.6 + 0.2 * (len(wire) + sources - 1))
        return Gate(
            "kill",
            HALT,
            f"'{', '.join(trigger.terms)}' via {basis} — {trigger.title[:90]}",
            0.0,
            round(conf, 2),
            evidence,
            s.at,
            hits[-1].ts + KILL_COOLDOWN,
        )
    stale = s.headlines_as_of is None or s.at - s.headlines_as_of > HEADLINES_MAX_AGE
    if s.intraday and stale:
        age = (
            "never"
            if s.headlines_as_of is None
            else f"{(s.at - s.headlines_as_of).total_seconds() / 3600:.1f}h ago"
        )
        return Gate(
            "kill",
            CAUTION,
            f"headline feeds stale (last fetch {age}); a halt could be missed",
            0.5,
            0.5,
            (),
            s.headlines_as_of,
        )
    if hits:
        k = hits[-1]
        return Gate(
            "kill",
            CAUTION,
            f"uncorroborated '{', '.join(k.terms)}' from {k.publisher}; one more publisher halts",
            1.0,
            0.4,
            evidence,
            s.at,
            k.ts + KILL_LOOKBACK,
        )
    return Gate("kill", OPEN, as_of=s.headlines_as_of or s.at)


def gate_engine(s: SlotInputs, root: str) -> Gate:
    chop = s.chop.get(chop_root(root))
    as_of = _at(s.fng_as_of, s)
    ev = (f"prior-day F&G {s.fng_prior}", f"chop({chop_root(root)}) {chop}")
    if s.fng_prior is None:
        return Gate(
            "engine", OPEN, "no prior-day F&G; the engine does not block", 1.0, 0.5, ev, as_of
        )
    fear = s.fng_prior <= FNG_FEAR
    if chop is None:
        if fear:
            return Gate(
                "engine",
                CAUTION,
                f"F&G {s.fng_prior:.0f} ≤ 30 and chop unknown; engine may block",
                0.5,
                0.5,
                ev,
                as_of,
            )
        return Gate("engine", OPEN, f"F&G {s.fng_prior:.0f} > 30", 1.0, 1.0, ev, as_of)
    if fear and chop <= CHOP_THRESHOLD:
        return Gate(
            "engine",
            BLOCKED,
            f"F&G {s.fng_prior:.0f} ≤ 30 and chop {chop:.4f} ≤ 0.03",
            0.0,
            0.9,
            ev,
            as_of,
        )
    why = f"F&G {s.fng_prior:.0f} > 30" if not fear else f"chop {chop:.4f} > 0.03"
    return Gate("engine", OPEN, why, 1.0, 0.9, ev, as_of)


def gate_vol(s: SlotInputs) -> Gate:
    v = s.vol
    ev = (f"VIX {v.vix}", f"VIX3M {v.vix3m}", f"5d mean {v.vix_5d_mean}", f"VXN {v.vxn}")
    as_of = _at(s.vix_as_of, s)
    if v.spike:
        return Gate(
            "vol",
            CAUTION,
            f"R3 spike: VIX {v.vix:.2f} ≥ 1.10× 5-day mean {v.vix_5d_mean:.2f} and above VIX3M",
            0.5,
            1.0,
            ev,
            as_of,
        )
    if v.backwardation:
        return Gate(
            "vol",
            CAUTION,
            f"VIX {v.vix:.2f} above VIX3M {v.vix3m:.2f} (backwardation)",
            0.5,
            1.0,
            ev,
            as_of,
        )
    if v.vix is not None and v.spike is None:
        return Gate(
            "vol",
            OPEN,
            "spike test incomplete (VIX3M or 5-day history missing)",
            1.0,
            0.6,
            ev,
            as_of,
        )
    return Gate("vol", OPEN, as_of=as_of, evidence=ev)


def gate_positioning(p: Positioning | None) -> Gate:
    if p is None or p.lev_z is None:
        return Gate("positioning", UNKNOWN, "no COT history", 1.0, 0.0)
    note = {
        "crowded short": "squeeze risk on rallies; dips are crowded",
        "crowded long": "air-pocket risk on bad news",
        "neutral": "no crowding",
    }[p.crowding]
    state = OPEN if p.crowding == "neutral" else CAUTION
    return Gate(
        "positioning",
        state,
        f"{p.crowding} (leveraged funds z {p.lev_z:+.2f}, COT {p.report_date:%m-%d}): {note}",
        1.0,
        0.7,
        (
            f"lev net/OI {p.lev_net_pct:+.3f} z {p.lev_z}",
            f"asset mgr net/OI {p.am_net_pct:+.3f} z {p.am_z}",
        ),
        datetime.combine(p.report_date, datetime.min.time()),
    )


def apply_overrides(gate: Gate, code: str, overrides: Sequence[Override], at: datetime) -> Gate:
    for o in overrides:
        if o.gate != gate.name or (o.strategy_code and o.strategy_code != code):
            continue
        if o.valid_to is not None and o.valid_to < at:
            continue
        tag = f"{o.action} by {o.created_by}: {o.reason}"
        if o.action == "FORCE_HALT":
            return Gate(
                gate.name,
                HALT,
                f"operator override — {o.reason}",
                0.0,
                1.0,
                gate.evidence,
                gate.as_of,
                o.valid_to,
                tag,
            )
        if o.action == "FORCE_OPEN" and gate.name != "data":
            return Gate(
                gate.name,
                OPEN,
                f"operator override — {o.reason} (was {gate.state})",
                1.0,
                1.0,
                gate.evidence,
                gate.as_of,
                o.valid_to,
                tag,
            )
    return gate


def recommend(
    s: SlotInputs,
    strategies: Sequence[StrategyRef],
    fits: dict[str, Fit],
    overrides: Sequence[Override] = (),
) -> list[Recommendation]:
    shared = (gate_data(s), gate_event(s), gate_kill(s), gate_vol(s))
    regime = regime_key(s.fng_prior, s.vol)
    drafts = []
    for st in strategies:
        gates = tuple(
            apply_overrides(g, st.code, overrides, s.at)
            for g in (
                *shared,
                gate_engine(s, st.root),
                gate_positioning(s.positioning.get(chop_root(st.root))),
            )
        )
        fit = fits.get(st.code) or Fit(regime, 0, None, 0, None)
        reasons = [
            f"{g.name}: {g.reason}" for g in gates if g.state not in (OPEN, UNKNOWN) and g.reason
        ]
        stand_down = any(g.stops for g in gates)
        size = 1.0
        for g in gates:
            size *= g.size_mult if g.name != "positioning" else 1.0
        if fit.score is not None and fit.score < 0 and fit.n >= 5:
            size *= 0.5
            reasons.append(f"fit: loses in {regime} historically (n={fit.n}, {fit.score:+.0f}/day)")
        ow = s.one_way_prior.get(chop_root(st.root))
        if ow is not None and ow.one_way:
            reasons.append(
                f"regime: prior session one-way {ow.direction} (body {ow.body_ratio:.0%} of range)"
            )
        size = 0.0 if stand_down else round(min(1.0, max(0.25, size)), 2)
        verdict = "STAND_DOWN" if stand_down else ("REDUCE" if size < 1.0 else "TRADE")
        drafts.append((st, verdict, size, fit, gates, reasons))
    order = sorted(
        drafts,
        key=lambda d: (
            d[1] == "STAND_DOWN",
            -(d[3].score if d[3].score is not None else 0.0),
            d[0].code,
        ),
    )
    return [
        Recommendation(st.code, verdict, size, rank, fit, gates, tuple(reasons))
        for rank, (st, verdict, size, fit, gates, reasons) in enumerate(order, start=1)
    ]
