"""Sentiment contributor — the same protocol as every technical signal.

The wrinkle (documented in docs/adding-a-signal.md): sentiment data comes from news, not
bars. We keep ``score()`` pure by *injecting* a pre-computed ``{symbol -> score}`` map into
the contributor; an async pre-step (:func:`build_sentiment_scores`) fetches news, scores it
with Claude, and aggregates to the normalized -10..+10 scale before constructing it. With an
empty map (the registry default) it simply contributes nothing — so sentiment is opt-in and
key-optional, exactly like the rest of the platform.
"""

from __future__ import annotations

from decimal import Decimal

from waystone3.core.types import Bar
from waystone3.news.base import NewsSource
from waystone3.signals.base import ContributorScore, clamp
from waystone3.signals.sentiment_scorer import ClaudeSentimentScorer

_POLARITY = {"bullish": Decimal(1), "bearish": Decimal(-1), "neutral": Decimal(0)}


class SentimentContributor:
    name = "sentiment"

    def __init__(
        self,
        scores: dict[str, Decimal] | None = None,
        drivers: dict[str, list[str]] | None = None,
    ) -> None:
        self.scores = scores or {}
        self.drivers = drivers or {}

    def score(self, bars: dict[str, list[Bar]]) -> dict[str, ContributorScore]:
        out: dict[str, ContributorScore] = {}
        for symbol in bars:
            if symbol in self.scores:
                out[symbol] = ContributorScore(
                    score=clamp(self.scores[symbol]),
                    drivers=self.drivers.get(symbol, []),
                )
        return out


async def build_sentiment_scores(
    *,
    news_source: NewsSource,
    scorer: ClaudeSentimentScorer,
    symbols: list[str],
) -> tuple[dict[str, Decimal], dict[str, list[str]]]:
    """Fetch + score news, aggregate to per-symbol -10..+10 scores and driver lists.

    Per article, ``value = polarity * conviction`` (already in [-10, 10]); a symbol's score
    is the mean of its article values. Returns ``({}, {})`` if the scorer is disabled.
    """
    if not scorer.enabled:
        return {}, {}
    wanted = {s.upper() for s in symbols}
    articles = await news_source.fetch(symbols)

    sums: dict[str, Decimal] = {}
    counts: dict[str, int] = {}
    drivers: dict[str, list[str]] = {}
    for article in articles:
        for symbol in article.symbols:
            if symbol not in wanted:
                continue
            result = await scorer.score(article.title, article.summary, symbol)
            value = _POLARITY[result.sentiment] * Decimal(result.conviction)
            sums[symbol] = sums.get(symbol, Decimal(0)) + value
            counts[symbol] = counts.get(symbol, 0) + 1
            drivers.setdefault(symbol, []).extend(result.drivers)

    scores = {sym: (sums[sym] / Decimal(counts[sym])) for sym in sums}
    # keep at most a few representative drivers per symbol
    drivers = {sym: ds[:3] for sym, ds in drivers.items()}
    return scores, drivers
