# Chat agent

Real-time conversational surface for "what do I want to watch **right now**" — the ephemeral
mood/intent layer over the same engine the rows use.

**One agent with good tools. Not multi-agent.** (A swarm is over-engineering here.)

- **Tools** (thin wrappers over the engine — same code the rows use): get/update taste profile,
  get history (this user only), search catalog, vector-similar, `recommend(intent_filters)`, and
  optionally check-availability / request-title.
- **Memory:** long-term = the taste profile (in-chat corrections flow into its user-overrides);
  short-term = the conversation. No separate memory system.
- **Job = intent parsing.** Turn "something funny but not dumb, under 2h, watchable tonight" into
  structured filters and call `recommend`. The agent does **not** rank titles itself.

## Guardrails (hard)

1. **Spoiler safety** — describe appeal (tone/themes/fit), never plot of unwatched content.
2. **Privacy** — never reveal or reference another user; no "because Bob liked this".
3. **No hallucinated titles** — only recommend what tools return; if nothing, say so.
4. **Confidence** — distinguish confident picks from guesses; admit thin data.
