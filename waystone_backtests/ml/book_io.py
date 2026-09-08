"""Book registry, instruction I/O, and calendar helpers for ml/allocator.py.

No network. YAML via PyYAML when installed; otherwise JSON or a small indent parser.
`read_instruction` implements ALLOCATOR_SPEC.md §7 fallback for the bots.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ET = "America/New_York"
STATUSES = ("live", "paper", "shadow", "off", "brake")
KPI_MIN = {"sharpe": 1.5, "dsr": 0.95, "oosis": 0.6, "coststress": 1.0, "ntrades": 200}


def r4(x):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return x
    return None if not np.isfinite(v) else round(v, 4)


def to_et(ts, localize_naive: bool = True) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize(ET) if localize_naive else t
    return t.tz_convert(ET)


def naive_date(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert(ET).tz_localize(None)
    return t.normalize()


def _holiday_cal():
    try:
        from pandas.tseries.holiday import USFederalHolidayCalendar
        return USFederalHolidayCalendar()
    except Exception:
        return None


def session_offset():
    cal = _holiday_cal()
    if cal is not None:
        from pandas.tseries.offsets import CustomBusinessDay
        return CustomBusinessDay(calendar=cal)
    return pd.offsets.BDay(1)


def is_session(d) -> bool:
    d = naive_date(d)
    if d.weekday() >= 5:
        return False
    cal = _holiday_cal()
    if cal is None:
        return True
    hol = set(pd.Timestamp(x).normalize() for x in cal.holidays(d - pd.Timedelta(days=7), d + pd.Timedelta(days=7)))
    return d not in hol


def next_session(d) -> pd.Timestamp:
    """First exchange session strictly after `d` (weekends + US federal holidays)."""
    d = naive_date(d)
    off = session_offset()
    nxt = (d + off).normalize()
    # CustomBusinessDay from a holiday lands on the next session; from a session also advances.
    return nxt


def next_session_on_or_after(d) -> pd.Timestamp:
    d = naive_date(d)
    return d if is_session(d) else next_session(d)


def is_first_session_of_month(d) -> bool:
    d = naive_date(d)
    prev = (d - session_offset()).normalize()
    return prev.month != d.month


# ── book.yaml ────────────────────────────────────────────────────────────────
def _parse_scalar(raw: str):
    s = raw.split("#", 1)[0].strip()
    if s in ("", "~", "null", "Null", "NULL"):
        return None
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    if (s.startswith("[") and s.endswith("]")) or (s.startswith("{") and s.endswith("}")):
        try:
            return json.loads(s.replace("'", '"'))
        except json.JSONDecodeError:
            inner = s[1:-1].strip()
            if not inner:
                return []
            return [_parse_scalar(p.strip()) for p in inner.split(",")]
    if (len(s) >= 2) and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        return s[1:-1]
    try:
        return int(s) if s.lstrip("-").isdigit() else float(s)
    except ValueError:
        return s


def _simple_yaml(text: str) -> dict:
    """Indent parser for the book.yaml subset (maps, lists, scalars, # comments)."""
    root: dict = {}
    stack: list[tuple[int, object]] = [(-1, root)]
    pending_key = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        while stack and indent <= stack[-1][0] and not (pending_key is not None and indent > stack[-1][0]):
            if indent == stack[-1][0]:
                break
            stack.pop()
        cur = stack[-1][1]
        if line.startswith("- "):
            val = _parse_scalar(line[2:])
            if isinstance(cur, list):
                cur.append(val)
            continue
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        key, rest = key.strip(), rest.strip()
        if rest:
            val = _parse_scalar(rest)
            if isinstance(cur, dict):
                cur[key] = val
            pending_key = None
        else:
            nxt: dict = {}
            if isinstance(cur, dict):
                cur[key] = nxt
            stack.append((indent, nxt))
            pending_key = key
    return root


def load_mapping(path: Path) -> dict:
    text = Path(path).read_text()
    try:
        import yaml
        out = yaml.safe_load(text)
        if isinstance(out, dict):
            return out
    except Exception:
        pass
    try:
        out = json.loads(text)
        if isinstance(out, dict):
            return out
    except json.JSONDecodeError:
        pass
    return _simple_yaml(text)


def load_book(path: Path | None = None) -> dict:
    p = Path(path) if path else ROOT / "book.yaml"
    if not p.exists():
        alt = p.with_suffix(".json")
        if alt.exists():
            p = alt
        else:
            raise FileNotFoundError(p)
    book = load_mapping(p)
    book.setdefault("sleeves", {})
    return book


def load_json(path: Path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text())


def save_json(path: Path, obj) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, default=str) + "\n")


