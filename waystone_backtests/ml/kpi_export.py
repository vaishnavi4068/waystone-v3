"""Compute the Stage-Gate KPI ids used by `Options Strategy KPI Dashboard — StageGate Scorecard.html`
from a backtest's trade list + daily P&L, and inject them as `window.__WEEKLY_KPI_PREFILL__` into a copy
of that dashboard so the gates light up for the ML sleeves exactly as they do for the weekly run.

Definitions follow the dashboard's own "calculation reference" section:
  sharpe      daily P&L / NAV (flat days = 0) mean/std x sqrt(252), OOS only
  sortino     same numerator over downside deviation sqrt(mean(min(r,0)^2)) x sqrt(252)
  maxdd       |min(cum P&L - running peak)| / start NAV, %          calmar   annualised return % / maxdd %
  pf          gross wins / gross losses                             ntrades  closed OOS trades
  worstmo     worst calendar-month loss, % NAV                      cvar     mean of worst 5 % daily returns, % NAV
  skew        skewness of monthly returns
  coststress  Sharpe after DOUBLING the recorded per-trade cost (or -$15 per unit round trip if no cost column)
  oosis       OOS Sharpe / IS Sharpe (IS = in-fold training predictions when available, else median-exit split)
  wfe         walk-forward efficiency = mean per-fold OOS daily return / mean per-fold IS daily return
  dsr         Bailey & López de Prado deflated Sharpe with the REAL trial count from results/trial_log.csv
  pbo         CSCV probability of backtest overfitting over the tuning grid (ml/cv.cscv_pbo)
  paramsens   worst % Sharpe drop when each free parameter is shifted ±20 %
  boot        5th percentile of Sharpe over 500 block-bootstraps (5-day blocks) of the OOS daily P&L
  regimes     distinct calendar years containing trades
  payoff      avg win / |avg loss|                                  expect   mean P&L per trade, USD
  margin      peak concurrent units x margin per unit / NAV, %       stress   worst rolling 5-day loss, % NAV
  corr        |corr| of daily P&L with the existing book (--book equity.csv)
  triallog / killswitch / ddstop / runbook  booleans (trial log is always true here; the others are flags)
Everything the sleeve cannot honestly measure (greeks attribution, live slippage, incubation) is left null so
the dashboard shows "—" rather than a fake pass.

CLI:
  python ml/kpi_export.py --results results/ml_meta_v221 --dashboard "<path to the uploaded html>" \
        --out results/ml_meta_v221/dashboard.html --name ml_meta_v221 --flags killswitch,ddstop
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ml.evaluate import daily_pnl, dsr_for, sharpe_of, to_et_naive, trial_count, trial_sr_variance  # noqa: E402

KPI_IDS = ["sharpe", "sortino", "maxdd", "calmar", "pf", "ntrades", "worstmo", "cvar", "skew", "coststress",
           "oosis", "wfe", "dsr", "pbo", "paramsens", "boot", "regimes", "triallog",
           "attrib", "gammatheta", "netvega", "netdelta", "payoff", "expect", "margin", "stress",
           "incmonths", "inctrades", "sliprat", "livebt", "opserr", "killswitch", "ddstop", "corr", "capacity", "runbook"]


def _r(x, nd=3):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return None
    return round(float(x), nd)


def block_bootstrap_sharpe(daily_ret: pd.Series, n: int = 500, block: int = 5, seed: int = 0, pct: float = 5.0) -> float | None:
    r = daily_ret.dropna().to_numpy(dtype=float)
    T = len(r)
    if T < 40:
        return None
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(T / block))
    out = np.empty(n)
    for i in range(n):
        starts = rng.integers(0, T - block + 1, nb)
        sample = np.concatenate([r[s:s + block] for s in starts])[:T]
        sd = sample.std(ddof=1)
        out[i] = sample.mean() / sd * np.sqrt(252) if sd > 0 else 0.0
    return float(np.percentile(out, pct))


def peak_concurrent_units(trades: pd.DataFrame, units_col: str = "units") -> float:
    if trades is None or len(trades) == 0 or "entry_time" not in trades or "exit_time" not in trades:
        return 0.0
    u = trades[units_col].astype(float) if units_col in trades else pd.Series(1.0, index=trades.index)
    ev = []
    for (a, z, k) in zip(to_et_naive(trades["entry_time"]), to_et_naive(trades["exit_time"]), u):
        ev.append((a, k)); ev.append((z, -k))
    ev.sort(key=lambda t: (t[0], t[1]))
    cur = peak = 0.0
    for _, dk in ev:
        cur += dk
        peak = max(peak, cur)
    return float(peak)


def compute_kpis(trades: pd.DataFrame, daily: pd.Series, nav: float, *, name: str,
                 is_daily: pd.Series | None = None, wfe: float | None = None, n_trials: int | None = None,
                 trial_sr_var: float | None = None, family: str | None = None, pbo: float | None = None,
                 paramsens: float | None = None, flags: dict | None = None, book_daily: pd.Series | None = None,
                 margin_per_unit: float | None = None, units_col: str = "units", cost_col: str = "cost",
                 attrib: float | None = None, capacity: float | None = None, extra: dict | None = None,
                 window: tuple | None = None, greeks: dict | None = None, incubation: dict | None = None) -> dict:
    flags = flags or {}
    d = daily.astype(float).fillna(0.0).sort_index()
    r = d / nav
    T = len(r)
    sd = r.std(ddof=1) if T > 1 else 0.0
    sharpe = float(r.mean() / sd * np.sqrt(252)) if sd > 0 else 0.0
    dd_dev = float(np.sqrt(np.mean(np.minimum(r, 0) ** 2)) * np.sqrt(252)) if T else 0.0
    sortino = float(r.mean() * 252 / dd_dev) if dd_dev > 0 else None
    cum = d.cumsum()
    dd = cum - cum.cummax()
    maxdd_usd = float(dd.min()) if T else 0.0
    maxdd_pct = abs(maxdd_usd) / nav * 100
    ann_ret_pct = float(r.mean() * 252 * 100) if T else 0.0
    calmar = ann_ret_pct / maxdd_pct if maxdd_pct > 0 else None
    monthly = d.groupby(d.index.to_period("M")).sum()
    worstmo = abs(min(0.0, float(monthly.min()))) / nav * 100 if len(monthly) else None
    q = np.quantile(r, 0.05) if T >= 20 else None
    cvar = abs(float(r[r <= q].mean())) * 100 if q is not None and (r <= q).any() else None
    skew_m = float((monthly / nav).skew()) if len(monthly) > 2 else None

    tr = trades if trades is not None else pd.DataFrame()
    pnl = tr["pnl"].astype(float) if len(tr) else pd.Series(dtype=float)
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    pf = float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else None
    payoff = float(wins.mean() / -losses.mean()) if len(wins) and len(losses) and losses.mean() < 0 else None
    expect = float(pnl.mean()) if len(pnl) else None

    # cost stress: double the recorded cost, or $15 per unit round trip
    if len(tr):
        if cost_col in tr:
            extra_cost = tr[cost_col].astype(float).abs()
        else:
            units = tr[units_col].astype(float).abs() if units_col in tr else 1.0
            extra_cost = 15.0 * units
        stressed = tr.assign(pnl=pnl - extra_cost)
        cs_daily = daily_pnl(stressed, index=d.index)
        coststress = sharpe_of(cs_daily / nav)
    else:
        coststress = None

    # oos / is
    if is_daily is not None and len(is_daily.dropna()) > 20:
        is_sharpe = sharpe_of(is_daily.astype(float) / nav)
        oosis = sharpe / is_sharpe if is_sharpe > 0 else (None if is_sharpe == 0 else 0.0)
    elif len(tr) >= 40 and "exit_time" in tr:
        et = to_et_naive(tr["exit_time"])
        med = et.sort_values().iloc[len(et) // 2]
        a, b = tr[et < med], tr[et >= med]
        s_a = sharpe_of(daily_pnl(a) / nav) if len(a) > 5 else 0.0
        s_b = sharpe_of(daily_pnl(b) / nav) if len(b) > 5 else 0.0
        oosis = s_b / s_a if s_a > 0 else (0.0 if s_a < 0 else None)
        is_sharpe = s_a
    else:
        oosis, is_sharpe = None, None
    if wfe is None:
        wfe = oosis

    fam = family or name
    if n_trials is None:
        n_trials = max(1, trial_count(fam))
    if trial_sr_var is None:
        trial_sr_var = trial_sr_variance(fam)
    dsr = dsr_for(d, n_trials, trial_sr_var)
    boot = block_bootstrap_sharpe(r)
    regimes = int(to_et_naive(tr["exit_time"]).dt.year.nunique()) if len(tr) and "exit_time" in tr else int(d.index.year.nunique())

    margin = None
    if margin_per_unit is not None and len(tr):
        margin = peak_concurrent_units(tr, units_col) * margin_per_unit / nav * 100
    stress = abs(min(0.0, float(d.rolling(5).sum().min()))) / nav * 100 if T >= 5 else None
    corr = None
    if book_daily is not None and len(book_daily) > 20:
        bd = book_daily.astype(float)
        bd.index = pd.DatetimeIndex(bd.index).tz_localize(None).normalize() if bd.index.tz is not None else pd.DatetimeIndex(bd.index).normalize()
        both = pd.concat([d.rename("a"), bd.rename("b")], axis=1).dropna()
        if len(both) > 20 and both["a"].std() > 0 and both["b"].std() > 0:
            corr = abs(float(both["a"].corr(both["b"])))
    greeks = greeks or {}
    incubation = incubation or {}
    k = {
        "sharpe": _r(sharpe), "sortino": _r(sortino), "maxdd": _r(maxdd_pct, 2), "calmar": _r(calmar),
        "pf": _r(pf), "ntrades": int(len(pnl)), "worstmo": _r(worstmo, 2), "cvar": _r(cvar), "skew": _r(skew_m),
        "coststress": _r(coststress), "oosis": _r(oosis), "wfe": _r(wfe), "dsr": dsr["dsr_probability"],
        "pbo": _r(pbo * 100 if pbo is not None and pbo <= 1 else pbo, 1), "paramsens": _r(paramsens, 1),
        "boot": _r(boot), "regimes": regimes, "triallog": True,
        "attrib": _r(attrib, 1), "gammatheta": _r(greeks.get("gammatheta")), "netvega": _r(greeks.get("netvega")),
        "netdelta": _r(greeks.get("netdelta")), "payoff": _r(payoff), "expect": _r(expect, 2),
        "margin": _r(margin, 2), "stress": _r(stress, 2),
        "incmonths": incubation.get("months"), "inctrades": incubation.get("trades"), "sliprat": _r(incubation.get("sliprat")),
        "livebt": _r(incubation.get("livebt")), "opserr": incubation.get("opserr"),
        "killswitch": bool(flags.get("killswitch", False)), "ddstop": bool(flags.get("ddstop", False)),
        "corr": _r(corr), "capacity": _r(capacity, 1), "runbook": bool(flags.get("runbook", False)),
        "_name": name,
        "_meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "window": {"start": str(d.index.min().date()) if T else None, "end": str(d.index.max().date()) if T else None},
            "nav": nav, "n_trials": int(n_trials), "trial_sr_var": _r(trial_sr_var, 6), "dsr_detail": dsr,
            "is_sharpe": _r(is_sharpe), "model_family": fam,
            "dollars": {
                "net_pnl_usd": _r(d.sum(), 2), "gross_profit_usd": _r(wins.sum(), 2), "gross_loss_usd": _r(-losses.sum(), 2),
                "avg_win_usd": _r(wins.mean(), 2) if len(wins) else None, "avg_loss_usd": _r(losses.mean(), 2) if len(losses) else None,
                "expectancy_usd": _r(expect, 2), "max_drawdown_usd": _r(maxdd_usd, 2), "max_drawdown_pct_start_nav": _r(maxdd_pct, 2),
                "max_drawdown_date": str(dd.idxmin().date()) if T else None, "worst_month_usd": _r(monthly.min(), 2) if len(monthly) else None,
                "cvar95_daily_usd": _r(-cvar / 100 * nav, 2) if cvar is not None else None,
                "ending_equity_usd": _r(nav + d.sum(), 2), "n_months": int(len(monthly)),
                "avg_month_usd": _r(monthly.mean(), 2) if len(monthly) else None,
                "monthly": [{"month": str(p), "pnl_usd": _r(v, 2), "pnl_pct_nav": _r(v / nav * 100, 3)} for p, v in monthly.items()],
            },
            "extra": {"win_rate": _r((pnl > 0).mean()) if len(pnl) else None,
                      "pnl_by_year_usd": {str(y): {"trades": int(len(g)), "pnl_usd": _r(g["pnl"].sum(), 2)}
                                          for y, g in tr.groupby(to_et_naive(tr["exit_time"]).dt.year.to_numpy())} if len(tr) and "exit_time" in tr else {},
                      "exits": {str(a): int(b) for a, b in tr["reason"].value_counts().items()} if len(tr) and "reason" in tr else {},
                      **(extra or {})},
        },
    }
    return k


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard injection
# ─────────────────────────────────────────────────────────────────────────────
def _fmt_usd(x):
    return "—" if x is None else (f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}")


def build_banner(k: dict, title_note: str = "") -> str:
    m, dol = k["_meta"], k["_meta"]["dollars"]
    def card(h, v, detail, cls=""):
        return (f'    <div class="gate {cls}">\n      <h3>{html.escape(h)}</h3>\n      <div class="verdict">{html.escape(str(v))}</div>\n'
                f'      <div class="detail">{html.escape(detail)}</div>\n    </div>\n')
    cards = [
        card("Net P&L", _fmt_usd(dol["net_pnl_usd"]), f"Ending equity {_fmt_usd(dol['ending_equity_usd'])}", "pass" if (dol["net_pnl_usd"] or 0) > 0 else "fail"),
        card("Trades (OOS)", k["ntrades"], f"Win {100 * (m['extra'].get('win_rate') or 0):.1f}% · {m['n_trials']} trials logged"),
        card("Max DD $", _fmt_usd(dol["max_drawdown_usd"]), f"{dol['max_drawdown_date'] or ''} · constant ${m['nav']:,.0f} NAV"),
        card("Max DD % start NAV", f"{k['maxdd']:.2f}%" if k["maxdd"] is not None else "—", f"|DD| / ${m['nav']:,.0f} start", "pass" if (k["maxdd"] or 99) <= 15 else ("warn" if (k["maxdd"] or 99) <= 25 else "fail")),
        card("Expectancy / trade", _fmt_usd(dol["expectancy_usd"]), f"Avg win {_fmt_usd(dol['avg_win_usd'])} · Avg loss {_fmt_usd(dol['avg_loss_usd'])}"),
        card("Profit Factor", k["pf"] if k["pf"] is not None else "—", f"Gross +{_fmt_usd(dol['gross_profit_usd'])} / −{_fmt_usd(dol['gross_loss_usd'])}"),
        card("Sharpe (daily ann., OOS)", k["sharpe"], f"Sortino {k['sortino']} · IS Sharpe {m.get('is_sharpe')}"),
        card("Deflated Sharpe", k["dsr"], f"{m['n_trials']} trials · SR0 {m['dsr_detail']['sr0_expected_max_of_trials']}", "pass" if (k["dsr"] or 0) >= 0.95 else "fail"),
        card("Worst month", _fmt_usd(dol["worst_month_usd"]), f"{k['worstmo']}% NAV · {dol['n_months']} months"),
        card("Daily CVaR 95%", _fmt_usd(dol["cvar95_daily_usd"]), f"{k['cvar']}% NAV"),
        card("Cost-stress Sharpe", k["coststress"], "costs doubled"),
        card("Bootstrap 5th pct Sharpe", k["boot"], "500 × 5-day block bootstrap"),
    ]
    warn = ""
    if k["margin"] is not None:
        warn = (f'  <div class="warnbox"><b>Size / margin:</b> peak concurrent margin {k["margin"]:.1f}% of NAV. '
                f'{html.escape(title_note)}</div>\n')
    elif title_note:
        warn = f'  <div class="warnbox">{html.escape(title_note)}</div>\n'
    return warn + '  <div class="summary" style="margin-bottom:18px;">\n' + "".join(cards) + "  </div>\n"


def build_sections(k: dict, notes: list[str] | None = None, trades: pd.DataFrame | None = None, max_rows: int = 300) -> str:
    m = k["_meta"]
    parts = []
    bullets = "".join(f'<li style="margin-bottom:6px;">{html.escape(n)}</li>' for n in (notes or []))
    parts.append(f'<section id="run-summary" style="margin-bottom:18px;"><div class="sec-body" style="padding:14px 18px;">'
                 f'<div class="sec-desc" style="padding:0; margin-bottom:12px;"><b>Run summary — {html.escape(k["_name"])}</b> '
                 f'· window {m["window"]["start"]} → {m["window"]["end"]} · generated {m["generated_at"]}</div>'
                 f'<ul style="margin:0; padding-left:18px; font-size:13px; line-height:1.45;">{bullets}</ul></div></section>\n')
    by = m["extra"].get("pnl_by_year_usd") or {}
    if by:
        rows = "".join(f'<tr><td class="kname">{y}</td><td>{v["trades"]}</td><td>{_fmt_usd(v["pnl_usd"])}</td></tr>' for y, v in by.items())
        ex = m["extra"].get("exits") or {}
        parts.append(f'<section style="margin-bottom:18px;"><div class="sec-body" style="padding:14px 18px;">'
                     f'<div class="sec-desc" style="padding:0; margin-bottom:8px;">P&amp;L by year (USD) · exits: {html.escape(json.dumps(ex))}</div>'
                     f'<table><thead><tr><th>Year</th><th>Trades</th><th>Net P&amp;L (USD)</th></tr></thead><tbody>{rows}</tbody></table></div></section>\n')
    if trades is not None and len(trades):
        cols = [c for c in ("entry_time", "exit_time", "symbol", "side", "direction", "units", "size", "p", "entry", "exit", "pnl", "reason", "tag") if c in trades.columns]
        t = trades[cols].head(max_rows)
        head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
        body = []
        for _, row in t.iterrows():
            cells = []
            for c in cols:
                v = row[c]
                if isinstance(v, float):
                    v = f"{v:,.3f}" if abs(v) < 100 else f"{v:,.2f}"
                cells.append(f"<td>{html.escape(str(v))}</td>")
            body.append("<tr>" + "".join(cells) + "</tr>")
        parts.append(f'<section id="weekly-trades" style="margin-bottom:18px;"><div class="sec-body" style="padding:14px 18px;">'
                     f'<div class="sec-desc" style="padding:0; margin-bottom:8px;"><b>OOS trades</b> (first {len(t)} of {len(trades)}) — P&amp;L in USD net of modelled costs.</div>'
                     f'<div class="trade-wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div></div></section>\n')
    return "".join(parts)


_STATE_BLOCK = re.compile(r"let state = \{\};\s*try \{.*?\} catch\(e\)\{ state = window\.__WEEKLY_KPI_PREFILL__ \|\| \{\}; \}\s*", re.S)


def repair_script(s: str) -> str:
    """The weekly generator appends its `let state = {}; try {...}` bootstrap block to the main script on every
    regeneration; a second `let state` is a SyntaxError and the whole scorecard silently renders empty
    (that is the state of the uploaded file).  Keep the first block, drop the duplicates."""
    blocks = list(_STATE_BLOCK.finditer(s))
    if len(blocks) <= 1:
        return s
    keep_end = blocks[0].end()
    out, last = s[:keep_end], keep_end
    for b in blocks[1:]:
        out += s[last:b.start()]
        last = b.end()
    return out + s[last:]


def inject(dashboard_html: str, prefill: dict, banner_html: str | None = None, sections_html: str | None = None,
           clean: bool = True) -> str:
    """Replace the prefill JSON; optionally replace the run banner and drop the old trade tables."""
    s = dashboard_html
    key = "window.__WEEKLY_KPI_PREFILL__ ="
    i = s.find(key)
    if i < 0:
        raise ValueError("dashboard has no window.__WEEKLY_KPI_PREFILL__ assignment")
    j = i + len(key)
    while s[j] in " \t":
        j += 1
    _, end = json.JSONDecoder().raw_decode(s, j)
    s = s[:j] + json.dumps(prefill, default=str) + s[end:]
    s = repair_script(s)
    if clean:
        a = s.find('<section id="run-summary"')
        b = s.find('<section id="kpi-calc-ref"')
        if a >= 0 and b > a:
            s = s[:a] + (sections_html or "") + (banner_html or "") + s[b:]
        s = re.sub(r'<section id="weekly-trades".*?</section>\s*', "", s, flags=re.S)
        m = prefill.get("_meta", {})
        s = re.sub(r'(<div class="big" style="color:var\(--accent\)">).*?(</div>\s*<div class="note">).*?(</div>)',
                   lambda mm: f"{mm.group(1)}{html.escape(prefill.get('_name', 'strategy'))} — results in USD{mm.group(2)}"
                              f"{m.get('window', {}).get('start')} → {m.get('window', {}).get('end')} · NAV ${m.get('nav', 0):,.0f} · "
                              f"generated {m.get('generated_at')} · {m.get('n_trials')} trials logged{mm.group(3)}", s, count=1, flags=re.S)
        # also drop the stale headline sub-text about the weekly run
        s = re.sub(r'(<div class="sub">).*?(</div>)', r'\1Stage-gate scorecard — prefilled by waystone_backtests/ml/kpi_export.py. '
                   r'Banner figures are USD; ratios use % of the constant NAV in the banner. All figures OOS and net of modelled costs. '
                   r'A stage FAILS if any <span style="color:var(--fail)">CRITICAL</span> KPI fails.\2', s, count=1, flags=re.S)
        s = re.sub(r"<title>.*?</title>", f"<title>KPI — {html.escape(prefill.get('_name', 'strategy'))}</title>", s, count=1, flags=re.S)
    return s


def export(results_dir: Path, dashboard: Path | None, out: Path | None, name: str | None = None, nav: float = 100_000.0,
           flags: dict | None = None, book: Path | None = None, margin_per_unit: float | None = None, notes: list[str] | None = None,
           attrib: float | None = None) -> dict:
    results_dir = Path(results_dir)
    metrics = json.loads((results_dir / "metrics.json").read_text()) if (results_dir / "metrics.json").exists() else {}
    trades = pd.read_csv(results_dir / "trades.csv") if (results_dir / "trades.csv").exists() else pd.DataFrame()
    if len(trades) and "size" in trades:
        trades = trades[trades["size"] > 0]                 # meta-labeled lists carry the skipped trades at size 0
    eq = pd.read_csv(results_dir / "equity.csv", index_col=0, parse_dates=True)
    nav = float(metrics.get("params", {}).get("nav", nav))
    daily = eq["daily_pnl"] if "daily_pnl" in eq else eq["daily_ret"] * nav
    is_daily = eq["is_daily_pnl"] if "is_daily_pnl" in eq else None
    ex = metrics.get("extra", {})
    book_daily = None
    if book is not None and Path(book).exists():
        b = pd.read_csv(book, index_col=0, parse_dates=True)
        book_daily = b["daily_pnl"] if "daily_pnl" in b else b["daily_ret"] * nav
    k = compute_kpis(trades, daily, nav, name=name or metrics.get("strategy", results_dir.name), is_daily=is_daily,
                     wfe=ex.get("wfe"), n_trials=ex.get("n_trials"), trial_sr_var=ex.get("trial_sr_var"),
                     family=ex.get("family"), pbo=ex.get("pbo"), paramsens=ex.get("paramsens"), flags=flags,
                     book_daily=book_daily, margin_per_unit=margin_per_unit or ex.get("margin_per_unit"),
                     attrib=attrib if attrib is not None else ex.get("attrib"), capacity=ex.get("capacity"),
                     extra={kk: v for kk, v in ex.items() if kk in ("model", "primary", "importance_stability", "auc_mean", "thresholds", "synthetic")})
    (results_dir / "kpi.json").write_text(json.dumps(k, indent=1, default=str))
    if dashboard is not None:
        tpl = Path(dashboard).read_text(encoding="utf-8", errors="ignore")
        note = "SYNTHETIC DATA — mechanics only" if metrics.get("synthetic") else ""
        out_html = inject(tpl, k, build_banner(k, note), build_sections(k, notes or ex.get("notes"), trades))
        out = Path(out) if out else results_dir / "dashboard.html"
        out.write_text(out_html, encoding="utf-8")
        k["_meta"]["dashboard"] = str(out)
    return k


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", required=True, help="results/<name> directory with metrics.json, trades.csv, equity.csv")
    ap.add_argument("--dashboard", help="path to the KPI dashboard HTML to prefill")
    ap.add_argument("--out", help="output HTML path (default results/<name>/dashboard.html)")
    ap.add_argument("--name")
    ap.add_argument("--nav", type=float, default=100_000.0)
    ap.add_argument("--flags", default="", help="comma list of true booleans: killswitch,ddstop,runbook")
    ap.add_argument("--book", help="equity.csv of the existing book for the correlation KPI")
    ap.add_argument("--margin-per-unit", type=float, help="$ margin per unit (e.g. 2500 for MNQ overnight)")
    ap.add_argument("--attrib", type=float, help="greeks attribution %% if you have it (delta-only sleeves: 100)")
    a = ap.parse_args()
    flags = {f.strip(): True for f in a.flags.split(",") if f.strip()}
    k = export(Path(a.results), Path(a.dashboard) if a.dashboard else None, Path(a.out) if a.out else None, a.name, a.nav, flags,
               Path(a.book) if a.book else None, a.margin_per_unit, attrib=a.attrib)
    print(json.dumps({i: k[i] for i in KPI_IDS}, indent=1))
    print(f"kpi.json written to {a.results}" + (f"; dashboard -> {k['_meta'].get('dashboard')}" if a.dashboard else ""))


if __name__ == "__main__":
    main()
