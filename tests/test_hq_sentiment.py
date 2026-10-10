"""Futures sentiment gate: parsers, features, gate rules, and the job end to end on Postgres."""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import conninfo

from waystone3.hq.calendar import NY
from waystone3.hq.db import connect
from waystone3.hq.reader import HqReader
from waystone3.hq.sentiment import features as fx
from waystone3.hq.sentiment import nlp
from waystone3.hq.sentiment import sources as src
from waystone3.hq.sentiment.job import SentimentJob, slot_label
from waystone3.hq.sentiment.policy import (
    BLOCKED,
    CAUTION,
    HALT,
    KILL_COOLDOWN,
    OPEN,
    Event,
    Fit,
    KillHeadline,
    Override,
    SlotInputs,
    StrategyRef,
    gate_engine,
    gate_event,
    gate_kill,
    recommend,
)


# ------------------------------------------------------------------ nlp
def test_normalize_folds_unicode_and_controls() -> None:
    assert nlp.normalize("Ｆｅｄ\u200b  holds\trates\x00") == "Fed holds rates"


def test_source_tiers() -> None:
    assert nlp.source_tier("Reuters", "google:stock market") == 1.0
    assert nlp.source_tier(None, "fed_press") == 1.0
    assert nlp.source_tier("Motley Fool", "google:x") == 0.0
    assert nlp.source_tier("Some Blog", "google:x") == 0.5


def test_kill_terms_need_market_relevance() -> None:
    title = "Stocks tumble as war fears grip markets"
    assert nlp.kill_terms(title) == ["war"]
    assert nlp.market_relevant(title)
    assert not nlp.market_relevant("Star Wars: the war of the clones review")
    assert nlp.kill_terms("Exchange halts trading after circuit breaker") == [
        "circuit breaker",
        "exchange halt",
    ]


def test_lexicon_sign_and_negation() -> None:
    assert nlp.lexicon_score("Stocks rally to record highs") > 0.3
    assert nlp.lexicon_score("Stocks plunge on recession fears") < -0.3
    assert nlp.lexicon_score("Stocks did not rally") < 0


def test_novelty_drops_repeats() -> None:
    idx = nlp.NoveltyIndex.of(["Fed holds rates steady at June meeting"])
    assert idx.novelty("Fed holds rates steady at June meeting") == 0.0
    assert idx.novelty("Oil jumps after OPEC cut") > 0.8


# ------------------------------------------------------------------ features
def test_one_way_day() -> None:
    ow = fx.one_way(100, 110, 99, 109)
    assert ow is not None and ow.one_way and ow.direction == "up"
    two = fx.one_way(100, 110, 90, 101)
    assert two is not None and not two.one_way


def _series(values: Sequence[float], end: date = date(2026, 10, 9)) -> dict[date, float]:
    days = []
    d = end
    while len(days) < len(values):
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return dict(zip(reversed(days), values, strict=True))


def test_vol_spike_needs_mean_and_backwardation() -> None:
    m = fx.Market(vix=_series([15, 15, 15, 15, 15, 18]), vix3m=_series([17] * 6))
    v = fx.vol_state(m, date(2026, 10, 9))
    assert v.spike and v.backwardation and v.label == "spike"
    calm = fx.Market(vix=_series([15] * 6), vix3m=_series([18] * 6))
    c = fx.vol_state(calm, date(2026, 10, 9))
    assert not c.spike and c.label == "calm"


def test_chop_ratio_trending_vs_flat() -> None:
    start = datetime(2026, 9, 1, 0, 0, tzinfo=NY)
    trend, flat = [], []
    for i in range(25 * 24):
        ts = start + timedelta(hours=i)
        trend.append((ts, 100 + i, 100 + i + 1.0))
        wiggle = 100 + (5 if i % 2 else -5)
        flat.append((ts, wiggle, wiggle))
    d = date(2026, 9, 30)
    t = fx.chop_ratio(trend, d)
    f = fx.chop_ratio(flat, d)
    assert t is not None and t > 0.9
    assert f is not None and f <= fx.CHOP_THRESHOLD


def test_positioning_uses_only_published_reports() -> None:
    rows = [
        fx.CotRow(date(2026, 1, 6) + timedelta(weeks=i), 1000, 300 + i % 3, 300, 500, 100)
        for i in range(40)
    ]
    rows.append(fx.CotRow(date(2026, 10, 6), 1000, 100, 600, 500, 100))
    before = fx.positioning(rows, date(2026, 10, 8))
    after = fx.positioning(rows, date(2026, 10, 9))
    assert before is not None and before.report_date < date(2026, 10, 6)
    assert after is not None and after.report_date == date(2026, 10, 6)
    assert after.lev_z is not None and after.lev_z < -2 and after.crowding == "crowded short"


