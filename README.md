# Phare

Open-source, self-hosted, AI-assisted movie & TV recommendations. Learns your taste from what
you've watched and rated (synced from Trakt first), then recommends through **UI rows** and a
**chat agent**.

Core bet: **the LLM steers, embeddings rank** — classic content-based retrieval does the
recommending; the LLM turns fuzzy human signal into an editable taste profile that steers it and
writes the explanations.

Design lives in [`docs/`](docs/); how to build lives in [`CLAUDE.md`](CLAUDE.md).
