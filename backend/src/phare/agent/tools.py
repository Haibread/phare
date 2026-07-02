"""Agent tools: the thin, deterministic wrappers the planner drives.

"LLM steers, embeddings rank, never picks from memory" — the planner (LLM) chooses which tools to
run and with what arguments; these functions *execute* against the same engine the rows use and
write structured signals. Every write returns an :class:`AgentAction` carrying an undo token, so a
mis-parse never silently poisons taste (auto-write + undo).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from phare.agent import commitments as commitments_store
from phare.agent import memory as memory_store
from phare.agent.schema import AgentAction, AgentPlan, ChatIntent
from phare.catalog.service import CatalogMetadataSource, search_titles
from phare.core.fallback import record_fallback
from phare.db.models import (
    CommitmentStatus,
    EventType,
    MemoryKind,
    RecommendationLog,
    Title,
    WatchCommitment,
    WatchEvent,
)
from phare.llm_json import as_str_list
from phare.recommend.schema import Recommendation
from phare.recommend.service import RecommendationService
from phare.taste.service import maybe_refresh_taste

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
    # TMDB provider: resolves named titles (search) and backfills candidate runtimes (get_title).
    metadata: CatalogMetadataSource | None


@dataclass
class ExecutionResult:
    """What running a plan produced: items to show, writes made, and outcome notes for the reply."""

    items: list[Recommendation] = field(default_factory=list)
    actions: list[AgentAction] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # e.g. "couldn't find 'Zxqyt'"
    # Tool calls that raised — surfaced to the composer so the reply is honest about actions that
    # didn't happen, instead of confirming a write that never landed (review B3).
    failed_calls: list[str] = field(default_factory=list)
    intent: ChatIntent = field(default_factory=ChatIntent)
    taste_dirty: bool = False
    # Items re-surfaced for an explanation ("why these?") are already-logged picks, not new ones —
    # don't log them again.
    suppress_logging: bool = False


def _resolve_title(ctx: ToolContext, query: str) -> Title | None:
    """Best match for a free-text title, upserting from TMDB when configured. None if unfound."""
    matches = search_titles(ctx.session, query, ctx.metadata, limit=1)
    return matches[0] if matches else None


# The top title must be at least this many times more voted than the runner-up to resolve a signal
# without asking — otherwise two plausible titles of similar popularity are treated as ambiguous.
_AMBIGUITY_RATIO = 3.0


def _recent_chat_titles(ctx: ToolContext, *, limit: int = 30) -> list[Title]:
    """Titles the agent recently put in front of the user in chat (most recent first, deduped).

    This is the conversation context: when the user reacts to "Get Out", the one the agent just
    recommended is almost certainly the one they mean — a far stronger signal than a TMDB search."""
    rows = ctx.session.execute(
        select(Title)
        .join(RecommendationLog, RecommendationLog.title_id == Title.id)
        .where(
            RecommendationLog.profile_id == ctx.profile_id,
            RecommendationLog.source == "chat",
        )
        .order_by(RecommendationLog.shown_at.desc())
        .limit(limit)
    ).scalars()
    seen: set[uuid.UUID] = set()
    unique: list[Title] = []
    for title in rows:
        if title.id not in seen:
            seen.add(title.id)
            unique.append(title)
    return unique


def _resolve_signal_title(ctx: ToolContext, query: str) -> tuple[Title | None, bool]:
    """Resolve a title the user is logging a signal for, and say whether we're sure enough to write.

    Order (review H3b — writing a signal on the wrong title corrupts the taste memory silently):
    1. a title recently recommended in *this conversation* whose name matches exactly (best signal);
    2. otherwise search, prefer an exact-name match, then most-voted — but if the top two are of
       similar popularity (no clear winner), return ``confident=False`` so the caller asks rather
       than guesses.
    """
    normalized = query.strip().lower()
    if not normalized:
        return None, False
    for title in _recent_chat_titles(ctx):
        if title.title.strip().lower() == normalized:
            return title, True
    candidates = search_titles(ctx.session, query, ctx.metadata, limit=6)
    if not candidates:
        return None, False
    exact = [c for c in candidates if c.title.strip().lower() == normalized]
    if len(exact) == 1:
        return exact[0], True
    ranked = sorted(exact or candidates, key=lambda c: c.vote_count or 0, reverse=True)
    top = ranked[0]
    if len(ranked) == 1:
        return top, True
    runner_up_votes = ranked[1].vote_count or 0
    confident = (top.vote_count or 0) >= max(1, runner_up_votes) * _AMBIGUITY_RATIO
    return top, confident


def _title_label(title: Title) -> str:
    """Human label naming exactly what was (or would be) written — 'Get Out (2017)'."""
    return f"{title.title} ({title.year})" if title.year else title.title


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
    # ChatIntent's validators coerce the LLM's loose arg shapes (a bare-string genre, a mood handed
    # back as a list, "90 minutes") so a mis-typed field can't raise and sink the recommend turn.
    intent = ChatIntent(
        max_runtime=args.get("max_runtime"),
        include_genres=args.get("include_genres"),
        exclude_genres=args.get("exclude_genres"),
        mood=args.get("mood"),
        kind=args.get("kind"),
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
        # Ephemeral mood ("slow-burn", "something cosy") nudges retrieval toward it (review A4).
        mood=intent.mood,
        # Only on a runtime-capped turn: backfill the pool's missing runtimes from TMDB so the cap
        # can actually filter (the broad import omits runtime). No-op without a metadata provider.
        runtime_source=ctx.metadata if intent.max_runtime is not None else None,
    )


def tool_log_signal(ctx: ToolContext, args: dict, result: ExecutionResult) -> None:
    query = str(args.get("title", "")).strip()
    signal = str(args.get("signal", "watched")).lower()
    text = args.get("note")
    rating = args.get("rating")
    title, confident = _resolve_signal_title(ctx, query) if query else (None, False)
    if title is None:
        result.notes.append(f"couldn't find a title matching '{query}'")
        return
    if not confident:
        # Ambiguous — do NOT write (a wrong signal silently poisons taste, review H3b). Ask which
        # one; the user's next message names it, and the recommended-context path then resolves it.
        result.notes.append(
            f"more than one title matches '{query}' — did you mean {_title_label(title)}? "
            "I haven't logged it yet"
        )
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
            # Name the resolved title + year so the reply confirms exactly what was written (H3b).
            summary=f"logged {_title_label(title)} as {signal}",
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
        for value in as_str_list(incoming):
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


def tool_explain_picks(ctx: ToolContext, args: dict, result: ExecutionResult) -> None:
    """Re-surface the profile's most recent chat slate so the reply can explain *why* those titles
    were picked. The picks come from the recommendation log — what was actually shown — never from
    the model's memory. Sets no new recommendations, so the slate isn't re-logged."""
    latest = ctx.session.scalar(
        select(func.max(RecommendationLog.shown_at)).where(
            RecommendationLog.profile_id == ctx.profile_id,
            RecommendationLog.source == "chat",
        )
    )
    if latest is None:
        result.notes.append("there are no earlier picks to explain yet")
        return
    rows = ctx.session.execute(
        select(Title, RecommendationLog.score, RecommendationLog.is_swing)
        .join(RecommendationLog, RecommendationLog.title_id == Title.id)
        .where(
            RecommendationLog.profile_id == ctx.profile_id,
            RecommendationLog.source == "chat",
            RecommendationLog.shown_at == latest,
        )
        .order_by(RecommendationLog.rank)
    ).all()
    result.items = [
        Recommendation(
            title_id=title.id,
            title=title.title,
            kind=title.kind.value,
            year=title.year,
            genres=list(title.genres),
            score=float(score) if score is not None else 0.0,
            is_swing=is_swing,
            poster_path=title.poster_path,
        )
        for title, score, is_swing in rows
    ]
    result.suppress_logging = True


_TOOLS = {
    "recommend": tool_recommend,
    "explain_picks": tool_explain_picks,
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
            record_fallback("agent_tool", "unknown_tool", tool=call.tool)
            continue
        try:
            handler(ctx, call.args, result)
        except Exception:  # noqa: BLE001 - one bad tool call must not sink the whole turn
            record_fallback("agent_tool", "exception", tool=call.tool)
            result.failed_calls.append(f"the {call.tool} action didn't complete")
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
        if kind == "taste":
            changed = _undo_taste(session, profile_id, ref) or changed
            continue
        parsed = _as_uuid(ref)
        if parsed is None:  # malformed token segment — skip it cleanly, never target the zero UUID
            record_fallback("agent_undo", "malformed_ref", ref=ref)
            continue
        if kind == "event":
            event = session.get(WatchEvent, parsed)
            if event is not None and event.profile_id == profile_id:
                session.delete(event)
                changed = True
        elif kind == "commitment":
            commitment = session.get(WatchCommitment, parsed)
            if commitment is not None and commitment.profile_id == profile_id:
                session.delete(commitment)
                changed = True
        elif kind == "commitment-status":
            commitment = session.get(WatchCommitment, parsed)
            if commitment is not None and commitment.profile_id == profile_id:
                commitment.status = CommitmentStatus.pending
                commitment.resolved_at = None
                changed = True
        elif kind == "note":
            note = memory_store.get_note(session, parsed)
            if note is not None and note.profile_id == profile_id:
                session.delete(note)
                changed = True
    return changed


def _as_uuid(ref: str) -> uuid.UUID | None:
    """Parse a token segment into a UUID, or ``None`` if malformed — never the zero UUID, which
    would silently target a (non-existent) row instead of failing (review G4)."""
    try:
        return uuid.UUID(ref)
    except ValueError:
        return None


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
