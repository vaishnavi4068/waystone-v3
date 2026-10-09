"""Parser for the after-hours same-day backtest replay.

VM files: BACK_TEST_DAILY/<PREFIX>YYYY-MM-DD_back_daily.txt.

The replay output is not formally specified, so this parser accepts the shapes the
replay scripts produce: ``key: value`` header lines, a trade table (whitespace, ``|``
or ``,`` separated, with a header row naming entry/exit columns) or ``key=value``
trade lines, and a totals line. Anything it cannot place is counted in ``unparsed``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal

from waystone3.hq.calendar import NY
from waystone3.hq.v221_log import to_decimal

FILE_NAME = re.compile(r"^(?P<prefix>[A-Za-z0-9]+_)(?P<d>\d{4}-\d{2}-\d{2})_back_daily\.txt$")

_KV_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 /&()%._-]*?)\s*[:=]\s*(.+?)\s*$")
_TRADE_KV = re.compile(r"\b([A-Za-z_]+)\s*=\s*(\S+)")
_DIRECTION = re.compile(r"\b(LONG|SHORT|BUY|SELL)\b", re.I)
# The replay engine prints timestamps as str(datetime): "2026-10-07 09:31:00-04:00".
# The UTC offset must not be read as a clock time, nor its digits as prices.
_STAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:\s?(?:[+-]\d{2}:?\d{2}|Z)\b)?"
)
_STAMP_TOKEN = re.compile(r"@TS(\d+)@")
_CLOCK = re.compile(r"(?<![\d:+-])(\d{1,2}:\d{2}(?::\d{2})?)\b")
_LABELLED_POINTS = re.compile(r"\b(?:pts|points|pnl_pts)\s*[:=]?\s*([+-]?[\d,]*\.?\d+)", re.I)
_SUFFIXED_POINTS = re.compile(r"(?<![\w.])([+-]?[\d,]*\.?\d+)\s*(?:pts|points)\b", re.I)
_LABELLED_NET = re.compile(
    r"\b(?:net(?:_pnl|_usd)?|pnl(?:_usd)?|p&l)\s*[:=]?\s*([+-]?\$?\s?[+-]?[\d,]*\.?\d+)", re.I
)
_DOLLARS = re.compile(r"(?<![\w.])([+-]?\$\s?[+-]?[\d,]*\.?\d+)")
_NUMBER = re.compile(r"-?\$?-?[\d,]*\.?\d+")
_TOTAL_TRADES = re.compile(r"\btrades?\s*[:=]?\s*(\d+)\b", re.I)
_TOTAL_NET = re.compile(r"\btotal(?:\s+net)?(?:\s+p&l)?\s*[:=]?\s*(-?\$?-?[\d,]*\.?\d+)", re.I)
_MAXDD = re.compile(r"\bmax\s*_?dd\s*[:=]?\s*(-?[\d.]+)\s*(%?)", re.I)
_SPLIT = re.compile(r"\s*\|\s*|\s*,\s*|\t+|\s{2,}")

_HEADER_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("params_fp", ("params fp", "param fp", "params_fp", "fingerprint", "fp")),
    ("bar_file", ("bars file", "bar file", "bars_file", "data file", "input")),
    ("bar_count", ("bar count", "bars", "bar_count", "n bars", "bars loaded")),
    ("vol_index", ("vol index", "volatility index", "vol_index", "vix file", "vol")),
    ("sentiment_source", ("sentiment", "fng", "sentiment source")),
    ("point_value", ("point value", "point_value", "pv", "multiplier")),
    ("flatten_time", ("flatten", "flatten time", "flatten_time")),
    ("daily_loss_cap", ("loss cap", "daily loss cap", "daily loss limit", "loss limit")),
    ("config_label", ("config", "config label", "strategy", "variant")),
)

_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "seq": ("#", "n", "no", "seq", "trade", "id"),
    "direction": ("dir", "direction", "side"),
    "entry_time": ("entry_time", "entry time", "entry_ts", "entry", "in", "open"),
    "entry_px": ("entry_px", "entry px", "entry price", "entry_price", "in_px", "open_px"),
    "exit_time": ("exit_time", "exit time", "exit_ts", "exit", "out", "close"),
    "exit_px": ("exit_px", "exit px", "exit price", "exit_price", "out_px", "close_px"),
    "points": ("pts", "points", "pnl_pts"),
    "contracts": ("contracts", "qty", "size", "cts"),
    "gross_pnl": ("gross", "gross_pnl", "gross p&l"),
    "commission": ("comm", "commission", "fees"),
    "net_pnl": ("net", "net_pnl", "net p&l", "pnl", "p&l"),
    "exit_reason": ("reason", "exit_reason", "exit reason"),
    "hold_min": ("held", "hold", "hold_min", "minutes", "mins"),
}


@dataclass
class BacktestTrade:
    seq: int
    direction: str
    entry_ts: datetime
    exit_ts: datetime | None = None
    entry_px: Decimal | None = None
    exit_px: Decimal | None = None
    points: Decimal | None = None
    contracts: Decimal | None = None
    gross_pnl: Decimal | None = None
    commission: Decimal | None = None
    net_pnl: Decimal | None = None
    exit_reason: str | None = None
    hold_min: Decimal | None = None


@dataclass
class BacktestDay:
    session_date: date
    config_label: str | None = None
    params_fp: str | None = None
    bar_file: str | None = None
    bar_count: int | None = None
    vol_index: str | None = None
    sentiment_source: str | None = None
    point_value: Decimal | None = None
    flatten_time: time | None = None
    daily_loss_cap: Decimal | None = None
    trades_reported: int | None = None
    total_net_reported: Decimal | None = None
    maxdd_reported: Decimal | None = None
    trades: list[BacktestTrade] = field(default_factory=list)
    unparsed: list[int] = field(default_factory=list)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower().replace("_", " "))


def _stamp_ts(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(
            re.sub(r"\s+(?=[+-]\d{2}:?\d{2}$)", "", value.strip()).replace(" ", "T")
        )
    except ValueError:
        return None
    return parsed.astimezone(NY) if parsed.tzinfo else parsed.replace(tzinfo=NY)


def _ts(value: str, day: date) -> datetime | None:
    value = value.strip()
    if stamp := _STAMP.search(value):
        return _stamp_ts(stamp.group(0))
    if "T" in value:
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.astimezone(NY) if parsed.tzinfo else parsed.replace(tzinfo=NY)
        except ValueError:
            pass
    clock = _CLOCK.search(value)
    if not clock:
        return None
    parts = [int(p) for p in clock.group(1).split(":")]
    return datetime.combine(day, time(parts[0], parts[1], parts[2] if len(parts) > 2 else 0), NY)


def _price_in(value: str) -> Decimal | None:
    after_at = value.split("@", 1)[1] if "@" in value else value
    after_at = _CLOCK.sub(" ", _STAMP.sub(" ", after_at))
    for token in _NUMBER.findall(after_at):
        if ":" not in token and (num := to_decimal(token)) is not None and abs(num) >= 1:
            return num
    return None


def _set_header(day: BacktestDay, key: str, value: str) -> bool:
    norm = _norm(key)
    for attr, aliases in _HEADER_KEYS:
        if norm not in aliases:
            continue
        if attr == "bar_count":
            digits = re.search(r"\d+", value.replace(",", ""))
            if not digits:
                return False
            day.bar_count = int(digits.group(0))
        elif attr in ("point_value", "daily_loss_cap"):
            numbers = _NUMBER.findall(value)
            setattr(day, attr, to_decimal(numbers[0]) if numbers else None)
        elif attr == "flatten_time":
            clock = _CLOCK.search(value)
            if clock:
                parts = [int(p) for p in clock.group(1).split(":")]
                day.flatten_time = time(parts[0], parts[1])
        elif attr == "params_fp":
            fp = re.search(r"[0-9a-f]{6,}", value)
            day.params_fp = fp.group(0) if fp else value[:64]
        else:
            setattr(day, attr, value[:200])
        return True
    return False


def _totals(day: BacktestDay, text: str) -> bool:
    found = False
    lowered = text.lower()
    if "total" in lowered or "summary" in lowered or "maxdd" in lowered.replace(" ", ""):
        if (trades := _TOTAL_TRADES.search(text)) and day.trades_reported is None:
            day.trades_reported = int(trades.group(1))
            found = True
        if (net := _TOTAL_NET.search(text)) and day.total_net_reported is None:
            day.total_net_reported = to_decimal(net.group(1))
            found = True
        if maxdd := _MAXDD.search(text):
            value = Decimal(maxdd.group(1))
            day.maxdd_reported = value / 100 if maxdd.group(2) == "%" or value > 1 else value
            found = True
    return found


def _column_map(cells: list[str]) -> dict[int, str] | None:
    mapping: dict[int, str] = {}
    for idx, cell in enumerate(cells):
        norm = _norm(cell)
        for name, aliases in _COLUMN_ALIASES.items():
            if norm in aliases and name not in mapping.values():
                mapping[idx] = name
                break
    names = set(mapping.values())
    if {"entry_time", "direction"} <= names or {"entry_time", "exit_time"} <= names:
        return mapping
    return None


def _trade_from_fields(fields: dict[str, str], seq: int, day: date) -> BacktestTrade | None:
    direction = fields.get("direction", "")
    dir_match = _DIRECTION.search(direction)
    entry_raw = fields.get("entry_time", "")
    entry_ts = _ts(entry_raw, day)
    if not dir_match or entry_ts is None:
        return None
    exit_raw = fields.get("exit_time", "")
    trade = BacktestTrade(
        seq=int(fields["seq"]) if fields.get("seq", "").isdigit() else seq,
        direction="LONG" if dir_match.group(1).upper() in ("LONG", "BUY") else "SHORT",
        entry_ts=entry_ts,
        exit_ts=_ts(exit_raw, day) if exit_raw else None,
        entry_px=to_decimal(fields["entry_px"]) if "entry_px" in fields else _price_in(entry_raw),
        exit_px=to_decimal(fields["exit_px"]) if "exit_px" in fields else _price_in(exit_raw),
    )
    for name in ("points", "contracts", "gross_pnl", "commission", "net_pnl"):
        if name in fields:
            setattr(trade, name, to_decimal(fields[name]))
    if "hold_min" in fields:
        trade.hold_min = to_decimal(fields["hold_min"].rstrip("m"))
    if reason := fields.get("exit_reason"):
        trade.exit_reason = reason
    return trade


def _free_trade(text: str, seq: int, day: date) -> BacktestTrade | None:
    """A trade written as ``key=value`` pairs and/or free text with full timestamps."""
    stamps = [m.group(0) for m in _STAMP.finditer(text)]
    rest = _STAMP.sub(lambda m: f"@TS{stamps.index(m.group(0))}@", text)
    direction = _DIRECTION.search(rest)
    pairs = {_norm(k): v for k, v in _TRADE_KV.findall(rest)}
    if not direction or not (pairs or stamps):
        return None
    fields: dict[str, str] = {"direction": direction.group(1)}
    for key, value in pairs.items():
        for name, aliases in _COLUMN_ALIASES.items():
            if key in aliases:
                fields[name] = value
    used = {int(t) for v in fields.values() for t in _STAMP_TOKEN.findall(v)}
    free = iter([s for i, s in enumerate(stamps) if i not in used])
    for name in ("entry_time", "exit_time"):
        if name not in fields and (stamp := next(free, None)):
            fields[name] = stamp
    fields = {k: _STAMP_TOKEN.sub(lambda m: stamps[int(m.group(1))], v) for k, v in fields.items()}
    plain = _STAMP_TOKEN.sub(" ", rest)
    if seq_match := re.match(r"^\s*#?(\d+)\b(?!-)", plain):
        fields.setdefault("seq", seq_match.group(1))
    if "entry_time" not in fields:
        clocks = _CLOCK.findall(plain)
        if clocks:
            fields["entry_time"] = clocks[0]
            if len(clocks) > 1:
                fields.setdefault("exit_time", clocks[1])
    if "points" not in fields and (
        found := _LABELLED_POINTS.search(plain) or _SUFFIXED_POINTS.search(plain)
    ):
        fields["points"] = found.group(1)
    if "net_pnl" not in fields:
        dollars = _DOLLARS.findall(plain)
        if found := _LABELLED_NET.search(plain):
            fields["net_pnl"] = found.group(1)
        elif len(dollars) == 1:
            fields["net_pnl"] = dollars[0]
    return _trade_from_fields(fields, seq, day)


def parse_backtest_daily(text: str, session_date: date) -> BacktestDay:
    day = BacktestDay(session_date=session_date)
    columns: dict[int, str] | None = None
    splitter = _SPLIT
    for line_no, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or set(stripped) <= set("-=_|+ "):
            continue
        cells = [c for c in splitter.split(stripped.strip("|")) if c != ""]
        if columns is None:
            header = _column_map(cells)
            if header is None and "=" not in stripped and ":" not in stripped:
                single = stripped.split()
                if (header := _column_map(single)) is not None:
                    splitter = re.compile(r"\s+")
            if header is not None:
                columns = header
                continue
        if columns is not None and len(cells) >= len(columns) and _DIRECTION.search(stripped):
            fields = {columns[i]: cells[i] for i in columns if i < len(cells)}
            trade = _trade_from_fields(fields, len(day.trades) + 1, session_date)
            if trade is not None:
                day.trades.append(trade)
                continue
        if ("=" in stripped or _STAMP.search(stripped)) and (
            trade := _free_trade(stripped, len(day.trades) + 1, session_date)
        ):
            day.trades.append(trade)
            continue
        if _totals(day, stripped):
            continue
        kv = _KV_LINE.match(stripped)
        if kv and _set_header(day, kv.group(1), kv.group(2)):
            continue
        if columns is None and len(day.trades) == 0 and line_no <= 3:
            continue
        day.unparsed.append(line_no)
    return day
