"""In-memory provider fakes for tests. The engine depends only on the Protocols."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterable, Sequence
from datetime import datetime

from phare.auth.provider import AuthChallenge, AuthPollResult, AuthStatus, ResolvedIdentity
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


class FakeAuthProvider:
    """AuthProvider returning a canned challenge then a preset poll result. Keeps tests off Plex.

    Defaults to immediately authorizing a fixed identity; set ``status`` to ``pending``/``expired``
    to exercise those branches. Records ``poll`` calls so retry/timing can be asserted.
    """

    name = "fake-auth"

    def __init__(
        self,
        identity: ResolvedIdentity,
        *,
        status: AuthStatus = AuthStatus.authorized,
        auth_url: str = "https://example.test/auth",
    ) -> None:
        self.identity = identity
        self.status = status
        self.auth_url = auth_url
        self.poll_calls = 0

    def start(self) -> AuthChallenge:
        return AuthChallenge(challenge_id="fake-pin", auth_url=self.auth_url)

    def poll(self, challenge_id: str) -> AuthPollResult:
        self.poll_calls += 1
        if self.status is AuthStatus.authorized:
            return AuthPollResult(status=AuthStatus.authorized, identity=self.identity)
        return AuthPollResult(status=self.status)


class FakeLLMProvider:
    """Deterministic LLMProvider: stable pseudo-embeddings + a canned completion.

    ``complete_delay`` sleeps that many seconds per ``complete`` call — used by tests to make a
    fan-out of LLM calls *observably* slow without a real provider (keep it tiny). Every call is
    recorded in ``prompts``, so call-count budgets can be asserted deterministically.
    """

    name = "fake-llm"

    def __init__(
        self, dim: int = 1536, completion: str = "{}", *, complete_delay: float = 0.0
    ) -> None:
        self.dim = dim
        self.completion = completion
        self.complete_delay = complete_delay
        self.embed_calls = 0
        # Records each embed() call's input texts, so retrieval tests can assert a chat mood
        # actually reached the embedding query (review A4).
        self.embed_inputs: list[list[str]] = []
        self.prompts: list[str] = []
        # Records the ``max_tokens`` passed to each complete/stream call, so cost-cap tests can
        # assert the call sites actually bound the response length.
        self.max_tokens: list[int | None] = []
        # Records the ``temperature`` passed to each ``complete`` call, so determinism tests can
        # assert the mechanical-JSON call sites pin it to 0.
        self.temperatures: list[float | None] = []

    def complete(
        self, prompt: str, *, max_tokens: int | None = None, temperature: float | None = None
    ) -> str:
        if self.complete_delay:
            time.sleep(self.complete_delay)
        self.prompts.append(prompt)
        self.max_tokens.append(max_tokens)
        self.temperatures.append(temperature)
        return self.completion

    def stream(self, prompt: str, *, max_tokens: int | None = None) -> Iterable[str]:
        """Yield the canned completion word-by-word, to exercise streaming assembly in tests."""
        self.prompts.append(prompt)
        self.max_tokens.append(max_tokens)
        for i, word in enumerate(self.completion.split(" ")):
            yield word if i == 0 else f" {word}"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.embed_calls += 1
        self.embed_inputs.append(list(texts))
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        return [digest[i % len(digest)] / 255.0 for i in range(self.dim)]