def default_state(book: dict | None = None) -> dict:
    sleeves = {sid: _blank_sleeve_state() for sid in (book or {}).get("sleeves", {})}
    return {"kill_switch": False, "book": _blank_book_state(), "sleeves": sleeves}


def _blank_sleeve_state() -> dict:
    return {"state": "normal", "recover_count": 0, "recover_high": None, "last_equity": None,
            "m_raw": None, "g_regime": 1.0, "g_event": 1.0, "g_dd": 1.0, "g_book": 1.0,
            "last_units": 0, "last_multiplier": 0.0}


def _blank_book_state() -> dict:
    return {"state": "normal", "recover_count": 0, "recover_high": None, "last_equity": None,
            "monthly_loss_off": False, "month": None, "peak": None}


def load_state(path: Path, book: dict | None = None) -> dict:
    st = load_json(path, None)
    base = default_state(book)
    if not st:
        return base
    base["kill_switch"] = bool(st.get("kill_switch", False))
    if isinstance(st.get("book"), dict):
        base["book"].update(st["book"])
    for sid, row in (st.get("sleeves") or {}).items():
        base["sleeves"].setdefault(sid, _blank_sleeve_state()).update(row)
    return base


def save_state(path: Path, state: dict) -> None:
    save_json(path, state)


# ── market / sleeve files ────────────────────────────────────────────────────
def load_equity(path: Path, nav: float = 100000.0) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        return pd.DataFrame(columns=["equity", "daily_ret", "daily_pnl"])
    df = pd.read_csv(p)
    col = "date" if "date" in df.columns else df.columns[0]
    df[col] = pd.to_datetime(df[col])
    df = df.set_index(col).sort_index()
    df.index = pd.DatetimeIndex(df.index).tz_localize(None).normalize()
    df = df[~df.index.duplicated(keep="last")]
    if "daily_pnl" not in df.columns and "equity" in df.columns:
        df["daily_pnl"] = df["equity"].astype(float).diff().fillna(0.0)
    if "equity" not in df.columns and "daily_pnl" in df.columns:
        df["equity"] = df["daily_pnl"].astype(float).cumsum()
    if "daily_ret" not in df.columns and "daily_pnl" in df.columns:
        df["daily_ret"] = df["daily_pnl"].astype(float) / float(nav)
    return df


def stitch_live(backtest: pd.DataFrame, live: pd.DataFrame | None) -> tuple[pd.DataFrame, str]:
    if live is None or live.empty:
        return backtest, "backtest"
    if backtest is None or backtest.empty:
        return live, "live"
    cut = live.index.min()
    bt = backtest[backtest.index < cut]
    return pd.concat([bt, live]).sort_index(), "live"


def load_kpi(results_dir: Path) -> dict:
    out = {}
    for name in ("metrics.json", "kpi.json"):
        p = Path(results_dir) / name
        if p.exists():
            try:
                out.update(json.loads(p.read_text()) or {})
            except json.JSONDecodeError:
                pass
    return out


def load_regime(data_dir: Path, name: str, as_of) -> tuple[int, str]:
    """Last state with date < as_of. Missing/stale (>3 sessions) → 1."""
    p = Path(data_dir) / "regime" / f"{name}_states.csv"
    if not p.exists():
        return 1, "regime=stale:assume_1"
    df = pd.read_csv(p, parse_dates=["date"])
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
    as_of = naive_date(as_of)
    df = df[df["date"] < as_of].sort_values("date")
    if df.empty:
        return 1, "regime=stale:assume_1"
    last = df.iloc[-1]
    gap = 0
    cur = last["date"]
    while next_session(cur) < as_of:
        gap += 1
        cur = next_session(cur)
        if gap > 3:
            return 1, "regime=stale:assume_1"
    return int(last["state"]), f"regime={int(last['state'])}"


