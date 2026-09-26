"""Dashboard API: JSON views over the engine and journal plus operator controls."""

from __future__ import annotations

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
