"""Startup fee viability check.

Both quotes are post-only, so a round trip pays the maker fee twice. If that
exceeds the minimum edge the quoter targets, the strategy cannot be profitable
and the bot refuses to start.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class FeeRates:
    maker: Decimal
    taker: Decimal
    tier: str = "unknown"


@dataclass(frozen=True)
class FeeCheckResult:
    ok: bool
    round_trip_cost_frac: Decimal
    edge_frac: Decimal
    message: str


def check_fees(fees: FeeRates, min_edge_ticks: int, tick: Decimal, ref_price: Decimal) -> FeeCheckResult:
    round_trip = fees.maker * 2
    edge = Decimal(2 * min_edge_ticks) * tick / ref_price
    ok = round_trip < edge
    msg = (
        f"fee tier={fees.tier} maker={fees.maker:.6%} taker={fees.taker:.6%}; "
        f"round-trip cost={round_trip:.6%} vs target edge={edge:.6%} -> {'OK' if ok else 'NOT VIABLE'}"
    )
    if not ok:
        msg += (
            ". USDT-GBP is not on stable-pair pricing for this account; passive spread capture "
            "cannot cover fees. Refusing to trade."
        )
    return FeeCheckResult(ok, round_trip, edge, msg)
