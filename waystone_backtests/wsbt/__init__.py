"""wsbt — Waystone backtest toolkit.

Small, dependency-light (pandas + numpy + scipy) helpers shared by every strategy
folder under strategies/.  Conventions that every backtest follows:

  * Daily bars are DataFrames indexed by a tz-naive DatetimeIndex (exchange dates),
    columns: open, high, low, close, volume  (adj_close optional).
  * A signal computed from the bar that closes on day t is executed no earlier than
    the OPEN of day t+1.  Nothing in this toolkit lets a strategy see the bar it
    trades on.  (engine.simulate_positions enforces this by construction.)
  * Costs are always applied: commission per unit + slippage in bps (or ticks).
  * Every backtest can run with --synthetic, which generates data with the right
    shape so the mechanics can be exercised without market data.  Synthetic results
    prove the code runs; they say NOTHING about the strategy.
"""
from . import data, engine, metrics, costs, report  # noqa: F401

__all__ = ["data", "engine", "metrics", "costs", "report"]
