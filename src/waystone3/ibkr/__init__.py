"""IBKR daily dump + GCS report reader.

The dump CLI talks to TWS/Gateway (read-only). The dashboard API only reads GCS
(or a local fixture tree that mirrors the same key layout).
"""

from waystone3.ibkr.models import (
    SCHEMA_VERSION,
    AccountSnapshot,
    Book,
    BookStats,
    DailyReport,
    DailySummary,
    Execution,
    Manifest,
    PositionSnapshot,
)
from waystone3.ibkr.paths import PREFIX, prefix_for

__all__ = [
    "PREFIX",
    "SCHEMA_VERSION",
    "AccountSnapshot",
    "Book",
    "BookStats",
    "DailyReport",
    "DailySummary",
    "Execution",
    "Manifest",
    "PositionSnapshot",
    "prefix_for",
]
