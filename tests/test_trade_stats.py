import pytest

from scalper.strategy.stats import aggregate, summarize


def test_empty():
    s = summarize(aggregate([]))
    assert s["buys"] == s["sells"] == s["pairs"] == 0
    assert s["avg_buy"] is None and s["captured_gbp"] == 0 and s["net_gbp"] == 0


def test_counts_volumes_and_captured_spread():
    fills = [
        ("BUY", 0.7500, 100.0, 0.045),
        ("BUY", 0.7502, 100.0, 0.045),
        ("SELL", 0.7510, 150.0, 0.0676),
    ]
    s = summarize(aggregate(fills))
    assert (s["buys"], s["sells"], s["pairs"]) == (2, 1, 1)
    assert s["buy_usdt"] == 200 and s["sell_usdt"] == 150 and s["matched_usdt"] == 150
    assert s["avg_buy"] == pytest.approx(0.7501)
    assert s["captured_gbp"] == pytest.approx(150 * (0.7510 - 0.7501))
    assert s["net_gbp"] == pytest.approx(s["captured_gbp"] - (0.045 + 0.045 + 0.0676))
