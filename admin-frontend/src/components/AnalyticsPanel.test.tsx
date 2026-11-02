import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AnalyticsPanel } from './AnalyticsPanel.tsx';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const COUNTS = {
  days: 30,
  total: 3,
  by_name: { 'search.served': 2, 'search.feedback': 1 },
};

const FEEDBACK = {
  total: 1,
  by_action: { helpful: 1 },
  by_reason: { unspecified: 1 },
  top_docs: [
    {
      doc_id: 'https://example.com/a',
      total: 1,
      helpful: 1,
      not_helpful: 0,
      reports: 0,
      top_reason: null,
    },
  ],
};

const TOP_OUTCOME = {
  name: 'crawl.page',
  prop: 'outcome',
  days: 30,
  total: 4,
  top: [
    { value: 'skipped', count: 2 },
    { value: 'failed', count: 1 },
    { value: 'fetched', count: 1 },
  ],
};

const TOP_REASON = {
  name: 'crawl.page',
  prop: 'reason',
  days: 30,
  total: 3,
  top: [
    { value: 'denied by deny_domains', count: 2 },
    { value: 'fetch timeout', count: 1 },
  ],
};

const TOP_DOMAIN = {
  name: 'crawl.page',
  prop: 'domain',
  days: 30,
  total: 4,
  top: [
    { value: 'example.com', count: 3 },
    { value: 'other.example', count: 1 },
  ],
};

const TOP_FAILING_DOMAIN = {
  name: 'crawl.page',
  prop: 'domain',
  days: 30,
  total: 1,
  top: [{ value: 'example.com', count: 1 }],
};

const SERIES = {
  name: 'crawl.page',
  bucket: 'day',
  days: 30,
  points: [
    { t: '2026-10-26', count: 0 },
    { t: '2026-10-27', count: 4 },
    { t: '2026-10-28', count: 8 },
  ],
};

function stubBackend(
  counts: unknown = COUNTS,
  feedback: unknown = FEEDBACK,
  topOutcome: unknown = TOP_OUTCOME,
  topReason: unknown = TOP_REASON,
  topDomain: unknown = TOP_DOMAIN,
  topFailingDomain: unknown = TOP_FAILING_DOMAIN,
  series: unknown = SERIES,
): void {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation((url: string) => {
      const text = String(url);
      const payload = text.includes('/api/v1/admin/feedback/summary')
        ? feedback
        : text.includes('/api/v1/admin/analytics/series')
          ? series
          : text.includes('/api/v1/admin/analytics/top') && text.includes('filter_value=failed')
            ? topFailingDomain
            : text.includes('/api/v1/admin/analytics/top') && text.includes('prop=domain')
              ? topDomain
              : text.includes('/api/v1/admin/analytics/top') && text.includes('prop=reason')
                ? topReason
                : text.includes('/api/v1/admin/analytics/top')
                  ? topOutcome
                  : counts;
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers(),
        text: () => Promise.resolve(JSON.stringify(payload)),
        json: () => Promise.resolve(payload),
      });
    }),
  );
}

describe('AnalyticsPanel', () => {
  it('renders durable event counts and the feedback summary', async () => {
    stubBackend();
    render(<AnalyticsPanel adminToken="" />);
    expect(await screen.findByTestId('analytics-total')).toHaveTextContent('3 events');
    expect(screen.getByTestId('analytics-by-name')).toHaveTextContent('search.served');
    expect(screen.getByTestId('feedback-total')).toHaveTextContent('1 signals');
    expect(screen.getByTestId('feedback-top-docs')).toHaveTextContent('https://example.com/a');
  });

  it('sends the admin token on every read', async () => {
    stubBackend();
    const seen: string[] = [];
    const impl = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const headers = new Headers(init?.headers);
      seen.push(headers.get('X-Admin-Token') ?? '');
      const text = String(url);
      const payload = text.includes('/api/v1/admin/feedback/summary')
        ? FEEDBACK
        : text.includes('/api/v1/admin/analytics/top')
          ? TOP_OUTCOME
          : COUNTS;
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers(),
        text: () => Promise.resolve(JSON.stringify(payload)),
        json: () => Promise.resolve(payload),
      });
    });
    vi.stubGlobal('fetch', impl);
    render(<AnalyticsPanel adminToken="s3cret" />);
    await screen.findByTestId('analytics-total');
    await screen.findByTestId('feedback-total');
    await screen.findByTestId('crawler-summary');
    await waitFor(() => expect(impl).toHaveBeenCalledTimes(7));
    expect(seen).toEqual(['s3cret', 's3cret', 's3cret', 's3cret', 's3cret', 's3cret', 's3cret']);
  });

  it('renders crawler outcomes and top reasons', async () => {
    stubBackend();
    render(<AnalyticsPanel adminToken="" />);
    expect(await screen.findByTestId('crawler-by-outcome')).toHaveTextContent('skipped');
    expect(screen.getByTestId('crawler-by-reason')).toHaveTextContent('denied by deny_domains (2)');
    expect(screen.getByTestId('crawler-success-rate')).toHaveTextContent('25% fetched (1/4)');
    expect(screen.getByTestId('crawler-throughput')).toHaveTextContent('peak 8/day');
    expect(screen.getByTestId('crawler-by-domain')).toHaveTextContent('example.com');
    expect(screen.getByTestId('crawler-failing-domains')).toHaveTextContent('example.com (1)');
  });

  it('degrades to empty panels when the store is unavailable', async () => {
    stubBackend(null, null, null, null, null, null, null);
    render(<AnalyticsPanel adminToken="" />);
    expect(await screen.findByTestId('analytics-empty')).toBeInTheDocument();
    expect(screen.getByTestId('feedback-empty')).toBeInTheDocument();
    expect(screen.getByTestId('crawler-empty')).toBeInTheDocument();
  });
});
