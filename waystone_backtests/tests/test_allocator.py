"""Allocator slice-1 tests (ALLOCATOR_SPEC.md §8–9 items 1–7 and 9).

Run:  python -m pytest tests/test_allocator.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ml.allocator import (  # noqa: E402
    apply_caps, apply_hysteresis, allocate, inverse_vol_weights, main, risk_shares,
    solve_erc, step_brake,
)
from ml.book_io import (  # noqa: E402
    _simple_yaml, default_state, derive_for_session, holdout_status, load_book,
    next_session_on_or_after, read_instruction, round_units, save_json,
)

PASS_KPI = {"sharpe": 2.0, "dsr": 0.99, "oosis": 0.8, "coststress": 1.2,
            "ntrades": 250, "paramsens": 10, "holdout_unlocked": True}


def _equity(path: Path, n=80, start="2023-01-02", mu=40.0, sd=400.0, seed=1, pnl=None):
    idx = pd.bdate_range(start, periods=n)
    if pnl is None:
        pnl = np.random.default_rng(seed).normal(mu, sd, n)
    pnl = np.asarray(pnl, float)
    df = pd.DataFrame({"equity": 100_000 + np.cumsum(pnl), "daily_pnl": pnl,
                       "daily_ret": pnl / 100_000.0}, index=idx)
    df.index.name = "date"
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path)
    return df


def _cfg(sid, status="paper", **kw):
    d = {"status": status, "results_dir": f"results/{sid}", "unit_type": "contracts",
         "unit_size": 1, "unit_step": 1, "instrument": sid,
         "regime_rules": {"off_in_states": [], "half_in_states": []},
         "event_blackout": [], "dd_half_pct": 8, "dd_off_pct": 12,
         "recover_sessions": 10, "min_incubation_months": 0}
    d.update(kw)
    return d


def _book_kw(**extra):
    b = {"nav": 100000, "target_daily_vol_pct": 0.8, "max_gross_multiplier": 5.0,
         "max_risk_share": 0.45, "kelly_fraction": 0.5, "shrink_corr": 0.0,
         "vol_halflife_days": 20, "corr_window_days": 120,
         "rebalance": {"schedule": "monthly", "hysteresis_pct": 20},
         "regime": {"name": "spx"},
         "book_brake": {"dd_half_pct": 6, "dd_off_pct": 10, "monthly_loss_off_pct": 5},
         "sleeves": {}}
    b.update(extra)
    return b


def _write_regime(root: Path, dates, state=1):
    p = root / "data" / "regime" / "spx_states.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"date": pd.DatetimeIndex(dates), "state": state}).to_csv(p, index=False)


def make_world(tmp: Path, sleeves: dict, n=80, kpi=None, start="2023-01-02", pnl=None):
    book = _book_kw(sleeves=sleeves)
    last = None
    for i, (sid, cfg) in enumerate(sleeves.items()):
        rdir = tmp / cfg["results_dir"]
        series = None if pnl is None else pnl.get(sid)
        df = _equity(rdir / "equity.csv", n=n, start=start, seed=i + 3, pnl=series)
        (rdir / "kpi.json").write_text(json.dumps(kpi or PASS_KPI))
        (rdir / "metrics.json").write_text(json.dumps(kpi or PASS_KPI))
        last = df
    if last is not None:
        _write_regime(tmp, last.index)
    save_json(tmp / "book.yaml", book)
    return book


# ── 1. ERC correctness ───────────────────────────────────────────────────────
def test_erc_correctness():
    sigmas = np.array([900.0, 600.0, 400.0])
    C = np.array([[1.0, 0.1, 0.0], [0.1, 1.0, 0.3], [0.0, 0.3, 1.0]])
    w, notes = solve_erc(sigmas, C, 800.0, shrink=0.0)
    assert notes == []
    assert np.allclose(w, [0.49, 0.64, 1.02], atol=0.06)
    Sigma = np.diag(sigmas) @ C @ np.diag(sigmas)
    assert abs(float(w @ Sigma @ w) - 800.0 ** 2) / 800.0 ** 2 < 0.03
    sh = risk_shares(w, Sigma)
    assert np.allclose(sh, np.full(3, 1 / 3), atol=0.05)

    w1, _ = solve_erc([900.0], [[1.0]], 800.0)
    assert abs(w1[0] - 800.0 / 900.0) < 1e-9

    s2, C2 = np.array([900.0, 400.0]), np.array([[1.0, 0.2], [0.2, 1.0]])
    w2, _ = solve_erc(s2, C2, 800.0, shrink=0.0)
    Sigma2 = np.diag(s2) @ C2 @ np.diag(s2)
    expect = inverse_vol_weights(s2, Sigma2, 800.0)
    assert np.allclose(w2, expect, atol=1e-9)
    assert abs(w2[0] / w2[1] - s2[1] / s2[0]) < 1e-9


def test_one_eligible_vol_targeting(tmp_path):
    sleeves = {"a": _cfg("a"), "b": _cfg("b", status="shadow")}
    book = make_world(tmp_path, sleeves, n=80)
    fs = pd.bdate_range("2023-01-02", periods=81)[-1]
    out = allocate(book, fs, tmp_path)
    a = out["instruction"]["sleeves"]["a"]
    assert a["status"] == "paper" and abs(a["risk_weight"] - 1.0) < 1e-9


# ── 2. Caps ──────────────────────────────────────────────────────────────────
def test_caps_kelly_gross_share():
    sigmas = np.array([100.0, 100.0, 100.0])
    C = np.eye(3)
    w_erc, _ = solve_erc(sigmas, C, 800.0, shrink=0.0)
    w, st = apply_caps(w_erc, sigmas, C, 800.0, kelly_cap=[0.3, 0.3, 0.3],
                       max_gross=10.0, max_share=0.9, shrink=0.0)
    assert np.all(st["kelly"] <= 0.3 + 1e-9)

    _, st2 = apply_caps(w_erc, sigmas, C, 800.0, kelly_cap=[10, 10, 10],
                        max_gross=1.5, max_share=0.9, shrink=0.0)
    assert st2["gross"].sum() <= 1.5 + 1e-9

    # ERC shares are ~1/3; a 0.30 cap must clip + re-solve on the rest
    _, st3 = apply_caps(w_erc, sigmas, C, 800.0, kelly_cap=[10, 10, 10],
                        max_gross=20.0, max_share=0.30, shrink=0.0)
    Sigma = np.diag(sigmas ** 2)
    sh = risk_shares(st3["share"], Sigma)
    assert (sh <= 0.30 + 1e-6).sum() >= 2


# ── 3. Walk-forward purity ───────────────────────────────────────────────────
def test_walk_forward_purity(tmp_path):
    sleeves = {"a": _cfg("a"), "c": _cfg("c")}
    book = make_world(tmp_path, sleeves, n=120, start="2023-01-02")
    idx = pd.bdate_range("2023-01-02", periods=120)
    fs = idx[90]
    out_full = allocate(book, fs, tmp_path)
    # append a huge future spike after the decision date — must not leak
    for sid in sleeves:
        p = tmp_path / f"results/{sid}" / "equity.csv"
        df = pd.read_csv(p, parse_dates=["date"]).set_index("date")
        extra = pd.bdate_range(idx[90], periods=15)
        spike = pd.DataFrame({"equity": 9e9, "daily_pnl": 1e6, "daily_ret": 10.0}, index=extra)
        spike.index.name = "date"
        pd.concat([df, spike]).to_csv(p)
    out_leak = allocate(book, fs, tmp_path)
    for sid in ("a", "c"):
        assert out_full["instruction"]["sleeves"][sid]["multiplier"] == \
            out_leak["instruction"]["sleeves"][sid]["multiplier"]
        assert out_full["state"]["sleeves"][sid]["m_raw"] == pytest.approx(
            out_leak["state"]["sleeves"][sid]["m_raw"], rel=1e-9, abs=1e-12)


# ── 4. Brake state machine ───────────────────────────────────────────────────
def test_brake_state_machine_scripted_path():
    st = {"state": "normal", "recover_count": 0, "recover_high": None, "last_equity": 100.0}
    st, g = step_brake(st, dd=7.9, equity=92.0, dd_half=8, dd_off=12, recover=3)
    assert st["state"] == "normal" and g == 1.0
    st, g = step_brake(st, dd=8.0, equity=90.0, dd_half=8, dd_off=12, recover=3)
    assert st["state"] == "half" and g == 0.5
    st, g = step_brake(st, dd=3.9, equity=97.0, dd_half=8, dd_off=12, recover=3)
    assert st["state"] == "normal"
    st, _ = step_brake(st, dd=8.0, equity=90.0, dd_half=8, dd_off=12, recover=3)
    st, g = step_brake(st, dd=12.0, equity=80.0, dd_half=8, dd_off=12, recover=3)
    assert st["state"] == "brake" and g == 0.0
    # three consecutive new highs → half
    for eq in (81.0, 82.0, 83.0):
        st, g = step_brake(st, dd=12.0, equity=eq, dd_half=8, dd_off=12, recover=3)
    assert st["state"] == "half" and g == 0.5
    # back to brake, then a new low resets the recovery counter
    st, _ = step_brake(st, dd=12.0, equity=70.0, dd_half=8, dd_off=12, recover=3)
    assert st["state"] == "brake"
    st, _ = step_brake(st, dd=12.0, equity=71.0, dd_half=8, dd_off=12, recover=3)
    st, _ = step_brake(st, dd=12.0, equity=72.0, dd_half=8, dd_off=12, recover=3)
    st, _ = step_brake(st, dd=12.0, equity=60.0, dd_half=8, dd_off=12, recover=3)  # new low
    assert st["recover_count"] == 0
    st, _ = step_brake(st, dd=12.0, equity=61.0, dd_half=8, dd_off=12, recover=3)
    st, _ = step_brake(st, dd=12.0, equity=62.0, dd_half=8, dd_off=12, recover=3)
    assert st["state"] == "brake"
    st, g = step_brake(st, dd=12.0, equity=62.0, dd_half=8, dd_off=12, recover=3, reset=True)
    assert st["state"] == "half"


# ── 5. Hysteresis ────────────────────────────────────────────────────────────
def test_hysteresis():
    assert apply_hysteresis(1.2, 1.0, False, 20, False) == 1.0          # 20% exactly: not greater
    assert apply_hysteresis(1.21, 1.0, False, 20, False) == pytest.approx(1.21)
    assert apply_hysteresis(1.05, 1.0, True, 20, False) == pytest.approx(1.05)   # month start
    assert apply_hysteresis(1.05, 1.0, False, 20, True) == pytest.approx(1.05)   # gate change
    assert apply_hysteresis(0.9, None, False, 20, False) == 0.9


def test_hysteresis_persisted_mid_month(tmp_path):
    # Gentle paths so DD brakes stay normal and gates do not flip between calls.
    n, start = 90, "2023-01-02"
    pnl = {s: np.full(n, 10.0) + 5.0 * np.sin(np.arange(n) / 8.0) for s in ("a", "b")}
    sleeves = {"a": _cfg("a"), "b": _cfg("b")}
    book = make_world(tmp_path, sleeves, n=n, start=start, pnl=pnl)
    fs = pd.Timestamp("2023-05-10")  # mid-month weekday
    out1 = allocate(book, fs, tmp_path)
    parked = {sid: out1["state"]["sleeves"][sid]["m_raw"] for sid in ("a", "b")}
    state = json.loads(json.dumps(out1["state"]))  # copy; allocate mutates state dicts
    for sid, mr in parked.items():
        if mr:
            state["sleeves"][sid]["m_raw"] = mr * 1.10
    out2 = allocate(book, fs, tmp_path, state=state)
    for sid, mr in parked.items():
        if mr:
            assert out2["state"]["sleeves"][sid]["m_raw"] == pytest.approx(mr * 1.10, rel=1e-6)


# ── 6. Rounding ──────────────────────────────────────────────────────────────
def test_rounding_shadow_and_one():
    assert round_units(0.4, 1, 1, "contracts") == 0
    assert round_units(0.6, 1, 1, "contracts") == 1
    assert round_units(0.4, 25000, 1000, "notional_usd") == 10000
    assert round_units(0.02, 25000, 1000, "notional_usd") == 0


def test_rounds_to_zero_sets_shadow(tmp_path):
    sleeves = {"a": _cfg("a", unit_size=1, unit_step=1)}
    book = make_world(tmp_path, sleeves, n=80)
    book["target_daily_vol_pct"] = 0.0001  # tiny book vol → tiny multiplier
    fs = pd.bdate_range("2023-01-02", periods=81)[-1]
    row = allocate(book, fs, tmp_path)["instruction"]["sleeves"]["a"]
    if row["units"] == 0:
        assert row["shadow"] is True
        assert "rounds_to_zero" in row["reasons"] or row["multiplier"] == 0


# ── 7. Fallback stale / missing / schema ─────────────────────────────────────
def test_fallback_stale_instruction(tmp_path):
    path = tmp_path / "book_instruction.json"
    stale = {
        "schema": 1, "generated_at": "2020-01-01T00:00:00-05:00",
        "for_session": "2020-01-02", "valid_until": "2020-01-02T16:00:00-05:00",
        "nav": 100000,
        "book": {},
        "sleeves": {
            "v221_mnq": {"status": "paper", "multiplier": 2.0, "units": 4,
                         "unit_type": "contracts", "risk_weight": 0.3,
                         "reasons": ["erc"], "shadow": False},
            "pullback_bb": {"status": "paper", "multiplier": 1.0, "units": 20000,
                            "unit_type": "notional_usd", "risk_weight": 0.3,
                            "reasons": ["erc"], "shadow": False},
        },
    }
    save_json(path, stale)
    now = pd.Timestamp("2026-09-08T06:00:00", tz="America/New_York")
    c = read_instruction(path, sleeve="v221_mnq", now=now)
    assert c["units"] == 2 and c["reasons"] == ["fallback", "CRITICAL"]
    # min 1 contract
    stale["sleeves"]["v221_mnq"]["units"] = 1
    save_json(path, stale)
    assert read_instruction(path, sleeve="v221_mnq", now=now)["units"] == 1
    # notional keeps half; missing last_known + not contracts → shadow + CRITICAL
    miss = tmp_path / "missing.json"
    sh = read_instruction(miss, sleeve="pullback_bb", now=now, unit_type="notional_usd")
    assert sh["shadow"] is True and "CRITICAL" in sh["reasons"]
    # schema != 1
    stale["schema"] = 2
    save_json(path, stale)
    assert "CRITICAL" in read_instruction(path, sleeve="v221_mnq", now=now)["reasons"]


# ── 9. Schema of written JSON ────────────────────────────────────────────────
def test_instruction_schema(tmp_path):
    sleeves = {"v221_mnq": _cfg("v221_mnq", unit_size=2),
               "vwap_options": _cfg("vwap_options"),
               "pullback_bb": _cfg("pullback_bb", status="shadow", unit_type="notional_usd",
                                   unit_size=25000, unit_step=1000)}
    book = make_world(tmp_path, sleeves, n=80)
    fs = pd.bdate_range("2023-01-02", periods=81)[-1]
    ins = allocate(book, fs, tmp_path)["instruction"]
    assert ins["schema"] == 1
    for k in ("generated_at", "for_session", "valid_until", "nav", "book", "sleeves"):
        assert k in ins
    for k in ("target_daily_vol_pct", "realised_daily_vol_pct_20d", "drawdown_pct",
              "brake", "kill_switch", "regime_state", "regime_name", "events_today"):
        assert k in ins["book"]
    assert set(ins["sleeves"]) == set(sleeves)
    for row in ins["sleeves"].values():
        assert set(row) >= {"status", "multiplier", "units", "unit_type", "risk_weight", "reasons", "shadow"}
        assert row["status"] in {"live", "paper", "shadow", "off", "brake"}
        assert isinstance(row["reasons"], list) and row["reasons"]
        assert isinstance(row["shadow"], bool)


# ── §8 edge cases ────────────────────────────────────────────────────────────
def test_zero_eligible_all_shadow(tmp_path):
    sleeves = {"a": _cfg("a"), "b": _cfg("b")}
    book = make_world(tmp_path, sleeves, n=20)  # <60
    fs = pd.bdate_range("2023-01-02", periods=21)[-1]
    out = allocate(book, fs, tmp_path)
    assert out["instruction"]["book"]["brake"] == "no_eligible_sleeves"
    for row in out["instruction"]["sleeves"].values():
        assert row["status"] == "shadow" and row["shadow"] is True


def test_missing_equity_off(tmp_path):
    sleeves = {"a": _cfg("a")}
    book = _book_kw(sleeves=sleeves)
    save_json(tmp_path / "book.yaml", book)
    out = allocate(book, "2023-06-01", tmp_path)
    assert out["instruction"]["sleeves"]["a"]["status"] == "off"
    assert "results_missing" in out["instruction"]["sleeves"]["a"]["reasons"]


def test_history_lt_60(tmp_path):
    sleeves = {"a": _cfg("a")}
    book = make_world(tmp_path, sleeves, n=40)
    fs = pd.bdate_range("2023-01-02", periods=41)[-1]
    row = allocate(book, fs, tmp_path)["instruction"]["sleeves"]["a"]
    assert "history<60" in row["reasons"]
    assert row["status"] in {"paper", "shadow"}


def test_regime_stale_assume_1(tmp_path):
    sleeves = {"a": _cfg("a")}
    book = make_world(tmp_path, sleeves, n=80, start="2023-01-02")
    _write_regime(tmp_path, pd.bdate_range("2023-01-02", periods=5), state=2)
    fs = pd.Timestamp("2023-04-20")
    ins = allocate(book, fs, tmp_path)["instruction"]
    assert ins["book"]["regime_state"] == 1
    assert any("regime=stale:assume_1" in r for r in ins["sleeves"]["a"]["reasons"])


def test_sigma_zero_shadow(tmp_path):
    sleeves = {"a": _cfg("a")}
    book = make_world(tmp_path, sleeves, n=80, pnl={"a": np.zeros(80)})
    fs = pd.bdate_range("2023-01-02", periods=81)[-1]
    row = allocate(book, fs, tmp_path)["instruction"]["sleeves"]["a"]
    assert row["status"] == "shadow" and "sigma=0" in row["reasons"]


def test_cov_not_pd_inverse_vol():
    C = np.array([[1.0, 0.9, -0.9], [0.9, 1.0, 0.9], [-0.9, 0.9, 1.0]])
    assert np.linalg.eigvalsh(C).min() <= 0
    w, notes = solve_erc([100.0, 100.0, 100.0], C, 50.0, shrink=0.0)
    assert "cov_not_pd" in notes
    Sigma = np.diag([100.0, 100.0, 100.0]) @ C @ np.diag([100.0, 100.0, 100.0])
    expect = inverse_vol_weights(np.array([100.0, 100.0, 100.0]), Sigma, 50.0)
    assert np.allclose(w, expect)


def test_holdout_locked_cannot_be_live(tmp_path):
    kpi = dict(PASS_KPI, holdout_unlocked=False)
    assert holdout_status(kpi)["unlocked"] is False
    sleeves = {"a": _cfg("a", status="live")}
    book = make_world(tmp_path, sleeves, n=80, kpi=kpi)
    fs = pd.bdate_range("2023-01-02", periods=81)[-1]
    row = allocate(book, fs, tmp_path)["instruction"]["sleeves"]["a"]
    assert row["status"] != "live"
    assert "holdout_locked" in row["reasons"]


def test_month_roll_clears_monthly_loss_keeps_book_brake(tmp_path):
    sleeves = {"a": _cfg("a", status="live"), "b": _cfg("b", status="live")}
    book = make_world(tmp_path, sleeves, n=80, start="2023-01-02")
    fs = pd.Timestamp("2023-04-03")  # first sessions of April 2023
    state = default_state(book)
    state["book"].update({"monthly_loss_off": True, "month": "2023-03", "state": "brake",
                          "recover_count": 0, "recover_high": 1e12, "last_equity": 1e12})
    out = allocate(book, fs, tmp_path, state=state)
    assert out["state"]["book"]["monthly_loss_off"] is False
    assert out["state"]["book"]["state"] == "brake"


def test_for_session_weekend_and_holiday():
    # Sat 2026-09-05 → skip Sunday + Labor Day Mon 2026-09-07 → Tue 2026-09-08
    assert next_session_on_or_after("2026-09-05") == pd.Timestamp("2026-09-08")
    assert str(derive_for_session({}, ROOT, override="2026-09-06").date()) == "2026-09-08"


def test_committed_book_yaml_never_live():
    book = load_book(ROOT / "book.yaml")
    # CI has no PyYAML — the indent fallback must still see all three sleeves.
    fallback = _simple_yaml((ROOT / "book.yaml").read_text())
    for parsed in (book, fallback):
        assert set(parsed["sleeves"]) == {"v221_mnq", "vwap_options", "pullback_bb"}
        for sid, cfg in parsed["sleeves"].items():
            assert cfg["status"] in {"paper", "shadow", "off"}, sid
            assert cfg["status"] != "live"
        assert parsed["sleeves"]["v221_mnq"]["unit_size"] == 2
        assert parsed["sleeves"]["v221_mnq"]["regime_rules"]["half_in_states"] == [2]
        assert parsed["sleeves"]["vwap_options"]["regime_rules"]["half_in_states"] == [1]
        assert parsed["sleeves"]["pullback_bb"]["event_blackout"] == ["FOMC"]


def test_live_dir_missing_is_backtest(tmp_path):
    sleeves = {"a": _cfg("a", live_dir="results/live/a")}
    book = make_world(tmp_path, sleeves, n=80)
    fs = pd.bdate_range("2023-01-02", periods=81)[-1]
    reasons = allocate(book, fs, tmp_path)["instruction"]["sleeves"]["a"]["reasons"]
    assert "pnl_source=backtest" in reasons


def test_cli_dry_run_and_ops(tmp_path):
    sleeves = {"a": _cfg("a")}
    make_world(tmp_path, sleeves, n=80)
    rc = main(["--root", str(tmp_path), "--dry-run", "--for-session", "2023-04-24",
               "--now", "2023-04-23T18:00:00-04:00"])
    assert rc == 0
    assert not (tmp_path / "results" / "book" / "book_instruction.json").exists()
    rc = main(["--root", str(tmp_path), "--for-session", "2023-04-24",
               "--now", "2023-04-23T18:00:00-04:00"])
    assert rc == 0
    assert (tmp_path / "results" / "book" / "book_instruction.json").exists()
    assert main(["--root", str(tmp_path), "--kill"]) == 0
    st = json.loads((tmp_path / "results" / "book" / "book_state.json").read_text())
    assert st["kill_switch"] is True
    assert main(["--root", str(tmp_path), "--unkill"]) == 0
    st = json.loads((tmp_path / "results" / "book" / "book_state.json").read_text())
    assert st["kill_switch"] is False
    st["sleeves"]["a"]["state"] = "brake"
    (tmp_path / "results" / "book" / "book_state.json").write_text(json.dumps(st))
    assert main(["--root", str(tmp_path), "--reset", "a"]) == 0
    st = json.loads((tmp_path / "results" / "book" / "book_state.json").read_text())
    assert st["sleeves"]["a"]["state"] == "half"
    assert main(["--root", str(tmp_path), "--show"]) == 0
    # slice 2 flags must not exist
    with pytest.raises(SystemExit):
        main(["--backtest"])


def test_holdout_stub_unlocked_without_metrics():
    assert holdout_status(None)["unlocked"] is True
    assert holdout_status({"holdout": "locked"})["unlocked"] is False
    spec = __import__("importlib.util", fromlist=["util"]).spec_from_file_location(
        "wsbt_data_probe", ROOT / "wsbt" / "data.py")
    mod = __import__("importlib.util", fromlist=["util"]).module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.holdout_status({"holdout_unlocked": False})["unlocked"] is False
