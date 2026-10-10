"""``waystone3 sentiment --job intraday|daily|backfill``: free feeds -> scores -> gates -> DB.

  intraday  every 30 min in the session: headlines, live VIX / futures bars, one interval
            (slot ``HH:MM`` ET) for today
  daily     pre-market and after the close: calendar, daily history, COT, the ``DAY`` slot for
            the last ``days`` sessions (today's stays provisional until after 16:00 ET),
            gate efficacy and drift
  backfill  the ``DAY`` slot for the last ``days`` sessions (default 365), no headlines before
            the feeds' own history

Every score lands in ``sentiment.score`` (one row per layer/component per slot), every gate
in ``sentiment.gate_decision``, every strategy verdict in ``sentiment.recommendation``.
Each slot is written in its own transaction, so a re-run replaces it exactly.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from functools import partial
from typing import Any, TypeVar

from psycopg.types.json import Jsonb

from waystone3.hq.calendar import NY, trading_days
from waystone3.hq.db import Conn, Cursor, upsert
from waystone3.hq.refdata import load_ref
from waystone3.hq.sentiment import features as fx
from waystone3.hq.sentiment import nlp
from waystone3.hq.sentiment.policy import (
    POLICY_VERSION,
    Event,
    Fit,
    KillHeadline,
    Override,
    SlotInputs,
    StrategyRef,
    policy_config,
    recommend,
    regime_key,
)
from waystone3.hq.sentiment.sources import (
    COT_MARKETS,
    ETFS,
    FUTURES,
    FeedSource,
    IntradayBar,
    RawHeadline,
    session_for,
)
from waystone3.hq.sentiment.summary import headline as summary_headline
from waystone3.hq.sentiment.summary import summary as summary_text

JOBS = ("intraday", "daily", "backfill")
DEFAULT_DAYS = {"intraday": 0, "daily": 7, "backfill": 365}
HISTORY_DAYS = 420
CLOSE = time(16, 0)
ONE_WAY_ETF = {"ES": "SPY", "NQ": "QQQ", "RTY": "IWM"}
T = TypeVar("T")


@dataclass
class SentimentReport:
    run_id: int
    job: str
    status: str = "RUNNING"
    scorer: str = "lexicon"
    slots: list[str] = field(default_factory=list)
    headlines_new: int = 0
    gaps: list[str] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)


def slot_label(ts: datetime) -> str:
    t = ts.astimezone(NY)
    return f"{t.hour:02d}:{t.minute // 30 * 30:02d}"


class SentimentJob:
    def __init__(
        self,
        conn: Conn,
        feeds: FeedSource,
        *,
        now: datetime | None = None,
        scorer: str = "auto",
        score_fn: nlp.Scorer | None = None,
    ) -> None:
        self.conn = conn
        self.feeds = feeds
        self.now = (now or datetime.now(UTC)).astimezone(NY)
        self.scorer_kind = scorer
        self._score_fn = score_fn
        self.market = fx.Market()
        self.fng_components: dict[str, tuple[float, str]] = {}
        self.cot: dict[str, list[fx.CotRow]] = {}
        self.bars5: dict[str, list[IntradayBar]] = {}
        self.bars60: dict[str, list[IntradayBar]] = {}
        self.vix_now: float | None = None
        self.pnl: dict[str, dict[date, float]] = {}
        self.regimes: dict[date, str] = {}
        self.calendar_ok_at: datetime | None = None
        self.headlines_ok_at: datetime | None = None

    # ------------------------------------------------------------------ run
    def run(self, job: str, days: int | None = None) -> SentimentReport:
        if job not in JOBS:
            raise ValueError(f"job must be one of {JOBS}")
        span = DEFAULT_DAYS[job] if days is None else days
        ref = load_ref(self.conn)
        strategies = [
            StrategyRef(s.code, s.instrument_root)
            for s in ref.strategies.values()
            if s.asset_class == "future"
        ]
        self.strategy_ids = {s.code: s.strategy_id for s in ref.strategies.values()}
        today = self.now.date()
        start = today - timedelta(days=span)
        sessions = list(trading_days(start, today, ref.holidays))
        with self.conn.cursor() as cur:
            row = upsert(
                cur,
                "sentiment.run",
                {
                    "job": job,
                    "policy_version": POLICY_VERSION,
                    "config": Jsonb(policy_config()),
                    "sessions": sessions,
                },
                returning="run_id",
            )
        assert row is not None
        self.conn.commit()
        rep = SentimentReport(run_id=row["run_id"], job=job)
        try:
            self._refresh(rep, job, sessions)
            self._load_history()
            if job == "intraday":
                if today in sessions and self.now.time() >= time(7, 0):
                    self._write_slot(rep, today, slot_label(self.now), self.now, strategies)
            else:
                for d in sessions:
                    at = datetime.combine(d, CLOSE, tzinfo=NY)
                    self._write_slot(rep, d, "DAY", min(at, self.now), strategies)
                if sessions:
                    self._write_efficacy_and_drift(rep, sessions[-1])
        except Exception as exc:
            self.conn.rollback()
            rep.status = "FAILED"
            self._finish(rep, f"{type(exc).__name__}: {exc}")
            raise
        rep.status = "PARTIAL" if rep.gaps else "OK"
        self._finish(rep, None)
        return rep

    def _finish(self, rep: SentimentReport, error: str | None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE sentiment.run SET finished_at = now(), status = %s, scorer = %s, "
                "sources = %s, error = %s WHERE run_id = %s",
                (
                    rep.status,
                    rep.scorer,
                    Jsonb(rep.sources),
                    error or ("; ".join(rep.gaps) or None),
                    rep.run_id,
                ),
            )
        self.conn.commit()

    # ------------------------------------------------------------------ sources
    def _try(
        self, rep: SentimentReport, name: str, fn: Callable[[], T], rows: Callable[[T], int]
    ) -> T | None:
        try:
            out = fn()
        except Exception as exc:
            msg = f"{type(exc).__name__}: {str(exc)[:200]}"
            rep.sources[name] = f"ERROR {msg}"
            rep.gaps.append(name)
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sentiment.source_health (source, last_error_at, last_error) "
                    "VALUES (%s, now(), %s) ON CONFLICT (source) DO UPDATE SET "
                    "last_error_at = now(), last_error = EXCLUDED.last_error",
                    (name, msg),
                )
            self.conn.commit()
            return None
        n = rows(out)
        rep.sources[name] = f"OK {n}"
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sentiment.source_health (source, last_ok_at, last_rows) "
                "VALUES (%s, now(), %s) ON CONFLICT (source) DO UPDATE SET "
                "last_ok_at = now(), last_rows = EXCLUDED.last_rows",
                (name, n),
            )
        self.conn.commit()
        return out

    def _refresh(self, rep: SentimentReport, job: str, sessions: Sequence[date]) -> None:
        today = self.now.date()
        hist_start = (sessions[0] if sessions else today) - timedelta(days=HISTORY_DAYS)
        m = self.market
        cal = self._try(rep, "calendar", self.feeds.calendar, len)
        if cal:
            with self.conn.cursor() as cur:
                for e in cal:
                    upsert(
                        cur,
                        "sentiment.macro_event",
                        {
                            "kind": e.kind,
                            "title": e.title,
                            "event_ts": e.ts,
                            "session_date": e.ts.astimezone(NY).date(),
                            "source": e.source,
                            "fetched_at": self.now,
                        },
                        ("kind", "event_ts"),
                    )
            self.conn.commit()
        for name, target in (
            ("VIX", m.vix),
            ("VIX3M", m.vix3m),
            ("VXN", m.vxn),
            ("VIX9D", m.vix9d),
        ):
            got = self._try(rep, f"cboe:{name}", partial(self.feeds.cboe_index, name), len)
            if got:
                target.update({d: v[3] for d, v in got.items()})
        fng = self._try(
            rep, "cnn:fear_greed", lambda: self.feeds.cnn_fng(hist_start), lambda f: len(f.history)
        )
        if fng:
            m.fng_cnn.update(fng.history)
            self.fng_components = fng.components
        for sid, target in (
            ("SP500", m.spx),
            ("NASDAQ100", m.ndx),
            ("BAMLH0A0HYM2", m.hy_oas),
            ("DGS10", m.ust10y),
        ):
            got2 = self._try(rep, f"fred:{sid}", partial(self.feeds.fred, sid, hist_start), len)
            if got2:
                target.update(got2)
        etf = self._try(
            rep,
            "yahoo:etf_daily",
            lambda: self.feeds.daily_bars(list(ETFS.values()), hist_start),
            lambda x: sum(len(v) for v in x.values()),
        )
        if etf:
            m.ohlc.update(etf)
            m.rsp.update({d: v[3] for d, v in etf.get("RSP", {}).items()})
            m.spy.update({d: v[3] for d, v in etf.get("SPY", {}).items()})
        for root in COT_MARKETS:
            got3 = self._try(
                rep,
                f"cftc:{root}",
                partial(self.feeds.cot, root, hist_start - timedelta(days=1100)),
                len,
            )
            if got3:
                self.cot[root] = got3
        b5 = self._try(
            rep,
            "yahoo:futures_5m",
            lambda: self.feeds.intraday_bars([*FUTURES.values(), "^VIX"], "60d", "5m"),
            lambda x: sum(len(v) for v in x.values()),
        )
        if b5:
            self.bars5 = b5
            vix_bars = [b for b in b5.get("^VIX", []) if b[0].date() == today]
            self.vix_now = vix_bars[-1][4] if vix_bars else None
        if job == "backfill":
            b60 = self._try(
                rep,
                "yahoo:futures_1h",
                lambda: self.feeds.intraday_bars(list(FUTURES.values()), "730d", "1h"),
                lambda x: sum(len(v) for v in x.values()),
            )
            if b60:
                self.bars60 = b60
        self._refresh_put_call(rep, sessions)
        if job != "backfill":
            raw = self._try(rep, "headlines", self.feeds.headlines, len)
            if raw is not None:
                rep.headlines_new = self._store_headlines(rep, raw)
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT source, last_ok_at FROM sentiment.source_health "
                "WHERE source IN ('calendar', 'headlines')"
            )
            ok = {r["source"]: r["last_ok_at"] for r in cur.fetchall()}
        self.calendar_ok_at = ok.get("calendar")
        self.headlines_ok_at = ok.get("headlines")

    def _refresh_put_call(self, rep: SentimentReport, sessions: Sequence[date]) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT session_date, component, value FROM sentiment.score "
                "WHERE layer = 'flow' AND slot_label = 'DAY' AND component LIKE 'put_call_%%'"
            )
            for r in cur.fetchall():
                target = {
                    "put_call_total": self.market.pcr_total,
                    "put_call_equity": self.market.pcr_equity,
                }.get(r["component"])
                if target is not None and r["value"] is not None:
                    target[r["session_date"]] = float(r["value"])
        if not sessions:
            return
        wanted = [
            d
            for d in trading_days(sessions[0] - timedelta(days=10), sessions[-1], set())
            if d not in self.market.pcr_total and d <= self.now.date()
        ]
        fetched: dict[date, dict[str, float]] = {}

        def pull() -> dict[date, dict[str, float]]:
            for d in wanted:
                got = self.feeds.put_call(d)
                if got:
                    fetched[d] = got
            return fetched

        self._try(rep, "cboe:put_call", pull, len)
        with self.conn.cursor() as cur:
            for d, vals in fetched.items():
                for k, v in vals.items():
                    self._score(
                        cur,
                        d,
                        "DAY",
                        "flow",
                        f"put_call_{k}",
                        v,
                        None,
                        None,
                        "cboe",
                        {},
                        rep.run_id,
                    )
                if "total" in vals:
                    self.market.pcr_total[d] = vals["total"]
                if "equity" in vals:
                    self.market.pcr_equity[d] = vals["equity"]
        self.conn.commit()

    def _scorer(self, rep: SentimentReport) -> nlp.Scorer:
        if self._score_fn is None:
            rep.scorer, self._score_fn = nlp.make_scorer(self.scorer_kind)
        return self._score_fn

    def _store_headlines(self, rep: SentimentReport, raw: Sequence[RawHeadline]) -> int:
        seen: dict[str, RawHeadline] = {}
        for h in raw:
            title = nlp.normalize(h.title)
            if title and h.published_at >= self.now - timedelta(days=3):
                seen.setdefault(
                    h.url_hash, RawHeadline(title, h.url, h.publisher, h.feed, h.published_at)
                )
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT url_hash FROM sentiment.headline WHERE url_hash = ANY(%s)", (list(seen),)
            )
            known = {r["url_hash"] for r in cur.fetchall()}
            cur.execute(
                "SELECT title FROM sentiment.headline WHERE published_at >= %s "
                "ORDER BY published_at",
                (self.now - timedelta(days=5),),
            )
            index = nlp.NoveltyIndex.of(r["title"] for r in cur.fetchall())
        fresh = sorted((h for k, h in seen.items() if k not in known), key=lambda h: h.published_at)
        if not fresh:
            if self._score_fn is None:
                rep.scorer = "none (no new headlines)"
            return 0
        scored = self._scorer(rep)([h.title for h in fresh])
        with self.conn.cursor() as cur:
            for h, s in zip(fresh, scored, strict=True):
                upsert(
                    cur,
                    "sentiment.headline",
                    {
                        "url_hash": h.url_hash,
                        "url": h.url,
                        "title": h.title,
                        "publisher": h.publisher,
                        "feed": h.feed,
                        "tier": nlp.source_tier(h.publisher, h.feed),
                        "published_at": h.published_at,
                        "session_date": session_for(h.published_at),
                        "scorer": rep.scorer,
                        "prob_pos": s.prob_pos,
                        "prob_neg": s.prob_neg,
                        "prob_neu": s.prob_neu,
                        "score": s.score,
                        "novelty": index.novelty(h.title),
                        "kill_terms": nlp.kill_terms(h.title),
                        "macro_tags": nlp.macro_tags(h.title),
                    },
                    ("url_hash",),
                    update=False,
                )
        self.conn.commit()
        return len(fresh)

    def _load_history(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT s.strategy_code, p.session_date, p.net_pnl FROM core.daily_pnl p "
                "JOIN ref.strategy s USING (strategy_id)"
            )
            for r in cur.fetchall():
                self.pnl.setdefault(r["strategy_code"], {})[r["session_date"]] = float(r["net_pnl"])
            cur.execute(
                "SELECT s.strategy_code, d.session_date, d.bt_net_pnl FROM core.daily_sync d "
                "JOIN ref.strategy s USING (strategy_id) WHERE d.bt_net_pnl IS NOT NULL"
            )
            for r in cur.fetchall():
                self.pnl.setdefault(r["strategy_code"], {}).setdefault(
                    r["session_date"], float(r["bt_net_pnl"])
                )
            cur.execute(
                "SELECT session_date, regime->>'key' AS k FROM sentiment.snapshot "
                "WHERE slot_label = 'DAY' AND regime ? 'key'"
            )
            self.regimes = {r["session_date"]: r["k"] for r in cur.fetchall()}

    # ------------------------------------------------------------------ one slot
    def _chop(self, root: str, d: date) -> tuple[float | None, str]:
        sym = FUTURES[root]
        bars5 = [(b[0], b[1], b[4]) for b in self.bars5.get(sym, [])]
        v = fx.chop_ratio(bars5, d)
        if v is not None:
            return v, "5m"
        bars60 = [(b[0], b[1], b[4]) for b in self.bars60.get(sym, [])]
        v = fx.chop_ratio(bars60, d)
        return (v, "1h") if v is not None else (None, "none")

    def _one_way_today(self, root: str, d: date, at: datetime) -> fx.OneWay | None:
        bars = [
            b
            for b in self.bars5.get(FUTURES[root], [])
            if b[0].date() == d and time(9, 30) <= b[0].time() < CLOSE and b[0] <= at
        ]
        if not bars:
            return None
        return fx.one_way(bars[0][1], max(b[2] for b in bars), min(b[3] for b in bars), bars[-1][4])

    def _headlines(self, at: datetime) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT title, publisher, feed, tier, published_at, score, novelty, kill_terms, "
                "macro_tags "
                "FROM sentiment.headline WHERE published_at > %s AND published_at <= %s",
                (at - timedelta(hours=24), at),
            )
            return list(cur.fetchall())

    def _fit(self, code: str, d: date, regime: str) -> Fit:
        hist = {k: v for k, v in self.pnl.get(code, {}).items() if k < d}
        same = [v for k, v in hist.items() if self.regimes.get(k) == regime]
        return Fit(
            regime,
            len(same),
            sum(same) / len(same) if same else None,
            len(hist),
            sum(hist.values()) / len(hist) if hist else None,
        )

    def _write_slot(
        self,
        rep: SentimentReport,
        d: date,
        label: str,
        at: datetime,
        strategies: Sequence[StrategyRef],
    ) -> None:
        m = self.market
        live = d == self.now.date()
        intraday = label != "DAY"
        gaps: list[str] = []
        fng_prior = fx.last(m.fng_cnn, d, strict=True, max_age_days=5)
        fng_src = "cnn"
        replica_prior, _ = fx.fng_replica(m, d - timedelta(days=1))
        if fng_prior is None and replica_prior is not None:
            fng_prior, fng_src = replica_prior, "replica"
        fng_keys = [k for k in sorted(m.fng_cnn) if k < d]
        fng_as_of = fng_keys[-1] if fng_keys else None
        fng_cnn = fx.last(m.fng_cnn, d, max_age_days=4)
        replica, parts = fx.fng_replica(m, d)
        if replica is None:
            gaps.append("F&G replica (<4 components)")
        vol = fx.vol_state(m, d, self.vix_now if (intraday and live) else None)
        vix_keys = [k for k in sorted(m.vix) if k <= d]
        vix_as_of = (
            d
            if (intraday and live and self.vix_now is not None)
            else (vix_keys[-1] if vix_keys else None)
        )
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT kind, title, event_ts FROM sentiment.macro_event WHERE session_date = %s "
                "ORDER BY event_ts",
                (d,),
            )
            events = [
                Event(r["kind"], r["title"], r["event_ts"].astimezone(NY)) for r in cur.fetchall()
            ]
            cur.execute(
                "SELECT count(*) AS n FROM sentiment.macro_event "
                "WHERE session_date BETWEEN %s AND %s",
                (d - timedelta(days=40), d + timedelta(days=40)),
            )
            covered = (cur.fetchone() or {"n": 0})["n"] > 0
            cur.execute(
                "SELECT s.strategy_code, o.gate, o.action, o.reason, o.created_by, o.valid_to "
                "FROM sentiment.gate_override o LEFT JOIN ref.strategy s USING (strategy_id) "
                "WHERE o.revoked_at IS NULL AND o.valid_from <= %s "
                "AND (o.valid_to IS NULL OR o.valid_to >= %s)",
                (at, at),
            )
            overrides = [
                Override(
                    r["gate"],
                    r["action"],
                    r["reason"],
                    r["created_by"],
                    r["strategy_code"],
                    r["valid_to"],
                )
                for r in cur.fetchall()
            ]
        fresh_cal = self.calendar_ok_at is not None and self.now - self.calendar_ok_at <= timedelta(
            days=7
        )
        calendar_ok = covered and (fresh_cal or not live)
        rows = self._headlines(at)
        items = [
            fx.Item(
                r["published_at"].astimezone(NY),
                float(r["score"]),
                float(r["tier"]),
                float(r["novelty"]),
                bool(r["kill_terms"]),
            )
            for r in rows
        ]
        narrative = fx.aggregate(items, at)
        kills = [
            KillHeadline(
                r["title"],
                tuple(r["kill_terms"]),
                r["published_at"].astimezone(NY),
                float(r["tier"]),
                float(r["score"]),
                r["publisher"] or r["feed"],
            )
            for r in rows
            if r["kill_terms"] and nlp.market_relevant(r["title"])
        ]
        chop: dict[str, float | None] = {}
        chop_src: dict[str, str] = {}
        for root in FUTURES:
            chop[root], chop_src[root] = self._chop(root, d)
        pos = {root: fx.positioning(self.cot.get(root, []), d) for root in COT_MARKETS}
        one_way_prior: dict[str, fx.OneWay | None] = {}
        for root, etf in ONE_WAY_ETF.items():
            bars = m.ohlc.get(etf, {})
            prev = [k for k in sorted(bars) if k < d]
            one_way_prior[root] = fx.one_way(*bars[prev[-1]]) if prev else None
        s = SlotInputs(
            d,
            label,
            at,
            fng_prior,
            vol,
            events,
            calendar_ok,
            narrative,
            kills,
            headlines_as_of=self.headlines_ok_at.astimezone(NY)
            if (self.headlines_ok_at and live)
            else None,
            vix_as_of=vix_as_of,
            fng_as_of=fng_as_of,
            chop=chop,
            positioning=pos,
            one_way_prior=one_way_prior,
        )
        regime = regime_key(fng_prior, vol)
        fits = {st.code: self._fit(st.code, d, regime) for st in strategies}
        recs = recommend(s, strategies, fits, overrides)
        digest = s.fingerprint()
        if vol.vix is None:
            gaps.append("VIX")
        if not calendar_ok:
            gaps.append("macro calendar")
        if intraday and not rows:
            gaps.append("headlines")
        run_id = rep.run_id
        with self.conn.cursor() as cur:
            for table in ("sentiment.score", "sentiment.gate_decision", "sentiment.recommendation"):
                where = "session_date = %s AND slot_label = %s"
                extra = (
                    " AND NOT (layer = 'flow' AND component LIKE 'put_call_%%')"
                    if table == "sentiment.score"
                    else ""
                )
                cur.execute(f"DELETE FROM {table} WHERE {where}{extra}", (d, label))
            sc = self._score
            sc(
                cur,
                d,
                label,
                "fng",
                "cnn",
                fng_cnn,
                fng_cnn,
                fx.fng_state(fng_cnn),
                "cnn",
                {},
                run_id,
            )
            sc(
                cur,
                d,
                label,
                "fng",
                "prior_day",
                fng_prior,
                fng_prior,
                fx.fng_state(fng_prior),
                fng_src,
                {"as_of": fng_as_of.isoformat() if fng_as_of else None},
                run_id,
            )
            sc(
                cur,
                d,
                label,
                "fng",
                "replica",
                replica,
                replica,
                fx.fng_state(replica),
                "replica",
                {"components": len(parts)},
                run_id,
            )
            for p in parts:
                sc(
                    cur,
                    d,
                    label,
                    "fng",
                    f"replica:{p.name}",
                    p.raw,
                    p.score,
                    fx.fng_state(p.score),
                    "replica",
                    {},
                    run_id,
                )
            if live:
                for name, (score, rating) in self.fng_components.items():
                    sc(cur, d, label, "fng", f"cnn:{name}", score, score, rating, "cnn", {}, run_id)
            for comp, val in (
                ("vix", vol.vix),
                ("vix3m", vol.vix3m),
                ("vxn", vol.vxn),
                ("vix9d", fx.last(m.vix9d, d, max_age_days=4)),
                ("vix_5d_mean", vol.vix_5d_mean),
                ("vix_term_ratio", vol.term_ratio),
            ):
                sc(cur, d, label, "flow", comp, val, None, None, "cboe", {}, run_id)
            sc(
                cur,
                d,
                label,
                "regime",
                "vol_state",
                None,
                None,
                vol.label,
                "cboe",
                {"spike": vol.spike, "backwardation": vol.backwardation},
                run_id,
            )
            sc(cur, d, label, "regime", "regime_key", None, None, regime, "policy", {}, run_id)
            for root in FUTURES:
                c = chop[root]
                sc(
                    cur,
                    d,
                    label,
                    "regime",
                    f"chop:{root}",
                    c,
                    None,
                    None if c is None else ("choppy" if c <= fx.CHOP_THRESHOLD else "trending"),
                    f"yahoo:{chop_src[root]}",
                    {"threshold": fx.CHOP_THRESHOLD},
                    run_id,
                )
                ow = one_way_prior.get(root)
                if ow is not None:
                    sc(
                        cur,
                        d,
                        label,
                        "regime",
                        f"one_way_prior:{root}",
                        ow.body_ratio,
                        None,
                        f"{'one-way' if ow.one_way else 'two-way'} {ow.direction}",
                        f"yahoo:{ONE_WAY_ETF[root]}",
                        {},
                        run_id,
                    )
                if intraday:
                    today_ow = self._one_way_today(root, d, at)
                    if today_ow is not None:
                        sc(
                            cur,
                            d,
                            label,
                            "regime",
                            f"one_way_today:{root}",
                            today_ow.body_ratio,
                            None,
                            f"{'one-way' if today_ow.one_way else 'two-way'} {today_ow.direction}",
                            f"yahoo:{FUTURES[root]}",
                            {},
                            run_id,
                        )
            for root, p_ in pos.items():
                if p_ is not None:
                    sc(
                        cur,
                        d,
                        label,
                        "positioning",
                        f"cot_lev:{root}",
                        p_.lev_net_pct,
                        p_.lev_z,
                        p_.crowding,
                        "cftc",
                        {"report_date": p_.report_date.isoformat()},
                        run_id,
                    )
                    sc(
                        cur,
                        d,
                        label,
                        "positioning",
                        f"cot_am:{root}",
                        p_.am_net_pct,
                        p_.am_z,
                        None,
                        "cftc",
                        {"report_date": p_.report_date.isoformat()},
                        run_id,
                    )
            sc(
                cur,
                d,
                label,
                "narrative",
                "all",
                narrative.score,
                narrative.score,
                None,
                "headlines",
                {
                    "n": narrative.n,
                    "effective_n": narrative.effective_n,
                    "dispersion": narrative.dispersion,
                    "kill_hits": narrative.kill_hits,
                },
                run_id,
            )
            for tag in nlp.MACRO_TAGS:
                sub = [
                    it for it, r in zip(items, rows, strict=True) if tag in (r["macro_tags"] or [])
                ]
                if sub:
                    n_ = fx.aggregate(sub, at)
                    sc(
                        cur,
                        d,
                        label,
                        "narrative",
                        f"topic:{tag}",
                        n_.score,
                        n_.score,
                        None,
                        "headlines",
                        {"n": n_.n, "dispersion": n_.dispersion},
                        run_id,
                    )
            for r in recs:
                sid = self.strategy_ids[r.strategy_code]
                upsert(
                    cur,
                    "sentiment.recommendation",
                    {
                        "session_date": d,
                        "slot_label": label,
                        "strategy_id": sid,
                        "verdict": r.verdict,
                        "size_mult": r.size_mult,
                        "rank": r.rank,
                        "fit_score": r.fit.score,
                        "fit_n": r.fit.n,
                        "fit_regime": r.fit.regime,
                        "gate_data": r.gate("data").text,
                        "gate_event": r.gate("event").text,
                        "gate_kill": r.gate("kill").text,
                        "gate_engine": r.gate("engine").text,
                        "gate_vol": r.gate("vol").text,
                        "positioning": r.gate("positioning").text,
                        "reasons": list(r.reasons),
                        "policy_version": POLICY_VERSION,
                        "inputs_hash": digest,
                        "run_id": run_id,
                    },
                    ("session_date", "slot_label", "strategy_id"),
                )
                for g in r.gates:
                    upsert(
                        cur,
                        "sentiment.gate_decision",
                        {
                            "session_date": d,
                            "slot_label": label,
                            "strategy_id": sid,
                            "gate": g.name,
                            "state": g.state,
                            "reason": g.reason,
                            "size_mult": g.size_mult,
                            "confidence": g.confidence,
                            "evidence": list(g.evidence),
                            "inputs_as_of": g.as_of,
                            "expires_at": g.expires_at,
                            "override": g.override,
                            "policy_version": POLICY_VERSION,
                            "inputs_hash": digest,
                            "run_id": run_id,
                        },
                        ("session_date", "slot_label", "strategy_id", "gate"),
                    )
            best = next((r.strategy_code for r in recs if r.verdict != "STAND_DOWN"), None)
            upsert(
                cur,
                "sentiment.snapshot",
                {
                    "session_date": d,
                    "slot_label": label,
                    "slot_ts": at,
                    "is_final": (not intraday)
                    and (d < self.now.date() or self.now.time() >= time(17, 0)),
                    "fng_cnn": fng_cnn,
                    "fng_replica": replica,
                    "fng_prior_day": fng_prior,
                    "vix": vol.vix,
                    "vix_term_ratio": vol.term_ratio,
                    "vol_spike": vol.spike,
                    "narrative_score": narrative.score,
                    "narrative_dispersion": narrative.dispersion,
                    "narrative_n": narrative.n,
                    "kill_hits": narrative.kill_hits,
                    "events": Jsonb(
                        [{"kind": e.kind, "title": e.title, "ts": e.ts.isoformat()} for e in events]
                    ),
                    "regime": Jsonb(
                        {
                            "key": regime,
                            "vol": vol.label,
                            "fng_source": fng_src,
                            "chop": chop,
                            "one_way_prior": {
                                k: asdict(v) if v else None for k, v in one_way_prior.items()
                            },
                        }
                    ),
                    "data_gaps": gaps,
                    "best_strategy": best,
                    "policy_version": POLICY_VERSION,
                    "inputs_hash": digest,
                    "headline": summary_headline(s, recs),
                    "summary": summary_text(
                        s, recs, fng_cnn=fng_cnn, fng_replica=replica, gaps=gaps
                    ),
                    "run_id": run_id,
                },
                ("session_date", "slot_label"),
            )
        self.conn.commit()
        if not intraday:
            self.regimes[d] = regime
        rep.slots.append(f"{d.isoformat()} {label}")

    @staticmethod
    def _score(
        cur: Cursor,
        d: date,
        label: str,
        layer: str,
        component: str,
        value: float | None,
        score: float | None,
        state: str | None,
        source: str,
        detail: dict[str, Any],
        run_id: int,
    ) -> None:
        upsert(
            cur,
            "sentiment.score",
            {
                "session_date": d,
                "slot_label": label,
                "layer": layer,
                "component": component,
                "value": None if value is None else round(float(value), 6),
                "score": None if score is None else round(float(score), 4),
                "state": state,
                "source": source,
                "detail": Jsonb(json.loads(json.dumps(detail, default=str))),
                "run_id": run_id,
            },
            ("session_date", "slot_label", "layer", "component"),
        )

    # ------------------------------------------------------------------ efficacy / drift
    def _write_efficacy_and_drift(self, rep: SentimentReport, d: date) -> None:
        """Per gate and strategy: average net P&L on sessions the gate fired vs stayed open.
        ``score`` is open − fired (positive: the gate avoided worse sessions)."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT s.strategy_code, g.session_date, g.gate, g.state "
                "FROM sentiment.gate_decision g JOIN ref.strategy s USING (strategy_id) "
                "WHERE g.slot_label = 'DAY' AND g.session_date <= %s",
                (d,),
            )
            stats: dict[tuple[str, str], dict[str, list[float]]] = {}
            for r in cur.fetchall():
                pnl = self.pnl.get(r["strategy_code"], {}).get(r["session_date"])
                if pnl is None or r["state"] == "UNKNOWN":
                    continue
                side = "open" if r["state"] == "OPEN" else "fired"
                stats.setdefault((r["gate"], r["strategy_code"]), {"open": [], "fired": []})[
                    side
                ].append(pnl)
            for (gate, code), v in sorted(stats.items()):
                fo, op = v["fired"], v["open"]
                mf = sum(fo) / len(fo) if fo else None
                mo = sum(op) / len(op) if op else None
                edge = None if mf is None or mo is None else mo - mf
                self._score(
                    cur,
                    d,
                    "DAY",
                    "efficacy",
                    f"{gate}:{code}",
                    mf,
                    edge,
                    "insufficient"
                    if len(fo) < 5 or len(op) < 5
                    else ("helps" if (edge or 0) > 0 else "hurts"),
                    "policy",
                    {"n_fired": len(fo), "n_open": len(op), "mean_open": mo},
                    rep.run_id,
                )
            for component, column in (
                ("narrative", "narrative_score"),
                ("fng_replica", "fng_replica"),
            ):
                cur.execute(
                    f"SELECT {column} AS v FROM sentiment.snapshot WHERE slot_label = 'DAY' "
                    f"AND {column} IS NOT NULL AND session_date <= %s "
                    "ORDER BY session_date DESC LIMIT 140",
                    (d,),
                )
                vals = [float(r["v"]) for r in cur.fetchall()][::-1]
                p = fx.psi(vals[:-20], vals[-20:]) if len(vals) > 40 else None
                self._score(
                    cur,
                    d,
                    "DAY",
                    "drift",
                    f"psi:{component}",
                    p,
                    p,
                    None if p is None else ("ALERT" if p >= fx.PSI_ALERT else "stable"),
                    "policy",
                    {"recent": 20, "baseline": max(0, len(vals) - 20)},
                    rep.run_id,
                )
        self.conn.commit()
