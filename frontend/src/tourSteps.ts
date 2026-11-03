export interface TourStep {
  target: string;
  title: string;
  body: string;
  /**
   * Gate step (target ''): shown centered with no highlight until
   * `untilTarget` mounts, then the tour advances on its own. Lets the
   * tour pause for a view change (search results, chat tab) instead of
   * silently skipping every step that needs it.
   */
  untilTarget?: string;
}

export const TOUR_STEPS: TourStep[] = [
  {
    target: 'search-hero',
    title: 'Your index, your query',
    body: 'Type a query and hit Enter — results come from an index this project crawled itself. No external search API.',
  },
  {
    target: '',
    untilTarget: 'results-rail',
    title: 'Your turn: run a search',
    body: 'Type "dating advice" and hit Enter. The tour picks up on the results page by itself — every stop from here needs real results.',
  },
  {
    target: 'search-header',
    title: 'Same box, sticky',
    body: 'After you search, the query box docks here so you can refine without losing your place.',
  },
  {
    target: 'results-rail',
    title: 'Results with receipts',
    body: 'Snippets, citations, and why-ranked reasons — every hit explains itself.',
  },
  {
    target: 'result-group-toggle',
    title: 'Grouped by source',
    body: 'Multiple hits from one domain collapse into a group. Expand it to see each URL instead of a wall of sameness.',
  },
  {
    target: 'result-confidence',
    title: 'Confidence, not just rank',
    body: 'Each hit carries a confidence signal, so you can tell solid matches from guesses at a glance.',
  },
  {
    target: 'result-breakdown',
    title: 'The score, opened up',
    body: 'Relevance, authority, trust, freshness — every additive scoring signal is listed. Nothing hides in a black box.',
  },
  {
    target: 'result-reasons',
    title: 'Why this ranked',
    body: 'Plain-language reasons per hit — and warnings when something smells off. Filters fail closed, never silently.',
  },
  {
    target: 'story-group',
    title: 'One story, many angles',
    body: 'Same-story coverage clusters with per-source stances and missing-side notes. Popularity never decides contested topics.',
  },
  {
    target: 'diagnostics-plugins',
    title: 'Every plugin, on the record',
    body: 'Which pipeline plugins ran, their stage and latency. Mandatory stages fail closed; optional ones degrade with a note. Dev-mode builds show this rail.',
  },
  {
    target: 'diagnostics-query',
    title: 'The path your query took',
    body: 'Parsing, rewrite, retrieval, scoring — the exact pipeline stages, so a weird result is always explainable.',
  },
  {
    target: 'result-report-incorrect',
    title: 'Your vote counts',
    body: 'Flag wrong or suspicious hits. Feedback is anonymous — no profile, no tracking — and it trains future ranking.',
  },
  {
    target: 'ai-summary',
    title: 'The short version',
    body: 'A model-backed summary with citations on top of the results. No model configured? It degrades to silence, never fiction.',
  },
  {
    target: 'nav-chat',
    title: 'Ask mode',
    body: 'Switch to AI chat for cited answers that admit gaps instead of hallucinating.',
  },
  {
    target: '',
    untilTarget: 'chat-view',
    title: 'Your turn: open AI chat',
    body: 'Click the AI chat tab. The tour continues there on its own — the trace breakdown needs a live answer.',
  },
  {
    target: 'chat-view',
    title: 'Cited answers, live',
    body: 'Ask in plain language — the model searches your index through budgeted tools and answers with citations.',
  },
  {
    target: 'chat-citations',
    title: 'Every claim has a receipt',
    body: 'Citation chips link back to the source result in a new tab.',
  },
  {
    target: 'chat-trace',
    title: 'Watch it think',
    body: 'The trace breaks the answer open: the rewritten query, model calls, filters, fallbacks — step by step, nothing hidden.',
  },
  {
    target: 'sidebar-mount',
    title: 'Steer it',
    body: 'Modes, filters, provider prefs, and your saved library live here.',
  },
];
