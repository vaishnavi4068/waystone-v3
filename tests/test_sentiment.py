from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from conftest import make_bars, rising

from waystone3.news.polygon_news import PolygonNewsSource
from waystone3.signals.sentiment import SentimentContributor, build_sentiment_scores
from waystone3.signals.sentiment_scorer import ClaudeSentimentScorer, SentimentScore

_NEWS = {
    "results": [
        {
            "title": "AAPL beats earnings",
            "description": "Big Q3 beat",
            "tickers": ["AAPL"],
            "published_utc": "2024-01-02T12:00:00Z",
            "article_url": "http://x/1",
            "publisher": {"name": "Acme Wire"},
        },
        {
            "title": "AAPL faces probe",
            "description": "Regulatory risk",
            "tickers": ["AAPL"],
            "published_utc": "2024-01-02T13:00:00Z",
            "article_url": "http://x/2",
            "publisher": {"name": "Acme Wire"},
        },
    ]
}


async def test_polygon_news_maps_articles() -> None:
    async def fetch(path: str, params: dict[str, Any]) -> dict[str, Any]:
        return _NEWS

    src = PolygonNewsSource(fetch_fn=fetch)
    articles = await src.fetch(["AAPL"])
    assert len(articles) == 2
    assert articles[0].title == "AAPL beats earnings"
    assert articles[0].symbols == ("AAPL",)
    assert articles[0].source == "Acme Wire"


def make_scorer(by_title: dict[str, tuple[str, int]]) -> ClaudeSentimentScorer:
    async def _score(title: str, summary: str | None, symbol: str) -> SentimentScore:
        sentiment, conviction = by_title[title]
        return SentimentScore(
            symbol=symbol,
            sentiment=sentiment,
            conviction=conviction,
            drivers=["d"],
            model="stub",
            scored_at=datetime.now(UTC),
        )

    return ClaudeSentimentScorer(score_fn=_score)


async def test_aggregation_averages_polarity_times_conviction() -> None:
    async def fetch(path: str, params: dict[str, Any]) -> dict[str, Any]:
        return _NEWS

    scorer = make_scorer(
        {"AAPL beats earnings": ("bullish", 8), "AAPL faces probe": ("bearish", 4)}
    )
    scores, drivers = await build_sentiment_scores(
        news_source=PolygonNewsSource(fetch_fn=fetch), scorer=scorer, symbols=["AAPL"]
    )
    # mean(+8, -4) = +2
    assert scores["AAPL"] == Decimal(2)
    assert drivers["AAPL"]


async def test_disabled_scorer_yields_empty() -> None:
    import os

    os.environ.pop("ANTHROPIC_API_KEY", None)
    scorer = ClaudeSentimentScorer()  # no key, no score_fn -> disabled
    assert scorer.enabled is False
    scores, drivers = await build_sentiment_scores(
        news_source=PolygonNewsSource(api_key="x", fetch_fn=lambda p, q: _async(_NEWS)),
        scorer=scorer,
        symbols=["AAPL"],
    )
    assert scores == {} and drivers == {}


async def _async(value: Any) -> Any:
    return value


def test_contributor_scores_only_known_symbols() -> None:
    c = SentimentContributor(scores={"AAPL": Decimal(5)}, drivers={"AAPL": ["beat"]})
    bars = {"AAPL": make_bars(rising(5), symbol="AAPL"), "MSFT": make_bars(rising(5), "MSFT")}
    out = c.score(bars)
    assert set(out) == {"AAPL"}  # MSFT has no sentiment score
    assert out["AAPL"].score == Decimal(5)
    assert out["AAPL"].drivers == ["beat"]


def test_empty_contributor_is_noop() -> None:
    c = SentimentContributor()
    assert c.score({"AAPL": make_bars(rising(5), "AAPL")}) == {}
