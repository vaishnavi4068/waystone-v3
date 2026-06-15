"""Moving-average crossover contributor.

Trend-regime signal. Sign comes from which MA is on top; magnitude from how far apart
they are (separation) and whether the fast MA is still moving in that direction (slope).
"""

from __future__ import annotations

from decimal import Decimal

from waystone3.core.types import Bar
from waystone3.indicators.core import ema, sma
from waystone3.signals.base import ContributorScore, clamp, clamp_unit


class MaCrossoverContributor:
    name = "ma_crossover"

    def __init__(
        self,
        fast: int = 10,
        slow: int = 30,
        use_ema: bool = True,
        sep_cap: Decimal = Decimal("0.05"),
    ) -> None:
        if fast >= slow:
            raise ValueError("fast must be shorter than slow")
        self.fast = fast
        self.slow = slow
        self.use_ema = use_ema
        self.sep_cap = sep_cap

    def _fast_now_prev_slow(
        self, closes: list[Decimal]
    ) -> tuple[Decimal, Decimal, Decimal] | None:
        if self.use_ema:
            fast_series = ema(closes, self.fast)
            return fast_series[-1], fast_series[-2], ema(closes, self.slow)[-1]
        fast_now = sma(closes, self.fast)
        fast_prev = sma(closes[:-1], self.fast)
        slow_now = sma(closes, self.slow)
        if fast_now is None or fast_prev is None or slow_now is None:
            return None
        return fast_now, fast_prev, slow_now

    def score(self, bars: dict[str, list[Bar]]) -> dict[str, ContributorScore]:
        out: dict[str, ContributorScore] = {}
        for symbol, series in bars.items():
            if len(series) < self.slow + 1:
                continue
            closes = [b.close for b in series]
            mas = self._fast_now_prev_slow(closes)
            if mas is None:
                continue
            fast_now, fast_prev, slow_now = mas
            if slow_now == 0 or fast_now == 0:
                continue
            sep = (fast_now - slow_now) / slow_now
            slope = (fast_now - fast_prev) / fast_now
            sep_term = clamp_unit(sep / self.sep_cap)
            slope_term = clamp_unit(slope / (self.sep_cap / 2))
            score = clamp((Decimal("0.7") * sep_term + Decimal("0.3") * slope_term) * 10)
            side = f"fast{self.fast}>{self.slow}" if sep >= 0 else f"fast{self.fast}<{self.slow}"
            out[symbol] = ContributorScore(
                score=score,
                confidence=clamp_unit(abs(sep) / self.sep_cap),
                drivers=[side, f"sep={sep:.2%}"],
            )
        return out
