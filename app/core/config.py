"""Runtime configuration. Every value can be overridden with an OPSIB_* environment variable."""
from __future__ import annotations

from decimal import Decimal
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPSIB_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://opsib:opsib@localhost:5432/opsib"
    # Single tenant for the pilot slice. In production this comes from the authenticated principal.
    tenant_id: str = "demo-retailer"
    # Scenario logic never reads the machine clock. "system" switches to the live clock.
    evaluation_time: str = "2026-10-16T15:00:00+05:30"
    # An availability sample older than this (relative to the evaluation time) is too stale to route on.
    availability_max_age_minutes: int = 60
    # Zero tolerance by default, as the brief requires. Expressed in the product base unit.
    shortfall_tolerance_base_units: Decimal = Decimal("0")
    task_due_hours: int = 24
    # Optional AI investigation summaries. "disabled" keeps the service fully deterministic.
    ai_provider: str = "disabled"
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-5-5"
    ai_timeout_seconds: float = 10.0
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
