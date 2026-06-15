"""News sources feeding the sentiment contributor."""

from waystone3.news.base import Article, NewsSource
from waystone3.news.polygon_news import PolygonNewsSource

__all__ = ["Article", "NewsSource", "PolygonNewsSource"]
