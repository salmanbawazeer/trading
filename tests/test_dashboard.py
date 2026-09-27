"""Dashboard API served by the metrics HTTP server, driven by a replayed engine."""

import json
import urllib.request

import pytest

from scalper.backtest.replay import replay
from scalper.observability.dashboard import DashboardAPI
from scalper.observability.metrics import start_http_server
from tests.test_engine_replay import build, synthetic_recording


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, json.loads(
            r.read()
        ) if r.headers.get_content_type() == "application/json" else r.read()


def post(url, body):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture
async def served(settings, meta, tmp_path):
    rec = tmp_path / "ws.jsonl.gz"
    synthetic_recording(rec)
    engine, clock, journal = build(settings, meta)
    await replay(engine, clock, [rec])
    server = start_http_server(0, engine.health, DashboardAPI(engine, journal), host="127.0.0.1")
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield engine, base
    server.shutdown()


async def test_snapshot_is_json_safe_and_complete(served):
    engine, _ = served
    snap = engine.snapshot()
    json.dumps(snap)  # no Decimals leak through
    assert snap["market"]["mid"] == 0.7501
    assert snap["position"]["fills"] == 2 and snap["position"]["pnl"] > 0
    assert {w["side"] for w in snap["working"]} == {"BUY", "SELL"}
    assert snap["book"]["bids"][0][0] == 0.75
    assert snap["quotes"]["bid"] == 0.75 and snap["quotes"]["ask"] >= 0.7502
    assert snap["gate"]["enabled"] is False
    assert len(engine.series) >= 2
    t = snap["trades"]["account"]
    assert (t["buys"], t["sells"], t["pairs"]) == (1, 1, 1)
    assert t["avg_buy"] == 0.75 and t["avg_sell"] == 0.7502 and t["net_gbp"] > 0
    assert snap["trades"]["run"] == {"buys": 1, "sells": 1}
    assert [f["side"] for f in snap["recent_fills"]] == ["SELL", "BUY"]


async def test_http_routes(served):
    _engine, base = served
    status, html = get(base + "/")
    assert status == 200 and b"SCALPER HUD" in html
    status, state = get(base + "/api/state")
    assert status == 200 and state["product"] == "USDT-GBP"
    _, series = get(base + "/api/series")
    assert series and set(series[0]) >= {"ts", "mid", "fair", "equity", "pnl", "dev"}
    _, fills = get(base + "/api/fills?limit=1")
    assert len(fills) == 1 and fills[0]["side"] == "SELL"
    _, eq = get(base + "/api/equity")
    assert isinstance(eq, list)
    status, _ = get(base + "/metrics")
    assert status == 200
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(base + "/nope")
    assert exc.value.code == 404


async def test_controls_halt_resume_and_kill(served):
    engine, base = served
    assert len(engine.working) == 2
    status, r = post(base + "/api/control", {"action": "halt"})
    assert status == 200 and r["halted"] and r["halt_reason"] == "operator"
    status, r = post(base + "/api/control", {"action": "resume"})
    assert status == 200 and not r["halted"]
    status, r = post(base + "/api/control", {"action": "kill_on"})
    assert r["kill_switch"] and r["halted"] and engine.guards.kill_switch_present()
    status, r = post(base + "/api/control", {"action": "kill_off"})
    assert not r["kill_switch"] and not r["halted"]
    status, r = post(base + "/api/control", {"action": "bogus"})
    assert status == 400 and not r["ok"]


async def test_request_halt_cancels_orders_inside_running_loop(settings, meta, tmp_path):
    import asyncio

    rec = tmp_path / "ws.jsonl.gz"
    synthetic_recording(rec)
    engine, clock, _ = build(settings, meta)
    await replay(engine, clock, [rec])
    assert len(engine.working) == 2
    # simulate the HTTP thread calling into the engine while the loop runs
    await asyncio.get_running_loop().run_in_executor(None, engine.request_halt, "operator")
    await asyncio.sleep(0.05)
    assert engine.working == {} and await engine.exchange.open_orders() == []
    assert engine.snapshot()["risk"]["halted"] is True
