"""Level-2 order book maintained from Coinbase `l2_data` events."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class BookTop:
    best_bid: Decimal | None
    best_ask: Decimal | None
    bid_qty: Decimal
    ask_qty: Decimal

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def microprice(self) -> Decimal | None:
        """Quantity-weighted mid: leans toward the side with less resting size."""
        if self.best_bid is None or self.best_ask is None:
            return None
        total = self.bid_qty + self.ask_qty
        if total <= 0:
            return self.mid
        return (self.best_bid * self.ask_qty + self.best_ask * self.bid_qty) / total


class OrderBook:
    def __init__(self, product_id: str) -> None:
        self.product_id = product_id
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.last_update_ts: float | None = None
        self.snapshot_seen = False

    # -- ingestion ---------------------------------------------------------
    def apply_event(self, event: dict, ts: float) -> None:
        """Apply one event from an `l2_data` message (type snapshot|update)."""
        if event.get("product_id") not in (None, self.product_id):
            return
        if event.get("type") == "snapshot":
            self.bids.clear()
            self.asks.clear()
            self.snapshot_seen = True
        for upd in event.get("updates", []):
            side = upd["side"]
            price = Decimal(upd["price_level"])
            qty = Decimal(upd["new_quantity"])
            levels = self.bids if side == "bid" else self.asks
            if qty <= 0:
                levels.pop(price, None)
            else:
                levels[price] = qty
        self.last_update_ts = ts

    # -- queries -----------------------------------------------------------
    @property
    def best_bid(self) -> Decimal | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return min(self.asks) if self.asks else None

    def top(self) -> BookTop:
        bb, ba = self.best_bid, self.best_ask
        return BookTop(
            best_bid=bb,
            best_ask=ba,
            bid_qty=self.bids.get(bb, Decimal(0)) if bb is not None else Decimal(0),
            ask_qty=self.asks.get(ba, Decimal(0)) if ba is not None else Decimal(0),
        )

    def qty_at(self, side: str, price: Decimal) -> Decimal:
        levels = self.bids if side == "BUY" else self.asks
        return levels.get(price, Decimal(0))

    def imbalance(self, depth: int = 5) -> Decimal | None:
        """(bid_vol - ask_vol) / (bid_vol + ask_vol) over the top `depth` levels, in [-1, 1]."""
        if not self.bids or not self.asks:
            return None
        bids = sorted(self.bids.items(), key=lambda kv: kv[0], reverse=True)[:depth]
        asks = sorted(self.asks.items(), key=lambda kv: kv[0])[:depth]
        bid_vol = sum((q for _, q in bids), Decimal(0))
        ask_vol = sum((q for _, q in asks), Decimal(0))
        total = bid_vol + ask_vol
        if total <= 0:
            return Decimal(0)
        return (bid_vol - ask_vol) / total

    def is_stale(self, now: float, max_age_s: float) -> bool:
        return self.last_update_ts is None or (now - self.last_update_ts) > max_age_s

    @property
    def ready(self) -> bool:
        return self.snapshot_seen and bool(self.bids) and bool(self.asks)
