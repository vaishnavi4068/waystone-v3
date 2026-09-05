"""Cost model.  One object per instrument; every fill goes through `fill()`."""
from dataclasses import dataclass


@dataclass
class CostModel:
    """commission: $ per unit per side (share, contract, or spread leg).
    slippage_bps: half-spread + impact, in basis points of price, per side.
    slippage_abs: alternative fixed slippage in price units per side (futures ticks).
    multiplier: $ per 1.0 price move per unit (1 for shares, 2 for MNQ, 20 for NQ, 100 for options)."""
    commission: float = 0.0
    slippage_bps: float = 0.0
    slippage_abs: float = 0.0
    multiplier: float = 1.0

    def fill(self, price: float, side: int) -> float:
        """Price actually paid/received.  side=+1 buy, -1 sell."""
        return price + side * (price * self.slippage_bps / 1e4 + self.slippage_abs)

    def round_trip_cost(self, price: float, units: float = 1.0) -> float:
        """$ cost of buying and selling `units` at `price` (commission + slippage both sides)."""
        slip = 2 * (price * self.slippage_bps / 1e4 + self.slippage_abs) * self.multiplier
        return units * (2 * self.commission + slip)


# Presets — edit to match your IBKR schedule.
US_STOCK = CostModel(commission=0.005, slippage_bps=2.0, multiplier=1.0)         # IBKR Pro ~$0.005/sh, tight ETF spreads
US_ETF = CostModel(commission=0.005, slippage_bps=1.0, multiplier=1.0)
MNQ = CostModel(commission=0.62, slippage_abs=0.25, multiplier=2.0)              # $0.62/side, one tick slippage
MES = CostModel(commission=0.62, slippage_abs=0.25, multiplier=5.0)
NQ = CostModel(commission=2.25, slippage_abs=0.25, multiplier=20.0)
SPX_OPTION_LEG = CostModel(commission=0.65, slippage_abs=0.05, multiplier=100.0)  # per leg, $0.05 slippage on a $1-wide market
XSP_OPTION_LEG = CostModel(commission=0.65, slippage_abs=0.02, multiplier=100.0)
