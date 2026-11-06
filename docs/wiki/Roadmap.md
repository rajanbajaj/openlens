# Roadmap (product)

Committed direction (see `docs/releases/` plans for detail):

- **Result quality**: ranking authority/performance packs, coherent
  snippets and summaries, smarter suggestions.
- **Coverage**: discovery feeds plus recrawl loops so news and blogs
  stay fresh without full recrawls.
- **Neutrality**: story clustering across outlets, stance coverage,
  missing-perspective surfacing, fake/misleading detection.
- **Commerce & local**: price comparison, sponsored separation,
  travel/hotel depth within the no-maps constraint.
- **Trust & safety**: site reputation, impersonation/copy detection,
  report-driven delisting review.
- **Scale**: Kafka/worker autoscaling signals, PgBouncer-class
  connection discipline, multi-node frontier.
- **Queue reliability**: lossless seed intake — an overflow holding pen
  that parks over-cap seeds and trickles them in as the queue drains
  (no more skipped pastes), priority-ordered admission; discovery
  overflow keeps shedding to skipped.

Out of scope stays out: external search APIs, maps, user accounts
and tracking profiles.
