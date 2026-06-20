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
- **Offline** (no `LLM_API_KEY`): no planner — the turn falls back to the keyword intent → a single
  `recommend` (read-only), since resolving "I saw <something>" to a catalog title needs the model.

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

1. **Spoiler safety** — describe appeal (tone/themes/fit), never plot of unwatched content. LLM
   explanations are also screened post-generation ([`recommend/explain.py`](../backend/src/phare/recommend/explain.py)).
2. **Privacy** — never reveal or reference another user; no "because Bob liked this".
3. **No hallucinated titles** — only act on titles the catalog can resolve; if resolution fails, the
   agent asks instead of guessing (no write).
4. **Confidence** — distinguish confident picks from guesses; admit thin data.
5. **Honesty over engagement** — proactive follow-ups must be useful and easy to ignore, never
   retention bait.
