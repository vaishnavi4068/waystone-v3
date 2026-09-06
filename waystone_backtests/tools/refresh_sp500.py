#!/usr/bin/env python3
"""Refresh data/sp500.csv from the public S&P 500 constituents list (503 names).

  python tools/refresh_sp500.py
  python tools/refresh_sp500.py --url https://datahub.io/.../constituents.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wsbt.data import DATA_DIR  # noqa: E402

DEFAULT_URL = "https://datahub.io/core/s-and-p-500-companies-financials/_r/-/data/constituents.csv"


def normalize_symbol(raw: str) -> str:
    return str(raw).strip().upper().replace(".", "-")


def refresh(url: str = DEFAULT_URL) -> Path:
    df = pd.read_csv(url)
    sym_col = "Symbol" if "Symbol" in df.columns else df.columns[0]
    name_col = "Name" if "Name" in df.columns else None
    out = pd.DataFrame({"symbol": [normalize_symbol(s) for s in df[sym_col]]})
    if name_col:
        out["company"] = df[name_col].astype(str).values
    out = out.drop_duplicates(subset=["symbol"]).sort_values("symbol").reset_index(drop=True)
    path = DATA_DIR / "sp500.csv"
    out.to_csv(path, index=False)
    print(f"wrote {len(out)} symbols -> {path}")
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL)
    a = ap.parse_args()
    refresh(a.url)


if __name__ == "__main__":
    main()
