"""Live adapter over the official coinbase-advanced-py REST client.

All SDK calls are synchronous; they run in a worker thread behind a token
bucket so the bot never exceeds its private-endpoint budget. Fills arrive via
the `user` WebSocket channel and are forwarded by the engine to `on_user_order`.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

import structlog

from scalper.execution.exchange import FillCallback, OpenOrder, OrderAck, ProductMeta
from scalper.risk.fee_check import FeeRates
from scalper.strategy.inventory import Fill

log = structlog.get_logger(__name__)


def _field(obj, key: str, default=None):
    """Read `key` from a dict or an attribute object.

    The SDK annotates nested response fields as typed objects but stores the raw
    dict it received (only top-level lists such as `orders` are wrapped), so the
    adapter must accept both shapes.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class TokenBucket:
    def __init__(self, rate_per_s: float, burst: int | None = None) -> None:
        self.rate = rate_per_s
        self.capacity = burst or max(1, int(rate_per_s))
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


class CoinbaseLiveExchange:
    name = "coinbase"

    def __init__(
        self, product_id: str, api_key: str, api_secret: str, requests_per_second: float = 5.0
    ) -> None:
        from coinbase.rest import RESTClient  # imported lazily so tests need no network

        self.product_id = product_id
        self.rest = RESTClient(api_key=api_key, api_secret=api_secret, rate_limit_headers=True)
        self.bucket = TokenBucket(requests_per_second)
        self._fill_cb: FillCallback | None = None
        self._orders: dict[str, OpenOrder] = {}
        self._filled_so_far: dict[str, Decimal] = {}

    async def _call(self, fn, *args, **kwargs):
        await self.bucket.acquire()
        return await asyncio.to_thread(fn, *args, **kwargs)

    # -- Exchange protocol ---------------------------------------------------
    async def product_meta(self) -> ProductMeta:
        p = await self._call(self.rest.get_product, self.product_id)
        return ProductMeta(
            product_id=self.product_id,
            quote_increment=Decimal(p.quote_increment),
            base_increment=Decimal(p.base_increment),
            base_min_size=Decimal(p.base_min_size),
        )

    async def fee_rates(self) -> FeeRates:
        s = await self._call(self.rest.get_transaction_summary, product_type="SPOT")
        tier = _field(s, "fee_tier")
        if tier is None:
            raise RuntimeError("transaction_summary response carried no fee_tier")
        return FeeRates(
            maker=Decimal(str(_field(tier, "maker_fee_rate", "0"))),
            taker=Decimal(str(_field(tier, "taker_fee_rate", "0"))),
            tier=str(_field(tier, "pricing_tier", "unknown")),
        )

    async def balances(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        cursor = None
        while True:
            resp = await self._call(self.rest.get_accounts, limit=250, cursor=cursor)
            for a in resp.accounts:
                bal = _field(a, "available_balance")
                cur = _field(a, "currency")
                if bal is not None and cur:
                    out[cur] = out.get(cur, Decimal(0)) + Decimal(str(_field(bal, "value", "0")))
            if not getattr(resp, "has_next", False):
                break
            cursor = resp.cursor
        return out

    def set_fill_callback(self, cb: FillCallback) -> None:
        self._fill_cb = cb

    async def place_post_only(
        self, side: str, price: Decimal, size: Decimal, client_order_id: str
    ) -> OrderAck:
        resp = await self._call(
            self.rest.limit_order_gtc,
            client_order_id=client_order_id,
            product_id=self.product_id,
            side=side,
            base_size=str(size),
            limit_price=str(price),
            post_only=True,
        )
        if not resp.success:
            err = _field(resp, "error_response") or _field(resp, "failure_reason") or {}
            reason = _field(err, "error") or _field(err, "message") or str(err)
            log.warning("order_rejected", side=side, price=str(price), size=str(size), reason=reason)
            return OrderAck(False, None, client_order_id, reason)
        oid = _field(_field(resp, "success_response"), "order_id") or _field(resp, "order_id")
        if not oid:
            log.error("order_ack_without_id", response=str(resp))
            return OrderAck(False, None, client_order_id, "no_order_id_in_response")
        self._orders[oid] = OpenOrder(oid, client_order_id, side, price, size, time.time())
        return OrderAck(True, oid, client_order_id)

    async def cancel(self, order_ids: list[str]) -> None:
        if not order_ids:
            return
        await self._call(self.rest.cancel_orders, order_ids=order_ids)
        for oid in order_ids:
            self._orders.pop(oid, None)

    async def cancel_all(self) -> int:
        resp = await self._call(self.rest.list_orders, product_ids=[self.product_id], order_status=["OPEN"])
        ids = [_field(o, "order_id") for o in (_field(resp, "orders") or []) if _field(o, "order_id")]
        if ids:
            await self.cancel(ids)
        self._orders.clear()
        return len(ids)

    async def open_orders(self) -> list[OpenOrder]:
        return list(self._orders.values())

    # -- fills from the `user` channel ---------------------------------------
    async def on_user_order(self, o: dict, ts: float) -> None:
        """Consume one order object from a `user` channel event."""
        if o.get("product_id") != self.product_id:
            return
        oid = o.get("order_id")
        if not oid:
            return
        cum = Decimal(o.get("cumulative_quantity") or "0")
        prev = self._filled_so_far.get(oid, Decimal(0))
        delta = cum - prev
        status = o.get("status", "")
        if delta > 0:
            self._filled_so_far[oid] = cum
            price = Decimal(o.get("avg_price") or o.get("limit_price") or "0")
            fee_total = Decimal(o.get("total_fees") or "0")
            fee = fee_total * (delta / cum) if cum > 0 else Decimal(0)
            known = self._orders.get(oid)
            if self._fill_cb:
                await self._fill_cb(
                    Fill(
                        order_id=oid,
                        client_order_id=o.get("client_order_id", known.client_order_id if known else ""),
                        side=(o.get("order_side") or (known.side if known else "")).upper(),
                        price=price,
                        size=delta,
                        fee=fee,
                        ts=ts,
                    )
                )
        if status in ("FILLED", "CANCELLED", "EXPIRED", "FAILED"):
            self._orders.pop(oid, None)
        # Keep cumulative-fill memory for terminal orders too, so a redelivered
        # final state never double-counts; prune oldest entries when large.
        if len(self._filled_so_far) > 5000:
            for old in list(self._filled_so_far)[:1000]:
                self._filled_so_far.pop(old, None)
