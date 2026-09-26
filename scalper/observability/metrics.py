"""Prometheus metrics plus a tiny HTTP server exposing /metrics, /healthz and the dashboard."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

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

HealthFn = Callable[[], tuple[bool, dict]]


def start_http_server(
    port: int, health_fn: HealthFn, dashboard=None, host: str = "0.0.0.0"
) -> ThreadingHTTPServer:
    """Serve /metrics, /healthz and, when `dashboard` (a DashboardAPI) is given, the HUD and its JSON API."""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            self._send(status, json.dumps(obj).encode(), "application/json")

        def do_GET(self):
            url = urlparse(self.path)
            path, qs = url.path, parse_qs(url.query)
            limit = int(qs.get("limit", ["100"])[0])
            if path == "/metrics":
                self._send(200, generate_latest(), CONTENT_TYPE_LATEST)
            elif path == "/healthz":
                ok, detail = health_fn()
                self._json(200 if ok else 503, detail)
            elif dashboard is None:
                self._send(404, b"not found", "text/plain")
            elif path in ("/", "/index.html", "/dashboard"):
                self._send(200, dashboard.html(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(200, dashboard.state())
            elif path == "/api/series":
                self._json(200, dashboard.series())
            elif path == "/api/fills":
                self._json(200, dashboard.fills(limit))
            elif path == "/api/equity":
                self._json(200, dashboard.equity(limit))
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            url = urlparse(self.path)
            if dashboard is None or url.path != "/api/control":
                self._send(404, b"not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                action = json.loads(raw or b"{}").get("action", "")
            except json.JSONDecodeError:
                self._json(400, {"ok": False, "error": "bad json"})
                return
            result = dashboard.control(action)
            self._json(200 if result.get("ok") else 400, result)

        def log_message(self, *_):  # silence default access log
            return

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="metrics-http").start()
    return server
