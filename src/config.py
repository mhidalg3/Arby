"""Centralized configuration loaded from environment.

All runtime parameters that vary between dev/staging/prod live here.
Code that needs configuration imports `settings` from this module rather
than reading os.environ directly.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    database_url: str = Field(...)
    database_url_sync: str = Field(...)

    # Redis
    redis_url: str = Field(...)

    # Anthropic
    anthropic_api_key: str = Field(...)
    anthropic_model_parser: str = Field(default="claude-haiku-4-5-20251001")
    anthropic_model_validator: str = Field(default="claude-sonnet-4-6")

    # Engine parameters
    min_margin_pct_base: float = Field(default=1.0, ge=0.0)
    garch_sensitivity: float = Field(default=2.0, ge=0.0)
    max_position_per_match: float = Field(default=500.0, gt=0.0)
    max_total_exposure: float = Field(default=2000.0, gt=0.0)
    max_daily_loss: float = Field(default=500.0, gt=0.0)
    odds_tolerance_pct: float = Field(default=1.0, ge=0.0)

    # Logging
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="json")


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor. Call this rather than instantiating Settings directly."""
    return Settings()  # type: ignore[call-arg]
