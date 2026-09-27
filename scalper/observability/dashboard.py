"""Dashboard API: JSON views over the engine and journal plus operator controls."""

from __future__ import annotations

import html
from importlib import resources


class DashboardAPI:
    def __init__(self, engine, journal) -> None:
        self.engine = engine
        self.journal = journal

    @staticmethod
    def html() -> bytes:
        return resources.files("scalper.observability").joinpath("dashboard.html").read_bytes()

    def state(self) -> dict:
        return self.engine.snapshot()

    def series(self) -> list[dict]:
        return list(self.engine.series)

    def fills(self, limit: int = 50) -> list[dict]:
        return self.journal.recent_fills(limit)

    def equity(self, limit: int = 500) -> list[dict]:
        return self.journal.recent_equity(limit)

    def control(self, action: str) -> dict:
        g = self.engine.guards
        if action == "halt":
            self.engine.request_halt("operator")
        elif action == "resume":
            self.engine.request_resume()
        elif action == "kill_on":
            g.kill_switch_path.parent.mkdir(parents=True, exist_ok=True)
            g.kill_switch_path.write_text("engaged from dashboard\n")
            self.engine.request_halt("kill_switch")
        elif action == "kill_off":
            if g.kill_switch_path.exists():
                g.kill_switch_path.unlink()
            if g.halt_reason == "kill_switch":
                self.engine.request_resume()
        else:
            return {"ok": False, "error": f"unknown action {action!r}"}
        return {
            "ok": True,
            "halted": g.halted,
            "halt_reason": g.halt_reason,
            "kill_switch": g.kill_switch_present(),
        }


BLOCKED_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SCALPER BLOCKED</title>
<style>
  :root { color-scheme: dark; --bg: #070b14; --panel: #0b1220; --line: #1c2b45; --text: #e6f1ff;
          --muted: #9bb0cc; --critical: #ff4d5e; --glow: #35d6f5;
          --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; background: var(--bg); color: var(--text); font-family: var(--mono);
         display: grid; place-items: center; padding: 16px;
         background-image: linear-gradient(rgba(53,214,245,.035) 1px, transparent 1px),
                           linear-gradient(90deg, rgba(53,214,245,.035) 1px, transparent 1px);
         background-size: 32px 32px; }
  .panel { max-width: 720px; width: 100%; background: var(--panel); border: 1px solid var(--critical);
           padding: 24px; box-shadow: 0 0 24px rgba(255,77,94,.25); }
  h1 { margin: 0 0 4px; color: var(--critical); letter-spacing: .3em; font-size: 20px; }
  .sub { color: var(--muted); letter-spacing: .12em; font-size: 11px; text-transform: uppercase; margin-bottom: 18px; }
  .reason { white-space: pre-wrap; line-height: 1.5; font-size: 14px; border-left: 2px solid var(--critical); padding-left: 12px; }
  .next { margin-top: 18px; color: var(--muted); font-size: 12px; line-height: 1.6; }
  code { color: var(--glow); }
</style></head>
<body><div class="panel">
  <h1>BLOCKED</h1>
  <div class="sub">__MODE__ &middot; __PRODUCT__ &middot; not trading</div>
  <div class="reason">__REASON__</div>
  <div class="next">Fix the setting in <code>.env</code>, then run
  <code>docker compose up -d --force-recreate</code>. No orders are placed while blocked.</div>
</div></body></html>
"""


class BlockedDashboard:
    """Stand-in dashboard served while the bot is parked on a configuration error."""

    def __init__(self, mode: str, product: str, reason: str, since: float) -> None:
        self.mode, self.product, self.reason, self.since = mode, product, reason, since

    def html(self) -> bytes:
        esc = html.escape
        page = (
            BLOCKED_HTML.replace("__MODE__", esc(self.mode.upper()))
            .replace("__PRODUCT__", esc(self.product))
            .replace("__REASON__", esc(self.reason))
        )
        return page.encode()

    def state(self) -> dict:
        return {
            "blocked": True,
            "mode": self.mode,
            "product": self.product,
            "reason": self.reason,
            "since": self.since,
        }

    def series(self) -> list[dict]:
        return []

    def fills(self, limit: int = 50) -> list[dict]:
        return []

    def equity(self, limit: int = 500) -> list[dict]:
        return []

    def control(self, action: str) -> dict:
        return {"ok": False, "error": "blocked on a configuration error; fix .env and restart"}
