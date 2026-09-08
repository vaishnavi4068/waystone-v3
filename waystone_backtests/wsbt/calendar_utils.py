"""Calendar helpers shared by the calendar-effects and options strategies."""
from __future__ import annotations

import pandas as pd


def third_friday(year: int, month: int) -> pd.Timestamp:
    d = pd.Timestamp(year=year, month=month, day=1)
    offset = (4 - d.weekday()) % 7          # Friday = 4
    return d + pd.Timedelta(days=offset + 14)


def is_opex_week(dates: pd.DatetimeIndex) -> pd.Series:
    """True for Mon..Fri of the week containing the monthly third Friday."""
    out = []
    for d in dates:
        tf = third_friday(d.year, d.month)
        monday = tf - pd.Timedelta(days=4)
        out.append(monday <= d.normalize() <= tf)
    return pd.Series(out, index=dates)


def month_position(dates: pd.DatetimeIndex) -> pd.DataFrame:
    """For each trading day: its ordinal within the month (1 = first trading day) and the
    count from the end (1 = last trading day).  Computed on the trading days actually present."""
    s = pd.Series(dates, index=dates)
    ym = s.dt.to_period("M")
    first_ord = ym.groupby(ym).cumcount() + 1
    last_ord = ym.groupby(ym).cumcount(ascending=False) + 1
    return pd.DataFrame({"from_start": first_ord.values, "from_end": last_ord.values}, index=dates)


def next_expiry_on_or_after(d: pd.Timestamp, min_dte: int) -> pd.Timestamp:
    """Nearest monthly (third-Friday) expiry with at least min_dte calendar days."""
    y, m = d.year, d.month
    for _ in range(6):
        tf = third_friday(y, m)
        if (tf - d.normalize()).days >= min_dte:
            return tf
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return third_friday(y, m)
