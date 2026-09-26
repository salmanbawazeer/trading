"""Hard risk vetoes. Every quoting cycle and every order passes through here."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from scalper.marketdata.orderbook import OrderBook
from scalper.strategy.inventory import Inventory


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str = ""


class RiskGuards:
    def __init__(
        self,
        *,
        capital_gbp: Decimal,
        daily_loss_cap_pct: Decimal,
        stale_book_seconds: float,
        max_spread_ticks: int,
        max_open_orders: int,
        tick: Decimal,
        kill_switch_path: Path,
    ) -> None:
        self.capital_gbp = capital_gbp
        self.daily_loss_cap = capital_gbp * daily_loss_cap_pct
        self.stale_book_seconds = stale_book_seconds
        self.max_spread_ticks = max_spread_ticks
        self.max_open_orders = max_open_orders
        self.tick = tick
        self.kill_switch_path = kill_switch_path
        self.halted = False
        self.halt_reason = ""

    def halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason

    def reset_halt(self) -> None:
        self.halted = False
        self.halt_reason = ""

    def kill_switch_present(self) -> bool:
        return self.kill_switch_path.exists()

    def check_market(self, book: OrderBook, now: float) -> Verdict:
        if self.halted:
            return Verdict(False, f"halted:{self.halt_reason}")
        if self.kill_switch_present():
            return Verdict(False, "kill_switch")
        if not book.ready:
            return Verdict(False, "book_not_ready")
        if book.is_stale(now, self.stale_book_seconds):
            return Verdict(False, "stale_book")
        top = book.top()
        if top.spread is None:
            return Verdict(False, "one_sided_book")
        if top.spread / self.tick > self.max_spread_ticks:
            return Verdict(False, "spread_too_wide")
        return Verdict(True)

    def check_daily_loss(self, inv: Inventory, price: Decimal) -> Verdict:
        loss = -inv.day_pnl(price)
        if loss >= self.daily_loss_cap:
            self.halt(f"daily_loss_cap loss={loss:.4f} cap={self.daily_loss_cap:.4f}")
            return Verdict(False, self.halt_reason)
        return Verdict(True)

    def check_order(
        self, side: str, price: Decimal, size: Decimal, inv: Inventory, open_orders: int
    ) -> Verdict:
        if size <= 0:
            return Verdict(False, "size_zero")
        if open_orders >= self.max_open_orders:
            return Verdict(False, "max_open_orders")
        notional = price * size
        if side == "BUY" and not inv.can_buy(price, notional):
            return Verdict(False, "inventory_limit_buy")
        if side == "SELL" and not inv.can_sell(price, size):
            return Verdict(False, "inventory_limit_sell")
        return Verdict(True)
