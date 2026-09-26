"""Optional TypeSafe Jev decision gate.

Pattern: code computes features and executes; Jev only judges closed questions.
Two Noul questions (quote bid? quote ask?) and one Score (size multiplier).
Any error or timeout falls back to the rule-based decision so the gate can
never take the bot down. Off by default (JEV_ENABLED=false).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal

import structlog

log = structlog.get_logger(__name__)

SIZE_LEVELS = ["do not quote", "half size", "normal size", "double size"]
SIZE_MULT = {0: Decimal(0), 1: Decimal("0.5"), 2: Decimal(1), 3: Decimal(2)}


@dataclass(frozen=True)
class GateFeatures:
    fair_basis_ticks: float  # (fair - mid) / tick
    spread_ticks: float
    imbalance: float  # [-1, 1]
    vol_ticks: float
    inventory_dev: float  # [-1, 1]
    recent_fill_side: str  # BUY | SELL | NONE
    minutes_since_last_fill: float

    def as_state(self) -> dict:
        return {
            "market": "USDT-GBP on Coinbase, passive post-only market making",
            "fair_minus_mid_ticks": round(self.fair_basis_ticks, 2),
            "spread_ticks": round(self.spread_ticks, 2),
            "book_imbalance_bid_minus_ask": round(self.imbalance, 3),
            "volatility_range_ticks_30s": round(self.vol_ticks, 2),
            "inventory_deviation": round(self.inventory_dev, 3),
            "recent_fill_side": self.recent_fill_side,
            "minutes_since_last_fill": round(self.minutes_since_last_fill, 1),
        }


@dataclass(frozen=True)
class GateDecision:
    quote_bid: bool
    quote_ask: bool
    size_mult: Decimal
    source: str  # jev | fallback | disabled
    latency_ms: float = 0.0
    p_bid: float | None = None
    p_ask: float | None = None
    score: float | None = None


QUESTIONS = {
    "quote_bid": {
        "type": "noul",
        "instructions": "Should the market maker post a passive BUY (bid) quote right now?",
        "criteria": {
            "true": "Posting a bid is likely to be filled by uninformed flow and earn the spread.",
            "false": "Price is about to fall through the bid (adverse selection) or inventory is already long.",
        },
    },
    "quote_ask": {
        "type": "noul",
        "instructions": "Should the market maker post a passive SELL (ask) quote right now?",
        "criteria": {
            "true": "Posting an ask is likely to be filled by uninformed flow and earn the spread.",
            "false": "Price is about to rise through the ask (adverse selection) or inventory is already short.",
        },
    },
    "size": {
        "type": "score",
        "instructions": "How large should the quotes be relative to normal size?",
        "criteria": SIZE_LEVELS,
    },
}


class JevGate:
    def __init__(
        self,
        enabled: bool,
        threshold: float = 0.55,
        timeout_s: float = 0.3,
        model: str | None = None,
        client=None,
    ) -> None:
        self.enabled = enabled
        self.threshold = threshold
        self.timeout_s = timeout_s
        self.model = model
        self._client = client
        self.fallbacks = 0
        self.calls = 0

    def _get_client(self):
        if self._client is None:
            from typesafe_sdk import AsyncTypeSafeClient  # optional dependency

            self._client = AsyncTypeSafeClient(timeout=self.timeout_s)
        return self._client

    async def decide(self, features: GateFeatures) -> GateDecision:
        if not self.enabled:
            return GateDecision(True, True, Decimal(1), "disabled")
        t0 = time.perf_counter()
        self.calls += 1
        try:
            client = self._get_client()
            resp = await asyncio.wait_for(
                client.system_one(state=features.as_state(), questions=QUESTIONS, model=self.model),
                timeout=self.timeout_s,
            )
            p_bid = float(resp.answers["quote_bid"].noul)
            p_ask = float(resp.answers["quote_ask"].noul)
            score = float(resp.answers["size"].score)
            mult = SIZE_MULT[max(0, min(3, round(score)))]
            return GateDecision(
                quote_bid=p_bid >= self.threshold,
                quote_ask=p_ask >= self.threshold,
                size_mult=mult,
                source="jev",
                latency_ms=(time.perf_counter() - t0) * 1000,
                p_bid=p_bid,
                p_ask=p_ask,
                score=score,
            )
        except Exception as exc:  # noqa: BLE001 - never let the gate take the bot down
            self.fallbacks += 1
            log.warning("jev_fallback", error=type(exc).__name__, detail=str(exc)[:200])
            return GateDecision(
                True, True, Decimal(1), "fallback", latency_ms=(time.perf_counter() - t0) * 1000
            )

    async def aclose(self) -> None:
        if self._client is not None and hasattr(self._client, "aclose"):
            await self._client.aclose()
