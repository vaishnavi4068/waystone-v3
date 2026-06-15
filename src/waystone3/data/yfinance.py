"""yfinance market data source — free, no API key.

Wraps the synchronous ``yfinance`` library in a thread so the async pipeline isn't
blocked. Suitable for daily/intraday momentum on stocks, ETFs, and futures tickers.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal

import yfinance as yf

from waystone3.core.types import Bar, Timeframe

# Map our timeframes to yfinance interval strings and a default fetch period.
_INTERVAL = {
    Timeframe.M1: "1m",
    Timeframe.M5: "5m",
    Timeframe.M15: "15m",
    Timeframe.H1: "60m",
    Timeframe.D1: "1d",
}
# Period must comfortably exceed the lookback; intraday has hard server-side limits.
_PERIOD = {
    Timeframe.M1: "7d",
    Timeframe.M5: "60d",
    Timeframe.M15: "60d",
    Timeframe.H1: "730d",
    Timeframe.D1: "2y",
}


def _dec(x: object) -> Decimal:
    return Decimal(str(x))


def _rows_to_bars(symbol: str, timeframe: Timeframe, df: object) -> list[Bar]:
    bars: list[Bar] = []
    # df is a pandas DataFrame; iterate without importing pandas types.
    for ts, row in df.iterrows():  # type: ignore[attr-defined]
        py_ts: datetime = ts.to_pydatetime()
        bars.append(
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                open=_dec(row["Open"]),
                high=_dec(row["High"]),
                low=_dec(row["Low"]),
                close=_dec(row["Close"]),
                volume=_dec(row["Volume"]),
                timestamp=py_ts,
            )
        )
    return bars


class YFinanceDataSource:
    name = "yfinance"

    def _fetch(self, symbol: str, timeframe: Timeframe, **kwargs: object) -> list[Bar]:
        interval = _INTERVAL[timeframe]
        df = yf.Ticker(symbol).history(interval=interval, auto_adjust=True, **kwargs)
        if df.empty:
            return []
        return _rows_to_bars(symbol, timeframe, df)

    async def get_bars(
        self, symbol: str, timeframe: Timeframe, lookback: int
    ) -> list[Bar]:
        period = _PERIOD[timeframe]
        bars = await asyncio.to_thread(self._fetch, symbol, timeframe, period=period)
        return bars[-lookback:]

    async def get_history(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        return await asyncio.to_thread(
            self._fetch, symbol, timeframe, start=start, end=end
        )
