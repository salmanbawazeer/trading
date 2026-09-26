"""Prometheus metrics plus a tiny HTTP server exposing /metrics and /healthz."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

EQUITY = Gauge("scalper_equity_gbp", "Mark-to-market equity in GBP")
PNL = Gauge("scalper_pnl_gbp", "PnL since start in GBP")
DAY_PNL = Gauge("scalper_day_pnl_gbp", "PnL since start of day in GBP")
INVENTORY_USDT = Gauge("scalper_inventory_usdt", "USDT held")
INVENTORY_DEV = Gauge("scalper_inventory_deviation", "Inventory deviation from target, -1..1")
MID = Gauge("scalper_mid", "Mid price")
FAIR = Gauge("scalper_fair", "Fair value")
SPREAD_TICKS = Gauge("scalper_spread_ticks", "Book spread in ticks")
OPEN_ORDERS = Gauge("scalper_open_orders", "Open orders")
HALTED = Gauge("scalper_halted", "1 if quoting is halted")
WS_LAG = Gauge("scalper_ws_lag_seconds", "Seconds since last market data message")
QUOTES = Counter("scalper_quotes_total", "Quotes placed", ["side"])
CANCELS = Counter("scalper_cancels_total", "Orders cancelled")
REJECTS = Counter("scalper_rejects_total", "Orders rejected", ["reason"])
FILLS = Counter("scalper_fills_total", "Fills", ["side"])
FEES = Counter("scalper_fees_gbp_total", "Fees paid in GBP")
GUARD_BLOCKS = Counter("scalper_guard_blocks_total", "Quoting cycles blocked by a guard", ["reason"])
GATE_DECISIONS = Counter("scalper_gate_decisions_total", "Jev gate decisions", ["source"])
GATE_LATENCY = Histogram(
    "scalper_gate_latency_seconds", "Jev gate latency", buckets=(0.05, 0.1, 0.2, 0.3, 0.5, 1.0)
)
ORDER_LATENCY = Histogram(
    "scalper_order_latency_seconds", "Order placement latency", buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0)
)


def start_http_server(port: int, health_fn: Callable[[], tuple[bool, dict]]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/metrics"):
                body = generate_latest()
                self.send_response(200)
                self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            elif self.path.startswith("/healthz"):
                ok, detail = health_fn()
                body = json.dumps(detail).encode()
                self.send_response(200 if ok else 503)
                self.send_header("Content-Type", "application/json")
            else:
                body = b"not found"
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):  # silence default access log
            return

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="metrics-http").start()
    return server
