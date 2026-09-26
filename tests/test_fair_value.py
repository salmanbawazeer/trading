from decimal import Decimal

from scalper.marketdata.fair_value import FairValueModel, RollingVol


def make_model(w="0.5"):
    return FairValueModel("USDT-USD", "BTC-GBP", "BTC-USD", Decimal(w), max_age_s=30)


def test_implied_cross(book):
    fm = make_model()
    assert fm.implied(now=0) is None
    fm.on_ticker("USDT-USD", Decimal("1.0002"), 0)
    fm.on_ticker("BTC-GBP", Decimal(75000), 0)
    fm.on_ticker("BTC-USD", Decimal(100000), 0)
    fm.on_ticker("ETH-USD", Decimal(1), 0)  # ignored
    imp = fm.implied(now=1)
    assert imp == Decimal("1.0002") * Decimal(75000) / Decimal(100000)
    # stale reference -> no implied
    assert fm.implied(now=100) is None


def test_fair_blends_or_falls_back(book):
    fm = make_model("0.5")
    top = book.top()
    assert fm.fair(top, now=0) == top.microprice  # no refs yet
    fm.on_ticker("USDT-USD", Decimal(1), 0)
    fm.on_ticker("BTC-GBP", Decimal(75000), 0)
    fm.on_ticker("BTC-USD", Decimal(100000), 0)
    fair = fm.fair(top, now=0)
    expected = (Decimal("0.75") + top.microprice) / 2
    assert abs(fair - expected) < Decimal("1e-12")


def test_rolling_vol_range():
    v = RollingVol(window_s=10, tick=Decimal("0.0001"))
    assert v.range_ticks() == 0
    v.update(0, Decimal("0.7500"))
    v.update(5, Decimal("0.7503"))
    assert v.range_ticks() == 3
    v.update(20, Decimal("0.7510"))  # old points expire
    assert v.range_ticks() == 0
