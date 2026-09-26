"""CLI entry point: python -m scalper [record|backtest|paper|live] [--recording GLOB]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from decimal import Decimal
from pathlib import Path

import structlog

from scalper import __version__
from scalper.backtest.recorder import Recorder
from scalper.backtest.replay import SimClock, replay
from scalper.config import Settings, load_settings
from scalper.engine import Engine
from scalper.execution.exchange import ProductMeta
from scalper.execution.paper import PaperExchange
from scalper.gate.jev_gate import JevGate
from scalper.journal.store import Journal
from scalper.marketdata.fair_value import FairValueModel
from scalper.marketdata.orderbook import OrderBook
from scalper.marketdata.ws_client import USER_URL, CoinbaseFeed, make_jwt_factory, public_subscriptions
from scalper.observability.metrics import start_http_server
from scalper.risk.fee_check import FeeRates, check_fees
from scalper.risk.guards import RiskGuards
from scalper.strategy.inventory import Inventory

log = structlog.get_logger("scalper")


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), stream=sys.stdout, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper(), logging.INFO)),
    )


def build_common(s: Settings, meta: ProductMeta):
    book = OrderBook(s.product_id)
    fair = FairValueModel(
        s.ref_usdt_usd, s.ref_btc_gbp, s.ref_btc_usd, s.fair_blend_weight, s.ref_price_max_age_s
    )
    inv = Inventory(s.capital_gbp, s.target_usdt_fraction, s.max_inventory_pct)
    guards = RiskGuards(
        capital_gbp=s.capital_gbp,
        daily_loss_cap_pct=s.daily_loss_cap_pct,
        stale_book_seconds=s.stale_book_seconds,
        max_spread_ticks=s.max_spread_ticks,
        max_open_orders=s.max_open_orders,
        tick=meta.quote_increment,
        kill_switch_path=s.kill_switch_path,
    )
    gate = JevGate(s.jev_enabled, s.jev_threshold, s.jev_timeout_s, s.jev_model)
    return book, fair, inv, guards, gate


def meta_from_settings(s: Settings) -> ProductMeta:
    return ProductMeta(s.product_id, s.quote_increment, s.base_increment, s.base_min_size)


def paper_balances(s: Settings) -> dict[str, Decimal]:
    # Placeholder until the first book snapshot; Engine seeds real numbers from the mid.
    base, quote = s.product_id.split("-")
    return {quote: s.capital_gbp, base: Decimal(0)}


async def run_record(s: Settings) -> None:
    rec = Recorder(s.recordings_dir)
    feed = CoinbaseFeed(
        rec.write, public_subscriptions(s.product_id, [s.ref_usdt_usd, s.ref_btc_gbp, s.ref_btc_usd])
    )
    start_http_server(
        s.metrics_port,
        lambda: (feed.connected, {"mode": "record", "messages": rec.count, "connected": feed.connected}),
    )
    log.info("recording", dir=str(s.recordings_dir))
    await _run_until_signal([feed.run()], on_stop=[feed.stop])
    rec.close()
    log.info("recording_stopped", messages=rec.count)


async def run_backtest(s: Settings, recording_glob: str) -> None:
    paths = (
        sorted(Path().glob(recording_glob))
        if any(c in recording_glob for c in "*?[")
        else [Path(recording_glob)]
    )
    if not paths:
        raise SystemExit(f"no recording files match {recording_glob}")
    meta = meta_from_settings(s)
    book, fair, inv, guards, gate = build_common(s, meta)
    clock = SimClock()
    fees = FeeRates(s.paper_maker_fee_rate, s.paper_taker_fee_rate, "configured")
    exchange = PaperExchange(meta, fees, book, paper_balances(s), clock)
    journal = Journal(s.data_dir / "backtest.sqlite", "backtest")
    engine = Engine(
        s, exchange, meta, book, fair, inv, guards, journal, gate, clock, trade_sink=exchange.on_market_trade
    )
    n = await replay(engine, clock, paths)
    mid = book.top().mid
    print_summary(engine, journal, n, mid)
    journal.close()


def print_summary(engine: Engine, journal: Journal, messages: int, mid) -> None:
    inv = engine.inv
    summary = {
        "messages": messages,
        "fills": len(inv.fills),
        "buys": sum(1 for f in inv.fills if f.side == "BUY"),
        "sells": sum(1 for f in inv.fills if f.side == "SELL"),
        "fees_gbp": str(inv.fees_paid),
        "final_gbp": str(inv.gbp),
        "final_usdt": str(inv.usdt),
        "pnl_gbp": str(inv.pnl(mid)) if (mid is not None and inv.start_equity is not None) else "n/a",
        "post_only_rejects": getattr(engine.exchange, "rejects", None),
        "halted": engine.guards.halted,
        "halt_reason": engine.guards.halt_reason,
    }
    log.info("backtest_summary", **summary)
    print("\n=== backtest summary ===")
    for k, v in summary.items():
        print(f"{k:>20}: {v}")


async def _resolve_meta_and_fees(s: Settings):
    """Read product metadata and the account's fee tier from Coinbase when a key is present."""
    if not s.has_api_key:
        log.warning(
            "no_api_key",
            detail="using configured product metadata and PAPER_* fee rates; fee check is not authoritative",
        )
        return (
            meta_from_settings(s),
            FeeRates(s.paper_maker_fee_rate, s.paper_taker_fee_rate, "configured"),
            None,
        )
    from scalper.execution.coinbase_live import CoinbaseLiveExchange

    live = CoinbaseLiveExchange(
        s.product_id, s.coinbase_api_key, s.coinbase_api_secret, s.rest_requests_per_second
    )
    meta = await live.product_meta()
    fees = await live.fee_rates()
    return meta, fees, live


