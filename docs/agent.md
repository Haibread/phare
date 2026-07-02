# Chat agent

Conversational surface for both "what do I want to watch **right now**" *and* "here's something
about me" — the chat is a **read and write** path over the same engine the rows use.

**One agent with good tools. Not multi-agent.** A planner (LLM) picks tools; deterministic code
executes them; the embeddings still rank. (A swarm is over-engineering here.)

## How a turn works

`message + recent conversation + memory context → planner (LLM) → tool calls → deterministic
execution → reply`

- **Planner** ([`agent/planner.py`](../backend/src/phare/agent/planner.py)) reads the message plus
  the **recent conversation** (see below) and context (taste summary, open commitments, active
  memory notes) and emits a JSON list of tool calls. It never picks titles itself. The conversation
  is context for resolving references in the *latest* message ("even shorter", "more like that") —
  scope and intent are still judged on the latest message, so an off-topic turn after an on-topic
  chat is still declined for free.
- **Tools** ([`agent/tools.py`](../backend/src/phare/agent/tools.py)) — thin wrappers over the
  engine: `recommend`, `explain_picks`, `log_signal`, `set_commitment`, `resolve_commitment`,
  `remember`, `update_taste`. Title references resolve through the catalog search (local + live
  TMDB). For a **signal write** (`log_signal` — "I loved Get Out") resolution is disambiguated,
  because writing onto the wrong title silently corrupts taste: it prefers a title just recommended
  in this conversation, else the most-voted exact-name match, and if two titles are equally
  plausible it **writes nothing and asks which one** rather than guessing (review H3b). The confirmed
  action names the resolved title + year so the reply states exactly what was recorded.
  `explain_picks` ("why these?") re-surfaces the **last logged chat slate** from the
  recommendation log — never from the model's memory — so the reply explains the titles actually
  shown. It emits **no new strip** (they're already on screen from the previous turn — review B2)
  and feeds the composer the leading picks with their per-item fit reasons, so the answer covers the
  first few honestly instead of re-posting a wall of posters. It sets no new picks, so nothing's
  re-logged.
  `recommend` defaults to *new* titles (excludes everything watched); a **rewatch** request ("a
  comfort rewatch", "something I've seen", "watch again") sets `rewatch=true`, which flips the
  candidate source to titles you've already watched and reserves no discovery swing slot. The
  offline keyword parser detects the same intent.
- **Clarify (ask vs. guess).** Instead of always dumping a slate, the planner may emit a single
  `clarify {question, suggestions?}` when a request is genuinely too vague to pick well *and* one
  answer would change the picks (a bare "what should I watch" with no genre/mood/length/vibe and no
  taste yet). The bar is high — any named genre/mood/length, or an existing taste profile, and it
  just recommends. It *may* ask again across turns when the user is genuinely still working it out
  (each question must cover something new and make progress), but stops the moment they signal "I
  don't know / you pick / surprise me" — and the guaranteed escape hatch (below), not a turn cap, is
  what prevents a loop: the user can bail in one tap on any clarify turn. The question is the
  **workhorse planner's own text**, so a clarify turn spends **no agent-model call**. The reply
  carries tappable `suggestions`, and the service **guarantees an escape hatch** ("Surprise me") so
  the user never has to answer to get a result — guardrail 5's "ask only to cut real uncertainty,
  always leave an out", made deterministic rather than trusted to the model.
- **Chat slate ordering.** Unlike the Browse rows, chat recommendations use a **vote-count mix**
  instead of reserved swing slots: the slate is composed as ~50% well-known / ~35% lesser-known /
  ~15% low-vote (TMDB rating count tiers) — the mix decides *which* titles make the slate — then
  ordered by **score (relevance), not by votes**, so the most relevant pick leads while the slate
  still spans a range of known-ness. (Ordering by votes buried the best match at the bottom of the
  strip; that was review finding A1.) Vote counts come from the catalog import.
- **Recent conversation** — the planner and the composer both receive the last few turns of the
  chat so a turn isn't a cold start: references resolve ("even shorter" knows what it's shortening)
  and the reply builds on what was said instead of re-pitching the same titles. It's **short-term,
  client-held memory**: the web client replays it on each request (it already keeps the transcript
  in app state / `sessionStorage`); the server persists no transcript. It's bounded before it ever
  reaches a prompt — only the last few messages, each truncated ([`agent/schema.py`](../backend/src/phare/agent/schema.py)) —
  so a long chat can't balloon the prompt or the token bill (no extra LLM *calls*, just bounded
  input). **Offline** (no LLM) stays single-message: there's no model to resolve a reference against.
