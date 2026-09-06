#!/usr/bin/env python3
"""Three sentiment backtests, each measured with the same KPI set and trial log as everything else.

  --mode shock        Per-name tone shock.  At the close of day D, if shock_z(D) >= z (tone jumped vs its own 20-day
                      history) and, optionally, relative volume >= rvol (the market noticed), go LONG at D+1's open
                      (SHORT on shock_z <= -z with --both-sides).  Stop = stop_atr x ATR(14) from entry, exit at the
                      close after `hold` sessions.  Equal $ per name, up to max_positions concurrent.
  --mode event-filter Stand-aside overlay.  Take a base trade list (any sleeve's primary_trades.csv / trades.csv) and
                      drop trades entered within `quiet` sessions after a negative binary event in that name
                      (regulatory, litigation, guidance cut, downgrade, management exit; confidence >= c).
                      Reports base vs filtered on identical trades otherwise.
  --mode macro        Contrarian macro tilt on the index.  Fear & Greed below its rolling 252-day 15th percentile
                      (and, when available, AAII bull-bear spread / put-call in their fear tails) -> long; above the
                      85th percentile -> flat (or short with --short-greed).  Percentiles are computed on trailing
                      data only.  Compared against buy-and-hold.

  python ml/sentiment/sentiment_backtest.py --mode shock --synthetic --plant 0.4     # positive control: a planted edge is caught
  python ml/sentiment/sentiment_backtest.py --mode shock --synthetic                 # no edge planted: gates should FAIL
  python ml/sentiment/sentiment_backtest.py --mode shock --symbols AAPL NVDA AMD --grid
  python ml/sentiment/sentiment_backtest.py --mode event-filter --trades-csv results/ml_meta_vwap/primary_trades.csv
  python ml/sentiment/sentiment_backtest.py --mode macro --symbol SPY
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from wsbt import costs, data as D, metrics as M  # noqa: E402
from wsbt.engine import TradeSpec, simulate_positions, simulate_trades  # noqa: E402
from wsbt.report import RESULTS  # noqa: E402
from ml import common as C  # noqa: E402
from ml.cv import cscv_pbo  # noqa: E402
from ml.evaluate import daily_pnl, log_trial, sharpe_of, to_et_naive, trial_count, trial_sr_variance  # noqa: E402
from ml.kpi_export import build_banner, build_sections, compute_kpis, inject  # noqa: E402
from ml.sentiment.finbert_score import daily_sentiment, lexicon_score, synthetic_news  # noqa: E402
from ml.sentiment.event_classifier import NEGATIVE_BINARY, classify_file  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# mode: shock
# ─────────────────────────────────────────────────────────────────────────────
def shock_specs(bars: pd.DataFrame, sent: pd.DataFrame, z: float, rvol: float, hold: int, stop_atr: float, both: bool) -> list[TradeSpec]:
    b = bars.sort_index()
    s = sent.reindex(b.index)
    tr = pd.concat([b["high"] - b["low"], (b["high"] - b["close"].shift(1)).abs(), (b["low"] - b["close"].shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    rv = b["volume"] / b["volume"].rolling(20).mean()
    specs = []
    idx = b.index
    for i in range(20, len(b) - 1):
        zs = s["shock_z"].iloc[i]
        if pd.isna(zs) or pd.isna(atr.iloc[i]):
            continue
        side = 1 if zs >= z else (-1 if (both and zs <= -z) else 0)
        if side == 0 or (rvol > 0 and (pd.isna(rv.iloc[i]) or rv.iloc[i] < rvol)):
            continue
        entry_ref = b["close"].iloc[i]
        specs.append(TradeSpec(date=idx[i + 1], side=side, entry="open", stop=entry_ref - side * stop_atr * atr.iloc[i],
                               max_hold=hold, tag="shock", meta={"shock_z": round(float(zs), 2), "rvol": round(float(rv.iloc[i]), 2)}))
    return specs


def run_shock(bars_by: dict, sent_by: dict, z, rvol, hold, stop_atr, both, cost, per_name, nav):
    rets, trades = [], []
    for sym, b in bars_by.items():
        if sym not in sent_by:
            continue
        specs = shock_specs(b, sent_by[sym], z, rvol, hold, stop_atr, both)
        if not specs:
            continue
        dr, tr, _ = simulate_trades(b, specs, cost, {"notional": per_name}, nav, max_concurrent=1)
        rets.append(dr)
        if len(tr):
            tr["symbol"] = sym
            trades.append(tr)
    if not rets:
        return pd.Series(dtype=float), pd.DataFrame()
    R = pd.concat(rets, axis=1).fillna(0.0).sum(axis=1)
    T = pd.concat(trades) if trades else pd.DataFrame()
    return R, T


# ─────────────────────────────────────────────────────────────────────────────
# mode: event-filter
# ─────────────────────────────────────────────────────────────────────────────
def event_filter(trades: pd.DataFrame, events_by: dict, quiet: int, conf: float) -> pd.DataFrame:
    t = trades.copy()
    t["_d"] = to_et_naive(t["entry_time"]).dt.normalize()
    keep = np.ones(len(t), dtype=bool)
    reasons = [""] * len(t)
    for i, (sym, d) in enumerate(zip(t["symbol"], t["_d"])):
        ev = events_by.get(sym)
        if ev is None or not len(ev):
            continue
        bad = ev[(ev["type"].isin(NEGATIVE_BINARY)) & (ev["confidence"] >= conf)]
        if not len(bad):
            continue
        sd = pd.to_datetime(bad["sdate"])
        recent = sd[(sd <= d) & (sd >= d - pd.Timedelta(days=int(quiet * 1.45) + 1))]
        if len(recent):
            keep[i] = False
            reasons[i] = bad.loc[sd.isin(recent), "type"].iloc[-1]
    t["allowed"] = keep
    t["blocked_by"] = reasons
    return t.drop(columns=["_d"])


# ─────────────────────────────────────────────────────────────────────────────
# mode: macro
# ─────────────────────────────────────────────────────────────────────────────
def macro_target(bars: pd.DataFrame, macro: dict, lo_pct=0.15, hi_pct=0.85, short_greed=False, window=252) -> pd.Series:
    idx = bars.index
    votes = pd.DataFrame(index=idx)
    for k, s in macro.items():
        if k not in ("fng", "aaii_spread", "pcr"):
            continue
        x = pd.Series(s).sort_index().reindex(idx).ffill()
        lo = x.shift(1).rolling(window, min_periods=120).quantile(lo_pct)
        hi = x.shift(1).rolling(window, min_periods=120).quantile(hi_pct)
        if k == "pcr":                              # high put/call = fear
            votes[k] = np.where(x >= hi, 1, np.where(x <= lo, -1, 0))
        else:
            votes[k] = np.where(x <= lo, 1, np.where(x >= hi, -1, 0))
    if votes.shape[1] == 0:
        return pd.Series(0.0, index=idx)
    v = votes.mean(axis=1)                          # +1 = every gauge in fear
    tgt = pd.Series(0.0, index=idx)
    tgt[v >= 0.5] = 1.0
    if short_greed:
        tgt[v <= -0.5] = -0.5
    return tgt


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["shock", "event-filter", "macro"], required=True)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--plant", type=float, default=0.0, help="synthetic: correlation between latent tone and next-day return")
    ap.add_argument("--symbols", nargs="*")
    ap.add_argument("--n-symbols", type=int, default=12)
    ap.add_argument("--days", type=int, default=600)
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--z", type=float, default=2.0)
    ap.add_argument("--rvol", type=float, default=1.5, help="0 = no volume confirmation")
    ap.add_argument("--hold", type=int, default=3)
    ap.add_argument("--stop-atr", type=float, default=1.5)
    ap.add_argument("--both-sides", action="store_true")
    ap.add_argument("--max-positions", type=int, default=5)
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--trades-csv")
    ap.add_argument("--quiet", type=int, default=3, help="event-filter: sessions to stand aside after a negative event")
    ap.add_argument("--conf", type=float, default=0.5)
    ap.add_argument("--short-greed", action="store_true")
    ap.add_argument("--nav", type=float, default=100_000.0)
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--dashboard")
    ap.add_argument("--flags", default="")
    ap.add_argument("--name")
    a = ap.parse_args()
    flags = {f.strip(): True for f in a.flags.split(",") if f.strip()}
    cost = costs.US_STOCK

    # ═══════════════════════════════ shock ═══════════════════════════════
    if a.mode == "shock":
        name = a.name or ("ml_sent_shock" + ("_syn" if a.synthetic else ""))
        family = name
        if a.synthetic:
            syms = a.symbols or [f"S{i:02d}" for i in range(a.n_symbols)]
            latent = {}
            news = synthetic_news(syms, a.days, seed=a.seed, latent=latent)
            rng = np.random.default_rng(a.seed)
            bars_by, sent_by = {}, {}
            for sym in syms:
                tone = latent[sym]
                base = D.synthetic_daily(n=a.days, start=str(tone.index[0].date()), s0=float(rng.uniform(30, 300)), seed=a.seed * 7 + hash(sym) % 97, regime=False)
                # plant: next-day return += plant * tone_z * daily vol  (tone at close D moves D+1)
                r = np.log(base["close"]).diff().fillna(0.0)
                tz = ((tone - tone.mean()) / tone.std()).reindex(base.index).fillna(0.0).shift(1).fillna(0.0)
                r2 = r + a.plant * tz * r.std()
                close = base["close"].iloc[0] * np.exp(np.cumsum(r2))
                scale = close / base["close"]
                b = base.copy()
                for c in ("open", "high", "low", "close"):
                    b[c] = base[c] * scale
                # volume responds to |tone| so the RVOL confirmation has something to confirm
                b["volume"] = (base["volume"] * (1 + 0.8 * tz.abs())).round()
                bars_by[sym] = b
                scores = np.array([lexicon_score(t) for t in news[sym]["title"]])
                sent_by[sym] = daily_sentiment(news[sym], scores)
            note = f"SYNTHETIC — planted tone/return correlation {a.plant}"
        else:
            syms = a.symbols or D.load_symbol_list(max_symbols=a.n_symbols)
            bars_by, sent_by = {}, {}
            for sym in syms:
                s = C.load_sentiment(sym)
                if s is None:
                    continue
                try:
                    bars_by[sym] = D.load_daily(sym)
                    sent_by[sym] = s
                except Exception:
                    pass
            if not bars_by:
                raise SystemExit("no names with both data/daily/<SYM>.csv and data/sentiment/<SYM>_daily.csv")
            note = ""
        per_name = a.nav / a.max_positions
        idx = sorted(set().union(*[set(b.index) for b in bars_by.values()]))
        grid = {"z": [1.5, 2.0, 2.5], "hold": [2, 3, 5], "rvol": [0.0, 1.5]} if a.grid else {"z": [a.z], "hold": [a.hold], "rvol": [a.rvol]}
        mat, trials = {}, []
        for z, hold, rv in itertools.product(grid["z"], grid["hold"], grid["rvol"]):
            R, T = run_shock(bars_by, sent_by, z, rv, hold, a.stop_atr, a.both_sides, cost, per_name, a.nav)
            R = R.reindex(pd.DatetimeIndex(idx)).fillna(0.0)
            mat[(z, hold, rv)] = R
            sr = sharpe_of(R)
            log_trial(family, {"z": z, "hold": hold, "rvol": rv, "stop_atr": a.stop_atr, "both": a.both_sides},
                      {"sharpe": round(sr, 3), "trades": len(T), "net_pnl": round(float(R.sum() * a.nav), 2), "sr_per_period": M.sharpe_per_period(R)})
            trials.append({"z": z, "hold": hold, "rvol": rv, "sharpe": round(sr, 3), "trades": len(T), "net_pnl": round(float(R.sum() * a.nav), 0)})
        tab = pd.DataFrame(trials)
        pbo = cscv_pbo(pd.DataFrame(mat).to_numpy(), n_blocks=8) if len(mat) > 1 else None
        if a.grid:
            Rm = pd.DataFrame({f"{k}": v for k, v in mat.items()})
            half = Rm.index[len(Rm) // 2]
            sel = Rm[Rm.index < half].apply(sharpe_of).idxmax()
            z, hold, rv = eval(sel)
            print("  grid (search log):"); print(tab.sort_values("sharpe", ascending=False).to_string(index=False))
            print(f"  chosen on first half: z={z} hold={hold} rvol={rv}  -> reported on the second half; PBO={pbo['pbo'] if pbo else None}")
            R, T = run_shock(bars_by, sent_by, z, rv, hold, a.stop_atr, a.both_sides, cost, per_name, a.nav)
            R = R.reindex(pd.DatetimeIndex(idx)).fillna(0.0)
            R = R[R.index >= half]
            T = T[pd.to_datetime(T["entry_date"]) >= half] if len(T) else T
        else:
            z, hold, rv = a.z, a.hold, a.rvol
            R, T = run_shock(bars_by, sent_by, z, rv, hold, a.stop_atr, a.both_sides, cost, per_name, a.nav)
            R = R.reindex(pd.DatetimeIndex(idx)).fillna(0.0)
        T = T.rename(columns={"entry_date": "entry_time", "exit_date": "exit_time"}) if len(T) else T
        if len(T):
            T["cost"] = 2 * cost.commission * T["units"] + 2 * T["entry"] * cost.slippage_bps / 1e4 * T["units"]
        stats = M.summary(R, T)
        n_trials = trial_count(family)
        extra = {"mode": "shock", "z": z, "hold": hold, "rvol": rv, "n_names": len(bars_by), "n_trials": n_trials, "family": family,
                 "trial_sr_var": trial_sr_variance(family), "pbo": pbo["pbo"] if pbo else None, "attrib": 100.0, "synthetic": a.synthetic,
                 "notes": [f"Tone-shock sleeve on {len(bars_by)} names: z>={z}, RVOL>={rv}, hold {hold}, stop {a.stop_atr} ATR; {n_trials} trials logged.", note]}
        _finish(name, a, R, T, stats, extra, flags, note)

    # ═══════════════════════════════ event-filter ═══════════════════════════════
    elif a.mode == "event-filter":
        name = a.name or ("ml_sent_eventfilter" + ("_syn" if a.synthetic else ""))
        family = name
        if not a.trades_csv:
            raise SystemExit("--trades-csv required (a sleeve's primary_trades.csv or trades.csv with symbol, entry_time, exit_time, pnl)")
        tr = pd.read_csv(a.trades_csv)
        syms = sorted(tr["symbol"].astype(str).unique())
        if a.synthetic:
            news = synthetic_news(syms, a.days, start=str(to_et_naive(tr["entry_time"]).min().date()), seed=a.seed)
            events_by = {s: classify_file(news[s]) for s in syms}
            note = "SYNTHETIC events"
        else:
            events_by = {}
            for s in syms:
                p = C.DATA / "events" / f"{D._safe_name(s)}.csv"
                if p.exists():
                    events_by[s] = pd.read_csv(p)
            note = ""
            if not events_by:
                raise SystemExit("no data/events/<SYM>.csv — run fetch_free_sentiment.py + event_classifier.py first")
        f = event_filter(tr, events_by, a.quiet, a.conf)
        kept = f[f["allowed"]]
        blocked = f[~f["allowed"]]
        idx = pd.DatetimeIndex(daily_pnl(tr).index)
        d_base, d_kept = daily_pnl(tr, index=idx), daily_pnl(kept, index=idx)
        cmp = {"base": {"trades": len(tr), "net": round(float(tr["pnl"].sum()), 0), "sharpe": round(sharpe_of(d_base / a.nav), 2)},
               "filtered": {"trades": len(kept), "net": round(float(kept["pnl"].sum()), 0), "sharpe": round(sharpe_of(d_kept / a.nav), 2)},
               "blocked": {"trades": len(blocked), "net": round(float(blocked["pnl"].sum()), 0),
                           "hit": round(float((blocked["pnl"] > 0).mean()), 3) if len(blocked) else None,
                           "by_type": blocked["blocked_by"].value_counts().to_dict()}}
        print(json.dumps(cmp, indent=1))
        log_trial(family, {"quiet": a.quiet, "conf": a.conf}, {"sharpe": cmp["filtered"]["sharpe"], "trades": len(kept),
                  "net_pnl": cmp["filtered"]["net"], "sr_per_period": M.sharpe_per_period(d_kept / a.nav)})
        R = d_kept / a.nav
        stats = M.summary(R, kept)
        extra = {"mode": "event-filter", "quiet": a.quiet, "conf": a.conf, "compare": cmp, "n_trials": trial_count(family), "family": family,
                 "trial_sr_var": trial_sr_variance(family), "synthetic": a.synthetic,
                 "notes": [f"Event stand-aside overlay: {len(blocked)} of {len(tr)} trades blocked ({a.quiet} sessions after negative events, conf>={a.conf}).",
                           f"Blocked trades' P&L {cmp['blocked']['net']} — the overlay earns its keep only if this is negative.", note]}
        _finish(name, a, R, kept, stats, extra, flags, note)

    # ═══════════════════════════════ macro ═══════════════════════════════
    else:
        name = a.name or (f"ml_sent_macro_{D._safe_name(a.symbol).lower()}" + ("_syn" if a.synthetic else ""))
        family = name
        if a.synthetic:
            bars = D.synthetic_daily(n=2500, seed=a.seed)
            macro = C.load_macro(True, bars.index, a.seed)
            note = "SYNTHETIC macro gauges"
        else:
            bars = D.load_daily(a.symbol)
            macro = C.load_macro()
            note = ""
            if not macro:
                raise SystemExit("no data/macro/*.csv — run fetch_free_sentiment.py fng / aaii / pcr")
        tgt = macro_target(bars, macro, short_greed=a.short_greed)
        dr, tr, eq = simulate_positions(bars, tgt, costs.US_ETF, {"notional": a.nav}, a.nav)
        bh = np.log(bars["close"]).diff().fillna(0.0)
        stats = M.summary(dr, tr)
        print(f"  exposure {float((tgt != 0).mean()):.2%} of days; gauges: {list(macro)}")
        print(M.format_report(name, stats, {"buy_and_hold_sharpe": M.summary(bh).get("sharpe"), "buy_and_hold_maxdd": M.summary(bh).get("max_drawdown_pct")}))
        log_trial(family, {"lo": 0.15, "hi": 0.85, "short_greed": a.short_greed}, {"sharpe": stats.get("sharpe"), "trades": len(tr),
                  "net_pnl": stats.get("net_pnl"), "sr_per_period": M.sharpe_per_period(dr)})
        tr = tr.rename(columns={"entry_date": "entry_time", "exit_date": "exit_time"}) if len(tr) else tr
        extra = {"mode": "macro", "gauges": list(macro), "buy_and_hold_sharpe": M.summary(bh).get("sharpe"), "n_trials": trial_count(family),
                 "family": family, "trial_sr_var": trial_sr_variance(family), "attrib": 100.0, "synthetic": a.synthetic,
                 "notes": [f"Contrarian macro tilt on {a.symbol}: long when the gauges are in their trailing fear tail (15th pct), flat/short in greed.", note]}
        _finish(name, a, dr, tr, stats, extra, flags, note, quiet_report=True)


def _finish(name, a, R, T, stats, extra, flags, note, quiet_report=False):
    out_dir = RESULTS / name
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metrics.json").write_text(json.dumps({"strategy": name, "synthetic": a.synthetic, "params": {**vars(a), "nav": a.nav},
                                                      "stats": stats, "extra": extra}, indent=1, default=str))
    if len(T):
        T.to_csv(out_dir / "trades.csv", index=False)
    pd.DataFrame({"equity": M.equity_from_returns(R, a.nav), "daily_ret": R, "daily_pnl": R * a.nav}).to_csv(out_dir / "equity.csv")
    kp = compute_kpis(T, R * a.nav, a.nav, name=name, n_trials=extra.get("n_trials"), trial_sr_var=extra.get("trial_sr_var"), family=extra.get("family"),
                      pbo=extra.get("pbo"), flags=flags, attrib=extra.get("attrib"))
    (out_dir / "kpi.json").write_text(json.dumps(kp, indent=1, default=str))
    if a.dashboard:
        tpl = Path(a.dashboard).read_text(encoding="utf-8", errors="ignore")
        (out_dir / "dashboard.html").write_text(inject(tpl, kp, build_banner(kp, note), build_sections(kp, extra.get("notes"), T)), encoding="utf-8")
    if not quiet_report:
        print(M.format_report(f"{name}  {'[SYNTHETIC]' if a.synthetic else ''}", stats))
    print(f"  kpi: { {k: kp[k] for k in ('sharpe', 'maxdd', 'ntrades', 'pf', 'oosis', 'dsr', 'pbo', 'boot')} }")
    print(f"  written -> {out_dir}/")


if __name__ == "__main__":
    main()
