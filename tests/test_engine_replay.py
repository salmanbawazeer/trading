"""End-to-end: synthetic recording -> replay -> engine -> paper fills -> deterministic PnL."""

import gzip
import json
from decimal import Decimal

from scalper.backtest.replay import SimClock, iter_recording, replay
from scalper.engine import Engine
from scalper.execution.paper import PaperExchange
from scalper.gate.jev_gate import JevGate
from scalper.journal.store import Journal
from scalper.marketdata.fair_value import FairValueModel
from scalper.marketdata.orderbook import OrderBook
from scalper.risk.fee_check import FeeRates
from scalper.risk.guards import RiskGuards
from scalper.strategy.inventory import Inventory
from tests.conftest import l2, ticker, trade

REFS = [ticker("USDT-USD", "1.0000"), ticker("BTC-GBP", "75010"), ticker("BTC-USD", "100000")]


def synthetic_recording(path):
    """Book 0.7500/0.7502; a seller prints through our bid, then a buyer prints through our ask."""
    t = 1_700_000_000.0
    msgs = [
        (
            l2(
                "USDT-GBP",
                [
                    ("bid", "0.7500", "5000"),
                    ("bid", "0.7499", "8000"),
                    ("offer", "0.7502", "4000"),
                    ("offer", "0.7503", "9000"),
                ],
                "snapshot",
            ),
            t,
        )
    ]
    msgs += [(r, t) for r in REFS]
    msgs.append(
        (l2("USDT-GBP", [("bid", "0.7500", "5001")]), t + 1)
    )  # triggers first requote (bid 0.7500 / ask 0.7502)
    msgs.append((trade("USDT-GBP", "0.7499", "50", "SELL"), t + 2))  # through our bid -> BUY fill
    msgs.append((l2("USDT-GBP", [("bid", "0.7500", "5002")]), t + 3))
    msgs.append((trade("USDT-GBP", "0.7503", "50", "BUY"), t + 4))  # through our ask -> SELL fill
    msgs.append((l2("USDT-GBP", [("bid", "0.7500", "5003")]), t + 15))
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for m, ts in msgs:
            fh.write(json.dumps({"ts": ts, "msg": m}) + "\n")
    return len(msgs)


ZERO_MAKER = FeeRates(Decimal(0), Decimal("0.00001"))


def build(settings, meta, fees=ZERO_MAKER):
    book = OrderBook(settings.product_id)
    fair = FairValueModel(
        "USDT-USD", "BTC-GBP", "BTC-USD", settings.fair_blend_weight, settings.ref_price_max_age_s
    )
    inv = Inventory(settings.capital_gbp, settings.target_usdt_fraction, settings.max_inventory_pct)
    guards = RiskGuards(
        capital_gbp=settings.capital_gbp,
        daily_loss_cap_pct=settings.daily_loss_cap_pct,
        stale_book_seconds=settings.stale_book_seconds,
        max_spread_ticks=settings.max_spread_ticks,
        max_open_orders=settings.max_open_orders,
        tick=meta.quote_increment,
        kill_switch_path=settings.kill_switch_path,
    )
    clock = SimClock()
    ex = PaperExchange(meta, fees, book, {"GBP": Decimal(1000), "USDT": Decimal(0)}, clock)
    journal = Journal(":memory:", "backtest")
    engine = Engine(
        settings,
        ex,
        meta,
        book,
        fair,
        inv,
        guards,
        journal,
        JevGate(False),
        clock,
        trade_sink=ex.on_market_trade,
        maker_fee_rate=fees.maker,
    )
    return engine, clock, journal


async def test_replay_round_trip_captures_spread(settings, meta, tmp_path):
    rec = tmp_path / "ws-1.jsonl.gz"
    n = synthetic_recording(rec)
    assert len(list(iter_recording([rec]))) == n
    engine, clock, journal = build(settings, meta)
    await replay(engine, clock, [rec])
    inv = engine.inv
    assert engine.seeded
    assert [f.side for f in inv.fills] == ["BUY", "SELL"]
    buy, sell = inv.fills
    assert buy.size == Decimal("33.33") and buy.price == Decimal("0.7500")  # GBP 25 at 0.7500
    assert sell.size == Decimal("33.32") and sell.price == Decimal("0.7502")  # GBP 25 at 0.7502
    # PnL = cash change + residual inventory marked at 0.7501, zero maker fee
    expected = -buy.price * buy.size + sell.price * sell.size + (buy.size - sell.size) * Decimal("0.7501")
    assert inv.pnl(Decimal("0.7501")) == expected
    assert expected > 0
    assert inv.fees_paid == 0
    # after fills, fresh quotes were re-posted on both sides
    assert set(engine.working) == {"BUY", "SELL"}
    rows = journal.conn.execute("SELECT action, COUNT(*) FROM orders GROUP BY action").fetchall()
    actions = dict(rows)
    assert actions["place"] >= 4
    assert journal.summary()["fills"] == 2


async def test_stale_book_cancels_quotes(settings, meta, tmp_path):
    engine, clock, _ = build(settings, meta)
    t = 1_700_000_000.0
    clock.now = t
    await engine.on_message(
        l2("USDT-GBP", [("bid", "0.7500", "5000"), ("offer", "0.7502", "4000")], "snapshot"), t
    )
    for r in REFS:
        await engine.on_message(r, t)
    await engine.on_message(l2("USDT-GBP", [("bid", "0.7500", "5001")]), t + 1)
    assert len(engine.working) == 2
    # no book updates for a long time; a ticker arrives and we evaluate guards at t+30
    clock.now = t + 30
    engine.last_requote = 0
    await engine.maybe_requote(t + 30)
    assert engine.working == {}
    assert await engine.exchange.open_orders() == []


async def test_daily_loss_cap_halts_and_cancels(settings, meta, tmp_path):
    settings = settings.model_copy(update={"daily_loss_cap_pct": Decimal("0.00001")})
    engine, clock, _ = build(settings, meta, fees=FeeRates(Decimal("0.001"), Decimal("0.002")))
    t = 1_700_000_000.0
    clock.now = t
    await engine.on_message(
        l2("USDT-GBP", [("bid", "0.7500", "5000"), ("offer", "0.7502", "4000")], "snapshot"), t
    )
    for r in REFS:
        await engine.on_message(r, t)
    await engine.on_message(l2("USDT-GBP", [("bid", "0.7500", "5001")]), t + 1)
    assert len(engine.working) == 2
    await engine.on_message(trade("USDT-GBP", "0.7499", "50"), t + 2)  # buy fill pays 0.1% fee -> loss > cap
    assert engine.guards.halted and engine.guards.halt_reason.startswith("daily_loss_cap")
    assert engine.working == {}
    _ok, detail = engine.health()
    assert detail["halted"] is True


async def test_kill_switch_blocks_quoting(settings, meta):
    engine, clock, _ = build(settings, meta)
    t = 1_700_000_000.0
    clock.now = t
    settings.kill_switch_path.parent.mkdir(parents=True, exist_ok=True)
    settings.kill_switch_path.write_text("")
    await engine.on_message(
        l2("USDT-GBP", [("bid", "0.7500", "5000"), ("offer", "0.7502", "4000")], "snapshot"), t
    )
    await engine.on_message(l2("USDT-GBP", [("bid", "0.7500", "5001")]), t + 1)
    assert engine.working == {}
