#!/usr/bin/env python3
"""Score news headlines/snippets -> per-name DAILY sentiment features.

Scorer modes (--scorer):
  massive   Use Massive/Polygon LLM `insights[].sentiment` when present (positive/neutral/negative -> numeric).
            Rows without `massive_sentiment` are skipped.  Default for backtests once polygon-news has backfilled.
  auto      Prefer `massive_sentiment` per row; fall back to FinBERT/lexicon on title+text for the rest.
  finbert   FinBERT only (ProsusAI/finbert via transformers, CPU is fine at ~20 items/s).
  lexicon   Embedded finance lexicon (Loughran-McDonald-style word lists with negation handling).

Daily aggregation per symbol (data/sentiment/<SYM>_daily.csv):
  date, score (mean), count, pos_share, neg_share,
  shock_z   = (score - mean of the previous 20 days with news) / std of those days     — "tone shock"
  count_z   = (count - mean count previous 20 days) / std                                — "attention shock"
  score_src, shock_src_z  — when Massive LLM labels exist, parallel shock on Massive's own sentiment
Only items with ts <= 16:00 ET count for that date; later items roll to the next session.

  python ml/sentiment/finbert_score.py --sp500 --scorer massive --source-filter massive
  python ml/sentiment/finbert_score.py --symbols AAPL NVDA --scorer auto
  python ml/sentiment/finbert_score.py --synthetic --symbols S00 S01 --days 300
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from wsbt.data import DATA_DIR, _safe_name, load_symbol_list  # noqa: E402

ET = "America/New_York"

POS = set("""achieve achieved achievement achieves advance advances advantage beat beats benefit benefits best better boost boosted
boosts breakthrough buyback confident deliver delivered delivers demand efficiency efficient enhance enhanced excellent exceed exceeded
exceeds expand expanded expanding expansion favorable gain gained gains grew grow growing growth high higher highest improve improved
improvement improves improving increase increased increases innovation innovative leading milestone momentum opportunity optimistic
outperform outperformed outperforms positive profit profitability profitable progress raise raised raises rally rallied rebound record
recover recovered recovery resilient reward robust solid strength strengthen strong stronger success successful surge surged surpass
surpassed tailwind top upbeat upgrade upgraded upgrades upside win wins won""".split())
NEG = set("""abandon accident adverse against alarm allegation allegations bankrupt bankruptcy breach challenge challenges challenging
charge charges claim claims closure collapse concern concerns crash crisis cut cuts cutting decline declined declines decrease decreased
default deficit delay delayed delays deteriorate deteriorated difficult disappoint disappointed disappointing dispute downgrade
downgraded downgrades downside downturn drop dropped drops fail failed failing fails failure fall falls fell fine fined fraud halt halted
headwind hurt impairment indict indicted injunction investigation lawsuit layoff layoffs litigation loss losses lost lower lowered lowers
lowest miss missed misses negative penalty plunge plunged plunges pressure probe problem problems recall recalled recession restate
restatement risk risks scandal sell-off selloff shortfall shrink shrinking slow slowdown slower slump slumped slumps struggle struggled
struggles subpoena suspend suspended tumble tumbled tumbles uncertain uncertainty underperform unfavorable volatile warn warned warning
warns weak weaken weakened weaker weakness worse worst write-down writedown""".split())
NEGATORS = {"not", "no", "never", "without", "fails", "failed", "unable", "n't"}
INTENSIFIERS = {"sharply": 1.5, "significantly": 1.4, "strongly": 1.4, "slightly": 0.6, "modestly": 0.7, "record": 1.3}
MASSIVE_LABEL_SCORE = {"positive": 0.65, "neutral": 0.0, "negative": -0.65, "bullish": 0.65, "bearish": -0.65}


def massive_to_score(label: str) -> float | None:
    s = str(label).strip().lower()
    if not s or s in ("nan", "none"):
        return None
    if s in MASSIVE_LABEL_SCORE:
        return MASSIVE_LABEL_SCORE[s]
    if "pos" in s or "bull" in s:
        return 0.65
    if "neg" in s or "bear" in s:
        return -0.65
    return 0.0


def filter_news(news: pd.DataFrame, source_filter: str) -> pd.DataFrame:
    if not len(news) or source_filter == "all":
        return news
    df = news.copy()
    if source_filter == "massive":
        ms = df.get("massive_sentiment", pd.Series("", index=df.index)).fillna("").astype(str).str.strip()
        return df[ms != ""].reset_index(drop=True)
    if source_filter == "no-yahoo":
        src = df.get("source", pd.Series("", index=df.index)).fillna("").astype(str).str.lower()
        return df[src != "yahoo_rss"].reset_index(drop=True)
    raise ValueError(f"unknown source_filter={source_filter!r}")


def score_items(news: pd.DataFrame, scorer: str, score_fn) -> tuple[np.ndarray, str]:
    """Return per-row scores in [-1, +1] and the effective scorer label."""
    texts = (news["title"].fillna("") + ". " + news["text"].fillna("")).tolist()
    massive_col = news.get("massive_sentiment", pd.Series("", index=news.index)).fillna("").astype(str)
    if scorer == "massive":
        scores = [massive_to_score(ms) for ms in massive_col]
        if any(v is None for v in scores):
            raise ValueError("massive scorer requires massive_sentiment on every row (use --source-filter massive)")
        return np.array(scores, dtype=float), "massive"
    fb_scores = score_fn(texts)
    if scorer == "auto":
        out = [massive_to_score(ms) if massive_to_score(ms) is not None else fb for ms, fb in zip(massive_col, fb_scores)]
        return np.array(out, dtype=float), "auto(massive+finbert/lexicon)"
    return np.array(fb_scores, dtype=float), scorer


def lexicon_score(text: str) -> float:
    toks = re.findall(r"[a-z][a-z\-']*", str(text).lower())
    if not toks:
        return 0.0
    score, hits = 0.0, 0
    for i, t in enumerate(toks):
        s = 1.0 if t in POS else (-1.0 if t in NEG else 0.0)
        if s == 0.0:
            continue
        window = toks[max(0, i - 3):i]
        if any(w in NEGATORS for w in window):
            s = -s
        mult = max([INTENSIFIERS.get(w, 1.0) for w in window] + [1.0])
        score += s * mult
        hits += 1
    if hits == 0:
        return 0.0
    return float(np.tanh(score / np.sqrt(hits + 2)))


class FinBERT:
    def __init__(self):
        from transformers import AutoModelForSequenceClassification, AutoTokenizer, pipeline  # noqa: F401
        self.pipe = pipeline("text-classification", model="ProsusAI/finbert", top_k=None, truncation=True, max_length=128)

    def score(self, texts: list[str]) -> list[float]:
        out = []
        for i in range(0, len(texts), 32):
            res = self.pipe(texts[i:i + 32])
            for r in res:
                d = {x["label"].lower(): x["score"] for x in r}
                out.append(float(d.get("positive", 0) - d.get("negative", 0)))
        return out


def get_scorer(kind: str):
    if kind in ("auto", "finbert"):
        try:
            fb = FinBERT()
            return "finbert", fb.score
        except Exception as exc:
            if kind == "finbert":
                raise SystemExit(f"FinBERT unavailable ({exc}); pip install torch transformers, or --scorer lexicon")
    return "lexicon", lambda texts: [lexicon_score(t) for t in texts]


def to_session_date(ts: pd.Series) -> pd.Series:
    t = pd.to_datetime(ts, utc=True).dt.tz_convert(ET)
    d = t.dt.normalize()
    late = t.dt.time > dtime(16, 0)
    d = d.where(~late, d + pd.offsets.BDay(1))
    wk = d.dt.dayofweek >= 5
    d = d.where(~wk, d + pd.offsets.BDay(0))
    return d.dt.tz_localize(None)


def _add_shocks(out: pd.DataFrame, window: int) -> pd.DataFrame:
    """shock_z / count_z against the PREVIOUS `window` news days (shift(1): today's value is never in its own baseline)."""
    prev_mean = out["score"].shift(1).rolling(window, min_periods=5).mean()
    prev_std = out["score"].shift(1).rolling(window, min_periods=5).std()
    out["shock_z"] = (out["score"] - prev_mean) / prev_std.replace(0, np.nan)
    cm = out["count"].shift(1).rolling(window, min_periods=5).mean()
    cs = out["count"].shift(1).rolling(window, min_periods=5).std()
    out["count_z"] = (out["count"] - cm) / cs.replace(0, np.nan)
    if "score_src" in out.columns and out["score_src"].notna().any():
        pm = out["score_src"].shift(1).rolling(window, min_periods=5).mean()
        ps = out["score_src"].shift(1).rolling(window, min_periods=5).std()
        out["shock_src_z"] = (out["score_src"] - pm) / ps.replace(0, np.nan)
    out.index.name = "date"
    return out.round(4)


def daily_sentiment(news: pd.DataFrame, scores: np.ndarray, window: int = 20) -> pd.DataFrame:
    """Per-session aggregation of item-level scores.  If the news frame carries `massive_sentiment` (Massive's own
    per-ticker LLM label, +1/0/-1) the daily mean of that is added as `score_src` and `shock_src_z`."""
    df = news.copy()
    df["s"] = scores
    df["sdate"] = to_session_date(df["ts"])
    if "massive_sentiment" in df.columns:
        df["src_sentiment"] = df["massive_sentiment"].map(massive_to_score)
    g = df.groupby("sdate")["s"]
    out = pd.DataFrame({"score": g.mean(), "count": g.size(), "pos_share": g.apply(lambda s: float((s > 0.2).mean())),
                        "neg_share": g.apply(lambda s: float((s < -0.2).mean()))})
    if "src_sentiment" in df.columns and df["src_sentiment"].notna().any():
        gs = df.dropna(subset=["src_sentiment"]).groupby("sdate")["src_sentiment"]
        out["score_src"] = gs.mean()
        out["count_src"] = gs.size()
    out["source"] = "headlines"
    return _add_shocks(out, window)


def daily_from_timeline(tone: pd.Series, articles: pd.Series | None = None, window: int = 20, scale: float = 5.0) -> pd.DataFrame:
    """GDELT timelinetone (daily average tone, roughly -10..+10) -> the same daily contract."""
    t = pd.Series(tone).astype(float).sort_index()
    idx = pd.DatetimeIndex(t.index)
    idx = idx.tz_localize(None) if idx.tz is not None else idx
    shifted = (idx + pd.offsets.BDay(1)).normalize()
    out = pd.DataFrame({"score": np.tanh(t.to_numpy() / scale)}, index=shifted)
    out["count"] = pd.Series(articles).astype(float).reindex(t.index).to_numpy() if articles is not None else np.nan
    out["pos_share"], out["neg_share"] = np.nan, np.nan
    out = out[~out.index.duplicated(keep="last")]
    out["source"] = "gdelt_timeline"
    return _add_shocks(out, window)


def merge_daily(headline_daily: pd.DataFrame | None, timeline_daily: pd.DataFrame | None) -> pd.DataFrame:
    """Headline-level scores win on the days they exist; GDELT tone fills the rest."""
    parts = [d for d in (headline_daily, timeline_daily) if d is not None and len(d)]
    if not parts:
        return pd.DataFrame()
    if len(parts) == 1:
        return parts[0]
    h, t = headline_daily.copy(), timeline_daily.copy()
    t = t[~t.index.isin(h.index)]
    m = pd.concat([h, t]).sort_index()
    return _add_shocks(m, 20)


def synthetic_news(symbols: list[str], days: int, start: str = "2026-01-05", seed: int = 3, latent: dict | None = None) -> dict[str, pd.DataFrame]:
    """Fabricated headlines with a latent daily tone per name (returned so the backtest can plant a signal)."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=days)
    pos_words = sorted(POS); neg_words = sorted(NEG)
    out, lat = {}, {}
    for sym in symbols:
        tone = np.zeros(len(dates))
        rows = []
        for i, d in enumerate(dates):
            t = 0.7 * (tone[i - 1] if i else 0) + rng.normal(0, 0.35)
            if rng.random() < 0.04:
                t += rng.choice([-1, 1]) * rng.uniform(1.0, 2.0)
            tone[i] = t
            n = max(0, int(rng.poisson(2 + 3 * abs(t))))
            for k in range(n):
                p_pos = 1 / (1 + np.exp(-2 * t))
                words = rng.choice(pos_words if rng.random() < p_pos else neg_words, 2)
                title = f"{sym} {' '.join(words)} as analysts weigh outlook"
                ts = pd.Timestamp(d) + pd.Timedelta(hours=int(rng.integers(6, 19)), minutes=int(rng.integers(0, 60)))
                rows.append({"date": d.strftime("%Y-%m-%d"), "ts": ts.tz_localize(ET).isoformat(), "symbol": sym, "source": "synthetic",
                             "title": title, "text": "", "url": f"syn://{sym}/{i}/{k}"})
        out[sym] = pd.DataFrame(rows, columns=["date", "ts", "symbol", "source", "title", "text", "url"])
        lat[sym] = pd.Series(tone, index=dates)
    if latent is not None:
        latent.update(lat)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="*")
    ap.add_argument("--sp500", action="store_true", help="score all names in data/sp500.csv (~503 constituents)")
    ap.add_argument("--max-symbols", type=int)
    ap.add_argument("--scorer", choices=["auto", "massive", "finbert", "lexicon"], default="massive")
    ap.add_argument("--source-filter", choices=["all", "massive", "no-yahoo"], default="massive",
                    help="massive=rows with Massive insight only; no-yahoo=drop yahoo_rss rows")
    ap.add_argument("--window", type=int, default=20)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--days", type=int, default=300)
    a = ap.parse_args()
    if a.sp500:
        symbols = load_symbol_list(max_symbols=a.max_symbols)
    elif a.symbols:
        symbols = [str(s).strip().upper().replace(".", "-") for s in a.symbols]
    else:
        raise SystemExit("pass --symbols or --sp500")
    fb_kind, score_fn = get_scorer("lexicon" if a.scorer == "massive" else a.scorer)
    print(f"scorer: {a.scorer} (fallback={fb_kind}) source_filter={a.source_filter} symbols={len(symbols)}")
    syn = synthetic_news(symbols, a.days) if a.synthetic else {}
    for sym in symbols:
        if a.synthetic:
            news = syn[sym]
            (DATA_DIR / "news").mkdir(parents=True, exist_ok=True)
            news.to_csv(DATA_DIR / "news" / f"{_safe_name(sym)}.csv", index=False)
            source_filter = "all"
            scorer = "lexicon"
        else:
            p = DATA_DIR / "news" / f"{_safe_name(sym)}.csv"
            if not p.exists():
                print(f"{sym}: no {p} (run fetch_free_sentiment.py polygon-news first)"); continue
            news = pd.read_csv(p)
            source_filter = a.source_filter
            scorer = a.scorer
        news = filter_news(news, source_filter)
        if not len(news):
            print(f"{sym}: empty after source_filter={source_filter}"); continue
        scores, used = score_items(news, scorer, score_fn)
        if not len(scores):
            print(f"{sym}: no scorable rows ({used})"); continue
        daily = daily_sentiment(news, scores, a.window)
        out = DATA_DIR / "sentiment" / f"{_safe_name(sym)}_daily.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        daily.to_csv(out)
        src_note = f", score_src on {int(daily['score_src'].notna().sum())} days" if "score_src" in daily.columns else ""
        print(f"{sym}: {len(news)} items ({used}) -> {len(daily)} days, mean score {daily['score'].mean():+.3f}, "
              f"|shock_z|>=2 on {int((daily['shock_z'].abs() >= 2).sum())} days{src_note} -> {out}")


if __name__ == "__main__":
    main()
