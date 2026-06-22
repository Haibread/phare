"""Agent tools: the thin, deterministic wrappers the planner drives.

"LLM steers, embeddings rank, never picks from memory" — the planner (LLM) chooses which tools to
run and with what arguments; these functions *execute* against the same engine the rows use and
write structured signals. Every write returns an :class:`AgentAction` carrying an undo token, so a
mis-parse never silently poisons taste (auto-write + undo).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from phare.agent import commitments as commitments_store
from phare.agent import memory as memory_store
from phare.agent.schema import AgentAction, AgentPlan, ChatIntent
from phare.catalog.service import CatalogSearchSource, search_titles
from phare.db.models import (
    CommitmentStatus,
    EventType,
    MemoryKind,
    Title,
    WatchCommitment,
    WatchEvent,
)
from phare.recommend.schema import Recommendation
from phare.recommend.service import RecommendationService
from phare.taste.service import maybe_refresh_taste

logger = logging.getLogger(__name__)

# A qualitative signal maps to one or more canonical events. "loved" stacks watched + liked so it
# both excludes the title and pulls the centroid hard; ratings come in via the numeric `rating` arg.
_SIGNAL_EVENTS: dict[str, list[EventType]] = {
    "watched": [EventType.watched],
    "loved": [EventType.watched, EventType.liked],
    "liked": [EventType.liked],
    "disliked": [EventType.disliked],
    "abandoned": [EventType.abandoned],
    "rewatched": [EventType.rewatched],
    "watchlist": [EventType.watchlisted],
}


@dataclass
class ToolContext:
    """Everything the tools need for one chat turn (request-scoped)."""

    session: Session
    profile_id: uuid.UUID
    recommender: RecommendationService
    now: datetime
    metadata: CatalogSearchSource | None  # TMDB provider for live title resolution


@dataclass
class ExecutionResult:
    """What running a plan produced: items to show, writes made, and outcome notes for the reply."""

    items: list[Recommendation] = field(default_factory=list)
    actions: list[AgentAction] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # e.g. "couldn't find 'Zxqyt'"
    intent: ChatIntent = field(default_factory=ChatIntent)
    taste_dirty: bool = False


def _resolve_title(ctx: ToolContext, query: str) -> Title | None:
    """Best match for a free-text title, upserting from TMDB when configured. None if unfound."""
    matches = search_titles(ctx.session, query, ctx.metadata, limit=1)
    return matches[0] if matches else None


def _write_event(
    ctx: ToolContext,
    title_id: uuid.UUID,
    event_type: EventType,
    *,
    text: str | None,
    rating: float | None,
) -> uuid.UUID:
    event = WatchEvent(
        profile_id=ctx.profile_id,
        title_id=title_id,
        type=event_type,
        rating=rating,
        value_text=text,
        occurred_at=ctx.now,
        source="chat",
        external_ref=f"chat:{uuid.uuid4()}",
    )
    ctx.session.add(event)
    ctx.session.flush()
    return event.id


def tool_recommend(ctx: ToolContext, args: dict, result: ExecutionResult) -> None:
    intent = ChatIntent(
        max_runtime=args.get("max_runtime"),
        include_genres=[str(g) for g in args.get("include_genres", [])],
        exclude_genres=[str(g) for g in args.get("exclude_genres", [])],
        mood=args.get("mood"),
        rewatch=bool(args.get("rewatch", False)),
    )
    result.intent = intent
    from phare.agent.service import intent_filter  # local import avoids a cycle

    result.items = ctx.recommender.recommend(
        ctx.profile_id,
        extra_hard_avoids=intent.exclude_genres,
        candidate_filter=intent_filter(intent),
        rewatch=intent.rewatch,
        vote_mix=True,  # chat slates mix well-known/lesser-known/low-vote, ordered by votes
        explain_with_llm=False,  # the composed reply frames the picks; skip per-item LLM calls
    )


def tool_log_signal(ctx: ToolContext, args: dict, result: ExecutionResult) -> None:
    query = str(args.get("title", "")).strip()
    signal = str(args.get("signal", "watched")).lower()
    text = args.get("note")
    rating = args.get("rating")
    title = _resolve_title(ctx, query) if query else None
    if title is None:
        result.notes.append(f"couldn't find a title matching '{query}'")
        return
    event_types = _SIGNAL_EVENTS.get(signal, [EventType.watched])
    created: list[uuid.UUID] = []
    for i, event_type in enumerate(event_types):
        created.append(
            _write_event(
                ctx,
                title.id,
                event_type,
                text=text if i == 0 else None,
                rating=float(rating)
                if rating is not None and event_type is EventType.watched
                else None,
            )
        )
    result.taste_dirty = True
    result.actions.append(
        AgentAction(
            kind="logged_signal",
            summary=f"logged {title.title} as {signal}",
            undo_token=",".join(f"event:{cid}" for cid in created),
        )
    )


def tool_set_commitment(ctx: ToolContext, args: dict, result: ExecutionResult) -> None:
    query = str(args.get("title", "")).strip()
    title = _resolve_title(ctx, query) if query else None
    if title is None:
        result.notes.append(f"couldn't find a title matching '{query}'")
        return
    commitment = commitments_store.create_commitment(
        ctx.session, ctx.profile_id, title.id, note=args.get("note")
    )
    result.actions.append(
        AgentAction(
            kind="commitment",
            summary=f"added {title.title} to your watch plans",
            undo_token=f"commitment:{commitment.id}",
        )
    )


def _pending_for_title(ctx: ToolContext, title_id: uuid.UUID) -> WatchCommitment | None:
    for commitment in commitments_store.pending_commitments(ctx.session, ctx.profile_id):
        if commitment.title_id == title_id:
            return commitment
    return None


def tool_resolve_commitment(ctx: ToolContext, args: dict, result: ExecutionResult) -> None:
    query = str(args.get("title", "")).strip()
    outcome = str(args.get("outcome", "watched")).lower()
    reaction = args.get("reaction")
    title = _resolve_title(ctx, query) if query else None
    commitment = _pending_for_title(ctx, title.id) if title is not None else None
    if title is None or commitment is None:
        result.notes.append(f"no pending plan found for '{query}'")
        return
    tokens = [f"commitment-status:{commitment.id}"]  # undo restores it to pending
    if outcome == "dropped":
        commitments_store.resolve_commitment(
            commitment, status=CommitmentStatus.dropped, resolved_at=ctx.now
        )
        summary = f"marked {title.title} as dropped"
    else:
        commitments_store.resolve_commitment(
            commitment, status=CommitmentStatus.watched, resolved_at=ctx.now
        )
        signal = "loved" if reaction and _is_positive(str(reaction)) else "watched"
        event_id = _write_event(
            ctx, title.id, EventType.watched, text=str(reaction) if reaction else None, rating=None
        )
        tokens.append(f"event:{event_id}")
        if signal == "loved":
            like_id = _write_event(ctx, title.id, EventType.liked, text=None, rating=None)
            tokens.append(f"event:{like_id}")
        result.taste_dirty = True
        summary = f"marked {title.title} watched" + (f" — {reaction}" if reaction else "")
    result.actions.append(
        AgentAction(kind="resolved", summary=summary, undo_token=",".join(tokens))
    )


_POSITIVE_WORDS = ("love", "great", "good", "enjoy", "amazing", "brilliant", "liked")


def _is_positive(reaction: str) -> bool:
    low = reaction.lower()
    return any(word in low for word in _POSITIVE_WORDS)


def tool_remember(ctx: ToolContext, args: dict, result: ExecutionResult) -> None:
    text = str(args.get("text", "")).strip()
    if not text:
        return
    try:
        kind = MemoryKind(str(args.get("kind", "fact")))
    except ValueError:
        kind = MemoryKind.fact
    expires_at: datetime | None = None
    if (days := args.get("expires_days")) is not None:
        expires_at = ctx.now + timedelta(days=int(days))
    note = memory_store.create_note(
        ctx.session, ctx.profile_id, text, kind=kind, expires_at=expires_at, source="chat"
    )
    result.actions.append(
        AgentAction(kind="memory", summary=f"remembered: {text}", undo_token=f"note:{note.id}")
    )


def tool_update_taste(ctx: ToolContext, args: dict, result: ExecutionResult) -> None:
    from sqlalchemy import select

    from phare.db.models import TasteProfile

    taste = ctx.session.scalar(
        select(TasteProfile).where(TasteProfile.profile_id == ctx.profile_id)
    )
    if taste is None:
        taste = TasteProfile(profile_id=ctx.profile_id)
        ctx.session.add(taste)
        ctx.session.flush()
    overrides = dict(taste.user_overrides)
    tokens: list[str] = []
    for key, incoming in (("hard_avoids", args.get("add_avoid")), ("likes", args.get("add_like"))):
        for value in incoming or []:
            current = list(overrides.get(key, []))
            if value not in current:
                current.append(value)
                overrides[key] = current
                tokens.append(f"taste:{key}:{value}")
    if not tokens:
        return
    taste.user_overrides = overrides
    ctx.session.flush()
    added = ", ".join(t.split(":", 2)[2] for t in tokens)
    result.actions.append(
        AgentAction(
            kind="taste",
            summary=f"noted a lasting preference: {added}",
            undo_token=",".join(tokens),
        )
    )


_TOOLS = {
    "recommend": tool_recommend,
    "log_signal": tool_log_signal,
    "set_commitment": tool_set_commitment,
    "resolve_commitment": tool_resolve_commitment,
    "remember": tool_remember,
    "update_taste": tool_update_taste,
}


def execute_plan(ctx: ToolContext, plan: AgentPlan) -> ExecutionResult:
    """Run each tool the planner asked for; refresh taste once if any write touched it."""
    result = ExecutionResult()
    for call in plan.calls:
        handler = _TOOLS.get(call.tool)
        if handler is None:
            logger.warning("agent.unknown_tool", extra={"tool": call.tool})
            continue
        try:
            handler(ctx, call.args, result)
        except Exception:  # noqa: BLE001 - one bad tool call must not sink the whole turn
            logger.warning("agent.tool_failed", extra={"tool": call.tool})
    if result.taste_dirty:
        # Taste extraction is mechanical JSON — use the recommender's (workhorse) model, not the
        # bigger agent model. No-ops when offline. The caller owns the commit.
        maybe_refresh_taste(
            ctx.session, ctx.profile_id, ctx.recommender.chat_llm, ctx.recommender.language
        )
    return result


def undo_action(session: Session, profile_id: uuid.UUID, token: str) -> bool:
    """Reverse a previously-applied agent action by its token. Returns True if anything changed."""
    changed = False
    for part in token.split(","):
        kind, _, ref = part.partition(":")
        if kind == "event":
            event = session.get(WatchEvent, _as_uuid(ref))
            if event is not None and event.profile_id == profile_id:
                session.delete(event)
                changed = True
        elif kind == "commitment":
            commitment = session.get(WatchCommitment, _as_uuid(ref))
            if commitment is not None and commitment.profile_id == profile_id:
                session.delete(commitment)
                changed = True
        elif kind == "commitment-status":
            commitment = session.get(WatchCommitment, _as_uuid(ref))
            if commitment is not None and commitment.profile_id == profile_id:
                commitment.status = CommitmentStatus.pending
                commitment.resolved_at = None
                changed = True
        elif kind == "note":
            note = memory_store.get_note(session, _as_uuid(ref))
            if note is not None and note.profile_id == profile_id:
                session.delete(note)
                changed = True
        elif kind == "taste":
            changed = _undo_taste(session, profile_id, ref) or changed
    return changed


def _as_uuid(ref: str) -> uuid.UUID:
    try:
        return uuid.UUID(ref)
    except ValueError:
        return uuid.UUID(int=0)


def _undo_taste(session: Session, profile_id: uuid.UUID, ref: str) -> bool:
    from sqlalchemy import select

    from phare.db.models import TasteProfile

    key, _, value = ref.partition(":")
    taste = session.scalar(select(TasteProfile).where(TasteProfile.profile_id == profile_id))
    if taste is None:
        return False
    overrides = dict(taste.user_overrides)
    current = list(overrides.get(key, []))
    if value not in current:
        return False
    overrides[key] = [v for v in current if v != value]
    taste.user_overrides = overrides
    return True
