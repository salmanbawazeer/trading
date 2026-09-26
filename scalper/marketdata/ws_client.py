"""Minimal asyncio client for the Coinbase Advanced Trade WebSocket.

Public channels (level2, market_trades, ticker, heartbeats) go to
`advanced-trade-ws.coinbase.com`; the authenticated `user` channel goes to
`advanced-trade-ws-user.coinbase.com` with a JWT. Both reconnect with backoff.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable

import structlog
import websockets

log = structlog.get_logger(__name__)

PUBLIC_URL = "wss://advanced-trade-ws.coinbase.com"
USER_URL = "wss://advanced-trade-ws-user.coinbase.com"

MessageHandler = Callable[[dict, float], Awaitable[None]]


class CoinbaseFeed:
    def __init__(
        self,
        handler: MessageHandler,
        subscriptions: list[tuple[str, list[str]]],
        url: str = PUBLIC_URL,
        jwt_factory: Callable[[], str] | None = None,
        name: str = "public",
    ) -> None:
        self.handler = handler
        self.subscriptions = subscriptions
        self.url = url
        self.jwt_factory = jwt_factory
        self.name = name
        self.connected = False
        self.last_message_ts: float | None = None
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self.url, ping_interval=20, ping_timeout=20, max_size=10 * 1024 * 1024
                ) as ws:
                    self.connected = True
                    backoff = 1.0
                    for channel, products in self.subscriptions:
                        msg = {"type": "subscribe", "channel": channel, "product_ids": products}
                        if self.jwt_factory:
                            msg["jwt"] = self.jwt_factory()
                        await ws.send(json.dumps(msg))
                    log.info("ws_connected", feed=self.name, url=self.url)
                    await self._pump(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                self.connected = False
                if self._stop.is_set():
                    break
                sleep = backoff + random.uniform(0, 0.5)
                log.warning("ws_disconnected", feed=self.name, error=str(exc), retry_in=round(sleep, 1))
                await asyncio.sleep(sleep)
                backoff = min(backoff * 2, 30)
        self.connected = False

    async def _pump(self, ws) -> None:
        stop_task = asyncio.create_task(self._stop.wait())
        try:
            while True:
                recv_task = asyncio.create_task(ws.recv())
                done, _ = await asyncio.wait({recv_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                if stop_task in done:
                    recv_task.cancel()
                    return
                raw = recv_task.result()
                ts = time.time()
                self.last_message_ts = ts
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "error":
                    log.error("ws_error", feed=self.name, message=msg.get("message"))
                    continue
                await self.handler(msg, ts)
        finally:
            stop_task.cancel()


def public_subscriptions(product_id: str, ref_products: list[str]) -> list[tuple[str, list[str]]]:
    return [
        ("heartbeats", []),
        ("level2", [product_id]),
        ("market_trades", [product_id]),
        ("ticker", sorted({product_id, *ref_products})),
    ]


def make_jwt_factory(api_key: str, api_secret: str) -> Callable[[], str]:
    from coinbase import jwt_generator

    return lambda: jwt_generator.build_ws_jwt(api_key, api_secret)
