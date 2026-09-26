"""Inventory and PnL accounting for a two-currency market maker.

Base currency: USDT. Quote currency: GBP. The bot targets holding
`target_usdt_fraction` of capital in USDT and skews quotes to return there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class Fill:
    order_id: str
    client_order_id: str
    side: str  # BUY = buy USDT with GBP, SELL = sell USDT for GBP
    price: Decimal
    size: Decimal  # USDT
    fee: Decimal  # GBP
    ts: float
    liquidity: str = "MAKER"


@dataclass
class Inventory:
    capital_gbp: Decimal
    target_usdt_fraction: Decimal = Decimal("0.5")
    max_inventory_pct: Decimal = Decimal("0.5")
    gbp: Decimal = Decimal(0)
    usdt: Decimal = Decimal(0)
    fees_paid: Decimal = Decimal(0)
    fills: list[Fill] = field(default_factory=list)
    start_equity: Decimal | None = None
    day_start_equity: Decimal | None = None

    def seed(self, gbp: Decimal, usdt: Decimal, price: Decimal) -> None:
        self.gbp, self.usdt = gbp, usdt
        eq = self.equity(price)
        self.start_equity = eq
        self.day_start_equity = eq

    def apply_fill(self, f: Fill) -> None:
        notional = f.price * f.size
        if f.side == "BUY":
            self.usdt += f.size
            self.gbp -= notional
        else:
            self.usdt -= f.size
            self.gbp += notional
        self.gbp -= f.fee
        self.fees_paid += f.fee
        self.fills.append(f)

    def equity(self, price: Decimal) -> Decimal:
        return self.gbp + self.usdt * price

    def pnl(self, price: Decimal) -> Decimal:
        return self.equity(price) - (self.start_equity or self.equity(price))

    def day_pnl(self, price: Decimal) -> Decimal:
        return self.equity(price) - (self.day_start_equity or self.equity(price))

    def roll_day(self, price: Decimal) -> None:
        self.day_start_equity = self.equity(price)

    def usdt_value_gbp(self, price: Decimal) -> Decimal:
        return self.usdt * price

    def deviation_fraction(self, price: Decimal) -> Decimal:
        """How far USDT holdings are from target, as a fraction of the allowed band.

        +1 means we hold the maximum allowed excess USDT (stop buying),
        -1 means we hold the minimum (stop selling). 0 is on target.
        """
        target = self.capital_gbp * self.target_usdt_fraction
        band = self.capital_gbp * self.max_inventory_pct
        if band <= 0:
            return Decimal(0)
        dev = (self.usdt_value_gbp(price) - target) / band
        return max(Decimal(-1), min(Decimal(1), dev))

    def can_buy(self, price: Decimal, notional: Decimal) -> bool:
        target = self.capital_gbp * self.target_usdt_fraction
        band = self.capital_gbp * self.max_inventory_pct
        return self.usdt_value_gbp(price) + notional <= target + band and self.gbp >= notional

    def can_sell(self, price: Decimal, size: Decimal) -> bool:
        target = self.capital_gbp * self.target_usdt_fraction
        band = self.capital_gbp * self.max_inventory_pct
        return (self.usdt - size) * price >= target - band and self.usdt >= size
