from __future__ import annotations

from decimal import Decimal

import pytest

from scalper.config import Settings
from scalper.execution.exchange import ProductMeta
from scalper.marketdata.orderbook import OrderBook

TICK = Decimal("0.0001")


def l2(product: str, updates: list[tuple[str, str, str]], typ: str = "update") -> dict:
    return {
        "channel": "l2_data",
        "events": [
            {
                "type": typ,
                "product_id": product,
                "updates": [{"side": s, "price_level": p, "new_quantity": q} for s, p, q in updates],
            }
        ],
    }


def trade(product: str, price: str, size: str, side: str = "SELL") -> dict:
    return {
        "channel": "market_trades",
        "events": [
            {
                "type": "update",
                "trades": [{"product_id": product, "price": price, "size": size, "side": side}],
            }
        ],
    }


def ticker(product: str, price: str) -> dict:
    return {"channel": "ticker", "events": [{"tickers": [{"product_id": product, "price": price}]}]}


@pytest.fixture
def meta() -> ProductMeta:
    return ProductMeta("USDT-GBP", TICK, Decimal("0.01"), Decimal(1))


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        mode="backtest",
        capital_gbp=Decimal(1000),
        data_dir=tmp_path / "data",
        kill_switch_path=tmp_path / "KILL",
        requote_min_interval_s=0.0,
        jev_enabled=False,
    )


@pytest.fixture
def book() -> OrderBook:
    b = OrderBook("USDT-GBP")
    b.apply_event(
        l2(
            "USDT-GBP",
            [
                ("bid", "0.7500", "5000"),
                ("bid", "0.7499", "8000"),
                ("offer", "0.7502", "4000"),
                ("offer", "0.7503", "9000"),
            ],
            "snapshot",
        )["events"][0],
        ts=1000.0,
    )
    return b