def test_aggregate_recency_tier_and_novelty() -> None:
    at = datetime(2026, 10, 9, 12, 0, tzinfo=NY)
    items = [
        fx.Item(at - timedelta(minutes=10), 0.8, 1.0, 1.0, False),
        fx.Item(at - timedelta(hours=20), -0.8, 1.0, 1.0, False),
        fx.Item(at - timedelta(minutes=5), -0.9, 0.0, 1.0, False),
        fx.Item(at - timedelta(minutes=5), -0.9, 1.0, 0.0, False),
    ]
    n = fx.aggregate(items, at)
    assert n.score is not None and n.score > 0.7
    assert n.n == 4 and n.dispersion is not None


def test_psi() -> None:
    base = [math.sin(i) for i in range(200)]
    p_same = fx.psi(base, base[:40])
    p_shift = fx.psi(base, [x + 2 for x in base[:40]])
    assert p_same is not None and p_same < fx.PSI_ALERT
    assert p_shift is not None and p_shift > fx.PSI_ALERT


def test_fng_replica_fails_closed_with_few_components() -> None:
    m = fx.Market(hy_oas=_series([3.0] * 30))
    value, parts = fx.fng_replica(m, date(2026, 10, 9))
    assert value is None and len(parts) == 1


# ------------------------------------------------------------------ gates
AT = datetime(2026, 10, 9, 11, 0, tzinfo=NY)
CALM = fx.VolState(15.0, 17.0, 18.0, 15.0, 15 / 17, False)
EMPTY = fx.Narrative(None, None, 0, 0.0, 0)
STRATS = [StrategyRef("es_v221", "ES"), StrategyRef("nq_v221", "NQ"), StrategyRef("r2_mnq", "MNQ")]


def _slot(**kw: Any) -> SlotInputs:
    base: dict[str, Any] = {
        "session_date": AT.date(),
        "slot_label": "11:00",
        "at": AT,
        "fng_prior": 45.0,
        "vol": CALM,
        "events": [],
        "calendar_ok": True,
        "narrative": EMPTY,
        "kills": [],
        "headlines_as_of": AT - timedelta(minutes=10),
        "vix_as_of": AT.date(),
        "chop": {"ES": 0.05, "NQ": 0.05, "RTY": 0.05},
    }
    base.update(kw)
    return SlotInputs(**base)


def _kill(publisher: str, tier: float, minutes: int = 20, score: float = -0.7) -> KillHeadline:
    return KillHeadline(
        "Stocks slide as war escalates",
        ("war",),
        AT - timedelta(minutes=minutes),
        tier,
        score,
        publisher,
    )


def test_kill_switch_asymmetric_trust() -> None:
    assert gate_kill(_slot(kills=[_kill("Some Blog", 0.5)])).state == CAUTION
    two = gate_kill(_slot(kills=[_kill("Blog A", 0.5), _kill("Blog B", 0.5, 30)]))
    assert two.state == HALT and "2 independent publishers" in two.reason
    wire = gate_kill(_slot(kills=[_kill("Reuters", 1.0)]))
    assert wire.state == HALT and wire.expires_at == AT - timedelta(minutes=20) + KILL_COOLDOWN
    assert gate_kill(_slot(kills=[_kill("Reuters", 1.0, score=0.4)])).state == OPEN
    assert gate_kill(_slot(kills=[_kill("Reuters", 1.0, minutes=240)])).state == OPEN


def test_kill_switch_stale_feeds_caution_intraday() -> None:
    g = gate_kill(_slot(headlines_as_of=AT - timedelta(hours=5)))
    assert g.state == CAUTION and g.size_mult == 0.5


def test_event_blackout_window() -> None:
    cpi = Event("CPI", "Consumer Price Index", datetime.combine(AT.date(), time(10, 30), tzinfo=NY))
    assert gate_event(_slot(events=[cpi])).state == HALT
    early = _slot(events=[cpi], at=AT - timedelta(hours=2), slot_label="09:00")
    assert gate_event(early).state == CAUTION
    day = gate_event(_slot(events=[cpi], slot_label="DAY"))
    assert day.state == CAUTION and day.size_mult == 0.75 and "10:00–11:30" in day.reason


