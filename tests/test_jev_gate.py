import asyncio
from decimal import Decimal
from types import SimpleNamespace

from scalper.gate.jev_gate import GateFeatures, JevGate

FEATS = GateFeatures(0.5, 2, 0.1, 1.0, 0.0, "NONE", 5.0)


class FakeClient:
    def __init__(self, p_bid=0.8, p_ask=0.3, score=2.6, delay=0.0, raise_exc=None):
        self.p_bid, self.p_ask, self.score, self.delay, self.raise_exc = p_bid, p_ask, score, delay, raise_exc
        self.calls = []

    async def system_one(self, state, questions, model=None):
        self.calls.append((state, questions, model))
        if self.raise_exc:
            raise self.raise_exc
        await asyncio.sleep(self.delay)
        return SimpleNamespace(
            answers={
                "quote_bid": SimpleNamespace(noul=self.p_bid),
                "quote_ask": SimpleNamespace(noul=self.p_ask),
                "size": SimpleNamespace(score=self.score),
            }
        )

    async def aclose(self):
        pass


async def test_disabled_gate_allows_everything():
    g = JevGate(enabled=False)
    d = await g.decide(FEATS)
    assert d.quote_bid and d.quote_ask and d.size_mult == 1 and d.source == "disabled"


async def test_jev_decision_thresholds_and_size():
    client = FakeClient(p_bid=0.8, p_ask=0.3, score=2.6)
    g = JevGate(enabled=True, threshold=0.55, client=client)
    d = await g.decide(FEATS)
    assert d.source == "jev" and d.quote_bid and not d.quote_ask
    assert d.size_mult == Decimal(2)  # round(2.6) = 3 -> double size
    state, questions, _ = client.calls[0]
    assert set(questions) == {"quote_bid", "quote_ask", "size"}
    assert state["inventory_deviation"] == 0.0


async def test_timeout_and_error_fall_back():
    g = JevGate(enabled=True, timeout_s=0.01, client=FakeClient(delay=0.5))
    d = await g.decide(FEATS)
    assert d.source == "fallback" and d.quote_bid and d.quote_ask and d.size_mult == 1
    g2 = JevGate(enabled=True, client=FakeClient(raise_exc=RuntimeError("boom")))
    d2 = await g2.decide(FEATS)
    assert d2.source == "fallback"
    assert g.fallbacks == 1 and g2.fallbacks == 1
