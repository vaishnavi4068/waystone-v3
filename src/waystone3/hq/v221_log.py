"""Parser for the V221 engine's daily paper log (shared by ES V221, NQ V221 and R2 MNQ).

The parser is line-oriented and tolerant: every line is kept in the raw layer, known
line types become structured rows, and anything unrecognised is counted so format
drift shows up as a PARTIAL parse instead of silently dropped data.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from waystone3.hq.calendar import NY

_TS_FULL = re.compile(
    r"^\[?(?P<d>\d{4}-\d{2}-\d{2})[ T](?P<t>\d{2}:\d{2}:\d{2})(?:[.,]\d+)?(?:\s*(?:ET|EDT|EST))?\]?"
    r"(?:\s+(?:INFO|WARNING|WARN|ERROR|DEBUG|CRITICAL)\b)?\s*(?:[-|:]\s+)?(?P<rest>.*)$"
)
_TS_TIME = re.compile(
    r"^\[?(?P<t>\d{2}:\d{2}:\d{2})(?:[.,]\d+)?\]?"
    r"(?:\s+(?:INFO|WARNING|WARN|ERROR|DEBUG|CRITICAL)\b)?\s*(?:[-|:]\s+)?(?P<rest>.*)$"
)
_NUM = r"-?\$?-?[\d,]*\.?\d+"

_PARAMS_FP = re.compile(r"\bparams?[ _]?fp\b\s*[:=]?\s*([0-9a-f]{6,})", re.I)
_PARAMS_INLINE = re.compile(r"\bparams=([0-9a-f]{6,})", re.I)
_ACCOUNT_HDR = re.compile(r"^account\s*[:=]\s*([A-Z0-9]+)", re.I)
_CLIENT_ID = re.compile(r"clientId\s*=\s*(\d+)", re.I)
_CONTRACT = re.compile(r"^\[CONTRACT\]\s+(\S+)(?:.*?exp\s*=\s*(\d{8}))?")
_ACCOUNT_JSON = re.compile(r"^\[ACCOUNT\]\s*(\{.*\})\s*$")
_TRIGGER = re.compile(
    r"^\[TRIGGER\]\s+(BUY|SELL|LONG|SHORT)\b.*?\bbar\s+(\d{1,2}:\d{2})(?P<mid>.*?)(?:->|→)\s*(?P<out>.+)$",
    re.I,
)
_CLOSE_PX = re.compile(r"\bclose\s*=\s*(" + _NUM + ")")
_NO_ENTRIES = re.compile("^no entries\\s*[\u2014\u2013-]+\\s*(.+)$", re.I)
_ENTRY = re.compile(
    r"^\*{3}\s*ENTRY\s+(BUY|SELL|LONG|SHORT)\s+(\d+)\s+(\S+)(?:.*?signal bar\s+(\S+))?", re.I
)
_ORDER_FILLED = re.compile(
    r"^\[ORDER\].*?\bFILLED\s+(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*@\s*(" + _NUM + ")", re.I
)
_COMMISSION = re.compile(r"\bcommission\s*=\s*(" + _NUM + ")", re.I)
_SIDE_TOKEN = re.compile(r"\b(BUY|SELL)\b")
_ORDER_REF = re.compile(r"#(\d+)")
_FILLED = re.compile(
    r"^\[FILLED\]\s+#(\d+)\s+(?:px\s*)?(" + _NUM + r")(?:.*?\bslip\s*=\s*(" + _NUM + "))?", re.I
)
_HOLD = re.compile(r"^\[HOLD\]", re.I)
_EXIT = re.compile(r"^\*{3}\s*EXIT\s+(\S+)\s+#(\d+)", re.I)
_CLOSED = re.compile(r"^\[CLOSED\]\s+#(\d+)\s*(?P<rest>.*)$", re.I)
_TOTALS_KEYS = ("pts", "gross", "commission", "net")
_KV = re.compile(r"\b([A-Za-z]+)\s*=\s*(" + _NUM + r"|\d+(?:\.\d+)?[mh]?)")
_DIRECTION = re.compile(r"\b(LONG|SHORT|BUY|SELL)\b")
_QTY_AFTER_DIR = re.compile("\\b(?:LONG|SHORT|BUY|SELL)\\s*(?:x|\u00d7)?\\s*(\\d+)\\b")
_ENTRY_PX = re.compile(r"\b(?:entry|in)\s*[=:@]?\s*(" + _NUM + ")", re.I)
_EXIT_PX = re.compile(r"\b(?:exit|out)\s*[=:@]?\s*(" + _NUM + ")", re.I)
_ARROW_PX = re.compile(r"(" + _NUM + r")\s*(?:->|→)\s*(" + _NUM + ")")
_CLOCK = re.compile(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b")
_SUMMARY = re.compile(r"^DAILY SUMMARY\s+(\d{4}-\d{2}-\d{2})\s+(\S+)", re.I)
_IB_CODE = re.compile(r"^\[IB\s+(\d{3,5})\]\s*(.*)$")
_IB_CONNECT = re.compile(r"^\[IB\]\s", re.I)
_HEADER_MISC = re.compile(r"^(size\s*/\s*stop|caps\b|V221\b.*\bstarting\b)", re.I)
_ROLL = re.compile(r"^\[ROLL\]", re.I)
_DISCONNECT = re.compile(r"\bDISCONNECTED\b", re.I)
_SIGTERM = re.compile(r"\bsignal\s+15\b|\bSIGTERM\b", re.I)
_ERROR_LINE = re.compile(r"^(Traceback|ERROR\b|\w+Error:)")

# IB "farm connection" notices are informational; everything else is a warning.
_IB_INFO_CODES = {2104, 2106, 2107, 2108, 2158}

_SUMMARY_INT_KEYS = {
    "triggers": re.compile(r"\btriggers?\s*[:=]?\s*(\d+)", re.I),
    "entries": re.compile(r"\bentries\s*[:=]?\s*(\d+)", re.I),
    "closed": re.compile(r"\bclosed\s*[:=]?\s*(\d+)", re.I),
    "wins": re.compile(r"\bwins?\s*[:=]?\s*(\d+)\b(?!\s*%)", re.I),
}
_SUMMARY_WIN_RATE = re.compile(r"\bwin(?:\s*rate|%)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*%?", re.I)
_SUMMARY_PNL = re.compile(r"\b(?:net|p&l|pnl)\b[^$\d-]*(" + _NUM + ")", re.I)
_SUMMARY_NLV = re.compile(r"(" + _NUM + r")\s*(?:->|→)\s*(" + _NUM + ")")


def to_decimal(text: str | float | int | None) -> Decimal | None:
    if text is None:
        return None
    if isinstance(text, int | float):
        return Decimal(str(text))
    cleaned = text.strip().replace(",", "").replace("$", "")
    if cleaned.endswith("%"):
        cleaned = cleaned[:-1]
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _direction(token: str) -> str:
    return "LONG" if token.upper() in ("BUY", "LONG") else "SHORT"


def _held_minutes(text: str) -> Decimal | None:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([mh]?)", text.strip())
    if not match:
        return to_decimal(text)
    value = Decimal(match.group(1))
    return value * 60 if match.group(2) == "h" else value


@dataclass
class LogLine:
    line_no: int
    ts: datetime | None
    text: str
    raw: str


@dataclass
class AccountSnapshot:
    ts: datetime
    line_no: int
    account: str
    nlv: Decimal | None
    cash: Decimal | None
    excess_liquidity: Decimal | None
    init_margin: Decimal | None
    maint_margin: Decimal | None


@dataclass
class Signal:
    bar_ts: datetime
    side: str
    line_no: int
    px: Decimal | None
    outcome: str = "SKIPPED"
    reason: str | None = None


@dataclass
class Fill:
    ts: datetime
    line_no: int
    action: str
    quantity: Decimal
    price: Decimal
    commission: Decimal | None
    role: str
    order_ref: str | None


@dataclass
class Trade:
    direction: str
    contracts: Decimal
    line_no: int
    symbol: str | None = None
    trade_no: int | None = None
    signal_bar_ts: datetime | None = None
    entry_signal_px: Decimal | None = None
    exit_signal_px: Decimal | None = None
    entry_ts: datetime | None = None
    exit_ts: datetime | None = None
    entry_px: Decimal | None = None
    exit_px: Decimal | None = None
    entry_slip_pts: Decimal | None = None
    exit_slip_pts: Decimal | None = None
    exit_reason: str | None = None
    points: Decimal | None = None
    gross_pnl: Decimal | None = None
    commission: Decimal | None = None
    net_pnl: Decimal | None = None
    hold_min: Decimal | None = None
    mae_pts: Decimal | None = None
    mfe_pts: Decimal | None = None
    signal_ts: datetime | None = None
    closed: bool = False


@dataclass
class OpsEvent:
    ts: datetime
    line_no: int
    category: str
    code: str | None
    severity: str
    message: str


@dataclass
class DailySummary:
    line_no: int
    session_date: date
    symbol: str
    params_fp: str | None
    triggers: int | None = None
    entries: int | None = None
    closed: int | None = None
    wins: int | None = None
    win_rate: Decimal | None = None
    net_pnl: Decimal | None = None
    nlv_start: Decimal | None = None
    nlv_end: Decimal | None = None


@dataclass
class PaperDay:
    file_date: date
    lines: list[LogLine] = field(default_factory=list)
    params_fp: str | None = None
    broker_account: str | None = None
    broker_client_id: int | None = None
    symbol: str | None = None
    snapshots: list[AccountSnapshot] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    events: list[OpsEvent] = field(default_factory=list)
    summary: DailySummary | None = None
    gate_blocks: int = 0
    loss_cap_blocks: int = 0
    unparsed: list[int] = field(default_factory=list)

    @property
    def first_ts(self) -> datetime | None:
        return next((ln.ts for ln in self.lines if ln.ts), None)

    @property
    def last_ts(self) -> datetime | None:
        return next((ln.ts for ln in reversed(self.lines) if ln.ts), None)

    @property
    def loss_cap_hit(self) -> bool:
        return self.loss_cap_blocks > 0

    @property
    def nlv_start(self) -> Decimal | None:
        if self.summary and self.summary.nlv_start is not None:
            return self.summary.nlv_start
        return next((s.nlv for s in self.snapshots if s.nlv is not None), None)

    @property
    def nlv_end(self) -> Decimal | None:
        if self.summary and self.summary.nlv_end is not None:
            return self.summary.nlv_end
        return next((s.nlv for s in reversed(self.snapshots) if s.nlv is not None), None)

    def checks(self) -> dict[str, object]:
        """Reconciles the parsed trades with the engine's own DAILY SUMMARY."""
        closed = [t for t in self.trades if t.closed]
        parsed_net = sum((t.net_pnl or Decimal(0) for t in closed), Decimal(0))
        out: dict[str, object] = {
            "trades_parsed": len(closed),
            "open_trades": len(self.trades) - len(closed),
            "net_parsed": float(parsed_net),
            "unparsed_lines": len(self.unparsed),
            "summary_present": self.summary is not None,
        }
        if self.summary is not None:
            if self.summary.closed is not None:
                out["closed_reported"] = self.summary.closed
                out["closed_match"] = self.summary.closed == len(closed)
            if self.summary.net_pnl is not None:
                out["net_reported"] = float(self.summary.net_pnl)
                out["net_match"] = abs(self.summary.net_pnl - parsed_net) <= Decimal("1")
        return out

    @property
    def consistent(self) -> bool:
        checks = self.checks()
        return (
            checks.get("closed_match", True) is not False
            and checks.get("net_match", True) is not False
        )


