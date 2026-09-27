"""Strategy engine: consumes market data, maintains quotes, tracks fills and risk.

The same engine runs in paper, live and backtest; only the Exchange and the
clock differ.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import structlog

from scalper.config import Settings
from scalper.execution.exchange import Exchange, OpenOrder, ProductMeta
from scalper.gate.jev_gate import GateDecision, GateFeatures, JevGate
from scalper.journal.store import Journal
from scalper.marketdata.fair_value import FairValueModel, RollingVol
from scalper.marketdata.orderbook import OrderBook
from scalper.observability import metrics as m
from scalper.risk.guards import RiskGuards
from scalper.strategy.inventory import Fill, Inventory
from scalper.strategy.lots import LotBook
from scalper.strategy.quoter import QuoteParams, Quotes, compute_quotes, size_for_notional
from scalper.strategy.stats import summarize

log = structlog.get_logger(__name__)

SIDES = ("BUY", "SELL")


def _f(v):
    """Decimal/None -> float/None for JSON."""
    return None if v is None else float(v)


class Engine:
    def __init__(
        self,
        settings: Settings,
        exchange: Exchange,
        meta: ProductMeta,
        book: OrderBook,
        fair_model: FairValueModel,
        inventory: Inventory,
        guards: RiskGuards,
        journal: Journal,
        gate: JevGate,
        clock: Callable[[], float],
        trade_sink=None,
        user_order_sink=None,
        seed_from_exchange: bool = False,
        maker_fee_rate: Decimal = Decimal(0),
    ) -> None:
        self.s = settings
        self.maker_fee = maker_fee_rate
        self.lots = LotBook(dust=meta.base_min_size)
        self.exchange = exchange
        self.meta = meta
        self.book = book
        self.fair_model = fair_model
        self.inv = inventory
        self.guards = guards
        self.journal = journal
        self.gate = gate
        self.clock = clock
        self.trade_sink = trade_sink  # PaperExchange.on_market_trade in paper/backtest
        self.user_order_sink = user_order_sink  # CoinbaseLiveExchange.on_user_order in live
        self.seed_from_exchange = seed_from_exchange
        self.tick = meta.quote_increment
        self.params = QuoteParams(
            tick=self.tick,
            min_edge_ticks=settings.min_edge_ticks,
            vol_multiplier=settings.vol_multiplier,
            inventory_skew_ticks=settings.inventory_skew_ticks,
            max_fair_basis_ticks=settings.max_fair_basis_ticks,
        )
        self.vol = RollingVol(settings.vol_window_seconds, self.tick)
        self.working: dict[str, OpenOrder] = {}
        self.seeded = False
        self.last_requote = 0.0
        self.last_equity_snapshot = 0.0
        self.last_fill: Fill | None = None
        self.last_market_ts: float | None = None
        self.current_day: str | None = None
        self.last_fair: Decimal | None = None
        self.last_gate: GateDecision | None = None
        self._lock = asyncio.Lock()
        self.run_id = uuid.uuid4().hex[:6]
        self.loop: asyncio.AbstractEventLoop | None = None
        self.series: deque[dict] = deque(maxlen=2000)  # for the dashboard charts
        self.last_series_ts = 0.0
        self.last_quotes: Quotes | None = None
        exchange.set_fill_callback(self.on_fill)
        # Paper and live keep their account, open trades and history across restarts.
        # Backtests always start clean.
        self.persist = settings.mode in ("paper", "live")
        self.account = journal.load_account() if (self.persist or seed_from_exchange) else None
        since = self.account.get("since_ts") if self.account else None
        self.since_ts = float(since) if since is not None else 0.0
        self.run_counts = {"BUY": 0, "SELL": 0}
        if self.persist or seed_from_exchange:
            for ts, side, price, size, _fee in journal.all_fills(self.since_ts):
                self.lots.apply(side, Decimal(price), Decimal(size), ts)
            if self.lots.buys or self.lots.sells:
                log.info("open_trades_restored", buys=len(self.lots.buys), sells=len(self.lots.sells))

    # -- market data --------------------------------------------------------
    async def on_message(self, msg: dict, ts: float) -> None:
        if self.loop is None:
            self.loop = asyncio.get_running_loop()
        channel = msg.get("channel")
        if channel == "l2_data":
            for ev in msg.get("events", []):
                self.book.apply_event(ev, ts)
            self.last_market_ts = ts
            top = self.book.top()
            if top.mid is not None:
                self.vol.update(ts, top.mid)
            await self._ensure_seeded(ts)
            await self.maybe_requote(ts)
        elif channel == "market_trades":
            for ev in msg.get("events", []):
                for t in ev.get("trades", []):
                    if t.get("product_id") != self.s.product_id:
                        continue
                    if self.trade_sink is not None:
                        await self.trade_sink(Decimal(t["price"]), Decimal(t["size"]), ts)
        elif channel == "ticker":
            for ev in msg.get("events", []):
                for t in ev.get("tickers", []):
                    pid, price = t.get("product_id"), t.get("price")
                    if pid and price:
                        self.fair_model.on_ticker(pid, Decimal(price), ts)
        elif channel == "user":
            if self.user_order_sink is not None:
                for ev in msg.get("events", []):
                    for o in ev.get("orders", []):
                        await self.user_order_sink(o, ts)

    async def _ensure_seeded(self, ts: float) -> None:
        if self.seeded or not self.book.ready:
            return
        mid = self.book.top().mid
        assert mid is not None
        acct = self.account
        today = self._day(ts)
        restored = False
        if self.seed_from_exchange:
            # Live: balances always come from the exchange; PnL baselines come from the journal.
            bal = await self.exchange.balances()
            base, quote = self.s.product_id.split("-")
            self.inv.seed(bal.get(quote, Decimal(0)), bal.get(base, Decimal(0)), mid)
            if acct and acct.get("start_equity"):
                self.inv.start_equity = Decimal(acct["start_equity"])
                self.inv.fees_paid = Decimal(acct.get("fees_paid") or "0")
                if acct.get("day") == today and acct.get("day_start_equity"):
                    self.inv.day_start_equity = Decimal(acct["day_start_equity"])
                restored = True
        elif acct and acct.get("gbp") is not None:
            # Paper: pick up the saved pretend account exactly where it was left.
            self.inv.restore(
                Decimal(acct["gbp"]),
                Decimal(acct["usdt"]),
                Decimal(acct["start_equity"]),
                Decimal(acct["day_start_equity"]),
                Decimal(acct.get("fees_paid") or "0"),
            )
            if acct.get("day") != today:
                self.inv.roll_day(mid)
            restored = True
        else:
            self._fresh_paper_seed(mid)
            if self.persist and acct is None:
                past = self.journal.all_fills(0.0)
                if past:
                    # Fills recorded before accounts were saved: rebuild the account from them.
                    for f_ts, side, price, size, fee in past:
                        self.inv.apply_fill(
                            Fill("", "", side, Decimal(price), Decimal(size), Decimal(fee or "0"), f_ts)
                        )
                    self.inv.fills.clear()
                    self.inv.roll_day(mid)
                    self.since_ts = float(past[0][0])
                    restored = True
                else:
                    self.since_ts = ts
        self.seeded = True
        self.current_day = today
        if self.persist:
            self._save_account(ts)
        log.info(
            "inventory_seeded",
            restored=restored,
            gbp=str(self.inv.gbp),
            usdt=str(self.inv.usdt),
            mid=str(mid),
            since=self.since_ts,
        )

    def _fresh_paper_seed(self, mid: Decimal) -> None:
        usdt_gbp = self.s.capital_gbp * self.s.target_usdt_fraction
        self.inv.seed(self.s.capital_gbp - usdt_gbp, (usdt_gbp / mid).quantize(Decimal("0.01")), mid)

    def _save_account(self, now: float) -> None:
        if not self.persist:
            return
        seeded = self.seeded
        self.journal.save_account(
            since_ts=self.since_ts,
            gbp=self.inv.gbp if seeded else None,
            usdt=self.inv.usdt if seeded else None,
            start_equity=self.inv.start_equity if seeded else None,
            day=self.current_day,
            day_start_equity=self.inv.day_start_equity if seeded else None,
            fees_paid=self.inv.fees_paid,
            updated_ts=now,
        )

    @staticmethod
    def _day(ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")

    # -- fills --------------------------------------------------------------
    async def on_fill(self, fill: Fill) -> None:
        self.inv.apply_fill(fill)
        self.lots.apply(fill.side, fill.price, fill.size, fill.ts)
        self.run_counts[fill.side] = self.run_counts.get(fill.side, 0) + 1
        self.last_fill = fill
        w = self.working.get(fill.side)
        if w is not None and w.order_id == fill.order_id:
            if fill.size >= w.size:
                self.working.pop(fill.side, None)
            else:
                w.size -= fill.size
        self.journal.fill(
            fill.ts,
            fill.order_id,
            fill.client_order_id,
            fill.side,
            fill.price,
            fill.size,
            fill.fee,
            self.inv.gbp,
            self.inv.usdt,
        )
        m.FILLS.labels(fill.side).inc()
        m.FEES.inc(float(fill.fee))
        log.info(
            "fill",
            side=fill.side,
            price=str(fill.price),
            size=str(fill.size),
            fee=str(fill.fee),
            gbp=str(self.inv.gbp),
            usdt=str(self.inv.usdt),
        )
        mid = self.book.top().mid or fill.price
        if not self.guards.check_daily_loss(self.inv, mid).allowed:
            await self._cancel_all_working("daily_loss_cap")

    # -- quoting ------------------------------------------------------------
    async def maybe_requote(self, now: float) -> None:
        if now - self.last_requote < self.s.requote_min_interval_s or self._lock.locked() or not self.seeded:
            return
        async with self._lock:
            self.last_requote = now
            await self._roll_day_if_needed(now)
            verdict = self.guards.check_market(self.book, now)
            m.HALTED.set(1 if self.guards.halted else 0)
            if not verdict.allowed:
                m.GUARD_BLOCKS.labels(verdict.reason).inc()
                if self.working:
                    await self._cancel_all_working(verdict.reason)
                return
            top = self.book.top()
            mid = top.mid
            assert mid is not None and top.spread is not None
            if not self.guards.check_daily_loss(self.inv, mid).allowed:
                await self._cancel_all_working("daily_loss_cap")
                return
            fair = self.fair_model.fair(top, now)
            if fair is None:
                return
            self.last_fair = fair
            vol = self.vol.range_ticks()
            dev = self.inv.deviation_fraction(mid)
            quotes = self._apply_profit_lock(compute_quotes(top, fair, vol, dev, self.params))
            self.last_quotes = quotes
            gate = await self._gate(quotes, top, mid, vol, dev, now)
            desired = {
                "BUY": quotes.bid if gate.quote_bid else None,
                "SELL": quotes.ask if gate.quote_ask else None,
            }
            notional = self.s.order_notional_gbp * gate.size_mult
            for side in SIDES:
                price = desired[side]
                size = (
                    size_for_notional(notional, price, self.meta.base_increment, self.meta.base_min_size)
                    if price
                    else Decimal(0)
                )
                await self._reconcile(side, price, size, now)
            self._update_metrics(top, fair, dev)
            if now - self.last_series_ts >= 1:
                self.last_series_ts = now
                self.series.append(
                    {
                        "ts": now,
                        "mid": float(mid),
                        "fair": float(fair),
                        "equity": float(self.inv.equity(mid)),
                        "pnl": float(self.inv.pnl(mid)),
                        "dev": float(dev),
                        "bid": float(desired["BUY"]) if desired["BUY"] is not None else None,
                        "ask": float(desired["SELL"]) if desired["SELL"] is not None else None,
                    }
                )
            if now - self.last_equity_snapshot >= 10:
                self.last_equity_snapshot = now
                self.journal.equity(
                    now,
                    mid,
                    fair,
                    self.inv.gbp,
                    self.inv.usdt,
                    self.inv.equity(mid),
                    self.inv.pnl(mid),
                    self.inv.day_pnl(mid),
                )
                self._save_account(now)

    def profit_limits(self) -> tuple[Decimal | None, Decimal | None]:
        """(ask floor, bid ceiling) from open trades, or (None, None) when the lock is off."""
        if not self.s.profit_lock:
            return None, None
        m, t, k = self.maker_fee, self.tick, self.s.min_profit_ticks
        return self.lots.ask_floor(m, t, k), self.lots.bid_ceiling(m, t, k)

    def _apply_profit_lock(self, q: Quotes) -> Quotes:
        floor, ceiling = self.profit_limits()
        ask = max(q.ask, floor) if (q.ask is not None and floor is not None) else q.ask
        bid = min(q.bid, ceiling) if (q.bid is not None and ceiling is not None) else q.bid
        if ask == q.ask and bid == q.bid:
            return q
        return dataclasses.replace(q, bid=bid, ask=ask, reason="profit_lock")

    async def _gate(
        self, quotes: Quotes, top, mid: Decimal, vol: Decimal, dev: Decimal, now: float
    ) -> GateDecision:
        if not self.gate.enabled:
            return GateDecision(True, True, Decimal(1), "disabled")
        feats = GateFeatures(
            fair_basis_ticks=float((quotes.fair - mid) / self.tick),
            spread_ticks=float(top.spread / self.tick),
            imbalance=float(self.book.imbalance() or 0),
            vol_ticks=float(vol),
            inventory_dev=float(dev),
            recent_fill_side=self.last_fill.side if self.last_fill else "NONE",
            minutes_since_last_fill=((now - self.last_fill.ts) / 60) if self.last_fill else 999.0,
        )
        decision = await self.gate.decide(feats)
        self.last_gate = decision
        m.GATE_DECISIONS.labels(decision.source).inc()
        m.GATE_LATENCY.observe(decision.latency_ms / 1000)
        self.journal.decision(now, quotes.bid, quotes.ask, decision)
        return decision

    async def _reconcile(self, side: str, price: Decimal | None, size: Decimal, now: float) -> None:
        w = self.working.get(side)
        if w is not None:
            stale = (now - w.placed_ts) > self.s.max_rest_seconds
            moved = price is not None and abs(w.price - price) > self.s.requote_tolerance_ticks * self.tick
            if price is None or moved or stale:
                await self._cancel_working(side, "requote" if price is not None else "no_quote")
            else:
                return
        if price is None or size <= 0:
            return
        verdict = self.guards.check_order(side, price, size, self.inv, len(self.working))
        if not verdict.allowed:
            m.GUARD_BLOCKS.labels(verdict.reason).inc()
            return
        coid = f"scalp-{self.run_id}-{uuid.uuid4().hex[:12]}"
        t0 = self.clock()
        ack = await self.exchange.place_post_only(side, price, size, coid)
        m.ORDER_LATENCY.observe(max(0.0, self.clock() - t0))
        if ack.accepted and ack.order_id:
            self.working[side] = OpenOrder(ack.order_id, coid, side, price, size, now)
            self.journal.order(now, coid, ack.order_id, side, price, size, "place")
            m.QUOTES.labels(side).inc()
        else:
            self.journal.order(now, coid, None, side, price, size, "reject", ack.reason)
            m.REJECTS.labels(ack.reason or "unknown").inc()

    async def _cancel_working(self, side: str, reason: str) -> None:
        w = self.working.pop(side, None)
        if w is None:
            return
        try:
            await self.exchange.cancel([w.order_id])
        except Exception as exc:  # noqa: BLE001
            log.warning("cancel_failed", order_id=w.order_id, error=str(exc))
        self.journal.order(
            self.clock(), w.client_order_id, w.order_id, side, w.price, w.size, "cancel", reason
        )
        m.CANCELS.inc()

    async def _cancel_all_working(self, reason: str) -> None:
        for side in list(self.working):
            await self._cancel_working(side, reason)

    async def _roll_day_if_needed(self, now: float) -> None:
        day = self._day(now)
        if self.current_day is None:
            self.current_day = day
            return
        if day != self.current_day:
            self.current_day = day
            mid = self.book.top().mid
            if mid is not None:
                self.inv.roll_day(mid)
            self._save_account(now)
            if self.guards.halted and self.guards.halt_reason.startswith("daily_loss_cap"):
                self.guards.reset_halt()
                log.info("daily_loss_halt_reset", day=day)

    def _update_metrics(self, top, fair: Decimal, dev: Decimal) -> None:
        mid = top.mid
        m.MID.set(float(mid))
        m.FAIR.set(float(fair))
        m.SPREAD_TICKS.set(float(top.spread / self.tick))
        m.EQUITY.set(float(self.inv.equity(mid)))
        m.PNL.set(float(self.inv.pnl(mid)))
        m.DAY_PNL.set(float(self.inv.day_pnl(mid)))
        m.INVENTORY_USDT.set(float(self.inv.usdt))
        m.INVENTORY_DEV.set(float(dev))
        m.OPEN_ORDERS.set(len(self.working))
        if self.last_market_ts is not None:
            m.WS_LAG.set(max(0.0, self.clock() - self.last_market_ts))

    # -- lifecycle ------------------------------------------------------------
    async def shutdown(self) -> None:
        await self._cancel_all_working("shutdown")
        if self.seeded:
            self._save_account(self.clock())
        try:
            n = await self.exchange.cancel_all()
            if n:
                log.info("cancelled_remaining_orders", count=n)
        except Exception as exc:  # noqa: BLE001
            log.error("cancel_all_failed", error=str(exc))
        await self.gate.aclose()

    # -- dashboard ------------------------------------------------------------
    def request_halt(self, reason: str = "operator") -> None:
        """Thread-safe: halt quoting and cancel working orders from outside the loop."""
        self.guards.halt(reason)
        if self.loop is not None and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._cancel_all_working(f"halt:{reason}"), self.loop)

    def request_resume(self) -> None:
        self.guards.reset_halt()

    def snapshot(self, depth: int = 8) -> dict:
        """JSON-safe view of the whole engine for the dashboard."""
        now = self.clock()
        top = self.book.top()
        mid = top.mid
        floor, ceiling = self.profit_limits()
        bids = sorted(self.book.bids.items(), key=lambda kv: kv[0], reverse=True)[:depth]
        asks = sorted(self.book.asks.items(), key=lambda kv: kv[0])[:depth]
        implied = self.fair_model.implied(now)
        seeded = self.seeded and mid is not None
        gate = self.last_gate
        q = self.last_quotes
        return {
            "ts": now,
            "mode": self.s.mode,
            "product": self.s.product_id,
            "exchange": getattr(self.exchange, "name", "?"),
            "tick": str(self.tick),
            "market": {
                "mid": _f(mid),
                "fair": _f(self.last_fair),
                "implied": _f(implied),
                "microprice": _f(top.microprice),
                "best_bid": _f(top.best_bid),
                "best_ask": _f(top.best_ask),
                "spread_ticks": _f(top.spread / self.tick) if top.spread is not None else None,
                "imbalance": _f(self.book.imbalance()),
                "vol_ticks": _f(self.vol.range_ticks()),
                "ws_lag_s": round(now - self.last_market_ts, 2) if self.last_market_ts else None,
                "book_ready": self.book.ready,
                "refs": {
                    k: {"price": _f(v.price), "age_s": round(now - v.ts, 1)}
                    for k, v in self.fair_model.refs.items()
                },
            },
            "book": {
                "bids": [[_f(p), _f(qy)] for p, qy in bids],
                "asks": [[_f(p), _f(qy)] for p, qy in asks],
            },
            "quotes": {
                "bid": _f(q.bid) if q else None,
                "ask": _f(q.ask) if q else None,
                "half_spread_ticks": _f(q.half_spread_ticks) if q else None,
                "skew_ticks": _f(q.skew_ticks) if q else None,
                "profit_lock": self.s.profit_lock,
                "ask_floor": _f(floor),
                "bid_ceiling": _f(ceiling),
                "open_buys": len(self.lots.buys),
                "open_sells": len(self.lots.sells),
                "open_buy_usdt": _f(self.lots.open_size("BUY")),
                "open_sell_usdt": _f(self.lots.open_size("SELL")),
                "cheapest_open_buy": _f(min((lt.price for lt in self.lots.buys), default=None)),
                "dearest_open_sell": _f(max((lt.price for lt in self.lots.sells), default=None)),
            },
            "working": [
                {
                    "side": w.side,
                    "price": _f(w.price),
                    "size": _f(w.size),
                    "age_s": round(now - w.placed_ts, 1),
                    "order_id": w.order_id,
                }
                for w in self.working.values()
            ],
            "position": {
                "seeded": self.seeded,
                "gbp": _f(self.inv.gbp),
                "usdt": _f(self.inv.usdt),
                "equity": _f(self.inv.equity(mid)) if seeded else None,
                "pnl": _f(self.inv.pnl(mid)) if seeded else None,
                "day_pnl": _f(self.inv.day_pnl(mid)) if seeded else None,
                "fees": _f(self.inv.fees_paid),
                "deviation": _f(self.inv.deviation_fraction(mid)) if seeded else None,
                "fills": len(self.inv.fills),
                "capital": _f(self.s.capital_gbp),
                "daily_loss_cap": _f(self.guards.daily_loss_cap),
            },
            "risk": {
                "halted": self.guards.halted,
                "halt_reason": self.guards.halt_reason,
                "kill_switch": self.guards.kill_switch_present(),
                "order_notional": _f(self.s.order_notional_gbp),
                "max_open_orders": self.s.max_open_orders,
            },
            "gate": {
                "enabled": self.gate.enabled,
                "calls": self.gate.calls,
                "fallbacks": self.gate.fallbacks,
                "last": None
                if gate is None
                else {
                    "source": gate.source,
                    "quote_bid": gate.quote_bid,
                    "quote_ask": gate.quote_ask,
                    "size_mult": _f(gate.size_mult),
                    "p_bid": gate.p_bid,
                    "p_ask": gate.p_ask,
                    "score": gate.score,
                    "latency_ms": round(gate.latency_ms, 1),
                },
            },
            "recent_fills": [
                {
                    "ts": r["ts"],
                    "side": r["side"],
                    "price": float(r["price"]),
                    "size": float(r["size"]),
                    "fee": float(r["fee"]),
                }
                for r in self.journal.recent_fills(30, self.since_ts)
            ],
            "trades": {
                "since_ts": self.since_ts,
                "account": summarize(self.journal.fill_aggregates(self.since_ts)),
                "run": {"buys": self.run_counts.get("BUY", 0), "sells": self.run_counts.get("SELL", 0)},
            },
        }

    def request_paper_reset(self, timeout: float = 5.0) -> dict:
        """Thread-safe: wipe the paper account and start again from CAPITAL_GBP. Paper mode only."""
        if self.s.mode != "paper":
            return {"ok": False, "error": "reset is only available in paper mode"}
        if self.loop is None or not self.loop.is_running():
            return {"ok": False, "error": "engine is not running yet"}
        asyncio.run_coroutine_threadsafe(self.reset_paper(), self.loop).result(timeout=timeout)
        return {"ok": True, "since_ts": self.since_ts}

    async def reset_paper(self) -> None:
        async with self._lock:
            await self._cancel_all_working("paper_reset")
            now = self.clock()
            self.since_ts = now
            self.lots = LotBook(dust=self.meta.base_min_size)
            self.inv.fills.clear()
            self.inv.fees_paid = Decimal(0)
            self.run_counts = {"BUY": 0, "SELL": 0}
            self.series.clear()
            self.last_fill = None
            mid = self.book.top().mid
            if mid is not None:
                self._fresh_paper_seed(mid)
                self.seeded = True
                self.current_day = self._day(now)
            else:
                self.seeded = False
            self.account = {"since_ts": now}
            self._save_account(now)
            log.warning("paper_account_reset", since=now, gbp=str(self.inv.gbp), usdt=str(self.inv.usdt))

    def health(self) -> tuple[bool, dict]:
        now = self.clock()
        fresh = (
            self.last_market_ts is not None and (now - self.last_market_ts) < self.s.stale_book_seconds * 3
        )
        mid = self.book.top().mid
        detail = {
            "mode": self.s.mode,
            "market_data_fresh": fresh,
            "book_ready": self.book.ready,
            "halted": self.guards.halted,
            "halt_reason": self.guards.halt_reason,
            "open_orders": len(self.working),
            "mid": str(mid) if mid is not None else None,
            "pnl_gbp": str(self.inv.pnl(mid)) if (mid is not None and self.seeded) else None,
        }
        return fresh, detail
