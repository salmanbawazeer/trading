"""SQLite journal: orders, fills, equity snapshots and gate decisions."""

from __future__ import annotations

import sqlite3
import threading
from decimal import Decimal
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
  ts REAL, mode TEXT, client_order_id TEXT, order_id TEXT, side TEXT,
  price TEXT, size TEXT, action TEXT, reason TEXT
);
CREATE TABLE IF NOT EXISTS fills (
  ts REAL, mode TEXT, order_id TEXT, client_order_id TEXT, side TEXT,
  price TEXT, size TEXT, fee TEXT, gbp_after TEXT, usdt_after TEXT
);
CREATE TABLE IF NOT EXISTS equity (
  ts REAL, mode TEXT, mid TEXT, fair TEXT, gbp TEXT, usdt TEXT, equity TEXT, pnl TEXT, day_pnl TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
  ts REAL, mode TEXT, rule_bid TEXT, rule_ask TEXT, gate_source TEXT,
  gate_quote_bid INTEGER, gate_quote_ask INTEGER, gate_size_mult TEXT,
  p_bid REAL, p_ask REAL, score REAL, latency_ms REAL
);
CREATE TABLE IF NOT EXISTS account (
  mode TEXT PRIMARY KEY, since_ts REAL, gbp TEXT, usdt TEXT, start_equity TEXT,
  day TEXT, day_start_equity TEXT, fees_paid TEXT, updated_ts REAL
);
CREATE INDEX IF NOT EXISTS fills_ts ON fills(ts);
CREATE INDEX IF NOT EXISTS equity_ts ON equity(ts);
"""


class Journal:
    def __init__(self, path: Path | str, mode: str) -> None:
        self.mode = mode
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()

    @staticmethod
    def _s(v) -> str | None:
        return None if v is None else str(v)

    def order(
        self,
        ts: float,
        client_order_id: str,
        order_id: str | None,
        side: str,
        price: Decimal,
        size: Decimal,
        action: str,
        reason: str = "",
    ) -> None:
        self.conn.execute(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, self.mode, client_order_id, order_id, side, self._s(price), self._s(size), action, reason),
        )

    def fill(
        self,
        ts: float,
        order_id: str,
        client_order_id: str,
        side: str,
        price: Decimal,
        size: Decimal,
        fee: Decimal,
        gbp: Decimal,
        usdt: Decimal,
    ) -> None:
        self.conn.execute(
            "INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                ts,
                self.mode,
                order_id,
                client_order_id,
                side,
                str(price),
                str(size),
                str(fee),
                str(gbp),
                str(usdt),
            ),
        )

    def equity(
        self,
        ts: float,
        mid: Decimal | None,
        fair: Decimal | None,
        gbp: Decimal,
        usdt: Decimal,
        equity: Decimal,
        pnl: Decimal,
        day_pnl: Decimal,
    ) -> None:
        self.conn.execute(
            "INSERT INTO equity VALUES (?,?,?,?,?,?,?,?,?)",
            (
                ts,
                self.mode,
                self._s(mid),
                self._s(fair),
                str(gbp),
                str(usdt),
                str(equity),
                str(pnl),
                str(day_pnl),
            ),
        )

    def decision(self, ts: float, rule_bid, rule_ask, gate) -> None:
        self.conn.execute(
            "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ts,
                self.mode,
                self._s(rule_bid),
                self._s(rule_ask),
                gate.source,
                int(gate.quote_bid),
                int(gate.quote_ask),
                str(gate.size_mult),
                gate.p_bid,
                gate.p_ask,
                gate.score,
                gate.latency_ms,
            ),
        )

    def recent_fills(self, limit: int = 50, since_ts: float = 0.0) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT ts, side, price, size, fee, order_id FROM fills WHERE mode = ? AND ts >= ?"
                " ORDER BY ts DESC, rowid DESC LIMIT ?",
                (self.mode, since_ts, int(limit)),
            ).fetchall()
        return [
            {"ts": r[0], "side": r[1], "price": r[2], "size": r[3], "fee": r[4], "order_id": r[5]}
            for r in rows
        ]

    def recent_equity(self, limit: int = 500) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT ts, mid, fair, equity, pnl, day_pnl, usdt FROM equity ORDER BY ts DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        rows.reverse()
        return [
            {
                "ts": r[0],
                "mid": r[1],
                "fair": r[2],
                "equity": r[3],
                "pnl": r[4],
                "day_pnl": r[5],
                "usdt": r[6],
            }
            for r in rows
        ]

    def all_fills(self, since_ts: float = 0.0) -> list[tuple[float, str, str, str, str]]:
        """(ts, side, price, size, fee) for this journal's mode, oldest first."""
        with self.lock:
            return self.conn.execute(
                "SELECT ts, side, price, size, fee FROM fills WHERE mode = ? AND ts >= ? ORDER BY ts, rowid",
                (self.mode, since_ts),
            ).fetchall()

    def fill_aggregates(self, since_ts: float = 0.0) -> dict[str, tuple[int, float, float, float]]:
        """Fills for this journal's mode since `since_ts`: {side: (count, qty, notional, fees)}."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT side, COUNT(*), COALESCE(SUM(CAST(size AS REAL)), 0),"
                " COALESCE(SUM(CAST(price AS REAL) * CAST(size AS REAL)), 0),"
                " COALESCE(SUM(CAST(fee AS REAL)), 0) FROM fills WHERE mode = ? AND ts >= ? GROUP BY side",
                (self.mode, since_ts),
            ).fetchall()
        return {r[0]: (int(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows}

    # -- account state, so paper/live survive restarts ------------------------
    ACCOUNT_FIELDS = (
        "since_ts",
        "gbp",
        "usdt",
        "start_equity",
        "day",
        "day_start_equity",
        "fees_paid",
        "updated_ts",
    )

    def load_account(self) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT " + ", ".join(self.ACCOUNT_FIELDS) + " FROM account WHERE mode = ?", (self.mode,)
            ).fetchone()
        return dict(zip(self.ACCOUNT_FIELDS, row, strict=True)) if row else None

    def save_account(self, **fields) -> None:
        vals = [fields.get(k) for k in self.ACCOUNT_FIELDS]
        vals = [str(v) if isinstance(v, Decimal) else v for v in vals]
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO account (mode, " + ", ".join(self.ACCOUNT_FIELDS) + ")"
                " VALUES (?" + ", ?" * len(self.ACCOUNT_FIELDS) + ")",
                (self.mode, *vals),
            )

    def summary(self) -> dict:
        cur = self.conn.execute("SELECT COUNT(*), COALESCE(SUM(CAST(fee AS REAL)),0) FROM fills")
        n_fills, fees = cur.fetchone()
        cur = self.conn.execute("SELECT pnl, equity FROM equity ORDER BY ts DESC LIMIT 1")
        row = cur.fetchone()
        return {
            "fills": n_fills,
            "fees_gbp": fees,
            "pnl_gbp": float(row[0]) if row else 0.0,
            "equity_gbp": float(row[1]) if row else None,
        }

    def close(self) -> None:
        self.conn.close()
