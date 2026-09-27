"""Configuration errors: clear messages, and parking instead of crash-looping in containers."""

import json
import urllib.request
from decimal import Decimal
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from requests.exceptions import HTTPError

from scalper import main
from scalper.config import Settings
from scalper.execution.exchange import ProductMeta
from scalper.observability.dashboard import BlockedDashboard
from scalper.observability.metrics import start_http_server
from scalper.risk.fee_check import FeeRates

META = ProductMeta("USDT-GBP", Decimal("0.0001"), Decimal("0.01"), Decimal(1))


def pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()


def test_secret_with_literal_backslash_n_is_unescaped():
    real = pem()
    one_line = real.strip().replace("\n", "\\n")
    s = Settings(_env_file=None, COINBASE_API_SECRET=one_line)
    assert s.coinbase_api_secret == real
    serialization.load_pem_private_key(s.coinbase_api_secret.encode(), password=None)
    # already-correct keys and non-PEM secrets pass through
    assert Settings(_env_file=None, COINBASE_API_SECRET=real).coinbase_api_secret == real
    assert Settings(_env_file=None, COINBASE_API_SECRET="abc=").coinbase_api_secret == "abc="


def test_fee_check_raises_fatal_with_hint(tmp_path):
    s = Settings(_env_file=None, mode="paper", data_dir=tmp_path)
    standard = FeeRates(Decimal("0.006"), Decimal("0.012"), "Advanced 1")
    with pytest.raises(main.FatalConfigError) as exc:
        main.enforce_fee_check(s, standard, META, Decimal("0.75"))
    assert "NOT VIABLE" in str(exc.value) and "ALLOW_UNPROFITABLE_FEES" in str(exc.value)
    main.enforce_fee_check(
        s.model_copy(update={"allow_unprofitable_fees": True}), standard, META, Decimal("0.75")
    )
    live = s.model_copy(update={"mode": "live", "allow_unprofitable_fees": True})
    with pytest.raises(main.FatalConfigError) as exc:
        main.enforce_fee_check(live, standard, META, Decimal("0.75"))
    assert "ALLOW_UNPROFITABLE_FEES" not in str(exc.value)


class _FakeLive:
    error: Exception | None = None

    def __init__(self, *a, **k):
        pass

    async def product_meta(self):
        if self.error:
            raise self.error
        return META

    async def fee_rates(self):
        return FeeRates(Decimal(0), Decimal(0))


@pytest.mark.parametrize(
    ("error", "expect"),
    [
        (ValueError("private key is neither PEM nor valid base64"), "COINBASE_API_SECRET"),
        (HTTPError("401", response=SimpleNamespace(status_code=401)), "HTTP 401"),
        (HTTPError("403", response=SimpleNamespace(status_code=403)), "HTTP 403"),
    ],
)
async def test_bad_keys_become_fatal_config_errors(monkeypatch, tmp_path, error, expect):
    import scalper.execution.coinbase_live as cl

    monkeypatch.setattr(cl, "CoinbaseLiveExchange", type("L", (_FakeLive,), {"error": error}))
    s = Settings(_env_file=None, data_dir=tmp_path, COINBASE_API_KEY="k", COINBASE_API_SECRET="s")
    with pytest.raises(main.FatalConfigError) as exc:
        await main._resolve_meta_and_fees(s)
    assert expect in str(exc.value)


async def test_server_errors_are_not_fatal(monkeypatch, tmp_path):
    import scalper.execution.coinbase_live as cl

    err = HTTPError("503", response=SimpleNamespace(status_code=503))
    monkeypatch.setattr(cl, "CoinbaseLiveExchange", type("L", (_FakeLive,), {"error": err}))
    s = Settings(_env_file=None, data_dir=tmp_path, COINBASE_API_KEY="k", COINBASE_API_SECRET="s")
    with pytest.raises(HTTPError):  # transient: let the restart policy retry
        await main._resolve_meta_and_fees(s)


@pytest.mark.parametrize("park", [False, True])
def test_cli_exits_or_parks_on_fatal(monkeypatch, tmp_path, park):
    async def boom(s):
        raise main.FatalConfigError("fees not viable")

    parked = []

    async def fake_park(s, reason):
        parked.append(reason)

    monkeypatch.setattr(main, "run_paper", boom)
    monkeypatch.setattr(main, "park", fake_park)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PARK_ON_FATAL", "true" if park else "false")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main.cli(["paper"])
    assert exc.value.code == 2
    assert parked == (["fees not viable"] if park else [])


def test_blocked_dashboard_served_and_escaped():
    dash = BlockedDashboard("paper", "USDT-GBP", "fee <script>alert(1)</script> NOT VIABLE", 0.0)
    health = lambda: (True, {"blocked": True})
    server = start_http_server(0, health, dash, host="127.0.0.1")
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/", timeout=5).read().decode()
        assert "BLOCKED" in page and "NOT VIABLE" in page and "<script>alert" not in page
        with urllib.request.urlopen(base + "/healthz", timeout=5) as r:
            assert r.status == 200 and json.loads(r.read())["blocked"] is True
        state = json.loads(urllib.request.urlopen(base + "/api/state", timeout=5).read())
        assert state["blocked"] and "NOT VIABLE" in state["reason"]
    finally:
        server.shutdown()
