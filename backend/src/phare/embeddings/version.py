"""Single source of truth for embedding space tags + provider selection + coverage cutover.

Every embedding vector is stamped with a *space tag* (the ``TitleEmbedding`` composite PK's
``model_version``). Retrieval MUST query the same tag it embedded with, or it queries an empty /
mismatched space. Centralising the choice here guarantees the embed step and the query agree.

Two concerns live here now, because the embed *document* changed (round 9):

- **Write tag** (:func:`embedding_write_version`): where new vectors go. It folds the document
  version into the model tag — ``f"{model}#d{DOC_VERSION}"`` — so a document change re-embeds the
  catalog exactly like a model change does (the version-tag mismatch is the re-embed trigger).
  Historical vectors carry the bare model tag; that bare tag *is* document v1.
- **Read / served tag** (:func:`active_embedding_version`): what retrieval queries. It stays on
  the previous (bare, doc-v1) tag until the new space is built out — coverage
  (embedded titles / total titles) >= :data:`CUTOVER_COVERAGE` — then flips to the write tag. So a
  deploy that changes the document serves the old space with identical behaviour while the new one
  builds in the background, and cuts over automatically once it's ready. The read path must resolve
  this ONCE per request and thread the single resolved tag through centroid + candidate query +
  any live query embedding, so a request never mixes spaces.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from phare.core.config import Settings
from phare.db.models import Title, TitleEmbedding
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider
from phare.providers.llm import OpenAILLMProvider
from phare.providers.types import LLMProvider

logger = logging.getLogger(__name__)

# Embed-document version. Bump this when :func:`phare.embeddings.service.build_embedding_text`
# changes in a way that alters vector meaning (new fields, reordering), so the catalog re-embeds.
# v1 = title/year/genres/keywords/overview (the bare tag, no suffix). v2 adds directors, top cast,
# and original language. Encoded into the write tag as ``#d<version>``.
DOC_VERSION = 2

# Fraction of the catalog that must carry a vector in the new space before reads flip to it. High
# enough that the flip is invisible (near-full coverage — a handful of un-embedded titles just miss
# from rows until the backfill finishes), low enough that a few permanently-unembeddable rows (a
# title the embed API keeps rejecting) can't wedge the cutover forever. Not a config knob: it's an
# internal safety margin, not an operator dial.
CUTOVER_COVERAGE = 0.95


def _base_model(settings: Settings) -> str:
    """The bare model tag (no document suffix) — the real model when keyed, else the local one."""
    if settings.llm_api_key:
        return settings.llm_embedding_model
    return LOCAL_MODEL_VERSION


def embedding_model_version(settings: Settings) -> str:
    """The **bare** space tag (no document suffix). This is document v1 — the historical space and
    the read fallback while the current-document space builds. Kept under the name callers already
    use for the doc-v1 tag; the write target is :func:`embedding_write_version`."""
    return _base_model(settings)


def embedding_write_version(settings: Settings) -> str:
    """The tag new vectors are stamped with: the model tag folded with the document version. A
    document bump changes this string, which re-triggers embedding exactly like a model change."""
    return f"{_base_model(settings)}#d{DOC_VERSION}"


def _coverage(session: Session, version: str) -> tuple[int, int]:
    """(embedded titles in ``version``, total titles). ``(0, 0)`` on an empty catalog."""
    total = session.scalar(select(func.count()).select_from(Title)) or 0
    embedded = (
        session.scalar(
            select(func.count())
            .select_from(TitleEmbedding)
            .where(TitleEmbedding.model_version == version)
        )
        or 0
    )
    return embedded, total


def active_embedding_version(session: Session, settings: Settings) -> str:
    """The tag retrieval should query *this request*.

    Returns the write (current-document) tag once its coverage reaches :data:`CUTOVER_COVERAGE`,
    otherwise the previous bare (doc-v1) tag — so reads serve the old, fully-built space while the
    new one backfills, and flip automatically when it's ready. Logs the flip (``space_cutover``)
    once it happens, so the cutover is visible in ops logs. One cheap COUNT; the caller resolves it
    once per request (see :func:`phare.api.deps.get_embedder`) and threads it everywhere.

    On an empty catalog (total == 0) there is nothing to serve either way, so it returns the write
    tag — the write side is authoritative and reads over an empty catalog return nothing regardless.
    """
    write = embedding_write_version(settings)
    previous = embedding_model_version(settings)
    if write == previous:  # defensive: only if DOC_VERSION were folded away — never in practice
        return write
    embedded, total = _coverage(session, write)
    if total == 0:
        return write
    coverage = embedded / total
    if coverage >= CUTOVER_COVERAGE:
        logger.info(
            "embeddings.space_cutover",
            extra={
                "served_version": write,
                "superseded_version": previous,
                "coverage": round(coverage, 4),
                "embedded": embedded,
                "total": total,
            },
        )
        return write
    return previous


def get_embedding_provider(settings: Settings) -> LLMProvider:
    """Embedding provider matching :func:`embedding_model_version` — OpenAI if keyed, else local.

    The provider is document-version-agnostic: it embeds whatever text it is given. The document
    version only affects the *stored-vector tag* (write side) and *which tag retrieval queries*
    (read side); a live free-text query embedding uses this provider directly and its result is
    only ever compared against vectors of the request's resolved tag.
    """
    if settings.llm_api_key:
        return OpenAILLMProvider(
            api_key=settings.llm_api_key,
            chat_model=settings.llm_chat_model,
            embedding_model=settings.llm_embedding_model,
            base_url=settings.llm_base_url,
            monthly_token_budget=settings.llm_monthly_token_budget,
            embedding_dimensions=(
                settings.llm_embedding_dim if settings.llm_embedding_request_dimensions else None
            ),
            timeout=settings.llm_timeout_seconds,
        )
    return LocalHashEmbeddingProvider(dim=settings.llm_embedding_dim)
