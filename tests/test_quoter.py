from decimal import Decimal

from scalper.marketdata.orderbook import BookTop
from scalper.strategy.quoter import QuoteParams, compute_quotes, size_for_notional

TICK = Decimal("0.0001")
P = QuoteParams(tick=TICK, min_edge_ticks=1, vol_multiplier=Decimal(1), inventory_skew_ticks=Decimal(2))


def top(bid="0.7500", ask="0.7502"):
    return BookTop(Decimal(bid), Decimal(ask), Decimal(100), Decimal(100))


def test_quotes_straddle_fair_and_never_cross():
    q = compute_quotes(top(), Decimal("0.7501"), Decimal(0), Decimal(0), P)
    assert q.bid == Decimal("0.7500") and q.ask == Decimal("0.7502")
    assert q.bid < top().best_ask and q.ask > top().best_bid


def test_wide_book_quotes_inside_spread():
    q = compute_quotes(top("0.7490", "0.7510"), Decimal("0.7500"), Decimal(0), Decimal(0), P)
    assert q.bid == Decimal("0.7499") and q.ask == Decimal("0.7501")


def test_fair_far_from_book_is_capped_to_mid():
    q = compute_quotes(top(), Decimal("0.7600"), Decimal(0), Decimal(0), P)
    assert q.fair == Decimal("0.7501")


def test_volatility_widens():
    q = compute_quotes(top("0.7480", "0.7520"), Decimal("0.7500"), Decimal(8), Decimal(0), P)
    assert q.half_spread_ticks == 4
    assert q.bid == Decimal("0.7496") and q.ask == Decimal("0.7504")


def test_inventory_skew_lowers_quotes_when_long():
    wide = top("0.7480", "0.7520")
    neutral = compute_quotes(wide, Decimal("0.7500"), Decimal(0), Decimal(0), P)
    long_ = compute_quotes(wide, Decimal("0.7500"), Decimal(0), Decimal(1), P)
    short = compute_quotes(wide, Decimal("0.7500"), Decimal(0), Decimal(-1), P)
    assert long_.bid < neutral.bid and long_.ask < neutral.ask
    assert short.bid > neutral.bid and short.ask > neutral.ask
    assert long_.skew_ticks == -2


def test_min_width_enforced_with_one_tick_spread():
    q = compute_quotes(top("0.7500", "0.7501"), Decimal("0.75005"), Decimal(0), Decimal(0), P)
    assert q.ask - q.bid >= 2 * TICK
    assert q.bid <= Decimal("0.7500") and q.ask >= Decimal("0.7501")


def test_disallowed_sides_and_empty_book():
    q = compute_quotes(top(), Decimal("0.7501"), Decimal(0), Decimal(0), P, allow_bid=False)
    assert q.bid is None and q.ask is not None
    e = compute_quotes(
        BookTop(None, None, Decimal(0), Decimal(0)), Decimal("0.75"), Decimal(0), Decimal(0), P
    )
    assert e.bid is None and e.ask is None and e.reason == "empty_book"


def test_size_for_notional():
    assert size_for_notional(Decimal(25), Decimal("0.75"), Decimal("0.01"), Decimal(1)) == Decimal("33.33")
    assert size_for_notional(Decimal("0.5"), Decimal("0.75"), Decimal("0.01"), Decimal(1)) == 0
