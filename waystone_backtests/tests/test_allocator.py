"""Tests for ml/allocator.py — book.yaml load, alignment, equal-weight combine."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ml import allocator as A  # noqa: E402


def test_load_book_config():
    cfg = A.load_book_config(ROOT / "book.yaml")
    assert cfg["name"] == "research_book_v1"
    assert cfg["method"] == "equal_weight"
    assert len(cfg["sleeves"]) == 4


def test_aligns_misaligned_calendars():
    idx_a = pd.bdate_range("2024-01-02", periods=5)
    idx_b = pd.bdate_range("2024-01-03", periods=5)
    frame = A.align_sleeve_returns({
        "a": pd.Series([0.01, 0.0, -0.01, 0.02, 0.0], index=idx_a),
        "b": pd.Series([0.02, -0.01, 0.0, 0.01, 0.005], index=idx_b),
    })
    assert len(frame) == 6
    assert frame.loc[idx_a[0], "b"] == 0.0
    assert not frame.isna().any().any()


def test_equal_weight_synthetic():
    frame = A.synthetic_sleeves(n_days=120, seed=3)
    ret = A.combine_returns(frame, {c: 1.0 for c in frame.columns}, "equal_weight")
    assert len(ret) == 120
    assert abs(ret.mean()) < 0.05
    w = pd.Series({c: 1.0 for c in frame.columns}) / len(frame.columns)
    expected = (frame * w).sum(axis=1)
    pd.testing.assert_series_equal(ret, expected, check_names=False)


def test_sleeve_mean_method():
    frame = A.synthetic_sleeves(n_days=60, seed=7)
    ret = A.combine_returns(frame, {}, "sleeve_mean")
    pd.testing.assert_series_equal(ret, frame.mean(axis=1), check_names=False)


def test_no_lookahead_on_truncated_sleeve():
    frame = A.synthetic_sleeves(n_days=100, seed=5)
    full = A.combine_returns(frame, {c: 1.0 for c in frame.columns}, "equal_weight")
    cut = frame.iloc[:70].copy()
    partial = A.combine_returns(cut, {c: 1.0 for c in cut.columns}, "equal_weight")
    pd.testing.assert_series_equal(full.iloc[:70], partial.iloc[:70], check_names=False)


def test_run_synthetic_writes_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "RESULTS", tmp_path / "results")
    cfg = {"name": "book_test", "nav": 100_000, "method": "equal_weight", "warmup_days": 10}
    out = A.run(cfg, synthetic=True)
    assert (out / "equity.csv").exists()
    assert (out / "metrics.json").exists()
    assert (out / "kpi.json").exists()
    metrics = json.loads((out / "metrics.json").read_text())
    assert metrics["strategy"] == "book_test"
    assert metrics["extra"]["n_sleeves"] == 3
