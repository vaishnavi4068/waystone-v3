"""Book allocator — ERC risk budgets + daily brakes. ALLOCATOR_SPEC.md slice 1.

No network. numpy/pandas only (no scipy). One decision per session, before the open.
CLI from waystone_backtests/:  python ml/allocator.py [--dry-run] [--for-session YYYY-MM-DD] [--show]
                              python ml/allocator.py --reset SLEEVE | --kill | --unkill
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ml.book_io import (  # noqa: E402
    ET, append_history, blank_sleeve, book_paths, default_state, derive_for_session,
    drawdown_pct, format_report, g_regime_of, incubation_months, is_first_session_of_month,
    kpi_ok, load_book, load_equity, load_events, load_kpi, load_regime, load_state,
    naive_date, next_session, r4, read_instruction, round_units, save_json, save_state,
    stitch_live, to_et,
)
from wsbt.data import holdout_status  # noqa: E402

G_DD = {"normal": 1.0, "half": 0.5, "brake": 0.0}


# ── Step 1 / 2: risk + ERC ───────────────────────────────────────────────────
def ewma_sigma(p: pd.Series, halflife: int = 20, lookback: int = 252) -> float:
    x = p.dropna().astype(float).iloc[-lookback:]
    if len(x) < 2:
        return 0.0
    ew = x.ewm(halflife=halflife, adjust=False).std().iloc[-1]
    floor = 0.3 * float(x.std(ddof=1) or 0.0)
    ew = float(ew) if np.isfinite(ew) else 0.0
    return max(ew, floor)


def kelly_k(p: pd.Series, lookback: int = 252) -> float:
    x = p.dropna().astype(float).iloc[-lookback:]
    n = len(x)
    if n < 2:
        return 0.0
    v = float(x.var(ddof=1) or 0.0)
    if v <= 0:
        return 0.0
    return float(x.mean()) / v * (n / (n + 252.0))


def shrink_corr(C: np.ndarray, lam: float) -> np.ndarray:
    n = C.shape[0]
    return (1.0 - lam) * C + lam * np.eye(n)


def is_pd(A: np.ndarray, eps: float = 1e-12) -> bool:
    try:
        return bool(np.linalg.eigvalsh(A).min() > eps)
    except np.linalg.LinAlgError:
        return False


def inverse_vol_weights(sigmas: np.ndarray, Sigma: np.ndarray, target_vol: float) -> np.ndarray:
    inv = 1.0 / np.maximum(sigmas, 1e-12)
    var = float(inv @ Sigma @ inv)
    if var <= 0:
        return np.zeros_like(sigmas)
    return inv * (target_vol / np.sqrt(var))


def solve_erc(sigmas, corr, target_vol, shrink=0.0, max_iter=200, tol=1e-6):
    """w>=0 s.t. w_i (Σw)_i = σ_book²/N. N=1 vol-target; N=2 inverse-vol. Returns w, notes."""
    sigmas = np.asarray(sigmas, float).reshape(-1)
    n = int(sigmas.shape[0])
    notes = []
    if n == 0:
        return np.zeros(0), notes
    if n == 1:
        s = sigmas[0]
        if s <= 0:
            notes.append("sigma=0")
            return np.array([0.0]), notes
        return np.array([target_vol / s]), notes
    C = np.asarray(corr, float).reshape(n, n)
    np.fill_diagonal(C, 1.0)
    Cp = shrink_corr(C, shrink)
    D = np.diag(sigmas)
    Sigma = D @ Cp @ D
    if np.any(sigmas <= 0):
        notes.append("sigma=0")
        return np.zeros(n), notes
    if (not is_pd(Cp)) or (not is_pd(Sigma)):
        notes.append("cov_not_pd")
        return inverse_vol_weights(sigmas, Sigma, target_vol), notes
    if n == 2:
        return inverse_vol_weights(sigmas, Sigma, target_vol), notes
    w = inverse_vol_weights(sigmas, Sigma, target_vol)
    rc = (target_vol ** 2) / n
    for _ in range(max_iter):
        Sw = np.maximum(Sigma @ w, 1e-18)
        w_new = np.maximum(rc / Sw, 0.0)
        if float(np.max(np.abs(w_new - w))) < tol:
            w = w_new
            break
        w = w_new
    return w, notes


def risk_shares(w: np.ndarray, Sigma: np.ndarray) -> np.ndarray:
    rc = w * (Sigma @ w)
    tot = float(w @ Sigma @ w)
    if tot <= 0:
        return np.zeros_like(w)
    return rc / tot


def _erc_on(sigmas, C, target_vol, shrink, idx):
    if len(idx) == 0:
        return np.zeros(0), []
    sub_s = np.asarray(sigmas)[idx]
    sub_c = np.asarray(C)[np.ix_(idx, idx)]
    return solve_erc(sub_s, sub_c, target_vol, shrink=shrink)


def apply_caps(w, sigmas, corr, target_vol, kelly_cap, max_gross, max_share, shrink=0.0):
    """Caps in order, then re-scale once. Returns w_final and stage dict."""
    n = len(w)
    C = np.asarray(corr, float).reshape(n, n)
    Cp = shrink_corr(C, shrink)
    Sigma = np.diag(sigmas) @ Cp @ np.diag(sigmas)
    stages = {"erc": w.copy()}
    # (1) max_risk_share clip + re-solve ERC on the rest
    frozen = {}
    for _ in range(n):
        free = [i for i in range(n) if i not in frozen]
        rem_share = max(0.0, 1.0 - max_share * len(frozen))
        w_f, _ = _erc_on(sigmas, C, target_vol * np.sqrt(rem_share), shrink, free)
        w = np.zeros(n)
        for k, i in enumerate(free):
            w[i] = w_f[k]
        for i, wi in frozen.items():
            w[i] = wi
        sh = risk_shares(w, Sigma)
        if len(free) <= 1 or sh[free].max() <= max_share + 1e-8:
            break
        i = int(free[int(np.argmax(sh[free]))])
        if sh[i] <= 0:
            break
        w[i] *= max_share / sh[i]
        frozen[i] = float(w[i])
    stages["share"] = w.copy()
    # (2) kelly_fraction * k
    cap = np.asarray(kelly_cap, float).reshape(-1)
    w = np.minimum(np.maximum(w, 0.0), np.maximum(cap, 0.0))
    stages["kelly"] = w.copy()
    # (3) max_gross_multiplier
    s = float(w.sum())
    if s > max_gross > 0:
        w = w * (max_gross / s)
    stages["gross"] = w.copy()
    # re-scale once to target book vol
    var = float(w @ Sigma @ w)
    if var > 1e-18 and target_vol > 0:
        w = w * (target_vol / np.sqrt(var))
    stages["final"] = w.copy()
    return w, stages


# ── Step 4 / 6: brakes + hysteresis ──────────────────────────────────────────
def step_brake(st: dict, dd: float, equity: float, dd_half: float, dd_off: float,
               recover: int, reset: bool = False) -> tuple[dict, float]:
    """Persisted normal|half|brake. Recovery resets on a new low."""
    st = dict(st)
    s = st.get("state") or "normal"
    if reset and s == "brake":
        s, st["recover_count"] = "half", 0
    if s == "normal" and dd >= dd_half:
        s = "half"
    if s == "half" and dd >= dd_off:
        s, st["recover_count"], st["recover_high"] = "brake", 0, equity
    if s == "half" and dd < dd_half / 2.0:
        s = "normal"
    if s == "brake":
        last, high = st.get("last_equity"), st.get("recover_high")
        if last is not None and equity < float(last) - 1e-12:
            st["recover_count"] = 0
            st["recover_high"] = equity if high is None else min(float(high), equity)
        elif high is None or equity > float(high) + 1e-12:
            st["recover_count"] = int(st.get("recover_count") or 0) + 1
            st["recover_high"] = equity
        if int(st.get("recover_count") or 0) >= recover:
            s, st["recover_count"] = "half", 0
    st["state"], st["last_equity"] = s, equity
    return st, G_DD[s]


def apply_hysteresis(m_raw, prev, first_of_month, hyst_pct, gate_changed) -> float:
    if prev is None or first_of_month or gate_changed:
        return float(m_raw)
    prev = float(prev)
    if abs(prev) < 1e-12:
        return float(m_raw)
    if abs(float(m_raw) - prev) / abs(prev) * 100.0 > float(hyst_pct):
        return float(m_raw)
    return prev


def allocate(book: dict, for_session, root: Path, state: dict | None = None,
             now=None, data_dir: Path | None = None) -> dict:
    """Run steps 0–7. Returns instruction, new state, report lines, warnings (no writes)."""
    root = Path(root)
    data_dir = Path(data_dir) if data_dir else root / "data"
    nav = float(book.get("nav") or 100000)
    target_pct = float(book.get("target_daily_vol_pct") or 0.8)
    sig_book = target_pct / 100.0 * nav
    shrink = float(book.get("shrink_corr") or 0.3)
    hl = int(book.get("vol_halflife_days") or 20)
    cw = int(book.get("corr_window_days") or 120)
    hyst = float((book.get("rebalance") or {}).get("hysteresis_pct") or 20)
    max_share = float(book.get("max_risk_share") or 0.45)
    max_gross = float(book.get("max_gross_multiplier") or 1.5)
    kf = float(book.get("kelly_fraction") or 0.5)
    regime_name = (book.get("regime") or {}).get("name") or "spx"
    bb = book.get("book_brake") or {}
    for_session = naive_date(for_session)
    now = to_et(now) if now is not None else pd.Timestamp.now(tz=ET)
    state = state or default_state(book)
    warnings = []

    r_state, r_reason = load_regime(data_dir, regime_name, for_session)
    events = load_events(data_dir, for_session)
    first_mo = is_first_session_of_month(for_session)

    # month roll: monthly_loss_off clears; book DD brake does not
    bst = dict(state.get("book") or {})
    mo = f"{for_session.year:04d}-{for_session.month:02d}"
    if bst.get("month") and bst["month"] != mo:
        bst["monthly_loss_off"] = False
    bst["month"] = mo
    if state.get("kill_switch"):
        warnings.append("kill_switch")

    prepared, pnl_panel = [], {}
    for sid, cfg in (book.get("sleeves") or {}).items():
        reasons, cfg = [], dict(cfg)
        yaml_st = cfg.get("status") or "shadow"
        rdir = root / cfg["results_dir"] if cfg.get("results_dir") else None
        if yaml_st == "off" or rdir is None or not (rdir / "equity.csv").exists():
            prepared.append((sid, cfg, blank_sleeve(cfg, "off", ["results_missing"]), None, 0.0, 0.0))
            continue
        bt = load_equity(rdir / "equity.csv", nav)
        live_p, src = None, "backtest"
        if cfg.get("live_dir"):
            lp = root / cfg["live_dir"] / "equity.csv"
            if lp.exists():
                live_p = load_equity(lp, nav)
        eq, src = stitch_live(bt, live_p)
        eq = eq[eq.index < for_session]
        reasons.append(f"pnl_source={src}")
        n = len(eq)
        kpi = load_kpi(rdir)
        ok, kpi_bad = kpi_ok(kpi)
        hold = holdout_status(kpi if kpi else None, rdir)
        inc = incubation_months(eq.index.min() if n else None, for_session)
        min_inc = int(cfg.get("min_incubation_months") or 0)
        # never promote; demote live→paper→shadow
        if yaml_st == "live" and ok and hold.get("unlocked", True) and inc >= min_inc and n >= 60:
            status = "live"
        elif yaml_st in ("live", "paper"):
            status = "paper"
            if yaml_st == "live":
                reasons += (kpi_bad or (["holdout_locked"] if not hold.get("unlocked", True) else [])
                            + (["incubation"] if inc < min_inc else []))
        else:
            status = "shadow"
        if n < 60:
            status = "paper" if yaml_st in ("live", "paper") else "shadow"
            reasons.append("history<60")
        if not hold.get("unlocked", True) and yaml_st == "live":
            status = "paper"
            reasons.append("holdout_locked")
        pnl = eq["daily_pnl"] if n and "daily_pnl" in eq.columns else pd.Series(dtype=float)
        ret = eq["daily_ret"] if n and "daily_ret" in eq.columns else pnl / nav
        sig = ewma_sigma(pnl, hl) if n >= 2 else 0.0
        kk = kelly_k(ret, 252) if n >= 2 else 0.0
        if n >= 60 and sig <= 0 and status in ("live", "paper"):
            status, reasons = "shadow", reasons + ["sigma=0"]
        row = blank_sleeve(cfg, status, reasons, shadow=status in ("shadow", "off"))
        prepared.append((sid, cfg, row, eq, sig, kk))
        if status in ("live", "paper") and n >= 60 and sig > 0:
            pnl_panel[sid] = pnl

    eligible = [i for i, (sid, *_) in enumerate(prepared) if sid in pnl_panel]
    erc_notes = []
    m_raw = {}
    rw = {}
    if not eligible:
        for sid, cfg, row, *_ in prepared:
            if row["status"] != "off":
                row["status"], row["shadow"] = "shadow", True
                if "no_eligible" not in row["reasons"]:
                    row["reasons"].append("no_eligible")
        book_brake_name = "no_eligible_sleeves"
    else:
        ids = [prepared[i][0] for i in eligible]
        sigmas = np.array([prepared[i][4] for i in eligible], float)
        kcaps = np.array([kf * prepared[i][5] for i in eligible], float)
        panel = pd.concat({s: pnl_panel[s] for s in ids}, axis=1).iloc[-cw:]
        C = panel.corr().to_numpy(float)
        C = np.nan_to_num(C, nan=0.0)
        np.fill_diagonal(C, 1.0)
        w, erc_notes = solve_erc(sigmas, C, sig_book, shrink=shrink)
        w, stages = apply_caps(w, sigmas, C, sig_book, kcaps, max_gross, max_share, shrink)
        Cp = shrink_corr(C, shrink)
        Sigma = np.diag(sigmas) @ Cp @ np.diag(sigmas)
        sh = risk_shares(w, Sigma)
        for i, sid in enumerate(ids):
            m_raw[sid] = float(w[i])
            rw[sid] = float(sh[i])
            prepared[eligible[i]][2]["reasons"].extend(erc_notes)
        book_brake_name = None

    # sleeve gates + hysteresis (g_book applied after book brake)
    g_book_prev = 0.0 if state.get("kill_switch") or bst.get("monthly_loss_off") else G_DD.get(bst.get("state") or "normal", 1.0)
    sized_pnl = []
    for sid, cfg, row, eq, sig, kk in prepared:
        ss = dict((state.get("sleeves") or {}).get(sid) or {})
        g_reg = g_regime_of(cfg.get("regime_rules") or {}, r_state)
        g_ev = 0.0 if any(e in set(cfg.get("event_blackout") or []) for e in events) else 1.0
        eq_s = eq["equity"] if eq is not None and not eq.empty and "equity" in eq.columns else pd.Series(dtype=float)
        last_eq = float(eq_s.iloc[-1]) if len(eq_s) else 0.0
        dd = drawdown_pct(eq_s, nav) if len(eq_s) else 0.0
        ss, g_dd = step_brake(ss, dd, last_eq, float(cfg.get("dd_half_pct") or 6),
                              float(cfg.get("dd_off_pct") or 10), int(cfg.get("recover_sessions") or 10))
        if r_reason:
            row["reasons"].append(r_reason)
        if g_reg != 1:
            row["reasons"].append(f"g_regime={g_reg}")
        if g_ev == 0:
            row["reasons"].append("event_blackout")
        if g_dd != 1:
            row["reasons"].append(f"dd={ss['state']}")
        gate_chg = any(abs(float(ss.get(k) or 1) - g) > 1e-12 for k, g in
                       (("g_regime", g_reg), ("g_event", g_ev), ("g_dd", g_dd)))
        raw = m_raw.get(sid, 0.0)
        raw = apply_hysteresis(raw, ss.get("m_raw"), first_mo, hyst, gate_chg)
        ss.update({"m_raw": raw, "g_regime": g_reg, "g_event": g_ev, "g_dd": g_dd})
        row["_raw"], row["_g"] = raw, (g_reg, g_ev, g_dd)
        row["_ss"] = ss
        if row["status"] in ("live", "paper") and eq is not None and not eq.empty:
            sized_pnl.append(eq["daily_pnl"].astype(float) * raw * g_reg * g_ev * g_dd)
        if sid in rw:
            row["risk_weight"] = r4(rw[sid]) or 0.0
        if row["status"] == "live" and len(eligible) == 1:
            row["risk_weight"] = 1.0
        if len(eligible) == 1 and sid in pnl_panel:
            row["risk_weight"] = 1.0

    # book DD on live/paper at applied sizes
    book_path = pd.concat(sized_pnl, axis=1).sum(axis=1) if sized_pnl else pd.Series(dtype=float)
    book_eq = book_path.cumsum() if len(book_path) else pd.Series(dtype=float)
    book_dd = drawdown_pct(book_eq, nav) if len(book_eq) else 0.0
    last_b = float(book_eq.iloc[-1]) if len(book_eq) else 0.0
    rec = int((book.get("sleeves") or {}).get(next(iter(book.get("sleeves") or {}), ""), {}).get("recover_sessions") or 10)
    bst, g_dd_book = step_brake(bst, book_dd, last_b, float(bb.get("dd_half_pct") or 6),
                                float(bb.get("dd_off_pct") or 10), rec)
    month_pnl = float(book_path[book_path.index.to_period("M") == for_session.to_period("M")].sum()) if len(book_path) else 0.0
    if month_pnl / nav * 100.0 <= -float(bb.get("monthly_loss_off_pct") or 5):
        bst["monthly_loss_off"] = True
    g_book = 0.0 if state.get("kill_switch") or bst.get("monthly_loss_off") else g_dd_book
    if book_brake_name is None:
        book_brake_name = "monthly_loss_off" if bst.get("monthly_loss_off") else bst.get("state") or "normal"
    if state.get("kill_switch"):
        book_brake_name = "kill_switch"

    sleeves_out, hist, new_sstate = {}, [], {}
    real20 = float(book_path.iloc[-20:].std(ddof=1) / nav * 100.0) if len(book_path) >= 2 else 0.0
    for sid, cfg, row, eq, sig, kk in prepared:
        g_reg, g_ev, g_dd = row.pop("_g")
        raw = row.pop("_raw")
        ss = row.pop("_ss")
        ss["g_book"] = g_book
        m = raw * g_reg * g_ev * g_dd * g_book
        ut = cfg.get("unit_type") or "contracts"
        units = round_units(m, float(cfg.get("unit_size") or 1), float(cfg.get("unit_step") or 1), ut)
        if units == 0 and row["status"] not in ("off",):
            row["shadow"] = True
            if m != 0 and "rounds_to_zero" not in row["reasons"]:
                row["reasons"].append("rounds_to_zero")
        if ss.get("state") == "brake" and row["status"] not in ("off",):
            row["status"] = "brake"
            row["shadow"] = True
        if book_brake_name == "no_eligible_sleeves" and row["status"] != "off":
            row["status"], row["shadow"], units, m = "shadow", True, 0, 0.0
        row.update({"multiplier": r4(m) or 0.0, "units": units, "risk_weight": r4(row.get("risk_weight") or 0) or 0.0})
        if not row["reasons"]:
            row["reasons"] = ["erc"]
        ss.update({"last_units": units, "last_multiplier": row["multiplier"]})
        new_sstate[sid] = ss
        sleeves_out[sid] = {k: row[k] for k in ("status", "multiplier", "units", "unit_type", "risk_weight", "reasons", "shadow")}
        hist.append({"generated_at": now.isoformat(), "for_session": str(for_session.date()), "sleeve": sid,
                     **{k: sleeves_out[sid][k] for k in ("status", "multiplier", "units", "risk_weight", "shadow")},
                     "reasons": "|".join(sleeves_out[sid]["reasons"])})

    vu = pd.Timestamp(for_session).tz_localize(ET).replace(hour=16, minute=0, second=0, microsecond=0)
    tomorrow = next_session(now.tz_convert(ET).tz_localize(None).normalize() if now.tzinfo else naive_date(now))
    # "tomorrow" = next session after today; warn if derived for_session is not that
    if for_session.normalize() != naive_date(tomorrow):
        warnings.append(f"for_session={for_session.date()} is not tomorrow ({naive_date(tomorrow).date()})")
    instr = {
        "schema": 1,
        "generated_at": now.isoformat(),
        "for_session": str(for_session.date()),
        "valid_until": vu.isoformat(),
        "nav": r4(nav),
        "book": {
            "target_daily_vol_pct": r4(target_pct),
            "realised_daily_vol_pct_20d": r4(real20) or 0.0,
            "drawdown_pct": r4(book_dd) or 0.0,
            "brake": book_brake_name,
            "kill_switch": bool(state.get("kill_switch")),
            "regime_state": int(r_state),
            "regime_name": regime_name,
            "events_today": events,
        },
        "sleeves": sleeves_out,
    }
    new_state = {"kill_switch": bool(state.get("kill_switch")), "book": bst, "sleeves": new_sstate}
    return {"instruction": instr, "state": new_state, "history": hist, "warnings": warnings,
            "for_session": for_session}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Book allocator (slice 1): ERC + brakes + instruction JSON")
    ap.add_argument("--dry-run", action="store_true", help="print report, write nothing")
    ap.add_argument("--for-session", metavar="YYYY-MM-DD", help="target session (weekend/holiday → next)")
    ap.add_argument("--show", action="store_true", help="print current instruction and exit")
    ap.add_argument("--reset", metavar="SLEEVE", help="human reset: sleeve brake → half")
    ap.add_argument("--kill", action="store_true")
    ap.add_argument("--unkill", action="store_true")
    ap.add_argument("--book", type=Path, default=None)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--now", default=None, help="override clock (ISO, tests)")
    a = ap.parse_args(argv)
    root = Path(a.root)
    book = load_book(a.book or (root / "book.yaml"))
    ip, sp, hp, _ = book_paths(root, a.out_dir)
    now = to_et(a.now) if a.now else pd.Timestamp.now(tz=ET)
    state = load_state(sp, book)

    if a.show:
        cur = read_instruction(ip, now=now, book=book, state_path=sp)
        print(__import__("json").dumps(cur, indent=2, default=str))
        return 0
    if a.reset or a.kill or a.unkill:
        if a.kill:
            state["kill_switch"] = True
        if a.unkill:
            state["kill_switch"] = False
        if a.reset:
            ss = state.setdefault("sleeves", {}).setdefault(a.reset, {})
            if ss.get("state") == "brake":
                ss["state"], ss["recover_count"] = "half", 0
            else:
                ss["state"] = ss.get("state") or "normal"
        try:
            save_state(sp, state)
        except OSError:
            print("ERROR could not write book_state.json", file=sys.stderr)
            return 2
        print(f"state written {sp}  kill={state['kill_switch']}" + (f"  reset={a.reset}" if a.reset else ""))
        return 0

    for_session = derive_for_session(book, root, a.for_session, now)
    out = allocate(book, for_session, root, state=state, now=now, data_dir=root / "data")
    print(format_report(out))
    if a.dry_run:
        return 0
    try:
        save_json(ip, out["instruction"])
        save_state(sp, out["state"])
        append_history(hp, out["history"])
    except OSError:
        print("ERROR could not write instruction", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