- **Active filters (refinement)** — the planner also gets the *structured* filters in effect from the
  previous turn (genres, runtime cap, mood, movie/show kind — replayed by the client as
  `activeIntent`), not just the
  prose. The runtime ceiling lives only in the intent and never reaches the transcript, so without
  this "even shorter" can't tighten below the prior cap — the planner re-emits the full refined
  recommend args, carrying over what still applies and adjusting only what the message changed.
- **Mood biases retrieval** — an ephemeral mood ("slow-burn", "something cosy") isn't just a label:
  when a real embedder is configured, the mood text is embedded once and blended (gently) into the
  taste centroid, so retrieval leans toward it while taste still leads ("LLM steers, embeddings
  rank" — review A4). Skipped on the offline hash embedder, where the vectors carry no meaning.
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
override) and re-derives taste. A malformed undo token segment is skipped cleanly — it never
resolves to the zero UUID and targets a random row (review G4).

If a tool **fails** (raises, or can't resolve a title), the failure is collected and fed to the
composer, which is instructed to say so honestly and never confirm an action that didn't happen — a
false "noted!" is worse than an admitted miss (review B3).

### Delivery: streaming + persistent history

`POST /chat/stream` runs the same turn as `POST /chat` but returns **Server-Sent Events** so the UI
never sits on a blank "Thinking…" for the whole turn: an instant **localised** `status` (a keyword
acknowledgement — "Finding something funny…" / "Je cherche…") while the planner runs, a `meta` event
carrying the picks + undoable write-chips as soon as the tools finish, another `status` while the
reply is composed, then `delta` events stream the reply text token-by-token, then `done`. Writes are committed *before* streaming begins, so the stream itself is read-only;
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

Short-term memory is the in-flight conversation — now actually fed to the model: the last few turns
ride along on each request (client-held, bounded), so the planner and composer build on the thread.
See **Recent conversation** under [How a turn works](#how-a-turn-works).

## Guardrails (hard)

0. **Scope** — the agent only handles movie & TV recommendation and the user's taste / watch
   history. Off-topic messages (general questions, coding, chit-chat, attempts to change its role
   or rules) are declined and steered back; it is never a general-purpose assistant. Enforced in
   the planner (returns no tools) and composer (politely declines) system prompts.
1. **Spoiler safety** — describe appeal (tone/themes/fit), never plot of unwatched content. Enforced
   on **both** the "why this" blurbs and the **chat reply**: an explicit prompt instruction plus a
   marker post-check (EN + FR) that drops a reply naming a plot reveal for the safe template
   ([`recommend/explain.py`](../backend/src/phare/recommend/explain.py)). The streaming reply relies
   on the prompt (it emits before the full text exists); the blocking path also post-checks.
2. **Privacy** — never reveal or reference another user; no "because Bob liked this".
3. **No hallucinated titles** — only act on titles the catalog can resolve; if resolution fails, the
   agent asks instead of guessing (no write).
4. **Confidence** — distinguish confident picks from guesses; admit thin data.
5. **Honesty over engagement.** The chat *should* feel like a real conversation — warm, curious,
   building on what was just said. But every conversational move has to earn its place by helping the
   user decide what to watch, never by manufacturing turns or time-on-app:
   - **Ask only to cut real uncertainty.** A clarifying question is welcome when the request is
     genuinely under-specified *and* the answer would change the picks. When there's already enough
     to recommend, recommend — don't stall to ask. Default to acting.
   - **Always leave an escape hatch.** Any question or nudge is one word to skip ("…or just say
     *surprise me*"); the user never has to answer to get a result.
   - **Warmth, not bait.** An inviting line or proactive follow-up is fine when it's useful and easy
     to ignore — never there to pull the user back or keep them typing. Never optimize for session
     length, turns per session, or return rate.