def _split_lines(text: str, file_date: date) -> list[LogLine]:
    out: list[LogLine] = []
    current_day = file_date
    last_clock: time | None = None
    for idx, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        ts: datetime | None = None
        rest = stripped
        full = _TS_FULL.match(stripped)
        if full:
            ts = datetime.fromisoformat(f"{full['d']}T{full['t']}").replace(tzinfo=NY)
            rest = full["rest"]
            current_day, last_clock = ts.date(), ts.time()
        else:
            only_time = _TS_TIME.match(stripped)
            if only_time:
                clock = time.fromisoformat(only_time["t"])
                # A log that crosses midnight with time-only stamps moves to the next day.
                if (
                    last_clock is not None
                    and clock < last_clock
                    and last_clock.hour - clock.hour >= 12
                ):
                    current_day += timedelta(days=1)
                last_clock = clock
                ts = datetime.combine(current_day, clock, tzinfo=NY)
                rest = only_time["rest"]
        out.append(LogLine(idx, ts, rest.strip(), raw))
    return out


def _clock(text: str) -> time:
    parts = [int(p) for p in text.split(":")]
    return time(parts[0], parts[1], parts[2] if len(parts) > 2 else 0)


def _bar_ts(clock: str, ref: datetime | None, file_date: date) -> datetime:
    day = ref.date() if ref else file_date
    return datetime.combine(day, _clock(clock), tzinfo=NY)


