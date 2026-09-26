from decimal import Decimal

from scalper.risk.fee_check import FeeRates, check_fees
from scalper.risk.guards import RiskGuards
from scalper.strategy.inventory import Fill, Inventory
from tests.conftest import TICK, l2


def make_inv():
    inv = Inventory(Decimal(1000), Decimal("0.5"), Decimal("0.5"))
    inv.seed(Decimal(500), Decimal("666.67"), Decimal("0.75"))
    return inv


def test_inventory_round_trip_pnl():
    inv = make_inv()
    assert inv.pnl(Decimal("0.75")).quantize(Decimal("0.0001")) == 0
    inv.apply_fill(Fill("1", "c1", "BUY", Decimal("0.7500"), Decimal(100), Decimal(0), 0))
    inv.apply_fill(Fill("2", "c2", "SELL", Decimal("0.7502"), Decimal(100), Decimal("0.001"), 1))
    assert inv.pnl(Decimal("0.75")) == Decimal("0.02") - Decimal("0.001")
    assert inv.fees_paid == Decimal("0.001")


def test_inventory_limits():
    inv = make_inv()
    assert inv.deviation_fraction(Decimal("0.75")).quantize(Decimal("0.01")) == 0
    assert inv.can_buy(Decimal("0.75"), Decimal(25))
    assert inv.can_sell(Decimal("0.75"), Decimal(33))
    inv.usdt = Decimal(1340)  # > GBP 1000 of USDT: at the cap
    assert inv.deviation_fraction(Decimal("0.75")) == 1
    assert not inv.can_buy(Decimal("0.75"), Decimal(25))
    inv.usdt = Decimal(0)
    assert inv.deviation_fraction(Decimal("0.75")) == -1
    assert not inv.can_sell(Decimal("0.75"), Decimal(1))


def guards(tmp_path):
    return RiskGuards(
        capital_gbp=Decimal(1000),
        daily_loss_cap_pct=Decimal("0.01"),
        stale_book_seconds=5,
        max_spread_ticks=20,
        max_open_orders=2,
        tick=TICK,
        kill_switch_path=tmp_path / "KILL",
    )


def test_market_guards(book, tmp_path):
    g = guards(tmp_path)
    assert g.check_market(book, now=1001.0).allowed
    assert g.check_market(book, now=1010.0).reason == "stale_book"
    (tmp_path / "KILL").write_text("")
    assert g.check_market(book, now=1001.0).reason == "kill_switch"
    (tmp_path / "KILL").unlink()
    book.apply_event(
        l2("USDT-GBP", [("offer", "0.7502", "0"), ("offer", "0.7503", "0"), ("offer", "0.7600", "1")])[
            "events"
        ][0],
        1001.0,
    )
    assert g.check_market(book, now=1001.0).reason == "spread_too_wide"
    g.halt("test")
    assert g.check_market(book, now=1001.0).reason == "halted:test"
    g.reset_halt()
    assert not g.halted


def test_daily_loss_and_order_guards(tmp_path):
    g = guards(tmp_path)
    inv = make_inv()
    assert g.check_daily_loss(inv, Decimal("0.75")).allowed
    inv.gbp -= Decimal(10)  # 1% loss
    v = g.check_daily_loss(inv, Decimal("0.75"))
    assert not v.allowed and g.halted and "daily_loss_cap" in g.halt_reason

    g2 = guards(tmp_path)
    inv = make_inv()
    assert g2.check_order("BUY", Decimal("0.75"), Decimal(33), inv, open_orders=0).allowed
    assert g2.check_order("BUY", Decimal("0.75"), Decimal(33), inv, open_orders=2).reason == "max_open_orders"
    assert g2.check_order("BUY", Decimal("0.75"), Decimal(0), inv, open_orders=0).reason == "size_zero"
    inv.usdt = Decimal(1340)
    assert g2.check_order("BUY", Decimal("0.75"), Decimal(33), inv, 0).reason == "inventory_limit_buy"
    inv.usdt = Decimal(0)
    assert g2.check_order("SELL", Decimal("0.75"), Decimal(33), inv, 0).reason == "inventory_limit_sell"


def test_fee_check():
    stable = check_fees(FeeRates(Decimal(0), Decimal("0.00001"), "stable"), 1, TICK, Decimal("0.75"))
    assert stable.ok
    standard = check_fees(FeeRates(Decimal("0.006"), Decimal("0.012"), "Intro 1"), 1, TICK, Decimal("0.75"))
    assert not standard.ok and "NOT VIABLE" in standard.message
    # two ticks of edge (~0.027%) vs 0.01% maker: viable
    assert check_fees(FeeRates(Decimal("0.0001"), Decimal("0.0002")), 1, TICK, Decimal("0.75")).ok
