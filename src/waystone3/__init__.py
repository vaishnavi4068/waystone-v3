"""Waystone v3 — nimble technical-momentum trading platform.

A greenfield rebuild. The core idea: every trading signal is a `SignalContributor`
(a pure function over OHLCV bars returning a normalized -10..+10 score). Technical
momentum signals (volume, MA crossover, price action) are first-class; sentiment is
just one optional contributor. Adding a new signal is one file + one registry line.
"""

__version__ = "0.1.0"
