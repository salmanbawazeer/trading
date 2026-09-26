"""Runtime configuration, loaded from environment variables / .env."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Mode = Literal["record", "backtest", "paper", "live"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # mode / arming
    mode: Mode = "paper"
    live_trading: str = Field(default="", description="Must equal I_UNDERSTAND for live mode")

    # exchange credentials (same env names the official SDK reads)
    coinbase_api_key: str | None = Field(default=None, alias="COINBASE_API_KEY")
    coinbase_api_secret: str | None = Field(default=None, alias="COINBASE_API_SECRET")

    # market
    product_id: str = "USDT-GBP"
    ref_usdt_usd: str = "USDT-USD"
    ref_btc_gbp: str = "BTC-GBP"
    ref_btc_usd: str = "BTC-USD"
    capital_gbp: Decimal = Decimal(1000)
    target_usdt_fraction: Decimal = Decimal("0.5")

    # product metadata fallbacks (overwritten from the API when available)
    quote_increment: Decimal = Decimal("0.0001")
    base_increment: Decimal = Decimal("0.01")
    base_min_size: Decimal = Decimal(1)

    # risk
    order_notional_gbp: Decimal = Decimal(25)
    max_inventory_pct: Decimal = Decimal("0.5")
    max_open_orders: int = 2
    daily_loss_cap_pct: Decimal = Decimal("0.01")
    stale_book_seconds: float = 5.0
    max_spread_ticks: int = 20
    ref_price_max_age_s: float = 30.0
    kill_switch_path: Path = Path("KILL_SWITCH")

    # strategy
    min_edge_ticks: int = 1
    vol_multiplier: Decimal = Decimal("1.0")
    vol_window_seconds: float = 30.0
    requote_min_interval_s: float = 1.0
    max_rest_seconds: float = 30.0
    inventory_skew_ticks: Decimal = Decimal(2)
    fair_blend_weight: Decimal = Decimal("0.5")
    max_fair_basis_ticks: int = 10

    # fees
    paper_maker_fee_rate: Decimal = Decimal(0)
    paper_taker_fee_rate: Decimal = Decimal("0.00001")
    allow_unprofitable_fees: bool = False

    # Jev gate
    jev_enabled: bool = False
    jev_threshold: float = 0.55
    jev_timeout_s: float = 0.3
    jev_model: str | None = None

    # rate limiting for private REST calls
    rest_requests_per_second: float = 5.0

    # storage / observability
    data_dir: Path = Path("./data")
    metrics_port: int = 9108
    log_level: str = "INFO"

    @field_validator("live_trading")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def live_armed(self) -> bool:
        return self.live_trading == "I_UNDERSTAND"

    @property
    def journal_path(self) -> Path:
        return self.data_dir / "journal.sqlite"

    @property
    def recordings_dir(self) -> Path:
        return self.data_dir / "recordings"

    @property
    def has_api_key(self) -> bool:
        return bool(self.coinbase_api_key and self.coinbase_api_secret)


def load_settings(**overrides) -> Settings:
    return Settings(**overrides)