class _Parser:
    def __init__(self, file_date: date) -> None:
        self.day = PaperDay(file_date=file_date)
        self.pending_entry: Trade | None = None
        self.exiting: Trade | None = None
        self.last_closed: Trade | None = None
        self.last_signal: Signal | None = None
        self.last_ts: datetime | None = None
        self.in_summary = False

    def _ts(self, line: LogLine) -> datetime:
        if line.ts is not None:
            self.last_ts = line.ts
            return line.ts
        if self.last_ts is not None:
            return self.last_ts
        return datetime.combine(self.day.file_date, time(0), tzinfo=NY)

    def _trade(self, trade_no: int) -> Trade | None:
        return next((t for t in self.day.trades if t.trade_no == trade_no), None)

    def _event(self, line: LogLine, category: str, code: str | None, severity: str) -> None:
        self.day.events.append(
            OpsEvent(self._ts(line), line.line_no, category, code, severity, line.text[:2000])
        )

    def feed(self, line: LogLine) -> None:
        text = line.text
        if not text:
            return
        if self._header(line) or self._ops(line):
            return
        if self.in_summary:
            self._summary_line(line)
            return
        handlers = (
            self._account,
            self._trigger,
            self._no_entries,
            self._entry,
            self._order,
            self._filled,
            self._exit,
            self._closed,
            self._totals,
            self._summary_start,
        )
        for handler in handlers:
            if handler(line):
                return
        if _HOLD.match(text):
            return
        self.day.unparsed.append(line.line_no)

    def _header(self, line: LogLine) -> bool:
        text = line.text
        if fp := _PARAMS_FP.search(text):
            self.day.params_fp = self.day.params_fp or fp.group(1)
            return not text.upper().startswith("DAILY SUMMARY")
        if acct := _ACCOUNT_HDR.match(text):
            self.day.broker_account = acct.group(1)
            return True
        if _IB_CONNECT.match(text):
            if cid := _CLIENT_ID.search(text):
                self.day.broker_client_id = int(cid.group(1))
            return True
        if contract := _CONTRACT.match(text):
            self.day.symbol = contract.group(1)
            return True
        if _HEADER_MISC.match(text):
            return True
        if _ROLL.match(text):
            self._event(line, "SYSTEM", "ROLL", "INFO")
            return True
        return False

    def _ops(self, line: LogLine) -> bool:
        text = line.text
        if ib := _IB_CODE.match(text):
            code = int(ib.group(1))
            severity = "INFO" if code in _IB_INFO_CODES else "WARN"
            self._event(line, "BROKER", f"IB_{code}", severity)
            return True
        if _DISCONNECT.search(text):
            self._event(line, "CONNECTIVITY", "DISCONNECTED", "ERROR")
            return True
        if _SIGTERM.search(text):
            self._event(line, "SYSTEM", "SIGTERM", "WARN")
            return True
        if _ERROR_LINE.match(text):
            self._event(line, "SYSTEM", "ERROR", "ERROR")
            return True
        return False

    def _account(self, line: LogLine) -> bool:
        match = _ACCOUNT_JSON.match(line.text)
        if not match:
            return False
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            self.day.unparsed.append(line.line_no)
            return True
        lowered = {str(k).lower().replace("_", ""): v for k, v in payload.items()}

        def pick(*keys: str) -> Decimal | None:
            for key in keys:
                if key in lowered and lowered[key] not in (None, ""):
                    return to_decimal(str(lowered[key]))
            return None

        account = str(lowered.get("account") or self.day.broker_account or "")
        self.day.snapshots.append(
            AccountSnapshot(
                ts=self._ts(line),
                line_no=line.line_no,
                account=account,
                nlv=pick("netliquidation", "netliq", "nlv"),
                cash=pick("totalcashvalue", "cash", "cashbalance"),
                excess_liquidity=pick("excessliquidity", "excess"),
                init_margin=pick("initmarginreq", "initmargin", "fullinitmarginreq"),
                maint_margin=pick("maintmarginreq", "maintmargin", "fullmaintmarginreq"),
            )
        )
        return True

    def _trigger(self, line: LogLine) -> bool:
        match = _TRIGGER.match(line.text)
        if not match:
            return False
        side = _direction(match.group(1))
        bar_ts = _bar_ts(match.group(2), line.ts or self.last_ts, self.day.file_date)
        self._ts(line)
        close = _CLOSE_PX.search(match["mid"])
        outcome_text = match["out"].strip()
        signal = Signal(bar_ts, side, line.line_no, to_decimal(close.group(1)) if close else None)
        if not outcome_text.upper().startswith("PASS"):
            signal.outcome = "BLOCKED"
            signal.reason = outcome_text[:500]
            self.day.gate_blocks += 1
        existing = next(
            (s for s in self.day.signals if s.bar_ts == bar_ts and s.side == side), None
        )
        if existing is None:
            self.day.signals.append(signal)
            self.last_signal = signal
        else:
            self.last_signal = existing
        return True

    def _no_entries(self, line: LogLine) -> bool:
        match = _NO_ENTRIES.match(line.text)
        if not match:
            return False
        reason = match.group(1).strip()
        if "loss limit" in reason.lower() or "loss cap" in reason.lower():
            self.day.loss_cap_blocks += 1
            code = "LOSS_CAP"
            severity = "WARN"
        elif "cutoff" in reason.lower():
            code, severity = "PAST_CUTOFF", "INFO"
        else:
            code, severity = "NO_ENTRY", "INFO"
        self._event(line, "RISK", code, severity)
        if self.last_signal is not None and self.last_signal.outcome == "SKIPPED":
            self.last_signal.outcome = "BLOCKED"
            self.last_signal.reason = reason[:500]
        return True

    def _entry(self, line: LogLine) -> bool:
        match = _ENTRY.match(line.text)
        if not match:
            return False
        ts = self._ts(line)
        trade = Trade(
            direction=_direction(match.group(1)),
            contracts=Decimal(match.group(2)),
            line_no=line.line_no,
            symbol=match.group(3),
            signal_ts=ts,
        )
        if close := _CLOSE_PX.search(line.text):
            trade.entry_signal_px = to_decimal(close.group(1))
        if match.group(4) and (bar_clock := _CLOCK.search(match.group(4))):
            trade.signal_bar_ts = _bar_ts(bar_clock.group(1), ts, self.day.file_date)
        if self.last_signal is not None and self.last_signal.side == trade.direction:
            self.last_signal.outcome = "ENTERED"
            self.last_signal.reason = None
            trade.signal_bar_ts = trade.signal_bar_ts or self.last_signal.bar_ts
            trade.entry_signal_px = trade.entry_signal_px or self.last_signal.px
        self.day.trades.append(trade)
        self.pending_entry = trade
        return True

    def _order(self, line: LogLine) -> bool:
        if not line.text.upper().startswith("[ORDER]"):
            return False
        match = _ORDER_FILLED.match(line.text)
        if not match:
            return True
        ts = self._ts(line)
        qty = Decimal(match.group(1))
        price = to_decimal(match.group(3))
        if price is None:
            self.day.unparsed.append(line.line_no)
            return True
        comm = _COMMISSION.search(line.text)
        commission = to_decimal(comm.group(1)) if comm else None
        side = _SIDE_TOKEN.search(line.text.split("FILLED")[0])
        ref = _ORDER_REF.search(line.text)
        role = "ADJUST"
        trade: Trade | None = None
        if self.pending_entry is not None and self.pending_entry.entry_ts is None:
            trade, role = self.pending_entry, "ENTRY"
            trade.entry_ts, trade.entry_px = ts, price
        elif self.exiting is not None and self.exiting.exit_px is None:
            trade = self.exiting
            reason = (trade.exit_reason or "").upper()
            role = "FLATTEN" if "FLATTEN" in reason else "STOP" if "STOP" in reason else "EXIT"
            trade.exit_ts, trade.exit_px = ts, price
        if side is not None:
            action = side.group(1)
        elif trade is not None:
            buy_side = (trade.direction == "LONG") == (role == "ENTRY")
            action = "BUY" if buy_side else "SELL"
        else:
            self.day.unparsed.append(line.line_no)
            return True
        self.day.fills.append(
            Fill(
                ts,
                line.line_no,
                action,
                qty,
                price,
                commission,
                role,
                ref.group(1) if ref else None,
            )
        )
        return True

    def _filled(self, line: LogLine) -> bool:
        match = _FILLED.match(line.text)
        if not match:
            return False
        trade_no = int(match.group(1))
        price = to_decimal(match.group(2))
        slip = to_decimal(match.group(3)) if match.group(3) else None
        trade = self._trade(trade_no)
        if trade is None and self.pending_entry is not None and self.pending_entry.trade_no is None:
            trade = self.pending_entry
            trade.trade_no = trade_no
        if trade is None:
            self.day.unparsed.append(line.line_no)
            return True
        if trade is self.exiting:
            trade.exit_px = trade.exit_px or price
            trade.exit_slip_pts = slip
        else:
            trade.entry_px = trade.entry_px or price
            trade.entry_ts = trade.entry_ts or self._ts(line)
            trade.entry_slip_pts = slip
            self.pending_entry = None
        return True

    def _exit(self, line: LogLine) -> bool:
        match = _EXIT.match(line.text)
        if not match:
            return False
        trade = self._trade(int(match.group(2))) or self.pending_entry
        if trade is None:
            self.day.unparsed.append(line.line_no)
            return True
        trade.trade_no = trade.trade_no or int(match.group(2))
        trade.exit_reason = match.group(1)
        if close := _CLOSE_PX.search(line.text):
            trade.exit_signal_px = to_decimal(close.group(1))
        self.exiting = trade
        self.pending_entry = None
        return True

    def _closed(self, line: LogLine) -> bool:
        match = _CLOSED.match(line.text)
        if not match:
            return False
        trade_no = int(match.group(1))
        trade = self._trade(trade_no)
        rest = match["rest"]
        if trade is None:
            dir_match = _DIRECTION.search(rest)
            qty_match = _QTY_AFTER_DIR.search(rest)
            trade = Trade(
                direction=_direction(dir_match.group(1)) if dir_match else "LONG",
                contracts=Decimal(qty_match.group(1)) if qty_match else Decimal(0),
                line_no=line.line_no,
                trade_no=trade_no,
                symbol=self.day.symbol,
            )
            self.day.trades.append(trade)
        self._closed_legs(trade, rest, line)
        trade.closed = True
        trade.exit_ts = trade.exit_ts or self._ts(line)
        self.last_closed = trade
        if self.exiting is trade:
            self.exiting = None
        if any(f"{k}=" in rest for k in _TOTALS_KEYS):
            self._apply_totals(trade, rest)
        return True

    def _closed_legs(self, trade: Trade, rest: str, line: LogLine) -> None:
        legs = re.split(r"\b(?:pts|gross)\s*=", rest)[0]
        entry_px = _ENTRY_PX.search(legs)
        exit_px = _EXIT_PX.search(legs)
        arrow = _ARROW_PX.search(legs)
        if entry_px and exit_px:
            trade.entry_px = trade.entry_px or to_decimal(entry_px.group(1))
            trade.exit_px = trade.exit_px or to_decimal(exit_px.group(1))
        elif arrow:
            trade.entry_px = trade.entry_px or to_decimal(arrow.group(1))
            trade.exit_px = trade.exit_px or to_decimal(arrow.group(2))
        clocks = _CLOCK.findall(legs)
        if len(clocks) >= 2:
            ref = line.ts or self.last_ts
            trade.entry_ts = trade.entry_ts or _bar_ts(clocks[0], ref, self.day.file_date)
            trade.exit_ts = trade.exit_ts or _bar_ts(clocks[-1], ref, self.day.file_date)

    def _apply_totals(self, trade: Trade, text: str) -> None:
        values = {k.lower(): v for k, v in _KV.findall(text)}
        if "pts" in values:
            trade.points = to_decimal(values["pts"])
        if "gross" in values:
            trade.gross_pnl = to_decimal(values["gross"])
        if "commission" in values:
            trade.commission = to_decimal(values["commission"])
        if "net" in values:
            trade.net_pnl = to_decimal(values["net"])
        if "held" in values:
            trade.hold_min = _held_minutes(values["held"])
        if "mae" in values:
            trade.mae_pts = to_decimal(values["mae"])
        if "mfe" in values:
            trade.mfe_pts = to_decimal(values["mfe"])

    def _totals(self, line: LogLine) -> bool:
        if self.last_closed is None:
            return False
        text = line.text
        if "pts=" not in text or "net=" not in text:
            return False
        self._apply_totals(self.last_closed, text)
        return True

    def _summary_start(self, line: LogLine) -> bool:
        match = _SUMMARY.match(line.text)
        if not match:
            return False
        params = _PARAMS_INLINE.search(line.text)
        self.day.summary = DailySummary(
            line_no=line.line_no,
            session_date=date.fromisoformat(match.group(1)),
            symbol=match.group(2),
            params_fp=params.group(1) if params else None,
        )
        self.day.params_fp = self.day.params_fp or self.day.summary.params_fp
        self.in_summary = True
        return True

    def _summary_line(self, line: LogLine) -> None:
        summary = self.day.summary
        assert summary is not None
        text = line.text
        if text.lstrip().startswith("#"):
            return
        lowered = text.lower()
        if ("account" in lowered or "netliq" in lowered or "nlv" in lowered) and (
            nlv := _SUMMARY_NLV.search(text)
        ):
            summary.nlv_start = to_decimal(nlv.group(1))
            summary.nlv_end = to_decimal(nlv.group(2))
            return
        for key, pattern in _SUMMARY_INT_KEYS.items():
            if (found := pattern.search(text)) and getattr(summary, key) is None:
                setattr(summary, key, int(found.group(1)))
        if (rate := _SUMMARY_WIN_RATE.search(text)) and summary.win_rate is None:
            value = Decimal(rate.group(1))
            summary.win_rate = value / 100 if value > 1 else value
        if (pnl := _SUMMARY_PNL.search(text)) and summary.net_pnl is None:
            summary.net_pnl = to_decimal(pnl.group(1))


def parse_paper_log(text: str, file_date: date) -> PaperDay:
    parser = _Parser(file_date)
    lines = _split_lines(text, file_date)
    parser.day.lines = lines
    for line in lines:
        parser.feed(line)
    return parser.day
