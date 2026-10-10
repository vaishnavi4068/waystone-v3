"""Plain-language summary for one interval or session, built from the stored scores."""

from __future__ import annotations

from collections.abc import Sequence

from waystone3.hq.sentiment.features import Positioning, fng_state
from waystone3.hq.sentiment.policy import Recommendation, SlotInputs


def _tone(score: float | None) -> str:
    if score is None:
        return "no scored headlines"
    if score <= -0.35:
        return "clearly negative"
    if score <= -0.1:
        return "mildly negative"
    if score < 0.1:
        return "neutral"
    if score < 0.35:
        return "mildly positive"
    return "clearly positive"


def headline(s: SlotInputs, recs: Sequence[Recommendation]) -> str:
    best = next((r for r in recs if r.verdict != "STAND_DOWN"), None)
    if best is None:
        stop = next((g for r in recs for g in r.gates if g.stops), None)
        return "Stand down — " + (f"{stop.name}: {stop.reason}" if stop else "all strategies gated")
    size = "" if best.size_mult >= 1 else f" at {best.size_mult:g}× size"
    return f"{best.strategy_code} first{size} — {fng_state(s.fng_prior)} tape, vol {s.vol.label}"


def summary(
    s: SlotInputs,
    recs: Sequence[Recommendation],
    *,
    fng_cnn: float | None,
    fng_replica: float | None,
    gaps: Sequence[str],
) -> str:
    lines: list[str] = []
    when = "Session" if not s.intraday else f"As of {s.slot_label} ET"
    fg = []
    if fng_cnn is not None:
        fg.append(f"CNN {fng_cnn:.0f}")
    if fng_replica is not None:
        fg.append(f"replica {fng_replica:.0f}")
    prior = (
        f"prior-day {s.fng_prior:.0f} ({fng_state(s.fng_prior)})"
        if s.fng_prior is not None
        else "prior-day unknown"
    )
    lines.append(f"{when}: Fear & Greed {', '.join(fg) or 'unavailable'}; {prior}.")
    v = s.vol
    if v.vix is not None:
        term = (
            f" vs VIX3M {v.vix3m:.2f} ({'backwardation' if v.backwardation else 'contango'})"
            if v.vix3m
            else ""
        )
        spike = " — R3 vol spike" if v.spike else ""
        lines.append(
            f"VIX {v.vix:.2f}{term}{spike}; VXN {v.vxn:.2f}."
            if v.vxn
            else f"VIX {v.vix:.2f}{term}{spike}."
        )
    if s.events:
        lines.append("Scheduled: " + ", ".join(f"{e.kind} {e.ts:%H:%M}" for e in s.events) + " ET.")
    else:
        lines.append("No CPI, NFP, PCE or FOMC release.")
    n = s.narrative
    if n.n:
        lines.append(
            f"Headlines: {n.n} in the last 24h, narrative {n.score:+.2f} ({_tone(n.score)}), "
            f"dispersion {n.dispersion:.2f}; kill-switch hits {n.kill_hits}."
            if n.score is not None
            else f"Headlines: {n.n}, all zero-weight (junk sources or repeats)."
        )
    else:
        lines.append("Headlines: none scored for this window.")
    for root, ow in sorted(s.one_way_prior.items()):
        if ow is not None and ow.one_way:
            lines.append(f"{root}: prior session was one-way {ow.direction} (R2).")
    for root, p in sorted(s.positioning.items()):
        if isinstance(p, Positioning) and p.lev_z is not None and p.crowding != "neutral":
            lines.append(f"{root} COT: leveraged funds {p.crowding} (z {p.lev_z:+.2f}).")
    for r in recs:
        tag = r.verdict.replace("_", " ").lower()
        size = f" {r.size_mult:g}×" if r.verdict == "REDUCE" else ""
        stop = next((g for g in r.gates if g.stops), None)
        why = (
            f" — {stop.name}: {stop.reason}"
            if stop
            else (f" — {r.reasons[0]}" if r.reasons else "")
        )
        fit = f", fit {r.fit.score:+.0f}/day n={r.fit.n}" if r.fit.score is not None else ""
        lines.append(f"#{r.rank} {r.strategy_code}: {tag}{size}{fit}{why}.")
    if gaps:
        lines.append("Data gaps: " + ", ".join(gaps) + ".")
    return " ".join(lines)
