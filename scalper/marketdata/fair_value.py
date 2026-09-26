"""Fair value for USDT-GBP: cross-implied price blended with the local microprice.

implied USDT-GBP = USDT-USD * (BTC-GBP / BTC-USD)

BTC-GBP / BTC-USD is a liquid proxy for GBP/USD; USDT-USD is deep. Both lead the
thin USDT-GBP book, which is the informational edge of the quoter.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from scalper.marketdata.orderbook import BookTop


@dataclass
class RefPrice:
    price: Decimal
    ts: float


@dataclass
class FairValueModel:
    usdt_usd_id: str
    btc_gbp_id: str
    btc_usd_id: str
    blend_weight: Decimal = Decimal("0.5")
    max_age_s: float = 30.0
    refs: dict[str, RefPrice] = field(default_factory=dict)

    def on_ticker(self, product_id: str, price: Decimal, ts: float) -> None:
        if product_id in (self.usdt_usd_id, self.btc_gbp_id, self.btc_usd_id):
            self.refs[product_id] = RefPrice(price, ts)

    def implied(self, now: float) -> Decimal | None:
        try:
            u, g, d = (self.refs[self.usdt_usd_id], self.refs[self.btc_gbp_id], self.refs[self.btc_usd_id])
        except KeyError:
            return None
        if any(now - r.ts > self.max_age_s for r in (u, g, d)) or d.price <= 0:
            return None
        return u.price * g.price / d.price

    def fair(self, top: BookTop, now: float) -> Decimal | None:
        micro = top.microprice
        imp = self.implied(now)
        if micro is None:
            return imp
        if imp is None:
            return micro
        w = self.blend_weight
        return w * imp + (1 - w) * micro


class RollingVol:
    """Range of the mid price over a trailing window, expressed in ticks."""

    def __init__(self, window_s: float, tick: Decimal) -> None:
        self.window_s = window_s
        self.tick = tick
        self._pts: deque[tuple[float, Decimal]] = deque()

    def update(self, ts: float, mid: Decimal) -> None:
        self._pts.append((ts, mid))
        cutoff = ts - self.window_s
        while self._pts and self._pts[0][0] < cutoff:
            self._pts.popleft()

    def range_ticks(self) -> Decimal:
        if len(self._pts) < 2:
            return Decimal(0)
        vals = [m for _, m in self._pts]
        return (max(vals) - min(vals)) / self.tick
