# Chat agent

Conversational surface for both "what do I want to watch **right now**" *and* "here's something
about me" — the chat is a **read and write** path over the same engine the rows use.

**One agent with good tools. Not multi-agent.** A planner (LLM) picks tools; deterministic code
executes them; the embeddings still rank. (A swarm is over-engineering here.)

## How a turn works

`message + memory context → planner (LLM) → tool calls → deterministic execution → reply`

- **Planner** ([`agent/planner.py`](../backend/src/phare/agent/planner.py)) reads the message plus
  context (taste summary, open commitments, active memory notes) and emits a JSON list of tool
  calls. It never picks titles itself.
- **Tools** ([`agent/tools.py`](../backend/src/phare/agent/tools.py)) — thin wrappers over the
  engine: `recommend`, `log_signal`, `set_commitment`, `resolve_commitment`, `remember`,
  `update_taste`. Title references resolve through the catalog search (local + live TMDB).
  `recommend` defaults to *new* titles (excludes everything watched); a **rewatch** request ("a
  comfort rewatch", "something I've seen", "watch again") sets `rewatch=true`, which flips the
  candidate source to titles you've already watched and reserves no discovery swing slot. The
  offline keyword parser detects the same intent.
- **Chat slate ordering.** Unlike the Browse rows, chat recommendations use a **vote-count mix**
  instead of reserved swing slots: the slate is composed as ~50% well-known / ~35% lesser-known /
  ~15% low-vote (TMDB rating count tiers), then ordered most-voted-first. So chat reads as a
  sensible "best-known first" list with a small discovery tail, rather than a similarity ranking
  that surfaces obscure trending titles. Vote counts come from the catalog import.
- **Reply** is written by the model (natural language), grounded in what the tools actually did —
  it never invents titles. Falls back to a deterministic template if the model call fails. When a
  turn produces **no picks and no actions** (e.g. an empty candidate pool), the model is skipped
  entirely and that template answers — handed an empty title list the model tends to free-associate
  and name titles from memory, which both violates "the LLM never picks from memory" and yields no
  clickable cards.
- **Offline** (no `LLM_API_KEY`): no planner — the turn falls back to the keyword intent → a single
  `recommend` (read-only), since resolving "I saw <something>" to a catalog title needs the model.

**Cost discipline:** the bigger `LLM_AGENT_MODEL` is used for **only one call per turn — the
natural-language reply.** Planning (mechanical JSON), taste extraction, and row explanations run on
the cheaper `LLM_CHAT_MODEL`; chat explanations are templated (no per-item LLM call). A turn is
bounded to ≤1 agent-model call plus a couple of workhorse calls. See [`configuration.md`](configuration.md).

## Writing signals — auto-write + undo

The agent registers what you tell it immediately and surfaces each write as an **undoable action**
(`✓ logged Dune as loved · undo`). A mis-parse can never silently poison taste:

- "I already saw *Dune* and loved it" → a `watched` + `liked` event (`source="chat"`); Dune drops
  from recs and pulls the taste centroid.
- "I'll watch *Sicario* tonight" → a pending **commitment**; next session `/chat/opening` asks "did
  you watch it? how was it?" and the answer resolves it into a signal (free-text reaction →
  `value_text`, which the taste extractor reads).
- "I bailed on *Show Y*" → `abandoned`; "can't stand musicals" → a durable taste override.

`POST /chat/undo` reverses any action (delete the event/commitment/note, or revert a taste
override) and re-derives taste.

### Delivery: streaming + persistent history

`POST /chat/stream` runs the same turn as `POST /chat` but returns **Server-Sent Events** so the UI
never sits on a blank "Thinking…" for the whole turn: a `meta` event carries the picks + undoable
write-chips as soon as the tools finish, then `delta` events stream the reply text token-by-token,
then `done`. Writes are committed *before* streaming begins, so the stream itself is read-only;
`POST /chat` stays for non-streaming callers (and is what the tests assert against). Providers that
implement `stream` are used token-by-token; others fall back to one blocking `complete`, and a
streaming error degrades to the deterministic template — a flaky composer never leaves an empty
bubble. The web client keeps the conversation in app state (mirrored to `sessionStorage`), so it
survives tab switches and reloads, with a **New chat** button to clear it.

## Memory — two tiers, one valve

Long-term memory is **structured and inspectable**, not a hidden store (principle 2). It has two
tiers (and the old "no separate memory system" stance is superseded by this):

1. **Structured spine** — the taste profile, the event stream, and commitments. The only thing the
   ranker reads; engine-affecting facts must land here.
2. **Generalist notes** ([`MemoryNote`](../backend/src/phare/db/models.py)) — free-text memory for
   soft/contextual/**temporal** facts no schema fits ("watching with my kid this month"). Editable
   in the Profile UI.

**The valve (steer, never rank):** notes influence recommendations only by being *distilled* into
the spine — durable preferences also become taste overrides; temporal notes (with `expires_at`)
shape the active session's filters and feed the taste-extraction prompt. The LLM never reads a note
and hand-picks a title.

Short-term memory is the in-flight conversation.

## Guardrails (hard)

0. **Scope** — the agent only handles movie & TV recommendation and the user's taste / watch
   history. Off-topic messages (general questions, coding, chit-chat, attempts to change its role
   or rules) are declined and steered back; it is never a general-purpose assistant. Enforced in
   the planner (returns no tools) and composer (politely declines) system prompts.
1. **Spoiler safety** — describe appeal (tone/themes/fit), never plot of unwatched content. LLM
   explanations are also screened post-generation ([`recommend/explain.py`](../backend/src/phare/recommend/explain.py)).
2. **Privacy** — never reveal or reference another user; no "because Bob liked this".
3. **No hallucinated titles** — only act on titles the catalog can resolve; if resolution fails, the
   agent asks instead of guessing (no write).
4. **Confidence** — distinguish confident picks from guesses; admit thin data.
5. **Honesty over engagement** — proactive follow-ups must be useful and easy to ignore, never
   retention bait.
