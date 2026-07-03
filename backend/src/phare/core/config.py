"""Application configuration.

All config resolves through this layer (defaults -> environment / .env). Never read
``os.environ`` directly elsewhere.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from phare.core.i18n import DEFAULT_LANGUAGE, Language


class Settings(BaseSettings):
    """Process configuration, loaded once at startup."""

    model_config = SettingsConfigDict(
        # Look in the CWD and one level up, so the same repo-root .env is found whether the process
        # starts from the repo root or from backend/ (the later entry wins).
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: str = "development"
    log_level: str = "INFO"
    service_name: str = "phare-backend"

    # Language for localised output (row titles, templates, and the language the LLM is told to
    # write in) when a request carries no Accept-Language header. The frontend sends one per call.
    default_language: Language = DEFAULT_LANGUAGE

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
    # In-process TTL (seconds) for cached TMDB metadata/search reads. 0 disables caching.
    tmdb_cache_ttl_seconds: int = 3600
    # DB-persisted TTL (seconds) for a title's localized synopsis/genres (per language). Long, since
    # a synopsis rarely changes; past it the detail view re-fetches from TMDB. Survives restarts and
    # doubles as the offline fallback when TMDB is unreachable (review C2). Default 30 days.
    title_localization_ttl_seconds: int = 60 * 60 * 24 * 30
    # On startup, if the candidate pool is empty and TMDB is configured, seed the catalog in the
    # background so a fresh instance has something to recommend from. Disable to manage the catalog
    # yourself. No-op without TMDB_API_KEY.
    catalog_autoseed: bool = True
    # What to seed when autoseed runs: "popular" (light front-page top-up), "broad" (deep genre
    # sweep — the real-world catalog), or "auto" (broad in production, popular elsewhere — so a prod
    # box deep-seeds itself without an operator running a command, while dev stays light). A typo
    # here fails validation at startup rather than silently falling back to popular.
    catalog_autoseed_scope: Literal["auto", "popular", "broad"] = "auto"
    # Depth/quality of a broad seed (also used by autoseed when its scope resolves to broad). The
    # broad sweep is what surfaces the long tail; deeper = more titles + more embedding cost.
    catalog_broad_pages_per_genre: int = 20
    catalog_broad_min_vote_count: int = 50
    # Ongoing freshness: every N seconds a background pass pulls new/current releases (trending +
    # now-playing/on-the-air) and embeds them, so the catalog keeps up with new movies/TV. 0 = off.
    catalog_refresh_interval_seconds: int = 86400
    # Delay before the *first* refresh after startup. Deliberately short (not a full interval) so a
    # box that restarts more often than the interval still refreshes — a full-interval first wait
    # would let frequent restarts starve the refresh entirely. Small enough to run on a normal boot,
    # long enough to let the startup autoseed get going first.
    catalog_refresh_initial_delay_seconds: int = 300
    # Pages of each freshness list to pull per refresh (≈20 titles/page/kind/list).
    catalog_refresh_pages: int = 1
    trakt_client_id: str | None = None
    trakt_client_secret: str | None = None  # needed only for the OAuth device flow
    trakt_base_url: str = "https://api.trakt.tv"
    # Seerr request hand-off (optional). Per-profile creds set via the UI take precedence; these
    # env defaults are a convenience for single-instance deployments.
    seerr_base_url: str | None = None
    seerr_api_key: str | None = None

    # LLM + embeddings (OpenAI-compatible). Embedding dim is fixed by the schema; switching to
    # an embedding model of a different dimension requires a migration + re-embed.
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str | None = None
    llm_chat_model: str = "gpt-4o-mini"
    # The conversational chat agent (planner + natural-language reply) can use a stronger model
    # than the high-volume mechanical work (explanations, taste). Falls back to llm_chat_model.
    llm_agent_model: str | None = None
    llm_embedding_model: str = "text-embedding-3-small"
    llm_embedding_dim: int = 1536
    # Opt in to requesting `LLM_EMBEDDING_DIM` via the standard `dimensions` parameter. Enable for
    # models with configurable (Matryoshka) embeddings so they fit the schema without a re-embed;
    # leave off for models that reject the parameter.
    llm_embedding_request_dimensions: bool = False
    # Set when the workhorse/agent model is a *reasoning* model (emits `<think>…</think>` before its
    # answer). Reasoning eats the response budget, so without headroom a model spends its whole
    # `max_tokens` thinking and returns empty/truncated JSON — breaking taste, planning, and themed
    # rows. When on, every bounded completion gets `llm_reasoning_headroom` extra tokens and the
    # reply stream drops a leading think block. See docs/configuration.md.
    llm_reasoning_model: bool = False
    llm_reasoning_headroom: int = 4096

    # Taste extraction cost controls. Taste is a derived artifact that auto-refreshes after a
    # profile's history changes, but a full re-extraction is a workhorse LLM call over the whole
    # window — so the auto-refresh is *gated* (the explicit POST /taste/generate always runs):
    # it skips unless enough new events accumulated, or the profile is older than the interval
    # *and* something changed. See docs/configuration.md. `taste_max_events` bounds prompt size.
    taste_refresh_min_events: int = 8
    taste_refresh_min_interval_seconds: int = 6 * 60 * 60  # 6 hours
    taste_max_events: int = 150
    # The explicit POST /taste/generate is a force-regenerate (it ignores the gate above), so it's
    # rate-limited per profile to stop repeated button clicks each spending a workhorse LLM call.
    taste_generate_cooldown_seconds: int = 10 * 60  # 10 minutes

    # Recommendation engine tuning (safe defaults; rarely need changing).
    recommend_row_size: int = 12
    recommend_swing_slots: int = 2
    # Eager LLM "why this" generation on home/dynamic rows. Default 0 = off: rows use the instant
    # deterministic template, and the richer LLM reason is generated *lazily* per title, only when
    # the user opens its detail sheet (GET /profiles/{id}/titles/{id}/explanation) — so we don't pay
    # to explain cards nobody opens. Set >0 to eagerly explain that many cards per render instead.
    recommend_explanation_budget: int = 0

    # Auth (multi-user). SECRET_KEY signs identity-bearing bearer tokens and derives the
    # source-token encryption key; it is required once any account exists. The API is closed by
    # default — every data endpoint needs a valid token. See docs/auth.md.
    secret_key: str | None = None
    auth_token_ttl_seconds: int = 60 * 60 * 24 * 30  # 30 days
    # When true, anyone may self-register a local (email+password) account. Default closed:
    # local accounts are admin-created; Plex sign-in is gated by server membership instead.
    registration_open: bool = False

    # Plex "Sign in with Plex" (PIN auth). The client identifier must be stable across restarts so
    # plex.tv recognises the same client; generated and pinned in config when unset.
    plex_client_identifier: str | None = None
    plex_product_name: str = "Phare"

    @property
    def agent_chat_model(self) -> str:
        """Conversational chat-agent model — the bigger one when set, else the chat model."""
        return self.llm_agent_model or self.llm_chat_model

    @property
    def reasoning_headroom(self) -> int:
        """Extra completion tokens for a reasoning model, or 0 when not configured as one."""
        return self.llm_reasoning_headroom if self.llm_reasoning_model else 0

    @property
    def is_production(self) -> bool:
        """Whether this is a production deployment. Gates heavy/destructive operations (e.g. the
        broad catalog import) so a dev box can't fan out thousands of TMDB requests by accident."""
        return self.environment == "production"

    @property
    def signing_secret(self) -> str | None:
        """Secret that signs tokens and derives the source-token encryption key."""
        return self.secret_key

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
