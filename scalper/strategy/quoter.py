"""Pure quoting logic: turn market state + inventory into desired bid/ask prices."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from scalper.marketdata.orderbook import BookTop


@dataclass(frozen=True)
class QuoteParams:
    tick: Decimal
    min_edge_ticks: int = 1
    vol_multiplier: Decimal = Decimal("1.0")
    inventory_skew_ticks: Decimal = Decimal(2)
    max_fair_basis_ticks: int = 10


@dataclass(frozen=True)
class Quotes:
    bid: Decimal | None
    ask: Decimal | None
    fair: Decimal
    half_spread_ticks: Decimal
    skew_ticks: Decimal
    reason: str = ""


def round_to_tick(price: Decimal, tick: Decimal, rounding: str) -> Decimal:
    return (price / tick).quantize(Decimal(1), rounding=rounding) * tick


def compute_quotes(
    top: BookTop,
    fair: Decimal,
    vol_ticks: Decimal,
    inventory_dev: Decimal,
    params: QuoteParams,
    allow_bid: bool = True,
    allow_ask: bool = True,
) -> Quotes:
    """Compute post-only quotes around `fair`.

    * half spread = max(min_edge, vol_multiplier * vol/2) ticks
    * skew shifts both quotes against inventory: long USDT (dev > 0) -> lower quotes
    * never cross the book (post-only would be rejected); join or improve the touch
    * bid/ask kept at least 2*min_edge ticks apart
    """
    tick = params.tick
    if top.best_bid is None or top.best_ask is None:
        return Quotes(None, None, fair, Decimal(0), Decimal(0), "empty_book")

    # Distrust a fair that is wildly off the book (bad reference feed).
    mid = top.mid or fair
    basis_ticks = abs(fair - mid) / tick
    if basis_ticks > params.max_fair_basis_ticks:
        fair = mid
    half = max(Decimal(params.min_edge_ticks), params.vol_multiplier * vol_ticks / 2)
    skew = -inventory_dev * params.inventory_skew_ticks

    raw_bid = fair - half * tick + skew * tick
    raw_ask = fair + half * tick + skew * tick
    bid = round_to_tick(raw_bid, tick, ROUND_DOWN)
    ask = round_to_tick(raw_ask, tick, ROUND_UP)

    # Post-only: must not cross the opposite touch.
    bid = min(bid, top.best_ask - tick)
    ask = max(ask, top.best_bid + tick)

    # Maintain minimum width.
    min_width = 2 * params.min_edge_ticks * tick
    if ask - bid < min_width:
        centre = (ask + bid) / 2
        bid = round_to_tick(centre - min_width / 2, tick, ROUND_DOWN)
        ask = round_to_tick(centre + min_width / 2, tick, ROUND_UP)
        bid = min(bid, top.best_ask - tick)
        ask = max(ask, top.best_bid + tick)

    return Quotes(
        bid=bid if allow_bid else None,
        ask=ask if allow_ask else None,
        fair=fair,
        half_spread_ticks=half,
        skew_ticks=skew,
    )


def size_for_notional(
    notional_gbp: Decimal, price: Decimal, base_increment: Decimal, base_min: Decimal
) -> Decimal:
    size = (notional_gbp / price / base_increment).quantize(Decimal(1), rounding=ROUND_DOWN) * base_increment
    return size if size >= base_min else Decimal(0)
