from decimal import Decimal

from scalper.marketdata.orderbook import OrderBook
from tests.conftest import l2


def test_snapshot_and_updates(book):
    assert book.ready
    assert book.best_bid == Decimal("0.7500")
    assert book.best_ask == Decimal("0.7502")
    top = book.top()
    assert top.spread == Decimal("0.0002")
    assert top.mid == Decimal("0.7501")
    # more size on the bid -> microprice leans toward the ask
    assert top.microprice > top.mid

    book.apply_event(
        l2("USDT-GBP", [("bid", "0.7500", "0"), ("offer", "0.7501", "100")])["events"][0], ts=1001.0
    )
    assert book.best_bid == Decimal("0.7499")
    assert book.best_ask == Decimal("0.7501")
    assert book.last_update_ts == 1001.0


def test_new_snapshot_replaces_book(book):
    book.apply_event(
        l2("USDT-GBP", [("bid", "0.7000", "1"), ("offer", "0.7010", "1")], "snapshot")["events"][0], ts=1002.0
    )
    assert book.bids == {Decimal("0.7000"): Decimal(1)}
    assert book.asks == {Decimal("0.7010"): Decimal(1)}


def test_ignores_other_products(book):
    book.apply_event(l2("BTC-GBP", [("bid", "1", "1")])["events"][0], ts=1003.0)
    assert Decimal(1) not in book.bids


def test_staleness_and_imbalance(book):
    assert not book.is_stale(now=1004.0, max_age_s=5)
    assert book.is_stale(now=1010.0, max_age_s=5)
    imb = book.imbalance(depth=2)
    # bids 13000 vs asks 13000 -> balanced
    assert imb == Decimal(0)
    empty = OrderBook("USDT-GBP")
    assert empty.imbalance() is None
    assert not empty.ready
