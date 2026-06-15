"""Price-action contributor.

The "what did price do before the trade" signal. Combines rate-of-change over a short
lookback with trend alignment (is price above its trend mean, and how persistent were
the up-closes). Strong recent gains in a persistent uptrend score near +10; sharp
recent drops below a falling trend score near -10.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise

from waystone3.core.types import Bar
from waystone3.indicators.core import roc, rolling_mean
from waystone3.signals.base import ContributorScore, clamp, clamp_unit


class PriceActionContributor:
    name = "price_action"

    def __init__(
        self,
        roc_lookback: int = 10,
        trend_lookback: int = 20,
        roc_cap: Decimal = Decimal("0.10"),
    ) -> None:
        if roc_lookback < 1 or trend_lookback < 2:
            raise ValueError("invalid lookbacks")
        self.roc_lookback = roc_lookback
        self.trend_lookback = trend_lookback
        self.roc_cap = roc_cap

    def score(self, bars: dict[str, list[Bar]]) -> dict[str, ContributorScore]:
        out: dict[str, ContributorScore] = {}
        need = max(self.roc_lookback + 1, self.trend_lookback)
        for symbol, series in bars.items():
            if len(series) < need:
                continue
            closes = [b.close for b in series]
            change = roc(closes, self.roc_lookback)
            trend_ma = rolling_mean(closes, self.trend_lookback)
            if change is None or trend_ma is None:
                continue
            roc_term = clamp_unit(change / self.roc_cap)

            align = (
                Decimal(1)
                if closes[-1] > trend_ma
                else Decimal(-1)
                if closes[-1] < trend_ma
                else Decimal(0)
            )
            window = closes[-self.trend_lookback :]
            ups = sum(1 for a, b in pairwise(window) if b > a)
            persist = Decimal(ups) / Decimal(len(window) - 1)
            # MA position sets direction; one-sidedness of closes sets conviction.
            conviction = abs(2 * persist - 1)
            align_term = align * conviction

            score = clamp((Decimal("0.6") * roc_term + Decimal("0.4") * align_term) * 10)
            out[symbol] = ContributorScore(
                score=score,
                confidence=clamp_unit(abs(change) / self.roc_cap),
                drivers=[f"roc={change:.2%}", f"persist={persist:.0%}"],
            )
        return out
