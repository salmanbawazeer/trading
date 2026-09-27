"""Profit lock: never sell a bought lot below cost + fees, never buy back a sold lot above price - fees."""

from decimal import Decimal as D

from scalper.journal.store import Journal
from scalper.strategy.lots import LotBook
from tests.conftest import TICK, l2, ticker, trade
from tests.test_engine_replay import REFS, build

MAKER = D("0.0006")  # the user's real VIP 1 maker rate
FEES = __import__("scalper.risk.fee_check", fromlist=["FeeRates"]).FeeRates(MAKER, D("0.0016"))


def test_lotbook_matches_and_floors():
    lb = LotBook()
    lb.apply("BUY", D("0.7500"), D(100), 1)
    lb.apply("BUY", D("0.7490"), D(100), 2)
    # floor protects the cheapest open buy: 0.7490 * 1.0006/0.9994 = 0.749899 -> 0.7499, +1 tick = 0.7500
    assert lb.ask_floor(MAKER, TICK, 1) == D("0.7500")
    assert lb.bid_ceiling(MAKER, TICK, 1) is None
    lb.apply("SELL", D("0.7505"), D(100), 3)  # closes the 0.7490 lot
    assert [lt.price for lt in lb.buys] == [D("0.7500")]
    assert lb.realized_gbp == D("0.0015") * 100 and lb.closed == 1
    # partial close keeps the remainder open
    lb.apply("SELL", D("0.7515"), D(40), 4)
    assert lb.buys[0].size == 60
    # selling more than is open flips into a short lot
    lb.apply("SELL", D("0.7520"), D(100), 5)
    assert lb.buys == [] and lb.sells[0].size == 40 and lb.sells[0].price == D("0.7520")
    # ceiling: 0.7520 * 0.9994/1.0006 = 0.75110 -> 0.7510 (-1 tick) = 0.7509
    assert lb.bid_ceiling(MAKER, TICK, 1) == D("0.7509")


def test_rounding_residue_does_not_leave_an_open_trade():
    lb = LotBook(dust=D(1))
    lb.apply("BUY", D("0.7500"), D("33.33"), 1)
    lb.apply("SELL", D("0.7502"), D("33.32"), 2)  # GBP-sized orders round to different USDT sizes
    assert lb.buys == [] and lb.sells == [] and lb.closed == 1
    lb.apply("SELL", D("0.7510"), D("0.5"), 3)  # a dust-sized fill never opens a lot
    assert lb.sells == []
    lb.apply("SELL", D("0.7510"), D("33.30"), 4)
    assert lb.sells[0].size == D("33.30")


def _seed(engine, clock, t, bid="0.7500", ask="0.7502"):
    clock.now = t
    return [
        l2("USDT-GBP", [("bid", bid, "5000"), ("offer", ask, "4000")], "snapshot"),
        *REFS,
        l2("USDT-GBP", [("bid", bid, "5001")]),
    ]


async def _run(engine, clock, msgs, t):
    for i, m in enumerate(msgs):
        clock.now = t + i
        await engine.on_message(m, t + i)


async def test_after_a_buy_the_sell_never_goes_below_cost_plus_fees(settings, meta):
    engine, clock, _ = build(settings, meta, fees=FEES)
    t = 1_700_000_000.0
    await _run(engine, clock, _seed(engine, clock, t), t)
    assert engine.working["BUY"].price == D("0.7500")
    # a seller hits our bid: we now hold a lot bought at 0.7500
    await engine.on_message(trade("USDT-GBP", "0.7499", "50"), t + 5)
    assert engine.lots.buys and engine.lots.buys[0].price == D("0.7500")
    # the market then falls 20 ticks; without the lock the ask would follow it down
    fall = [
        l2(
            "USDT-GBP",
            [
                ("bid", "0.7500", "0"),
                ("offer", "0.7502", "0"),
                ("bid", "0.7480", "5000"),
                ("offer", "0.7482", "4000"),
            ],
        ),
        ticker("BTC-GBP", "74810"),
    ]
    engine.last_requote = 0
    await _run(engine, clock, fall, t + 10)
    floor = D("0.7500") * (1 + MAKER) / (1 - MAKER)
    ask = engine.working["SELL"].price
    assert ask >= floor and ask == D("0.7511")  # 0.750901 -> 0.7510, +1 tick margin
    snap = engine.snapshot()["quotes"]
    assert snap["ask_floor"] == 0.7511 and snap["open_buys"] == 1 and snap["cheapest_open_buy"] == 0.75


async def test_lock_off_lets_the_ask_follow_the_market(settings, meta):
    engine, clock, _ = build(settings.model_copy(update={"profit_lock": False}), meta, fees=FEES)
    t = 1_700_000_000.0
    await _run(engine, clock, _seed(engine, clock, t), t)
    await engine.on_message(trade("USDT-GBP", "0.7499", "50"), t + 5)
    fall = [
        l2(
            "USDT-GBP",
            [
                ("bid", "0.7500", "0"),
                ("offer", "0.7502", "0"),
                ("bid", "0.7480", "5000"),
                ("offer", "0.7482", "4000"),
            ],
        ),
        ticker("BTC-GBP", "74810"),
    ]
    engine.last_requote = 0
    await _run(engine, clock, fall, t + 10)
    assert engine.working["SELL"].price < D("0.7500")
    assert engine.snapshot()["quotes"]["ask_floor"] is None


async def test_live_restores_open_trades_from_journal(settings, meta, tmp_path):
    from scalper.engine import Engine

    path = tmp_path / "j.sqlite"
    j = Journal(path, "live")
    j.fill(1.0, "o1", "c1", "BUY", D("0.7500"), D("33.33"), D("0.015"), D(0), D(0))
    j.fill(2.0, "o2", "c2", "BUY", D("0.7490"), D("33.37"), D("0.015"), D(0), D(0))
    j.fill(3.0, "o3", "c3", "SELL", D("0.7505"), D("33.37"), D("0.015"), D(0), D(0))
    j.fill(4.0, "p1", "c4", "BUY", D("0.7000"), D(10), D(0), D(0), D(0))
    j.conn.execute("UPDATE fills SET mode = 'paper' WHERE order_id = 'p1'")  # other modes are ignored
    engine, _, _ = build(settings, meta, fees=FEES)
    restored = Engine(
        settings,
        engine.exchange,
        meta,
        engine.book,
        engine.fair_model,
        engine.inv,
        engine.guards,
        j,
        engine.gate,
        engine.clock,
        seed_from_exchange=True,
        maker_fee_rate=MAKER,
    )
    assert [(lt.price, lt.size) for lt in restored.lots.buys] == [(D("0.7500"), D("33.33"))]
    # 0.7500 * 1.0006/0.9994 = 0.750901 -> 0.7510, +1 tick = 0.7511
    assert restored.profit_limits()[0] == D("0.7511")
