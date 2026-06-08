"""In-memory provider fakes for tests. The engine depends only on the Protocols."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from datetime import datetime

from phare.db.models import TitleKind
from phare.providers.types import ExternalMatch, RawEvent, TitleMetadata


class FakeMetadataProvider:
    """MetadataProvider returning canned metadata; records lookups for assertions."""

    name = "fake-metadata"

    def __init__(
        self,
        titles: dict[tuple[int, TitleKind], TitleMetadata] | None = None,
        imdb: dict[str, ExternalMatch] | None = None,
    ) -> None:
        self.titles = titles or {}
        self.imdb = imdb or {}
        self.calls: list[tuple[int, TitleKind]] = []

    def get_title(self, tmdb_id: int, kind: TitleKind) -> TitleMetadata | None:
        self.calls.append((tmdb_id, kind))
        return self.titles.get((tmdb_id, kind))

    def find_by_imdb(self, imdb_id: str) -> ExternalMatch | None:
        return self.imdb.get(imdb_id)


class FakeSourceProvider:
    """SourceProvider yielding a fixed list of events."""

    name = "fake-source"

    def __init__(self, events: list[RawEvent]) -> None:
        self.events = events

    def pull(self, since: datetime | None = None) -> Iterable[RawEvent]:
        for event in self.events:
            if since is not None and event.occurred_at and event.occurred_at < since:
                continue
            yield event


class FakeLLMProvider:
    """Deterministic LLMProvider: stable pseudo-embeddings + a canned completion."""

    name = "fake-llm"

    def __init__(self, dim: int = 1536, completion: str = "{}") -> None:
        self.dim = dim
        self.completion = completion
        self.embed_calls = 0
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.completion

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.embed_calls += 1
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        return [digest[i % len(digest)] / 255.0 for i in range(self.dim)]
