"""Live adapter tests with a fake REST client; no network."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from scalper.execution import coinbase_live as cl


class FakeRest:
    def __init__(self):
        self.orders = []
        self.cancelled = []
        self.preview_errs: list = []

    preview_commission = "0.06"  # GBP on a ~GBP 100 preview -> 0.06 % maker
    preview_exc: Exception | None = None

    def get_product(self, product_id):
        return SimpleNamespace(
            quote_increment="0.0001", base_increment="0.01", base_min_size="1", price="0.7550"
        )

    def preview_limit_order_gtc(self, **kw):
        self.preview_kw = kw
        if self.preview_exc:
            raise self.preview_exc
        # nested fields come back as raw values, like the real SDK
        return SimpleNamespace(
            commission_total=self.preview_commission, errs=self.preview_errs, order_total="100"
        )

    def get_transaction_summary(self, product_type=None):
        # The real SDK stores nested fields as raw dicts despite its type annotations.
        return SimpleNamespace(
            fee_tier={"maker_fee_rate": "0", "taker_fee_rate": "0.00001", "pricing_tier": "Stable Pairs"}
        )

    def get_accounts(self, limit=None, cursor=None):
        return SimpleNamespace(
            accounts=[
                SimpleNamespace(currency="GBP", available_balance={"value": "500.5"}),
                SimpleNamespace(
                    currency="USDT", available_balance=SimpleNamespace(value="600")
                ),  # object form too
                SimpleNamespace(currency="EUR", available_balance=None),
            ],
            has_next=False,
            cursor=None,
        )

    def limit_order_gtc(self, **kw):
        self.orders.append(kw)
        if kw["limit_price"] == "9":
            return SimpleNamespace(
                success=False,
                error_response={"error": "INVALID_PRICE", "message": "Price out of range"},
                failure_reason={"error": "UNKNOWN_FAILURE_REASON"},
                order_id=None,
                success_response=None,
            )
        return SimpleNamespace(
            success=True, order_id="o1", success_response={"order_id": "o1"}, error_response=None
        )

    def cancel_orders(self, order_ids):
        self.cancelled.extend(order_ids)

    def list_orders(self, product_ids=None, order_status=None):
        return SimpleNamespace(
            orders=[SimpleNamespace(order_id="stale1"), SimpleNamespace(order_id="stale2")]
        )


@pytest.fixture
def live(monkeypatch):
    ex = cl.CoinbaseLiveExchange.__new__(cl.CoinbaseLiveExchange)
    ex.product_id = "USDT-GBP"
    ex.rest = FakeRest()
    ex.bucket = cl.TokenBucket(100)
    ex._fill_cb = None
    ex._orders = {}
    ex._filled_so_far = {}
    return ex


async def test_meta_fees_balances(live):
    meta = await live.product_meta()
    assert meta.quote_increment == Decimal("0.0001")
    fees = await live.fee_rates()
    assert fees.maker == 0 and fees.tier == "Stable Pairs"
    bal = await live.balances()
    assert bal == {"GBP": Decimal("500.5"), "USDT": Decimal(600)}


async def test_place_cancel_and_cancel_all(live):
    ack = await live.place_post_only("BUY", Decimal("0.7500"), Decimal("33.33"), "c1")
    assert ack.accepted and ack.order_id == "o1"
    assert live.rest.orders[0]["post_only"] is True and live.rest.orders[0]["base_size"] == "33.33"
    bad = await live.place_post_only("BUY", Decimal(9), Decimal(1), "c2")
    assert not bad.accepted and bad.reason == "INVALID_PRICE"
    await live.cancel(["o1"])
    assert live.rest.cancelled == ["o1"] and await live.open_orders() == []
    assert await live.cancel_all() == 2
    assert set(live.rest.cancelled) >= {"stale1", "stale2"}


async def test_user_channel_delta_fills(live):
    fills = []

    async def cb(f):
        fills.append(f)

    live.set_fill_callback(cb)
    base = {
        "product_id": "USDT-GBP",
        "order_id": "o1",
        "client_order_id": "c1",
        "order_side": "BUY",
        "avg_price": "0.7500",
        "limit_price": "0.7500",
    }
    await live.on_user_order({**base, "status": "OPEN", "cumulative_quantity": "0", "total_fees": "0"}, 1.0)
    assert fills == []
    await live.on_user_order(
        {**base, "status": "OPEN", "cumulative_quantity": "10", "total_fees": "0.001"}, 2.0
    )
    await live.on_user_order(
        {**base, "status": "FILLED", "cumulative_quantity": "30", "total_fees": "0.003"}, 3.0
    )
    assert [f.size for f in fills] == [Decimal(10), Decimal(20)]
    assert sum(f.fee for f in fills) == Decimal("0.003")
    assert fills[1].side == "BUY" and fills[1].price == Decimal("0.7500")
    # duplicate delivery of the final state produces no extra fill
    await live.on_user_order(
        {**base, "status": "FILLED", "cumulative_quantity": "30", "total_fees": "0.003"}, 4.0
    )
    assert len(fills) == 2
    await live.on_user_order({"product_id": "BTC-GBP", "order_id": "x", "cumulative_quantity": "5"}, 5.0)
    assert len(fills) == 2


async def test_token_bucket_limits_rate():
    import time

    b = cl.TokenBucket(50, burst=1)
    t0 = time.monotonic()
    for _ in range(4):
        await b.acquire()
    assert time.monotonic() - t0 >= 0.05


async def test_pair_maker_rate_measures_from_preview(live):
    meta = await live.product_meta()
    assert live.last_price == Decimal("0.7550")
    rate, note = await live.pair_maker_rate(meta)
    kw = live.rest.preview_kw
    assert kw["post_only"] is True and kw["side"] == "BUY"
    assert Decimal(kw["limit_price"]) == Decimal("0.7474")  # 1 % below, rounded down to the tick
    notional = Decimal(kw["limit_price"]) * Decimal(kw["base_size"])
    assert Decimal(99) < notional <= Decimal(100)
    assert rate == Decimal("0.06") / notional and "preview of" in note
    # a genuinely free pair measures as exactly zero
    live.rest.preview_commission = "0"
    assert (await live.pair_maker_rate(meta))[0] == 0
    # zero commission alongside errors is not trusted
    live.rest.preview_errs = [{"error": "INSUFFICIENT_FUND"}]
    rate, note = await live.pair_maker_rate(meta)
    assert rate is None and "errs" in note
    live.rest.preview_errs = []
    live.rest.preview_exc = RuntimeError("boom")
    rate, note = await live.pair_maker_rate(meta)
    assert rate is None and "preview failed" in note
    live.rest.preview_exc = None
    live.last_price = None
    assert (await live.pair_maker_rate(meta))[0] is None
