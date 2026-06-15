"""Alert domain types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


class Role(StrEnum):
    TRADER = "trader"
    ENGINEER = "engineer"
    OPS = "ops"


_RANK = {Severity.INFO: 0, Severity.WARN: 1, Severity.CRITICAL: 2}


def severity_rank(s: Severity) -> int:
    return _RANK[s]


@dataclass(frozen=True)
class Alert:
    severity: Severity
    role: Role
    title: str
    body: str


@dataclass
class Recipient:
    id: int
    name: str
    role: Role
    channel: str  # "log" | "sms" | "whatsapp"
    contact: str = ""  # phone for sms/whatsapp
    min_severity: Severity = Severity.WARN
