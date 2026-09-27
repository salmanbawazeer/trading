"""Open-trade ledger for the profit lock.

Every fill either closes an open trade on the other side or opens a new one:
* a BUY first closes the highest-priced open SELL (buying back what was sold),
  otherwise it opens a BUY lot;
* a SELL first closes the lowest-priced open BUY, otherwise it opens a SELL lot.

From the open lots the quoter gets a floor for the ask (never sell an open buy
below its cost plus both fees plus a margin) and a ceiling for the bid (never buy
back an open sell above its price minus both fees minus a margin). Inventory the
bot started with has no lot, so it is not restricted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_UP, Decimal


@dataclass
class Lot:
    price: Decimal
    size: Decimal
    ts: float


@dataclass
class LotBook:
    buys: list[Lot] = field(default_factory=list)  # open long trades, waiting to be sold
    sells: list[Lot] = field(default_factory=list)  # open short trades, waiting to be bought back
    realized_gbp: Decimal = Decimal(0)  # gross, before fees
    closed: int = 0
    # Leftovers smaller than this count as closed. Orders are sized in GBP, so a buy of
    # 33.33 USDT is usually matched by a sell of 33.32; the 0.01 residue must not keep a
    # floor on the ask forever. Set to the product's minimum order size.
    dust: Decimal = Decimal(0)

    def apply(self, side: str, price: Decimal, size: Decimal, ts: float) -> None:
        remaining = size
        if side == "SELL":
            book, opposite, pick = self.buys, self.sells, min  # close cheapest buy first
        else:
            book, opposite, pick = self.sells, self.buys, max  # close dearest sell first
        while remaining > 0 and book:
            lot = pick(book, key=lambda lt: lt.price)
            take = min(remaining, lot.size)
            pnl = (price - lot.price) * take if side == "SELL" else (lot.price - price) * take
            self.realized_gbp += pnl
            lot.size -= take
            remaining -= take
            if lot.size <= self.dust:
                book.remove(lot)
                self.closed += 1
        if remaining > self.dust:
            opposite.append(Lot(price, remaining, ts))

    @staticmethod
    def _round(price: Decimal, tick: Decimal, rounding: str) -> Decimal:
        return (price / tick).to_integral_value(rounding) * tick

    def ask_floor(self, maker_fee: Decimal, tick: Decimal, min_profit_ticks: int) -> Decimal | None:
        """Lowest ask that still sells the cheapest open buy at a profit after fees."""
        if not self.buys:
            return None
        cost = min(lt.price for lt in self.buys)
        breakeven = cost * (1 + maker_fee) / (1 - maker_fee)
        return self._round(breakeven, tick, ROUND_UP) + min_profit_ticks * tick

    def bid_ceiling(self, maker_fee: Decimal, tick: Decimal, min_profit_ticks: int) -> Decimal | None:
        """Highest bid that still buys back the dearest open sell at a profit after fees."""
        if not self.sells:
            return None
        sold = max(lt.price for lt in self.sells)
        breakeven = sold * (1 - maker_fee) / (1 + maker_fee)
        return self._round(breakeven, tick, ROUND_DOWN) - min_profit_ticks * tick

    def open_size(self, side: str) -> Decimal:
        return sum((lt.size for lt in (self.buys if side == "BUY" else self.sells)), Decimal(0))