def load_events(data_dir: Path, session) -> list[str]:
    session = naive_date(session)
    names: list[str] = []
    fomc = Path(data_dir) / "fomc_dates.csv"
    if fomc.exists():
        df = pd.read_csv(fomc)
        col = "date" if "date" in df.columns else df.columns[0]
        days = pd.to_datetime(df[col]).dt.tz_localize(None).dt.normalize()
        if (days == session).any():
            names.append("FOMC")
    cal = Path(data_dir) / "events_calendar.csv"
    if cal.exists():
        df = pd.read_csv(cal)
        dcol = "date" if "date" in df.columns else df.columns[0]
        days = pd.to_datetime(df[dcol]).dt.tz_localize(None).dt.normalize()
        mask = days == session
        ecol = "event" if "event" in df.columns else ("name" if "name" in df.columns else None)
        if ecol:
            names.extend(str(x) for x in df.loc[mask, ecol].tolist())
        elif mask.any() and "FOMC" not in names:
            names.append("EVENT")
    # unique, stable order
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def drawdown_pct(equity: pd.Series, nav: float, lookback: int = 252) -> float:
    x = equity.dropna().astype(float).iloc[-lookback:]
    if x.empty or nav <= 0:
        return 0.0
    return max(0.0, (float(x.max()) - float(x.iloc[-1])) / nav * 100.0)


def round_units(multiplier: float, unit_size: float, unit_step: float, unit_type: str) -> float:
    raw = float(multiplier) * float(unit_size)
    step = float(unit_step or (1 if unit_type == "contracts" else 1000))
    u = round(raw / step) * step
    return int(u) if unit_type == "contracts" or abs(u - int(u)) < 1e-9 else u


def latest_completed(equities: dict[str, pd.DataFrame]) -> pd.Timestamp | None:
    last = None
    for df in equities.values():
        if df is None or df.empty:
            continue
        m = df.index.max()
        last = m if last is None else max(last, m)
    return None if last is None else pd.Timestamp(last).normalize()