def test_engine_gate_mirrors_v221() -> None:
    assert gate_engine(_slot(fng_prior=25.0, chop={"NQ": 0.02}), "MNQ").state == BLOCKED
    assert gate_engine(_slot(fng_prior=25.0, chop={"NQ": 0.05}), "NQ").state == OPEN
    assert gate_engine(_slot(fng_prior=60.0, chop={"NQ": 0.01}), "NQ").state == OPEN
    assert gate_engine(_slot(fng_prior=None), "NQ").state == OPEN
    assert gate_engine(_slot(fng_prior=20.0, chop={}), "NQ").state == CAUTION


def test_recommend_fail_closed_and_overrides() -> None:
    recs = recommend(_slot(calendar_ok=False), STRATS, {})
    assert all(r.verdict == "STAND_DOWN" and r.size_mult == 0 for r in recs)
    forced = recommend(
        _slot(calendar_ok=False), STRATS, {}, [Override("data", "FORCE_OPEN", "test", "ops")]
    )
    assert all(r.verdict == "STAND_DOWN" for r in forced)
    halted = recommend(
        _slot(), STRATS, {}, [Override("kill", "FORCE_HALT", "drill", "ops", "nq_v221")]
    )
    by = {r.strategy_code: r for r in halted}
    assert by["nq_v221"].verdict == "STAND_DOWN"
    assert by["nq_v221"].gate("kill").override == "FORCE_HALT by ops: drill"
    assert by["es_v221"].verdict == "TRADE"


def test_recommend_ranks_by_fit_and_never_upsizes() -> None:
    fits = {
        "es_v221": Fit("neutral/normal", 6, 300.0, 10, 200.0),
        "nq_v221": Fit("neutral/normal", 6, -400.0, 10, -100.0),
        "r2_mnq": Fit("neutral/normal", 0, None, 0, None),
    }
    recs = recommend(_slot(vol=fx.VolState(20.0, 18.0, 22.0, 15.0, 20 / 18, True)), STRATS, fits)
    assert [r.strategy_code for r in recs] == ["es_v221", "r2_mnq", "nq_v221"]
    assert all(0 < r.size_mult <= 1.0 for r in recs)
    assert recs[0].verdict == "REDUCE" and recs[0].size_mult == 0.5
    assert recs[-1].size_mult == 0.25


def test_fingerprint_changes_with_inputs() -> None:
    assert _slot().fingerprint() == _slot().fingerprint()
    assert _slot().fingerprint() != _slot(fng_prior=10.0).fingerprint()


