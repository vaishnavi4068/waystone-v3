"""Tune the research window to whatever history exists (2-5 years)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

MIN_YEARS = 2.0
MAX_YEARS = 5.0
_DAYS_PER_YEAR = 365.25


def _shift_years(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, day=28)


def default_window(years: int = 5) -> tuple[str, str]:
    end = date.today()
    start = _shift_years(end, -years)
    return start.isoformat(), end.isoformat()


def span_years(start: date, end: date) -> float:
    return max(0.0, (end - start).days / _DAYS_PER_YEAR)


def tune_years(
    available_years: float,
    *,
    min_years: float = MIN_YEARS,
    max_years: float = MAX_YEARS,
) -> float | None:
    """Clamp available history into [min, max]. None if shorter than the minimum."""
    if available_years + 1e-9 < min_years:
        return None
    return min(available_years, max_years)


def clamp_span(
    start: date,
    end: date,
    *,
    min_years: float = MIN_YEARS,
    max_years: float = MAX_YEARS,
) -> tuple[date, date] | None:
    """Keep the most recent history, at most max_years, at least min_years."""
    if end <= start:
        return None
    years = span_years(start, end)
    if years + 1e-9 < min_years:
        return None
    if years > max_years:
        start = _shift_years(end, -int(max_years))
        # int(max_years) is 5; if max_years is 5.0 this is exact.
        if span_years(start, end) > max_years:
            start = end - timedelta(days=int(max_years * _DAYS_PER_YEAR))
    return start, end


def intersect_spans(spans: list[tuple[date, date]]) -> tuple[date, date] | None:
    if not spans:
        return None
    start = max(row[0] for row in spans)
    end = min(row[1] for row in spans)
    if end <= start:
        return None
    return start, end


def csv_date_span(path: Path) -> tuple[date, date] | None:
    """First and last ISO date in a daily or 1-min CSV (date or ts column)."""
    if not path.is_file():
        return None
    first: date | None = None
    last: date | None = None
    with path.open() as handle:
        header = handle.readline()
        if not header:
            return None
        for raw in handle:
            token = raw.split(",", 1)[0].strip().strip('"')
            if len(token) < 10:
                continue
            try:
                day = date.fromisoformat(token[:10])
            except ValueError:
                continue
            if first is None:
                first = day
            last = day
    if first is None or last is None:
        return None
    if last < first:
        first, last = last, first
    return first, last


def _gcs_daily_span(symbol: str) -> tuple[date, date] | None:
    try:
        from waystone3.research.nsdq250 import pick_widest_daily

        picked = pick_widest_daily(symbol)
    except Exception:
        return None
    if picked is None:
        return None
    return picked.start, picked.end


def collect_spans(
    symbols: list[str],
    data_dir: Path,
    *,
    roots: list[str] | None = None,
    prefer_gcs: bool = True,
) -> list[tuple[date, date]]:
    """Local daily CSVs first, then NSDQ250 filename ranges (no download)."""
    spans: list[tuple[date, date]] = []
    daily = data_dir / "daily"
    for symbol in symbols:
        raw = symbol.upper()
        safe = raw.replace("^", "_").replace(":", "_").replace("/", "_")
        local = csv_date_span(daily / f"{raw}.csv") or csv_date_span(daily / f"{safe}.csv")
        if local is not None:
            spans.append(local)
            continue
        if prefer_gcs:
            remote = _gcs_daily_span(symbol)
            if remote is not None:
                spans.append(remote)
    for root in roots or []:
        intra = csv_date_span(data_dir / "intraday" / f"{root.upper()}_1min.csv")
        if intra is not None:
            spans.append(intra)
    return spans


@dataclass(frozen=True)
class TunedWindow:
    start: str
    end: str
    years: float
    source: str

    @property
    def args(self) -> list[str]:
        return ["--start", self.start, "--end", self.end]


def resolve_window(
    symbols: list[str],
    data_dir: Path,
    *,
    roots: list[str] | None = None,
    min_years: float = MIN_YEARS,
    max_years: float = MAX_YEARS,
) -> TunedWindow | None:
    """Intersection of available series, clamped to [min_years, max_years].

    Example: GCS files covering ~4 years → a 4-year window. History longer
    than ``max_years`` is trimmed to the most recent ``max_years``. Below
    ``min_years`` returns None (do not run a 1-year research sleeve).
    """
    spans = collect_spans(symbols, data_dir, roots=roots)
    if not spans:
        start_s, end_s = default_window(int(max_years))
        return TunedWindow(start_s, end_s, float(int(max_years)), "calendar")
    overlap = intersect_spans(spans)
    if overlap is None:
        return None
    clamped = clamp_span(*overlap, min_years=min_years, max_years=max_years)
    if clamped is None:
        return None
    start, end = clamped
    return TunedWindow(start.isoformat(), end.isoformat(), span_years(start, end), "data")


def years_from_equity(path: Path) -> float | None:
    span = csv_date_span(path)
    if span is None:
        return None
    return round(span_years(*span), 2)
