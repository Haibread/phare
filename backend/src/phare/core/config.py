"""Application configuration.

All config resolves through this layer (defaults -> environment / .env). Never read
``os.environ`` directly elsewhere.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration, loaded once at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: str = "development"
    log_level: str = "INFO"
    service_name: str = "phare-backend"

    database_url: str = "postgresql+psycopg://phare:phare@localhost:5432/phare"

    # NoDecode: keep pydantic-settings from JSON-decoding the env value so the
    # validator below can accept a plain comma-separated string.
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # OpenTelemetry: when unset, no exporter is wired (local dev / tests stay self-contained).
    otlp_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices("otlp_endpoint", "otel_exporter_otlp_endpoint"),
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Accept a comma-separated string as well as a JSON list for CORS_ORIGINS."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return json.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()