def append_history(path: Path, rows: list[dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    header = not p.exists()
    df.to_csv(p, mode="a", header=header, index=False)


def _fallback_units(last_known, unit_type: str | None) -> tuple[float, bool, list[str]]:
    """stale/missing/schema!=1 → last_known × 0.5; min 1 contract; else shadow + CRITICAL."""
    reasons = ["fallback", "CRITICAL"]
    if unit_type == "contracts":
        raw = 0.0 if last_known is None else float(last_known) * 0.5
        units = max(int(round(raw)), 1)
        return units, False, reasons
    if last_known is None:
        return 0, True, reasons
    units = float(last_known) * 0.5
    if abs(units) < 1e-9:
        return 0, True, reasons
    return units, False, reasons


def read_instruction(path, sleeve=None, now=None, book=None, state_path=None, unit_type=None):
    """Load book_instruction.json. §7: stale / missing / schema!=1 → last_known × 0.5.

    min 1 contract if unit_type is contracts, else shadow + CRITICAL.
    """
    now = to_et(now) if now is not None else pd.Timestamp.now(tz=ET)
    p = Path(path)
    raw = load_json(p, None)
    book = book or {}
    sleeves_cfg = book.get("sleeves") or {}
    state = load_json(state_path, {}) if state_path else {}
    state_sleeves = (state or {}).get("sleeves") or {}

    def _unit_type(sid):
        if unit_type:
            return unit_type
        if sid in sleeves_cfg:
            return sleeves_cfg[sid].get("unit_type")
        if raw and sid in (raw.get("sleeves") or {}):
            return raw["sleeves"][sid].get("unit_type")
        return None

    def _last(sid):
        if raw and sid in (raw.get("sleeves") or {}):
            u = raw["sleeves"][sid].get("units")
            if u is not None:
                return u
        if sid in state_sleeves and state_sleeves[sid].get("last_units") is not None:
            return state_sleeves[sid]["last_units"]
        return None

    ok = bool(raw) and raw.get("schema") == 1 and "sleeves" in raw
    if ok:
        try:
            vu = to_et(raw.get("valid_until"))
            ok = vu >= now
        except Exception:
            ok = False

    if ok:
        return raw["sleeves"][sleeve] if sleeve else raw

    ids = [sleeve] if sleeve else list((raw or {}).get("sleeves") or sleeves_cfg or {"_unknown": {}})
    out_sleeves = {}
    for sid in ids:
        ut = _unit_type(sid)
        units, shadow, reasons = _fallback_units(_last(sid), ut)
        prev = (raw or {}).get("sleeves", {}).get(sid, {}) if raw else {}
        out_sleeves[sid] = {
            "status": "shadow" if shadow else prev.get("status", "paper"),
            "multiplier": r4(0.5 * float(prev["multiplier"])) if prev.get("multiplier") is not None else None,
            "units": units, "unit_type": ut, "risk_weight": 0.0,
            "reasons": reasons, "shadow": shadow,
        }
    if sleeve:
        return out_sleeves[sleeve]
    return {
        "schema": 1, "generated_at": None, "for_session": None, "valid_until": None,
        "nav": (raw or {}).get("nav"), "book": (raw or {}).get("book") or {},
        "sleeves": out_sleeves, "fallback": True,
    }


def kpi_ok(kpi: dict) -> tuple[bool, list[str]]:
    if not kpi:
        return False, ["kpi_missing"]
    bad = []
    for k, thr in KPI_MIN.items():
        v = kpi.get(k, kpi.get("n_trades") if k == "ntrades" else None)
        if v is None or float(v) < thr:
            bad.append(f"{k}<{thr}")
    if kpi.get("paramsens") is not None and float(kpi["paramsens"]) > 30:
        bad.append("paramsens>30")
    return not bad, bad


def incubation_months(first, as_of) -> int:
    if first is None:
        return 0
    return int((naive_date(as_of).to_period("M") - naive_date(first).to_period("M")).n)


def g_regime_of(rules: dict, state: int) -> float:
    rules = rules or {}
    if state in set(rules.get("off_in_states") or []):
        return 0.0
    if state in set(rules.get("half_in_states") or []):
        return 0.5
    return 1.0


def blank_sleeve(cfg, status, reasons, shadow=True, **extra):
    row = {"status": status, "multiplier": 0.0, "units": 0, "unit_type": cfg.get("unit_type", "contracts"),
           "risk_weight": 0.0, "reasons": list(reasons), "shadow": shadow}
    row.update(extra)
    return row


def book_paths(root: Path, out_dir: Path | None = None):
    out = Path(out_dir) if out_dir else Path(root) / "results" / "book"
    return out / "book_instruction.json", out / "book_state.json", out / "book_history.csv", out


def derive_for_session(book, root, override=None, now=None) -> pd.Timestamp:
    if override:
        return next_session_on_or_after(override)
    nav = float(book.get("nav") or 100000)
    eqs = {}
    for cfg in (book.get("sleeves") or {}).values():
        p = Path(root) / cfg["results_dir"] / "equity.csv" if cfg.get("results_dir") else None
        if p is not None and p.exists():
            eqs[id(cfg)] = load_equity(p, nav)
    last = latest_completed(eqs)
    if last is None:
        now = to_et(now) if now is not None else pd.Timestamp.now(tz=ET)
        return next_session(now)
    return next_session(last)


def format_report(out: dict) -> str:
    ins, w = out["instruction"], out.get("warnings") or []
    b = ins["book"]
    lines = [
        f"BOOK ALLOCATOR  for_session={ins['for_session']}  nav={ins['nav']}  "
        f"brake={b['brake']}  kill={b['kill_switch']}",
        f"regime={b['regime_name']} state={b['regime_state']}  events={b['events_today']}",
        f"target_vol={b['target_daily_vol_pct']}%  realised_20d={b['realised_daily_vol_pct_20d']}%  "
        f"dd={b['drawdown_pct']}%",
        f"{'sleeve':<16}{'status':<8}{'m':>8}{'units':>10}{'rw':>8}  reasons",
    ]
    for sid, s in ins["sleeves"].items():
        lines.append(f"{sid:<16}{s['status']:<8}{s['multiplier']:>8.4f}{s['units']:>10}{s['risk_weight']:>8.4f}  "
                     f"{','.join(s['reasons'])}")
    lines.extend(f"WARN  {x}" for x in w)
    return "\n".join(lines)
