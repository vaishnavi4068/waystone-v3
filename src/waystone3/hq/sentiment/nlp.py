"""Headline hygiene and scoring: NFKC, source tiers, kill lexicon, FinBERT, novelty.

FinBERT (``ProsusAI/finbert``) runs on CPU when ``transformers`` and ``torch`` are
installed (the sentiment image has both); otherwise a finance word list is used and every
row records which scorer produced it.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

FINBERT_MODEL = "ProsusAI/finbert"

# Wire and official sources 1.0, aggregators and unknown publishers 0.5, junk 0 (thesis §10).
WIRE = (
    "reuters",
    "associated press",
    "ap news",
    "bloomberg",
    "dow jones",
    "marketwatch",
    "wall street journal",
    "wsj",
    "financial times",
    "cnbc",
    "federal reserve",
    "bureau of labor statistics",
    "barron's",
    "barrons",
)
WIRE_FEEDS = ("fed_press", "marketwatch", "cnbc_top", "cnbc_markets")
JUNK = (
    "motley fool",
    "zacks",
    "investorplace",
    "24/7 wall st",
    "247wallst",
    "benzinga",
    "seeking alpha",
    "gurufocus",
    "insidermonkey",
    "insider monkey",
    "tipranks",
)

KILL_TERMS: dict[str, re.Pattern[str]] = {
    "war": re.compile(
        r"\b(war|invasion|invades|missile strikes?|air ?strikes?|military strikes?)\b"
    ),
    "tariff shock": re.compile(
        r"\b(tariff (shock|hike|escalation)|new tariffs?|retaliatory tariffs?|trade war)\b"
    ),
    "emergency meeting": re.compile(r"\bemergency (meeting|rate cut|rate hike|session)\b"),
    "circuit breaker": re.compile(r"\bcircuit[- ]breakers?\b|\blimit[- ]down\b"),
    "exchange halt": re.compile(
        r"\b(trading|exchange|market)[- ]wide halt\b|\b(trading|exchange|markets?) halt(ed|s)?\b"
    ),
    "default": re.compile(r"\b(sovereign|debt|treasury) default\b|\bdebt ceiling breach\b"),
    "bank failure": re.compile(r"\bbank (run|failure|collapse)s?\b"),
    "flash crash": re.compile(r"\bflash crash\b|\bmarket crash\b"),
}

MACRO_TAGS: dict[str, re.Pattern[str]] = {
    "CPI": re.compile(r"\b(cpi|consumer price|inflation)\b"),
    "FOMC": re.compile(r"\b(fomc|fed(eral reserve)?|powell|rate (cut|hike|decision))\b"),
    "NFP": re.compile(r"\b(payrolls?|nfp|jobs report|nonfarm|unemployment rate)\b"),
    "PCE": re.compile(r"\b(pce|personal (income|consumption))\b"),
}

_POS = frozenset(
    """
    advance advances beat beats boost boosted boosts bullish climb climbs climbed confident
    cools cooled cooling easing eases gain gained gains growth higher high highs improve
    improved improves jump jumped jumps optimism optimistic outperform rally rallies rallied
    rebound rebounds record recovery resilient rise rises rising rose soar soared solid
    strong stronger surge surged surges upbeat upgrade upgraded upside win wins
    """.split()  # noqa: SIM905
)
_NEG = frozenset(
    """
    bearish collapse collapses concern concerns crash crashes crisis cut cuts decline
    declines declined deficit downgrade downgraded drop dropped drops fall falls fear fears
    fell hot hotter loss losses lower miss missed misses plunge plunged plunges recession
    risk risks selloff sell-off shock shocks slump slumps slide slides slid sink sinks sank
    slowdown stall stalls tumble tumbles tumbled turmoil uncertainty volatile warn warns
    warning weak weaker worse worst worries worry
    """.split()  # noqa: SIM905
)
_NEGATORS = {"not", "no", "never", "without", "despite", "n't"}


def normalize(text: str) -> str:
    """NFKC, strip control characters and collapse whitespace (thesis §11 hygiene)."""
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"[\t\n\r\f\v]", " ", text)
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C" or ch == " ")
    return re.sub(r"\s+", " ", text).strip()


def source_tier(publisher: str | None, feed: str) -> float:
    if publisher is None and feed in WIRE_FEEDS:
        return 1.0
    name = f"{publisher or ''} {feed}".lower()
    if any(j in name for j in JUNK):
        return 0.0
    if any(w in name for w in WIRE):
        return 1.0
    return 0.5


def kill_terms(text: str) -> list[str]:
    low = text.lower()
    return [name for name, rx in KILL_TERMS.items() if rx.search(low)]


_MARKET = re.compile(
    r"\b(stocks?|equit(y|ies)|markets?|wall street|s&p|nasdaq|dow|russell|futures|index|indices|"
    r"treasur(y|ies)|bonds?|yields?|fed|central bank|economy|global|u\.?s\.?|united states|china|"
    r"europe|oil|dollar|banks?|exchange|nyse|cme|investors?)\b"
)


def market_relevant(text: str) -> bool:
    """Entity relevance for the kill switch: the story must touch markets or the macro."""
    low = text.lower()
    return bool(_MARKET.search(low) or macro_tags(low))


def macro_tags(text: str) -> list[str]:
    low = text.lower()
    return [name for name, rx in MACRO_TAGS.items() if rx.search(low)]


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z][a-z'\-]*", text.lower())


def lexicon_score(text: str) -> float:
    toks = _tokens(text)
    total, hits = 0.0, 0
    for i, tok in enumerate(toks):
        s = 1.0 if tok in _POS else (-1.0 if tok in _NEG else 0.0)
        if not s:
            continue
        if any(w in _NEGATORS for w in toks[max(0, i - 3) : i]):
            s = -s
        total += s
        hits += 1
    return 0.0 if hits == 0 else math.tanh(total / math.sqrt(hits + 2))


@dataclass(frozen=True)
class Scored:
    score: float
    prob_pos: float | None = None
    prob_neg: float | None = None
    prob_neu: float | None = None


Scorer = Callable[[Sequence[str]], list[Scored]]


def lexicon_scorer(texts: Sequence[str]) -> list[Scored]:
    return [Scored(round(lexicon_score(t), 4)) for t in texts]


def finbert_scorer() -> Scorer:
    from transformers import pipeline  # type: ignore[import-not-found,unused-ignore]

    pipe = pipeline(
        "text-classification", model=FINBERT_MODEL, top_k=None, truncation=True, max_length=96
    )

    def score(texts: Sequence[str]) -> list[Scored]:
        out: list[Scored] = []
        for i in range(0, len(texts), 32):
            for res in pipe(list(texts[i : i + 32])):
                p = {r["label"].lower(): float(r["score"]) for r in res}
                pos, neg, neu = (
                    p.get("positive", 0.0),
                    p.get("negative", 0.0),
                    p.get("neutral", 0.0),
                )
                out.append(Scored(round(pos - neg, 4), round(pos, 4), round(neg, 4), round(neu, 4)))
        return out

    return score


def make_scorer(kind: str = "auto") -> tuple[str, Scorer]:
    """``finbert`` | ``lexicon`` | ``auto`` (FinBERT when installed)."""
    if kind in ("auto", "finbert"):
        try:
            return "finbert", finbert_scorer()
        except Exception as exc:
            if kind == "finbert":
                raise RuntimeError(f"FinBERT unavailable: {exc}") from exc
    return "lexicon", lexicon_scorer


def _vec(text: str) -> Counter[str]:
    return Counter(t for t in _tokens(text) if len(t) > 2)


def cosine(a: Counter[str], b: Counter[str]) -> float:
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return 0.0 if na == 0 or nb == 0 else dot / (na * nb)


@dataclass
class NoveltyIndex:
    """1 − max cosine against the titles already seen in the last five days."""

    seen: list[Counter[str]] = field(default_factory=list)

    @classmethod
    def of(cls, titles: Iterable[str]) -> NoveltyIndex:
        return cls([_vec(t) for t in titles])

    def novelty(self, title: str) -> float:
        v = _vec(title)
        best = max((cosine(v, s) for s in self.seen), default=0.0)
        self.seen.append(v)
        return round(max(0.0, 1.0 - best), 4)
