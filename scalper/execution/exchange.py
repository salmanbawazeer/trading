"""Exchange abstraction shared by the live Coinbase adapter and the paper simulator."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from scalper.risk.fee_check import FeeRates
from scalper.strategy.inventory import Fill

FillCallback = Callable[[Fill], Awaitable[None]]


@dataclass(frozen=True)
class ProductMeta:
    product_id: str
    quote_increment: Decimal  # tick
    base_increment: Decimal
    base_min_size: Decimal


@dataclass(frozen=True)
class OrderAck:
    accepted: bool
    order_id: str | None
    client_order_id: str
    reason: str = ""


@dataclass
class OpenOrder:
    order_id: str
    client_order_id: str
    side: str
    price: Decimal
    size: Decimal
    placed_ts: float


class Exchange(Protocol):
    name: str

    async def product_meta(self) -> ProductMeta: ...

    async def fee_rates(self) -> FeeRates: ...

    async def balances(self) -> dict[str, Decimal]: ...

    async def place_post_only(
        self, side: str, price: Decimal, size: Decimal, client_order_id: str
    ) -> OrderAck: ...

    async def cancel(self, order_ids: list[str]) -> None: ...

    async def cancel_all(self) -> int: ...

    async def open_orders(self) -> list[OpenOrder]: ...

    def set_fill_callback(self, cb: FillCallback) -> None: ...
