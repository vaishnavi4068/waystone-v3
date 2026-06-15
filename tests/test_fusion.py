from __future__ import annotations

from decimal import Decimal

from waystone3.fusion.fuse import fuse
from waystone3.signals.base import ContributorScore


def cs(score: float, confidence: float = 1.0, drivers: list[str] | None = None) -> ContributorScore:
    return ContributorScore(
        score=Decimal(str(score)),
        confidence=Decimal(str(confidence)),
        drivers=drivers or [],
    )


def test_weighted_average() -> None:
    per = {
        "a": {"X": cs(10)},
        "b": {"X": cs(0)},
    }
    # weights 0.75 / 0.25 -> (0.75*10 + 0.25*0)/1.0 = 7.5
    out = fuse(per, {"a": 0.75, "b": 0.25})
    assert out[0].symbol == "X"
    assert out[0].score == Decimal("7.5")


def test_zero_weight_ignored() -> None:
    per = {"a": {"X": cs(10)}, "b": {"X": cs(-10)}}
    out = fuse(per, {"a": 1.0, "b": 0.0})
    assert out[0].score == Decimal(10)


def test_missing_symbol_does_not_vote() -> None:
    per = {"a": {"X": cs(8)}, "b": {"Y": cs(-8)}}
    out = fuse(per, {"a": 1.0, "b": 1.0})
    by = {c.symbol: c.score for c in out}
    assert by["X"] == Decimal(8)
    assert by["Y"] == Decimal(-8)


def test_sorted_by_abs_score() -> None:
    per = {"a": {"WEAK": cs(2), "STRONG": cs(-9)}}
    out = fuse(per, {"a": 1.0})
    assert [c.symbol for c in out] == ["STRONG", "WEAK"]


def test_confidence_scaling() -> None:
    # With confidence weighting, a low-confidence +10 is pulled toward the other vote.
    per = {"a": {"X": cs(10, confidence=0.2)}, "b": {"X": cs(0, confidence=1.0)}}
    plain = fuse(per, {"a": 1.0, "b": 1.0})[0].score
    weighted = fuse(per, {"a": 1.0, "b": 1.0}, use_confidence=True)[0].score
    assert plain == Decimal(5)
    assert weighted < plain  # 0.2*10/(0.2+1.0) ≈ 1.67


def test_no_votes_omitted() -> None:
    per = {"a": {"X": cs(10)}}
    out = fuse(per, {"a": 0.0})  # only contributor zero-weighted
    assert out == []
