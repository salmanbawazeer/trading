"""Paper and live keep their account, open trades and history across restarts."""

import asyncio
from decimal import Decimal as D

from scalper.journal.store import Journal
from scalper.risk.fee_check import FeeRates
from tests.conftest import l2, trade
from tests.test_engine_replay import REFS, build

FEES = FeeRates(D("0.0006"), D("0.0016"))
T = 1_700_000_000.0


def opening():
    return [
        l2("USDT-GBP", [("bid", "0.7500", "5000"), ("offer", "0.7502", "4000")], "snapshot"),
        *REFS,
        l2("USDT-GBP", [("bid", "0.7500", "5001")]),
    ]


async def feed(engine, clock, msgs, t):
    for i, m in enumerate(msgs):
        clock.now = t + i
        engine.last_requote = 0
        await engine.on_message(m, t + i)


async def paper_engine(settings, meta, path):
    s = settings.model_copy(update={"mode": "paper"})
    return build(s, meta, fees=FEES, journal=Journal(path, "paper"))


async def test_paper_account_survives_restart(settings, meta, tmp_path):
    path = tmp_path / "journal.sqlite"
    e1, c1, j1 = await paper_engine(settings, meta, path)
    await feed(e1, c1, opening(), T)
    await e1.on_message(trade("USDT-GBP", "0.7499", "50"), T + 5)  # our bid is hit: we bought
    assert e1.run_counts["BUY"] == 1 and len(e1.lots.buys) == 1
    before = (e1.inv.gbp, e1.inv.usdt, e1.inv.start_equity, e1.inv.fees_paid, e1.since_ts)
    await e1.shutdown()
    j1.close()

    e2, c2, _ = await paper_engine(settings, meta, path)
    assert [lt.price for lt in e2.lots.buys] == [D("0.7500")]  # open trade remembered
    await feed(e2, c2, opening(), T + 100)
    assert (e2.inv.gbp, e2.inv.usdt, e2.inv.start_equity, e2.inv.fees_paid, e2.since_ts) == before
    snap = e2.snapshot()
    assert snap["trades"]["account"]["buys"] == 1 and snap["trades"]["run"] == {"buys": 0, "sells": 0}
    assert [f["side"] for f in snap["recent_fills"]] == ["BUY"]
    assert snap["quotes"]["open_buys"] == 1 and snap["quotes"]["ask_floor"] == 0.7511
    assert e2.working["SELL"].price >= D("0.7511")  # still only sells at a profit


async def test_existing_fills_are_adopted_when_no_account_was_saved(settings, meta, tmp_path):
    path = tmp_path / "journal.sqlite"
    j = Journal(path, "paper")
    j.fill(T - 3600, "o1", "c1", "BUY", D("0.7500"), D("33.33"), D("0.015"), D(0), D(0))
    j.close()
    e, c, j = await paper_engine(settings, meta, path)
    await feed(e, c, opening(), T)
    fresh_usdt = (D(500) / D("0.7501")).quantize(D("0.01"))
    assert e.inv.usdt == fresh_usdt + D("33.33")
    assert e.inv.fees_paid == D("0.015") and e.since_ts == T - 3600
    assert e.lots.buys and j.load_account()["gbp"] is not None
    assert e.snapshot()["trades"]["account"]["buys"] == 1


async def test_reset_starts_a_clean_paper_account(settings, meta, tmp_path):
    path = tmp_path / "journal.sqlite"
    e1, c1, j1 = await paper_engine(settings, meta, path)
    await feed(e1, c1, opening(), T)
    await e1.on_message(trade("USDT-GBP", "0.7499", "50"), T + 5)
    c1.now = T + 10
    await e1.reset_paper()
    snap = e1.snapshot()
    assert e1.lots.buys == [] and e1.since_ts == T + 10
    assert snap["trades"]["account"]["buys"] == 0 and snap["recent_fills"] == []
    assert e1.inv.gbp == D(500) and e1.inv.pnl(D("0.7501")).quantize(D("0.01")) == 0
    j1.close()
    e2, c2, _ = await paper_engine(settings, meta, path)
    assert e2.lots.buys == [] and e2.since_ts == T + 10
    await feed(e2, c2, opening(), T + 20)
    assert e2.snapshot()["trades"]["account"]["buys"] == 0


async def test_reset_request_is_paper_only_and_thread_safe(settings, meta, tmp_path):
    e, _c, _ = build(settings, meta)  # backtest mode
    assert e.request_paper_reset()["ok"] is False
    p, pc, _ = await paper_engine(settings, meta, tmp_path / "j.sqlite")
    assert "not running" in p.request_paper_reset()["error"]
    await feed(p, pc, opening(), T)
    result = await asyncio.get_running_loop().run_in_executor(None, p.request_paper_reset)
    assert result["ok"] and p.since_ts == result["since_ts"]


async def test_backtests_do_not_save_an_account(settings, meta, tmp_path):
    j = Journal(tmp_path / "bt.sqlite", "backtest")
    e, c, _ = build(settings, meta, journal=j)
    await feed(e, c, opening(), T)
    assert j.load_account() is None and e.persist is False


async def test_requote_tolerance_keeps_orders_in_place(settings, meta, tmp_path):
    s = settings.model_copy(update={"requote_tolerance_ticks": 2, "max_rest_seconds": 3600})
    e, c, _ = build(s, meta)
    await feed(e, c, opening(), T)
    bid0 = e.working["BUY"]
    # fair moves by one tick: within tolerance, the order is left alone (queue position kept)
    await feed(
        e,
        c,
        [
            l2(
                "USDT-GBP",
                [
                    ("bid", "0.7500", "0"),
                    ("bid", "0.7501", "5000"),
                    ("offer", "0.7502", "0"),
                    ("offer", "0.7503", "4000"),
                ],
            )
        ],
        T + 10,
    )
    assert e.working["BUY"] is bid0
    # three ticks: beyond tolerance, it is replaced
    await feed(
        e,
        c,
        [
            l2(
                "USDT-GBP",
                [
                    ("bid", "0.7501", "0"),
                    ("bid", "0.7503", "5000"),
                    ("offer", "0.7503", "0"),
                    ("offer", "0.7505", "4000"),
                ],
            )
        ],
        T + 20,
    )
    assert e.working["BUY"] is not bid0 and e.working["BUY"].price == D("0.7503")