def enforce_fee_check(s: Settings, fees: FeeRates, meta: ProductMeta, ref_price: Decimal) -> None:
    res = check_fees(fees, s.min_edge_ticks, meta.quote_increment, ref_price)
    (log.info if res.ok else log.error)("fee_check", message=res.message)
    print(res.message)
    if not res.ok:
        if s.mode == "paper" and s.allow_unprofitable_fees:
            log.warning(
                "fee_check_overridden",
                detail="ALLOW_UNPROFITABLE_FEES=true; paper trading for data collection only",
            )
            return
        raise SystemExit(2)


async def run_paper(s: Settings) -> None:
    meta, fees, _ = await _resolve_meta_and_fees(s)
    enforce_fee_check(s, fees, meta, ref_price=Decimal("0.75"))
    book, fair, inv, guards, gate = build_common(s, meta)
    exchange = PaperExchange(meta, fees, book, paper_balances(s), time.time)
    journal = Journal(s.journal_path, "paper")
    engine = Engine(
        s,
        exchange,
        meta,
        book,
        fair,
        inv,
        guards,
        journal,
        gate,
        time.time,
        trade_sink=exchange.on_market_trade,
    )
    feed = CoinbaseFeed(
        engine.on_message, public_subscriptions(s.product_id, [s.ref_usdt_usd, s.ref_btc_gbp, s.ref_btc_usd])
    )
    start_http_server(s.metrics_port, engine.health)
    log.info("paper_trading", product=s.product_id, fees=str(fees), meta=str(meta))
    await _run_until_signal([feed.run()], on_stop=[feed.stop])
    await engine.shutdown()
    mid = book.top().mid
    print_summary(engine, journal, 0, mid)
    journal.close()


async def run_live(s: Settings) -> None:
    if not s.live_armed:
        raise SystemExit("MODE=live requires LIVE_TRADING=I_UNDERSTAND")
    if not s.has_api_key:
        raise SystemExit("MODE=live requires COINBASE_API_KEY and COINBASE_API_SECRET")
    meta, fees, live = await _resolve_meta_and_fees(s)
    assert live is not None
    enforce_fee_check(s, fees, meta, ref_price=Decimal("0.75"))
    cancelled = await live.cancel_all()
    log.info("startup_cancel_all", cancelled=cancelled)
    book, fair, inv, guards, gate = build_common(s, meta)
    journal = Journal(s.journal_path, "live")
    engine = Engine(
        s,
        live,
        meta,
        book,
        fair,
        inv,
        guards,
        journal,
        gate,
        time.time,
        user_order_sink=live.on_user_order,
        seed_from_exchange=True,
    )
    public = CoinbaseFeed(
        engine.on_message, public_subscriptions(s.product_id, [s.ref_usdt_usd, s.ref_btc_gbp, s.ref_btc_usd])
    )
    user = CoinbaseFeed(
        engine.on_message,
        [("user", [s.product_id])],
        url=USER_URL,
        jwt_factory=make_jwt_factory(s.coinbase_api_key, s.coinbase_api_secret),
        name="user",
    )
    start_http_server(s.metrics_port, engine.health)
    log.warning(
        "LIVE_TRADING_STARTED", product=s.product_id, fees=str(fees), order_notional=str(s.order_notional_gbp)
    )
    await _run_until_signal([public.run(), user.run()], on_stop=[public.stop, user.stop])
    await engine.shutdown()
    journal.close()


async def _run_until_signal(coros, on_stop) -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _stop(*_):
        log.info("shutdown_signal")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:  # Windows
            signal.signal(sig, _stop)
    tasks = [asyncio.create_task(c) for c in coros]
    stop_task = asyncio.create_task(stop.wait())
    await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)
    for fn in on_stop:
        fn()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="scalper", description="Coinbase USDT-GBP spread-capture bot")
    parser.add_argument(
        "mode", nargs="?", choices=["record", "backtest", "paper", "live"], help="overrides MODE env"
    )
    parser.add_argument("--recording", default="data/recordings/*.jsonl.gz", help="file or glob for backtest")
    parser.add_argument("--version", action="version", version=f"scalper {__version__}")
    args = parser.parse_args(argv)

    overrides = {"mode": args.mode} if args.mode else {}
    s = load_settings(**overrides)
    configure_logging(s.log_level)
    s.data_dir.mkdir(parents=True, exist_ok=True)
    log.info("starting", version=__version__, mode=s.mode, product=s.product_id)

    runner = {
        "record": run_record,
        "backtest": lambda s: run_backtest(s, args.recording),
        "paper": run_paper,
        "live": run_live,
    }[s.mode]
    try:
        asyncio.run(runner(s))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
