#!/usr/bin/env python3
"""Rule-based event classifier for headlines (with an optional LLM hook) -> data/events/<SYM>.csv

Event types and polarity (what the event does to the NEXT session's tradability, not to the stock's value):
  earnings         results / EPS / revenue / quarter                          polarity from the tone words
  guidance_cut     lowers / cuts guidance, outlook, forecast                  -1
  guidance_raise   raises guidance / outlook                                  +1
  downgrade        analyst downgrade / price-target cut                        -1
  upgrade          analyst upgrade / price-target raise                        +1
  regulatory       FDA / FTC / SEC / DOJ / antitrust / probe / investigation   -1
  litigation       lawsuit / settlement / verdict / class action              -1
  m_and_a          acquire / merger / takeover / buyout / bid                   0 (binary event, direction is name-specific)
  product          launch / unveil / recall (recall -> -1)                      +1 / -1
  capital          buyback / dividend / offering / convertible (offering -> -1) +1 / -1
  management       CEO / CFO steps down / resigns / appointed                   -1 / 0
  macro            Fed / FOMC / CPI / jobs report / tariff                      0
Each headline gets at most one type (first match in priority order) and a confidence 0..1 from the number of
matched cue words.  Set EVENT_LLM_CMD to a shell command that reads JSON lines {title,text} on stdin and writes
{type,polarity,confidence} per line to override the rules with a model (kept out of the default path so the
backtest stays reproducible and free).

The consumer is ml/sentiment/sentiment_backtest.py --mode event-filter: stand aside from a base sleeve's trades
in a name for N sessions after a negative binary event (regulatory/litigation/guidance_cut/downgrade/recall),
and measure whether that helps.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from wsbt.data import DATA_DIR, _safe_name  # noqa: E402
from ml.sentiment.finbert_score import lexicon_score, to_session_date  # noqa: E402

RULES = [  # (type, polarity or None=tone, [regex cues])
    ("guidance_cut", -1, [r"\b(cuts?|lowers?|slash(es)?|trims?|reduc(es|ed))\b.{0,40}\b(guidance|outlook|forecast|view)\b",
                          r"\b(guidance|outlook|forecast)\b.{0,30}\b(cut|lowered|reduced|below)\b"]),
    ("guidance_raise", 1, [r"\b(rais(es|ed)|lifts?|boosts?|hikes?)\b.{0,40}\b(guidance|outlook|forecast|view)\b",
                           r"\b(guidance|outlook|forecast)\b.{0,30}\b(raised|above|lifted)\b"]),
    ("downgrade", -1, [r"\bdowngrad(e|es|ed)\b", r"\b(price target|pt)\b.{0,20}\b(cut|lowered|trimmed)\b", r"\bcut to (sell|underperform|neutral|hold)\b"]),
    ("upgrade", 1, [r"\bupgrad(e|es|ed)\b", r"\b(price target|pt)\b.{0,20}\b(raised|lifted|hiked)\b", r"\braised to (buy|outperform|overweight)\b"]),
    ("regulatory", -1, [r"\b(fda|ftc|sec|doj|antitrust|regulator[s]?|probe|investigat(es|ion|ing)|subpoena|crl|complete response letter)\b"]),
    ("litigation", -1, [r"\b(lawsuit|sued|sues|class action|verdict|settlement|litigation|jury|patent infringement)\b"]),
    ("m_and_a", 0, [r"\b(acquir(e|es|ed|ing)|acquisition|merger|takeover|buyout|to buy|deal to|bid for|agrees to (buy|acquire))\b"]),
    ("management", -1, [r"\b(ceo|cfo|chief executive|chief financial|chairman)\b.{0,40}\b(steps? down|resign(s|ed)?|depart(s|ure)|ousted|fired|exits?)\b"]),
    ("product", None, [r"\b(recall(s|ed)?)\b", r"\b(launch(es|ed)?|unveil(s|ed)?|introduc(es|ed)|debut(s|ed)?|approv(al|ed|es))\b"]),
    ("capital", None, [r"\b(buyback|repurchase|dividend)\b", r"\b(offering|secondary|convertible notes?|share sale|dilut)\b"]),
    ("earnings", None, [r"\b(earnings|eps|revenue|quarter(ly)?|results|q[1-4])\b"]),
    ("macro", 0, [r"\b(fed|fomc|cpi|inflation|jobs report|payrolls|tariffs?|rate (hike|cut)|treasury yields?)\b"]),
]
NEG_PRODUCT = re.compile(r"\brecall", re.I)
NEG_CAPITAL = re.compile(r"\b(offering|secondary|convertible|share sale|dilut)", re.I)
NEGATIVE_BINARY = {"regulatory", "litigation", "guidance_cut", "downgrade", "management"}


def classify(title: str, text: str = "") -> dict:
    s = f"{title} {text}".lower()
    for etype, pol, cues in RULES:
        hits = sum(1 for c in cues if re.search(c, s, re.I))
        if hits:
            if pol is None:
                if etype == "product":
                    pol = -1 if NEG_PRODUCT.search(s) else 1
                elif etype == "capital":
                    pol = -1 if NEG_CAPITAL.search(s) else 1
                else:
                    tone = lexicon_score(s)
                    pol = 1 if tone > 0.15 else (-1 if tone < -0.15 else 0)
            return {"type": etype, "polarity": int(pol), "confidence": round(min(1.0, 0.5 + 0.25 * hits), 2)}
    return {"type": "none", "polarity": 0, "confidence": 0.0}


def classify_llm(rows: list[dict]) -> list[dict] | None:
    cmd = os.environ.get("EVENT_LLM_CMD")
    if not cmd:
        return None
    payload = "\n".join(json.dumps({"title": r.get("title", ""), "text": r.get("text", "")}) for r in rows)
    try:
        out = subprocess.run(cmd, shell=True, input=payload, capture_output=True, text=True, timeout=600, check=True).stdout
        return [json.loads(l) for l in out.strip().splitlines()]
    except Exception as exc:
        print(f"LLM classifier failed ({exc}); falling back to rules")
        return None


def classify_file(news: pd.DataFrame) -> pd.DataFrame:
    rows = news.to_dict("records")
    llm = classify_llm(rows)
    labels = llm if llm and len(llm) == len(rows) else [classify(r.get("title", ""), r.get("text", "")) for r in rows]
    ev = news[["date", "ts", "symbol", "source", "title", "url"]].copy()
    ev["type"] = [l["type"] for l in labels]
    ev["polarity"] = [int(l.get("polarity", 0)) for l in labels]
    ev["confidence"] = [float(l.get("confidence", 0)) for l in labels]
    ev["sdate"] = to_session_date(ev["ts"]).dt.strftime("%Y-%m-%d")
    return ev[ev["type"] != "none"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="+", required=True)
    a = ap.parse_args()
    for sym in a.symbols:
        p = DATA_DIR / "news" / f"{_safe_name(sym)}.csv"
        if not p.exists():
            print(f"{sym}: no {p}"); continue
        news = pd.read_csv(p)
        ev = classify_file(news)
        out = DATA_DIR / "events" / f"{_safe_name(sym)}.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        ev.to_csv(out, index=False)
        print(f"{sym}: {len(news)} headlines -> {len(ev)} events  {ev['type'].value_counts().to_dict()} -> {out}")


if __name__ == "__main__":
    main()
