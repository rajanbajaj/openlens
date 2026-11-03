export interface TourStep {
  target: string;
  title: string;
  body: string;
}

export const TOUR_STEPS: TourStep[] = [
  {
    target: 'search-hero',
    title: 'Your index, your query',
    body: 'Type a query and hit Enter — results come from an index this project crawled itself. No external search API.',
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
    target: 'nav-chat',
    title: 'Ask mode',
    body: 'Switch to AI chat for cited answers that admit gaps instead of hallucinating.',
  },
  {
    target: 'sidebar-mount',
    title: 'Steer it',
    body: 'Modes, filters, provider prefs, and your saved library live here.',
  },
];
