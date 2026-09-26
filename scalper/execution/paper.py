"""Paper exchange: simulates post-only resting orders against the live (or replayed) tape.

Fill model (deliberately conservative):
* A resting BUY at P fills fully when a market trade prints strictly below P.
* At exactly P we assume we are behind the queue that was resting when we
  posted: we fill only after cumulative traded volume at P (since posting)
  exceeds queue_ahead + our size.
* Mirror logic for SELL orders.
* An order that would cross the book at placement is rejected, like a real
  post-only order.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from decimal import Decimal

from scalper.execution.exchange import FillCallback, OpenOrder, OrderAck, ProductMeta
from scalper.marketdata.orderbook import OrderBook
from scalper.risk.fee_check import FeeRates
from scalper.strategy.inventory import Fill


@dataclass
class _Resting:
    order: OpenOrder
    queue_ahead: Decimal
    traded_since: Decimal = field(default=Decimal(0))


class PaperExchange:
    name = "paper"

    def __init__(
        self,
        meta: ProductMeta,
        fees: FeeRates,
        book: OrderBook,
        balances: dict[str, Decimal],
        clock,
    ) -> None:
        self.meta = meta
        self.fees = fees
        self.book = book
        self._balances = dict(balances)
        self._clock = clock
        self._orders: dict[str, _Resting] = {}
        self._ids = itertools.count(1)
        self._fill_cb: FillCallback | None = None
        self.rejects = 0

    # -- Exchange protocol -------------------------------------------------
    async def product_meta(self) -> ProductMeta:
        return self.meta

    async def fee_rates(self) -> FeeRates:
        return self.fees

    async def balances(self) -> dict[str, Decimal]:
        return dict(self._balances)

    def set_fill_callback(self, cb: FillCallback) -> None:
        self._fill_cb = cb

    async def place_post_only(
        self, side: str, price: Decimal, size: Decimal, client_order_id: str
    ) -> OrderAck:
        top = self.book.top()
        if side == "BUY" and top.best_ask is not None and price >= top.best_ask:
            self.rejects += 1
            return OrderAck(False, None, client_order_id, "post_only_would_cross")
        if side == "SELL" and top.best_bid is not None and price <= top.best_bid:
            self.rejects += 1
            return OrderAck(False, None, client_order_id, "post_only_would_cross")
        order_id = f"paper-{next(self._ids)}"
        oo = OpenOrder(order_id, client_order_id, side, price, size, self._clock())
        self._orders[order_id] = _Resting(oo, queue_ahead=self.book.qty_at(side, price))
        return OrderAck(True, order_id, client_order_id)

    async def cancel(self, order_ids: list[str]) -> None:
        for oid in order_ids:
            self._orders.pop(oid, None)

    async def cancel_all(self) -> int:
        n = len(self._orders)
        self._orders.clear()
        return n

    async def open_orders(self) -> list[OpenOrder]:
        return [r.order for r in self._orders.values()]

    # -- simulation ----------------------------------------------------------
    async def on_market_trade(self, price: Decimal, size: Decimal, ts: float) -> None:
        for oid, r in list(self._orders.items()):
            o = r.order
            if o.side == "BUY":
                if price < o.price:
                    await self._fill(oid, ts)
                elif price == o.price:
                    r.traded_since += size
                    if r.traded_since >= r.queue_ahead + o.size:
                        await self._fill(oid, ts)
            else:
                if price > o.price:
                    await self._fill(oid, ts)
                elif price == o.price:
                    r.traded_since += size
                    if r.traded_since >= r.queue_ahead + o.size:
                        await self._fill(oid, ts)

    async def _fill(self, order_id: str, ts: float) -> None:
        r = self._orders.pop(order_id, None)
        if r is None:
            return
        o = r.order
        notional = o.price * o.size
        fee = notional * self.fees.maker
        base, quote = self.meta.product_id.split("-")
        if o.side == "BUY":
            self._balances[base] = self._balances.get(base, Decimal(0)) + o.size
            self._balances[quote] = self._balances.get(quote, Decimal(0)) - notional - fee
        else:
            self._balances[base] = self._balances.get(base, Decimal(0)) - o.size
            self._balances[quote] = self._balances.get(quote, Decimal(0)) + notional - fee
        if self._fill_cb:
            await self._fill_cb(Fill(o.order_id, o.client_order_id, o.side, o.price, o.size, fee, ts))
