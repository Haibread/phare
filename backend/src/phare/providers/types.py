"""Provider boundary types and interfaces.

Everything external (sources, metadata, LLM, actions) sits behind these Protocols so the
engine depends only on interfaces and providers can be faked in tests. The DTOs here are
internal value objects parsed at the provider edge — they never cross the HTTP wire.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from phare.db.models import EventType, TitleKind


class RawMediaType(enum.StrEnum):
    """Granularity of a raw event as reported by a source."""

    movie = "movie"
    show = "show"
    episode = "episode"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class RawEvent(_Frozen):
    """A single event pulled from a source, before canonical resolution.

    For episodes, the external ids identify the *show*; ``season_number`` and
    ``episode_number`` locate the episode within it.
    """

    source: str
    media_type: RawMediaType
    type: EventType
    tmdb_id: int | None = None
    imdb_id: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    rating: float | None = None
    occurred_at: datetime | None = None
    value_text: str | None = None
    external_ref: str | None = None


class TitleMetadata(_Frozen):
    """Canonical title metadata resolved from a metadata provider."""

    kind: TitleKind
    tmdb_id: int | None = None
    imdb_id: str | None = None
    title: str
    year: int | None = None
    runtime_minutes: int | None = None
    overview: str | None = None
    poster_path: str | None = None
    genres: list[str] = []
    keywords: list[str] = []
    popularity: float | None = None


class ExternalMatch(_Frozen):
    """Result of mapping a secondary id (e.g. IMDb) to a TMDB id + kind."""

    tmdb_id: int
    kind: TitleKind


@runtime_checkable
class MetadataProvider(Protocol):
    """Resolves canonical title metadata and external ids (e.g. TMDB)."""

    name: str

    def get_title(self, tmdb_id: int, kind: TitleKind) -> TitleMetadata | None: ...

    def find_by_imdb(self, imdb_id: str) -> ExternalMatch | None: ...


@runtime_checkable
class SourceProvider(Protocol):
    """Pulls a user's watch/rating history as a normalized event stream (e.g. Trakt)."""

    name: str

    def pull(self, since: datetime | None = None) -> Iterable[RawEvent]: ...


@runtime_checkable
class LLMProvider(Protocol):
    """Chat completion + embeddings (OpenAI-compatible). Used from M2 onward."""

    def complete(self, prompt: str) -> str: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def stream_text(llm: LLMProvider, prompt: str) -> Iterator[str]:
    """Yield reply chunks from a provider that implements ``stream``; otherwise one ``complete``
    call as a single chunk. Lets streaming callers degrade gracefully on providers without it."""
    streamer = getattr(llm, "stream", None)
    if callable(streamer):
        yield from streamer(prompt)
    else:
        yield llm.complete(prompt)


class MediaAvailability(enum.StrEnum):
    """How a title sits in the user's library, via a request/availability provider (Seerr)."""

    available = "available"  # in the library, ready to watch
    queued = "queued"  # requested / processing, not yet available
    requestable = "requestable"  # not in the library; can be requested
    unknown = "unknown"  # provider couldn't determine (or no tmdb id)


@runtime_checkable
class ActionProvider(Protocol):
    """Acquire/watch hand-off (Seerr). Optional, used from M6."""

    name: str

    def availability(self, tmdb_id: int, kind: TitleKind) -> MediaAvailability: ...

    def request_title(self, tmdb_id: int, kind: TitleKind) -> bool: ...
