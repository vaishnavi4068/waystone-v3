from __future__ import annotations

from decimal import Decimal

from conftest import make_bars, rising

from waystone3.signals.volume import VolumeContributor


def test_volume_spike_on_up_bar_is_positive() -> None:
    closes = rising(30)  # last bar is an up move
    vols = [1000.0] * 29 + [5000.0]  # big expansion on the final up-bar
    c = VolumeContributor()
    out = c.score({"X": make_bars(closes, symbol="X", volumes=vols)})
    assert out["X"].score > Decimal(5)


def test_volume_spike_on_down_bar_is_negative() -> None:
    closes = [100.0 + i for i in range(29)] + [100.0]  # final bar is a sharp down move
    vols = [1000.0] * 29 + [5000.0]
    c = VolumeContributor()
    out = c.score({"X": make_bars(closes, symbol="X", volumes=vols)})
    assert out["X"].score < Decimal(-5)


def test_quiet_volume_scores_near_zero() -> None:
    closes = rising(30)
    vols = [1000.0] * 30  # no expansion -> rel_vol ~ 1 -> magnitude ~ 0
    c = VolumeContributor()
    out = c.score({"X": make_bars(closes, symbol="X", volumes=vols)})
    assert abs(out["X"].score) < Decimal(1)


def test_warmup_skips_short_series() -> None:
    c = VolumeContributor(lookback=20)
    out = c.score({"X": make_bars(rising(10), symbol="X")})
    assert "X" not in out


def test_spike_saturates_at_cap() -> None:
    closes = rising(30)
    vols = [1000.0] * 29 + [50_000.0]  # far beyond cap
    c = VolumeContributor(cap=Decimal("3"))
    out = c.score({"X": make_bars(closes, symbol="X", volumes=vols)})
    assert out["X"].score == Decimal(10)
