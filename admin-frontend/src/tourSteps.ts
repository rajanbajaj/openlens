export interface TourStep {
  target: string;
  title: string;
  body: string;
}

export const TOUR_STEPS: TourStep[] = [
  {
    target: 'frontier-checkpoint',
    title: 'Crawl state at a glance',
    body: 'Phase, pages attempted vs indexed — is the fleet running or paused?',
  },
  {
    target: 'frontier-workers',
    title: 'The fleet',
    body: 'Every live crawler, what it claimed, pages and tokens so far.',
  },
  {
    target: 'frontier-kafka-lag',
    title: 'The pulse',
    body: 'Kafka lag plus throughput — watch the backlog drain in real time.',
  },
  {
    target: 'frontier-failed',
    title: 'Failures explain themselves',
    body: 'Every failed URL with attempts and reason — no silent drops.',
  },
  {
    target: 'frontier-auto-triage',
    title: 'One-click triage',
    body: '429s wait and retry, robots and 404s skip, timeouts join the backoff pool.',
  },
  {
    target: 'frontier-failed-domains',
    title: 'Steer by domain',
    body: 'Requeue, flush, skip, or block whole domains; drill into their URLs.',
  },
  {
    target: 'admin-tab-analytics',
    title: 'Then measure',
    body: 'Crawl telemetry lives under the Analytics tab.',
  },
];