# ------------------------------------------------------------------ parsers
def test_parse_bls_ics() -> None:
    ics = (
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART;TZID=US-Eastern:20261014T083000\n"
        "SUMMARY:Consumer Price Index\nEND:VEVENT\nBEGIN:VEVENT\n"
        "DTSTART;TZID=US-Eastern:20261106T083000\nSUMMARY:Employment Situation\nEND:VEVENT\n"
        "BEGIN:VEVENT\nDTSTART;TZID=US-Eastern:20261015T083000\nSUMMARY:Real Earnings\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    ev = src.parse_bls_ics(ics)
    assert [(e.kind, e.ts.isoformat()) for e in ev] == [
        ("CPI", "2026-10-14T08:30:00-04:00"),
        ("NFP", "2026-11-06T08:30:00-05:00"),
    ]


def test_parse_fomc_skips_notation_votes_and_spans_months() -> None:
    html = (
        '<div class="panel-heading"><h4><a id="1">2026 FOMC Meetings</a></h4></div>'
        '<div class="fomc-meeting__month x"><strong>October</strong></div>'
        '<div class="fomc-meeting__date x">27-28</div>'
        '<div class="fomc-meeting__month x"><strong>April/May</strong></div>'
        '<div class="fomc-meeting__date x">30-1*</div>'
        '<div class="fomc-meeting__month x"><strong>August</strong></div>'
        '<div class="fomc-meeting__date x">22 (notation vote)</div>'
    )
    days = [e.ts for e in src.parse_fomc(html)]
    assert days == [
        datetime(2026, 10, 28, 14, 0, tzinfo=NY),
        datetime(2026, 5, 1, 14, 0, tzinfo=NY),
    ]


def test_parse_bea_rolls_year() -> None:
    row = (
        '<tr class="scheduled-releases-type-press"><td><div class="release-date">January 30</div>'
        "<small>8:30 AM</small></td><td>Personal Income and Outlays, December 2026</td></tr>"
    )
    ev = src.parse_bea_schedule("<table>" + row + "</table>", date(2026, 10, 9))
    assert [(e.kind, e.ts) for e in ev] == [("PCE", datetime(2027, 1, 30, 8, 30, tzinfo=NY))]


def test_parse_cnn_cboe_fred_rss() -> None:
    cnn = src.parse_cnn(
        {
            "fear_and_greed": {"score": 45, "timestamp": "2026-10-09T23:59:54+00:00"},
            "fear_and_greed_historical": {"data": [{"x": 1791547200000.0, "y": 38.2}]},
            "put_call_options": {"score": 30.5, "rating": "fear"},
        }
    )
    assert cnn.history[date(2026, 10, 9)] == 45.0
    assert cnn.components == {"put_call_options": (30.5, "fear")}
    vix = src.parse_cboe_csv("DATE,OPEN,HIGH,LOW,CLOSE\n10/09/2026,15.3,15.3,14.7,14.84\n")
    assert vix == {date(2026, 10, 9): (15.3, 15.3, 14.7, 14.84)}
    assert src.parse_fred_csv("observation_date,SP500\n2026-10-08,7765.36\n2026-10-09,.\n") == {
        date(2026, 10, 8): 7765.36
    }
    rss = (
        b"<rss><channel><item><title>Stocks rise - Reuters</title><link>https://x/1</link>"
        b"<pubDate>Fri, 09 Oct 2026 14:00:00 GMT</pubDate><source url='r'>Reuters</source>"
        b"</item></channel></rss>"
    )
    (h,) = src.parse_rss(rss, "google:stock market")
    assert h.title == "Stocks rise" and h.publisher == "Reuters"
    assert h.published_at == datetime(2026, 10, 9, 10, 0, tzinfo=NY)


def test_session_for_rolls_after_close_and_weekend() -> None:
    assert src.session_for(datetime(2026, 10, 9, 15, 0, tzinfo=NY)) == date(2026, 10, 9)
    assert src.session_for(datetime(2026, 10, 9, 17, 0, tzinfo=NY)) == date(2026, 10, 12)


def test_slot_label() -> None:
    assert slot_label(datetime(2026, 10, 9, 14, 47, tzinfo=NY)) == "14:30"


# ------------------------------------------------------------------ job on Postgres
ADMIN_DSN = os.environ.get("WAYSTONE_TEST_PG_ADMIN_DSN", "")
SQL_DIR = Path(__file__).parents[1] / "deploy" / "db" / "sql"
NOW = datetime(2026, 10, 9, 15, 10, tzinfo=UTC)  # 11:10 ET


class FakeFeeds:
    def __init__(self) -> None:
        self.calls: list[str] = []
        end = date(2026, 10, 9)
        days = []
        d = end
        while len(days) < 300:
            if d.weekday() < 5:
                days.append(d)
            d -= timedelta(days=1)
        self.days = list(reversed(days))

    def _walk(self, base: float, step: float) -> dict[date, float]:
        return {d: base + step * math.sin(i / 7) + i * step / 50 for i, d in enumerate(self.days)}

    def cboe_index(self, name: str) -> dict[date, src.OHLC]:
        base = {"VIX": 16.0, "VIX3M": 18.0, "VXN": 20.0, "VIX9D": 15.0}[name]
        return {d: (v, v + 0.5, v - 0.5, v) for d, v in self._walk(base, 1.0).items()}

    def cnn_fng(self, start: date) -> src.FngNow:
        hist = {d: 25.0 if d == date(2026, 10, 8) else 50.0 for d in self.days}
        return src.FngNow(hist, {"put_call_options": (40.0, "fear")})

    def fred(self, series_id: str, start: date) -> dict[date, float]:
        base = {"SP500": 7000.0, "NASDAQ100": 28000.0, "BAMLH0A0HYM2": 3.0, "DGS10": 4.5}[series_id]
        return self._walk(base, base / 100)

    def put_call(self, day: date) -> dict[str, float] | None:
        self.calls.append(f"pcr:{day}")
        return {"total": 0.9, "equity": 0.6, "index": 1.1}

    def cot(self, root: str, start: date) -> list[fx.CotRow]:
        return [
            fx.CotRow(date(2026, 1, 6) + timedelta(weeks=i), 1000, 300 + 5 * (i % 4), 300, 500, 100)
            for i in range(40)
        ]

    def daily_bars(self, symbols: Sequence[str], start: date) -> dict[str, dict[date, src.OHLC]]:
        out = {}
        for sym in symbols:
            out[sym] = dict.fromkeys(self.days, (100.0, 102.0, 99.0, 101.9))
        return out

    def intraday_bars(
        self, symbols: Sequence[str], period: str, interval: str
    ) -> dict[str, list[src.IntradayBar]]:
        out: dict[str, list[src.IntradayBar]] = {}
        for sym in symbols:
            bars = []
            for d in self.days[-30:]:
                for k in range(0, 24 * 60, 60):
                    ts = datetime.combine(d, time(0), tzinfo=NY) + timedelta(minutes=k)
                    px = 100 + (1 if k % 120 else -1)
                    bars.append((ts, px, px + 1, px - 1, px))
            out[sym] = bars
        return out

    def calendar(self) -> list[src.CalendarEvent]:
        return [
            src.CalendarEvent(
                "CPI", "Consumer Price Index", datetime(2026, 10, 9, 8, 30, tzinfo=NY), "bls.gov"
            ),
            src.CalendarEvent(
                "FOMC",
                "FOMC statement",
                datetime(2026, 10, 28, 14, 0, tzinfo=NY),
                "federalreserve.gov",
            ),
        ]

    def headlines(self) -> list[src.RawHeadline]:
        t = datetime(2026, 10, 9, 10, 40, tzinfo=NY)
        return [
            src.RawHeadline(
                "Stocks rally as inflation cools", "https://x/1", "Reuters", "google:x", t
            ),
            src.RawHeadline(
                "Stocks rally as inflation cools", "https://x/2", "Some Blog", "google:x", t
            ),
            src.RawHeadline(
                "Missile strikes rattle global markets, war fears",
                "https://x/3",
                "Reuters",
                "google:x",
                t + timedelta(minutes=5),
            ),
        ]


pg = pytest.mark.skipif(
    not ADMIN_DSN or shutil.which("psql") is None,
    reason="needs WAYSTONE_TEST_PG_ADMIN_DSN and psql",
)


@pytest.fixture
def db() -> Iterator[str]:
    name = f"waystone_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    params = conninfo.conninfo_to_dict(ADMIN_DSN)
    admin_db = conninfo.make_conninfo(**{**params, "dbname": name})  # type: ignore[arg-type]
    load_db = conninfo.make_conninfo(
        **{**params, "dbname": name, "user": "waystone_load", "password": "test-load"}  # type: ignore[arg-type]
    )

    def psql(dsn: str, path: Path, env: dict[str, str] | None = None) -> None:
        subprocess.run(
            ["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q", "-f", str(path)],
            check=True,
            env={**os.environ, **(env or {})},
            capture_output=True,
        )

    try:
        psql(
            admin_db,
            SQL_DIR / "01_roles.sql",
            {"WAYSTONE_DB": name, "WAYSTONE_LOAD_PW": "test-load", "WAYSTONE_READ_PW": "test-read"},
        )
        for path in sorted(SQL_DIR.glob("0[2-9]_*.sql")):
            psql(load_db, path)
        yield load_db
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _count(dsn: str, sql: str) -> int:
    with connect(dsn) as conn:
        row = conn.execute(sql).fetchone()
        assert row is not None
        return int(next(iter(row.values())))


@pg
def test_job_stores_every_score_gate_and_recommendation(db: str) -> None:
    feeds = FakeFeeds()
    with connect(db) as conn:
        daily = SentimentJob(conn, feeds, now=NOW, scorer="lexicon").run("daily", days=7)
        assert daily.status == "OK", daily.sources
        intraday = SentimentJob(conn, feeds, now=NOW, scorer="lexicon").run("intraday")
    assert daily.headlines_new == 3 and intraday.headlines_new == 0
    assert intraday.slots == ["2026-10-09 11:00"]
    assert _count(db, "SELECT count(*) FROM sentiment.snapshot WHERE slot_label = 'DAY'") == 6
    assert _count(db, "SELECT count(*) FROM sentiment.headline WHERE novelty = 0") == 1
    assert (
        _count(db, "SELECT count(*) FROM sentiment.gate_decision WHERE slot_label = '11:00'")
        == 3 * 6
    )
    layers = _count(
        db, "SELECT count(DISTINCT layer) FROM sentiment.score WHERE slot_label = '11:00'"
    )
    assert layers >= 5
    with connect(db) as conn:
        gates = {
            (r["strategy_code"], r["gate"]): r
            for r in conn.execute(
                "SELECT * FROM api.v_sentiment_gate WHERE session_date = '2026-10-09' "
                "AND slot_label = '11:00'"
            ).fetchall()
        }
        kill = gates[("nq_v221", "kill")]
        assert kill["state"] == "HALT" and kill["expires_at"] is not None
        assert gates[("nq_v221", "event")]["state"] == "OPEN"
        assert gates[("nq_v221", "engine")]["state"] == "BLOCKED"
        recs = conn.execute(
            "SELECT verdict FROM api.v_sentiment_recommendation WHERE slot_label = '11:00'"
        ).fetchall()
        assert {r["verdict"] for r in recs} == {"STAND_DOWN"}
        snap = conn.execute(
            "SELECT * FROM sentiment.snapshot WHERE slot_label = '11:00'"
        ).fetchone()
        assert snap is not None and snap["headline"].startswith("Stand down")
        day9 = conn.execute(
            "SELECT * FROM api.v_sentiment_gate WHERE session_date = '2026-10-09' "
            "AND slot_label = 'DAY' AND gate = 'event' AND strategy_code = 'es_v221'"
        ).fetchone()
        assert day9 is not None and day9["state"] == "CAUTION"
    pcr_calls = len(feeds.calls)
    with connect(db) as conn:
        SentimentJob(conn, feeds, now=NOW, scorer="lexicon").run("daily", days=7)
    assert len(feeds.calls) == pcr_calls, "put/call already stored is not fetched again"
    assert _count(db, "SELECT count(*) FROM sentiment.snapshot WHERE slot_label = 'DAY'") == 6
    read_dsn = db.replace("user=waystone_load", "user=waystone_read").replace(
        "password=test-load", "password=test-read"
    )
    reader = HqReader(read_dsn)
    days = reader.sentiment_range()
    assert days[0]["session_date"] == "2026-10-09" and days[0]["intervals"] == 1
    detail = reader.sentiment_day(date(2026, 10, 9), "nq_v221")
    assert len(detail["slots"]) == 2 and detail["headlines"] and detail["scores"]
    assert reader.sentiment_series("fng", "replica")
    assert reader.sentiment_health()["sources"]


@pg
def test_mcp_sentiment_tools(db: str) -> None:
    import asyncio
    import json

    from waystone3.hq.mcp_http import _token, build_hq_mcp

    with connect(db) as conn:
        SentimentJob(conn, FakeFeeds(), now=NOW, scorer="lexicon").run("daily", days=7)
        SentimentJob(conn, FakeFeeds(), now=NOW, scorer="lexicon").run("intraday")
    read_dsn = conninfo.make_conninfo(db, user="waystone_read", password="test-read")
    mcp = build_hq_mcp(HqReader(read_dsn), lambda t: "manoj" if t == "ok" else None)

    def call(name: str, args: dict[str, Any] | None = None) -> Any:
        result = asyncio.run(mcp.call_tool(name, args or {}))
        if isinstance(result, tuple):
            structured = result[1]
            return structured["result"] if set(structured) == {"result"} else structured
        return json.loads(result[0].text)  # type: ignore[index, union-attr]

    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {
        "hq_sentiment",
        "hq_sentiment_now",
        "hq_sentiment_day",
        "hq_sentiment_gates",
        "hq_sentiment_series",
        "hq_sentiment_quality",
    } <= names
    reset = _token.set("bogus")
    try:
        with pytest.raises(Exception, match="invalid or missing"):
            asyncio.run(mcp.call_tool("hq_sentiment_now", {}))
    finally:
        _token.reset(reset)
    reset = _token.set("ok")
    try:
        now = call("hq_sentiment_now")
        assert now["snapshot"]["slot_label"] == "11:00"
        assert [r["rank"] for r in now["recommendations"]] == [1, 2, 3]
        assert len(now["gates"]) == 18
        halts = call("hq_sentiment_gates", {"gate": "kill", "state": "HALT"})
        assert halts and all(g["gate"] == "kill" for g in halts)
        assert (
            call("hq_sentiment", {"start": "2026-10-05"})["days"][-1]["session_date"]
            == "2026-10-05"
        )
        day = call("hq_sentiment_day", {"date": "2026-10-09", "strategy": "r2_mnq"})
        assert {r["strategy_code"] for r in day["recommendations"]} == {"r2_mnq"}
        assert call("hq_sentiment_series", {"layer": "flow", "component": "vix"})
        assert "efficacy" in call("hq_sentiment_quality")
        with pytest.raises(Exception, match="unknown strategy"):
            asyncio.run(mcp.call_tool("hq_sentiment_gates", {"strategy": "nope"}))
    finally:
        _token.reset(reset)
