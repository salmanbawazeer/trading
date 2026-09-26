from decimal import Decimal

import pytest

from scalper.execution.paper import PaperExchange
from scalper.risk.fee_check import FeeRates


@pytest.fixture
def paper(meta, book):
    fees = FeeRates(Decimal("0.0001"), Decimal("0.0002"))
    ex = PaperExchange(meta, fees, book, {"GBP": Decimal(500), "USDT": Decimal(600)}, clock=lambda: 1000.0)
    fills = []

    async def cb(f):
        fills.append(f)

    ex.set_fill_callback(cb)
    ex.fills = fills
    return ex


async def test_post_only_reject_when_crossing(paper):
    ack = await paper.place_post_only("BUY", Decimal("0.7502"), Decimal(10), "c1")
    assert not ack.accepted and ack.reason == "post_only_would_cross"
    ack = await paper.place_post_only("SELL", Decimal("0.7500"), Decimal(10), "c2")
    assert not ack.accepted
    assert paper.rejects == 2


async def test_fill_when_trade_prints_through(paper):
    ack = await paper.place_post_only("BUY", Decimal("0.7500"), Decimal(10), "c1")
    assert ack.accepted
    await paper.on_market_trade(Decimal("0.7500"), Decimal(100), 1001.0)  # at price: behind 5000 queue
    assert paper.fills == []
    await paper.on_market_trade(Decimal("0.7499"), Decimal(1), 1002.0)  # through price: fill
    assert len(paper.fills) == 1
    f = paper.fills[0]
    assert f.side == "BUY" and f.price == Decimal("0.7500") and f.size == 10
    assert f.fee == Decimal("0.7500") * 10 * Decimal("0.0001")
    bal = await paper.balances()
    assert bal["USDT"] == 610
    assert bal["GBP"] == Decimal(500) - Decimal("7.5") - f.fee
    assert await paper.open_orders() == []


async def test_queue_position_model(paper):
    # ask level 0.7502 has 4000 resting; we join behind it with 10
    await paper.place_post_only("SELL", Decimal("0.7502"), Decimal(10), "c1")
    await paper.on_market_trade(Decimal("0.7502"), Decimal(4000), 1001.0)
    assert paper.fills == []
    await paper.on_market_trade(Decimal("0.7502"), Decimal(10), 1002.0)
    assert len(paper.fills) == 1 and paper.fills[0].side == "SELL"


async def test_cancel(paper):
    ack = await paper.place_post_only("BUY", Decimal("0.7500"), Decimal(10), "c1")
    await paper.cancel([ack.order_id])
    assert await paper.open_orders() == []
    await paper.place_post_only("BUY", Decimal("0.7500"), Decimal(10), "c2")
    assert await paper.cancel_all() == 1
