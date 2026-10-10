"""Free data feeds (no keys): CBOE, CNN, FRED, CFTC, BLS / BEA / Fed calendars, RSS, Yahoo.

Every method returns plain values; the job records each source's health separately so
one feed being down never blocks the others (and the policy fails closed on the ones it
needs). ``Feeds`` is the network implementation; tests pass a fake with the same methods.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from xml.etree import ElementTree

import httpx

from waystone3.hq.calendar import NY
from waystone3.hq.sentiment.features import CotRow

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120 Safari/537.36"
)
CONTACT_UA = "waystone-hq sentiment (ops@arqflo.ai)"
CBOE_INDEX = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{name}_History.csv"
CBOE_PCR = "https://cdn.cboe.com/data/us/options/market_statistics/daily/{day}_daily_options"
CNN_FNG = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{start}"
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start}"
CFTC_TFF = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
BLS_ICS = "https://www.bls.gov/schedule/news_release/bls.ics"
BEA_SCHEDULE = "https://www.bea.gov/news/schedule"
FOMC_CALENDAR = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
COT_MARKETS = {"ES": "E-MINI S&P 500", "NQ": "NASDAQ MINI", "RTY": "RUSSELL E-MINI"}
FUTURES = {"ES": "ES=F", "NQ": "NQ=F", "RTY": "RTY=F"}
ETFS = {"SPY": "SPY", "QQQ": "QQQ", "IWM": "IWM", "RSP": "RSP"}
RSS_FEEDS = {
    "fed_press": "https://www.federalreserve.gov/feeds/press_all.xml",
    "marketwatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "cnbc_top": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "cnbc_markets": "https://www.cnbc.com/id/15839069/device/rss/rss.html",
}
GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}+when:1d&hl=en-US&gl=US&ceid=US:en"
GOOGLE_QUERIES = (
    "stock market",
    "S&P 500 futures",
    "Nasdaq futures",
    "Federal Reserve",
    "Treasury yields",
    "Russell 2000",
)
_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ],
        1,
    )
}

OHLC = tuple[float, float, float, float]
IntradayBar = tuple[datetime, float, float, float, float]


@dataclass(frozen=True)
class RawHeadline:
    title: str
    url: str
    publisher: str | None
    feed: str
    published_at: datetime

    @property
    def url_hash(self) -> str:
        return hashlib.sha256(self.url.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class CalendarEvent:
    kind: str
    title: str
    ts: datetime
    source: str


@dataclass(frozen=True)
class FngNow:
    history: dict[date, float]
    components: dict[str, tuple[float, str]]


class FeedSource(Protocol):
    def cboe_index(self, name: str) -> dict[date, OHLC]: ...
    def cnn_fng(self, start: date) -> FngNow: ...
    def fred(self, series_id: str, start: date) -> dict[date, float]: ...
    def put_call(self, day: date) -> dict[str, float] | None: ...
    def cot(self, root: str, start: date) -> list[CotRow]: ...
    def daily_bars(self, symbols: Sequence[str], start: date) -> dict[str, dict[date, OHLC]]: ...
    def intraday_bars(
        self, symbols: Sequence[str], period: str, interval: str
    ) -> dict[str, list[IntradayBar]]: ...
    def calendar(self) -> list[CalendarEvent]: ...
    def headlines(self) -> list[RawHeadline]: ...


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def parse_cboe_csv(text: str) -> dict[date, OHLC]:
    out: dict[date, OHLC] = {}
    for row in csv.DictReader(io.StringIO(text)):
        keys = {k.strip().upper(): v for k, v in row.items() if k}
        try:
            d = datetime.strptime(keys["DATE"].strip(), "%m/%d/%Y").date()
        except (KeyError, ValueError):
            continue
        c = _f(keys.get("CLOSE") or keys.get(next(iter(keys)) if len(keys) == 2 else ""))
        if c is None:
            vals = [_f(v) for k, v in keys.items() if k != "DATE"]
            c = next((v for v in reversed(vals) if v is not None), None)
        if c is None:
            continue
        o, h, lo = (_f(keys.get(k)) for k in ("OPEN", "HIGH", "LOW"))
        out[d] = (o or c, h or c, lo or c, c)
    return out


def parse_fred_csv(text: str) -> dict[date, float]:
    out: dict[date, float] = {}
    rows = csv.reader(io.StringIO(text))
    next(rows, None)
    for row in rows:
        if len(row) < 2:
            continue
        v = _f(row[1])
        try:
            d = date.fromisoformat(row[0])
        except ValueError:
            continue
        if v is not None:
            out[d] = v
    return out


def parse_cnn(payload: dict[str, Any]) -> FngNow:
    hist: dict[date, float] = {}
    for row in payload.get("fear_and_greed_historical", {}).get("data", []):
        ts, v = row.get("x"), _f(row.get("y"))
        if ts is None or v is None:
            continue
        d = datetime.fromtimestamp(ts / 1000, tz=NY).date()
        hist[d] = round(v, 2)
    now = payload.get("fear_and_greed") or {}
    if _f(now.get("score")) is not None and now.get("timestamp"):
        d = (
            datetime.fromisoformat(str(now["timestamp"]).replace("Z", "+00:00"))
            .astimezone(NY)
            .date()
        )
        hist[d] = round(float(now["score"]), 2)
    comps: dict[str, tuple[float, str]] = {}
    for key, val in payload.items():
        if key.startswith("fear_and_greed") or not isinstance(val, dict):
            continue
        s = _f(val.get("score"))
        if s is not None:
            comps[key] = (round(s, 2), str(val.get("rating") or ""))
    return FngNow(hist, comps)


def parse_bls_ics(text: str) -> list[CalendarEvent]:
    kinds = {"Consumer Price Index": "CPI", "Employment Situation": "NFP"}
    out: list[CalendarEvent] = []
    for block in text.split("BEGIN:VEVENT")[1:]:
        summary = re.search(r"^SUMMARY:(.+)$", block, re.M)
        start = re.search(r"^DTSTART[^:]*:(\d{8}T\d{4,6})", block, re.M)
        if not summary or not start:
            continue
        title = summary.group(1).strip()
        kind = kinds.get(title)
        if kind is None:
            continue
        raw = start.group(1)
        ts = datetime.strptime(raw[:13], "%Y%m%dT%H%M").replace(tzinfo=NY)
        out.append(CalendarEvent(kind, title, ts, "bls.gov"))
    return out


def parse_bea_schedule(html: str, today: date) -> list[CalendarEvent]:
    out: list[CalendarEvent] = []
    rows = re.split(r"<tr[^>]*scheduled-releases-type", html)[1:]
    for row in rows:
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", row))
        if "personal income and outlays" not in text.lower():
            continue
        m = re.search(r"([A-Z][a-z]+) (\d{1,2}) (\d{1,2}:\d{2} [AP]M)", text)
        t = re.search(r"Personal Income and Outlays,? ([A-Z][a-z]+) (\d{4})", text)
        if not m or not t or m.group(1).lower() not in _MONTHS:
            continue
        month = _MONTHS[m.group(1).lower()]
        data_month, data_year = _MONTHS.get(t.group(1).lower(), month), int(t.group(2))
        year = data_year if month >= data_month else data_year + 1
        clock = datetime.strptime(m.group(3), "%I:%M %p").time()
        ts = datetime.combine(date(year, month, int(m.group(2))), clock, tzinfo=NY)
        out.append(
            CalendarEvent(
                "PCE", f"Personal Income and Outlays, {t.group(1)} {t.group(2)}", ts, "bea.gov"
            )
        )
    return out


def parse_fomc(html: str) -> list[CalendarEvent]:
    out: list[CalendarEvent] = []
    for section in re.split(r'<div class="panel-heading">', html)[1:]:
        year_m = re.search(r"(\d{4}) FOMC Meetings", section)
        if not year_m:
            continue
        year = int(year_m.group(1))
        pairs = re.findall(
            r"fomc-meeting__month[^>]*><strong>([^<]+)</strong>.*?fomc-meeting__date[^>]*>([^<]+)<",
            section,
            re.S,
        )
        for month_txt, date_txt in pairs:
            if "notation" in date_txt.lower() or "unscheduled" in date_txt.lower():
                continue
            days = re.findall(r"\d{1,2}", date_txt)
            months = [_MONTHS.get(p.strip().lower()) for p in month_txt.split("/")]
            if not days or not all(months):
                continue
            last_month = months[-1]
            assert last_month is not None
            d = date(year, last_month, int(days[-1]))
            out.append(
                CalendarEvent(
                    "FOMC",
                    f"FOMC statement ({month_txt} {date_txt.strip()})",
                    datetime.combine(d, time(14, 0), tzinfo=NY),
                    "federalreserve.gov",
                )
            )
    return out


def parse_rss(xml: bytes, feed: str) -> list[RawHeadline]:
    out: list[RawHeadline] = []
    root = ElementTree.fromstring(xml)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = item.findtext("pubDate")
        if not title or not link or not pub:
            continue
        try:
            ts = parsedate_to_datetime(pub)
        except (TypeError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=NY)
        src = item.find("source")
        publisher = src.text.strip() if src is not None and src.text else None
        if feed.startswith("google") and publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3]
        out.append(RawHeadline(title, link, publisher, feed, ts.astimezone(NY)))
    return out


class Feeds:
    def __init__(self, timeout: float = 30.0) -> None:
        self.http = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": UA})

    def _get(self, url: str, **kw: Any) -> httpx.Response:
        r = self.http.get(url, **kw)
        r.raise_for_status()
        return r

    def cboe_index(self, name: str) -> dict[date, OHLC]:
        return parse_cboe_csv(self._get(CBOE_INDEX.format(name=name)).text)

    def cnn_fng(self, start: date) -> FngNow:
        r = self._get(
            CNN_FNG.format(start=start.isoformat()),
            headers={
                "Referer": "https://edition.cnn.com/markets/fear-and-greed",
                "Origin": "https://edition.cnn.com",
                "Accept": "application/json, text/plain, */*",
            },
        )
        return parse_cnn(r.json())

    def fred(self, series_id: str, start: date) -> dict[date, float]:
        return parse_fred_csv(self._get(FRED.format(sid=series_id, start=start.isoformat())).text)

    def put_call(self, day: date) -> dict[str, float] | None:
        r = self.http.get(CBOE_PCR.format(day=day.isoformat()))
        if r.status_code in (403, 404):
            return None
        r.raise_for_status()
        out = {}
        for row in r.json().get("ratios", []):
            v = _f(row.get("value"))
            name = str(row.get("name", "")).upper()
            if v is None:
                continue
            if name.startswith("TOTAL"):
                out["total"] = v
            elif name.startswith("EQUITY"):
                out["equity"] = v
            elif name.startswith("INDEX"):
                out["index"] = v
        return out or None

    def cot(self, root: str, start: date) -> list[CotRow]:
        r = self._get(
            CFTC_TFF,
            params={
                "$where": (
                    f"contract_market_name='{COT_MARKETS[root]}' "
                    f"AND report_date_as_yyyy_mm_dd >= '{start.isoformat()}'"
                ),
                "$order": "report_date_as_yyyy_mm_dd",
                "$limit": "2000",
            },
        )
        out = []
        for row in r.json():
            vals = [
                _f(row.get(k))
                for k in (
                    "open_interest_all",
                    "lev_money_positions_long",
                    "lev_money_positions_short",
                    "asset_mgr_positions_long",
                    "asset_mgr_positions_short",
                )
            ]
            if any(v is None for v in vals):
                continue
            oi, ll, ls, al, ash = (float(v) for v in vals if v is not None)
            out.append(
                CotRow(
                    date.fromisoformat(row["report_date_as_yyyy_mm_dd"][:10]), oi, ll, ls, al, ash
                )
            )
        return out

    def daily_bars(self, symbols: Sequence[str], start: date) -> dict[str, dict[date, OHLC]]:
        import yfinance as yf

        out: dict[str, dict[date, OHLC]] = {}
        for sym in symbols:
            df = yf.Ticker(sym).history(start=start.isoformat(), interval="1d", auto_adjust=False)
            rows: dict[date, OHLC] = {}
            for ts, r in df.iterrows():
                vals = (_f(r["Open"]), _f(r["High"]), _f(r["Low"]), _f(r["Close"]))
                if all(v is not None for v in vals):
                    rows[ts.date()] = tuple(float(v) for v in vals if v is not None)  # type: ignore[assignment]
            out[sym] = rows
        return out

    def intraday_bars(
        self, symbols: Sequence[str], period: str, interval: str
    ) -> dict[str, list[IntradayBar]]:
        import yfinance as yf

        out: dict[str, list[IntradayBar]] = {}
        for sym in symbols:
            df = yf.Ticker(sym).history(period=period, interval=interval, auto_adjust=False)
            bars: list[IntradayBar] = []
            for ts, r in df.iterrows():
                vals = (_f(r["Open"]), _f(r["High"]), _f(r["Low"]), _f(r["Close"]))
                if all(v is not None for v in vals):
                    o, h, lo, c = (float(v) for v in vals if v is not None)
                    bars.append((ts.to_pydatetime().astimezone(NY), o, h, lo, c))
            out[sym] = bars
        return out

    def calendar(self) -> list[CalendarEvent]:
        today = datetime.now(NY).date()
        events = parse_bls_ics(self._get(BLS_ICS, headers={"User-Agent": CONTACT_UA}).text)
        events += parse_fomc(self._get(FOMC_CALENDAR).text)
        events += parse_bea_schedule(self._get(BEA_SCHEDULE).text, today)
        return events

    def headlines(self) -> list[RawHeadline]:
        out: list[RawHeadline] = []
        errors: list[str] = []
        feeds = dict(RSS_FEEDS)
        for q in GOOGLE_QUERIES:
            feeds[f"google:{q}"] = GOOGLE_NEWS.format(q=q.replace(" ", "+").replace("&", "%26"))
        for name, url in feeds.items():
            try:
                out += parse_rss(self._get(url).content, name)
            except (httpx.HTTPError, ElementTree.ParseError) as exc:
                errors.append(f"{name}: {exc}")
        if not out and errors:
            raise RuntimeError("; ".join(errors[:3]))
        return out


def session_for(ts: datetime) -> date:
    """News after the 16:00 ET close counts toward the next weekday session."""
    t = ts.astimezone(NY)
    d = t.date()
    if t.time() > time(16, 0):
        d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d
