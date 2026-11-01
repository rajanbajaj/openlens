# Glossary (product)

- **Mode** — intent-scoped search (default, news, shopping, travel,
  child-safe, agent). Pre-filters inside retrieval.
- **Frontier** — the URL queue: seeds, discovered links, priorities,
  leases, checkpoints. Managed from the admin app.
- **Claim** — one factual span extracted from a page (title +
  snippet sentences that assert something checkable).
- **Story** — same-event articles grouped by headline overlap
  within a time window.
- **Perspective view** — one story's claim plus per-source stances,
  coverage-gap notes, and trust caveats. Never affects ranking.
- **Stance** — supports / refutes / neutral / unrated: a source's
  position on the claim (keyword heuristic unless a model judged it).
- **Trace** — the agent's visible work log: rewrite, tool calls,
  fallbacks, model-call count.
- **Fallback** — degraded-but-honest output (extractive summary,
  standard results + notice) when AI or a provider fails.
- **Lease** — a worker's time-boxed claim on a queued URL; expiry
  returns the URL to the queue without loss or duplication.
