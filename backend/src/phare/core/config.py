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

    # Run Alembic upgrade on app startup. Convenient for dev / E2E / compose; off by default
    # so production controls migrations explicitly.
    migrate_on_startup: bool = False

    database_url: str = "postgresql+psycopg://phare:phare@localhost:5432/phare"

    # NoDecode: keep pydantic-settings from JSON-decoding the env value so the
    # validator below can accept a plain comma-separated string. Defaults to the Vite dev
    # server so `phare serve` works with the SPA out of the box; override in production.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    # OpenTelemetry: when unset, no exporter is wired (local dev / tests stay self-contained).
    otlp_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices("otlp_endpoint", "otel_exporter_otlp_endpoint"),
    )

    # External providers (optional; ingestion needs these to run against live accounts).
    tmdb_api_key: str | None = None
    tmdb_base_url: str = "https://api.themoviedb.org/3"
    # Base for poster image URLs; a TMDB poster_path is appended (e.g. ".../w342/abc.jpg").
    tmdb_image_base_url: str = "https://image.tmdb.org/t/p/w342"
    trakt_client_id: str | None = None
    trakt_client_secret: str | None = None  # needed only for the OAuth device flow
    trakt_base_url: str = "https://api.trakt.tv"

    # LLM + embeddings (OpenAI-compatible). Embedding dim is fixed by the schema; switching to
    # an embedding model of a different dimension requires a migration + re-embed.
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str | None = None
    llm_chat_model: str = "gpt-4o-mini"
    llm_embedding_model: str = "text-embedding-3-small"
    llm_embedding_dim: int = 1536

    # Recommendation engine tuning (safe defaults; rarely need changing).
    recommend_row_size: int = 12
    recommend_swing_slots: int = 2

    # Auth (opt-in): when AUTH_PASSWORD is unset the API is open (single-user dev posture).
    # SECRET_KEY signs bearer tokens and derives the source-token encryption key; it falls back
    # to AUTH_PASSWORD when unset, so the minimal config is just AUTH_PASSWORD.
    auth_password: str | None = None
    secret_key: str | None = None
    auth_token_ttl_seconds: int = 60 * 60 * 24 * 30  # 30 days

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_password)

    @property
    def signing_secret(self) -> str | None:
        return self.secret_key or self.auth_password

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
