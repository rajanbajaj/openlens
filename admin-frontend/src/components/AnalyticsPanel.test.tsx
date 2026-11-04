import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AnalyticsPanel } from './AnalyticsPanel.tsx';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const COUNTS = {
  days: 30,
  total: 4,
  by_name: { 'crawl.fetched': 2, 'crawl.failed': 1, 'crawl.skipped': 1 },
};

const SERIES = {
  name: 'crawl.page',
  bucket: 'day',
  days: 30,
  points: [
    { t: '2026-10-27', count: 1 },
    { t: '2026-10-28', count: 3 },
  ],
};

const TOP_OUTCOME = {
  name: 'crawl.page',
  prop: 'outcome',
  days: 30,
  total: 4,
  top: [
    { value: 'fetched', count: 2 },
    { value: 'failed', count: 1 },
    { value: 'skipped', count: 1 },
  ],
};

const TOP_REASON = {
  name: 'crawl.page',
  prop: 'reason',
  days: 30,
  total: 1,
  top: [{ value: 'http 404', count: 1 }],
};

const ALL_DOMAINS = Array.from({ length: 25 }, (_, i) => ({
  value: `example${i}.com`,
  count: 25 - i,
}));

function stubBackend(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation((url: string) => {
      const text = String(url);
      const params = new URL(text, 'http://localhost').searchParams;
      let payload: unknown = COUNTS;
      if (text.includes('/api/v1/admin/analytics/series')) {
        payload = SERIES;
      } else if (text.includes('/api/v1/admin/analytics/top')) {
        payload = TOP_OUTCOME;
        if (text.includes('prop=reason')) payload = TOP_REASON;
        if (text.includes('prop=domain') && text.includes('filter_value=failed')) {
          payload = {
            name: 'crawl.page',
            prop: 'domain',
            days: 30,
            total: 1,
            top: [{ value: 'example0.com', count: 1 }],
          };
        } else if (text.includes('prop=domain')) {
          const match = (params.get('match') ?? '').toLowerCase();
          const offset = Number(params.get('offset') ?? 0);
          const limit = Number(params.get('limit') ?? 10);
          const rows = ALL_DOMAINS.filter((row) => row.value.includes(match));
          payload = {
            name: 'crawl.page',
            prop: 'domain',
            days: 30,
            total: rows.length,
            top: rows.slice(offset, offset + limit),
          };
        }
      }
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
  it('renders key events, throughput, and breakdowns', async () => {
    stubBackend();
    render(<AnalyticsPanel adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('analytics-domains-list')).toHaveTextContent('example0.com');
    });
    expect(screen.getByTestId('analytics-failing-domains-list')).toHaveTextContent('example0.com');
    expect(screen.getByTestId('analytics-reasons-list')).toHaveTextContent('http 404');
  });

  it('sends an absolute range instead of the trailing window', async () => {
    const seen: string[] = [];
    const impl = vi.fn().mockImplementation((url: string) => {
      seen.push(String(url));
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers(),
        text: () => Promise.resolve(JSON.stringify(COUNTS)),
        json: () => Promise.resolve(COUNTS),
      });
    });
    vi.stubGlobal('fetch', impl);
    render(<AnalyticsPanel adminToken="" />);
    fireEvent.change(screen.getByTestId('analytics-from'), {
      target: { value: '2026-10-01' },
    });
    fireEvent.change(screen.getByTestId('analytics-to'), {
      target: { value: '2026-10-10' },
    });
    await waitFor(() => {
      expect(screen.getByTestId('analytics-range-note')).toBeInTheDocument();
    });
    const ranged = seen.filter(
      (url) => url.includes('start=2026-10-01') && url.includes('end=2026-10-10'),
    );
    expect(ranged.length).toBeGreaterThan(0);
    expect(screen.getAllByText(/2026-10-01 → 2026-10-10/).length).toBeGreaterThan(1);
  });

  it('searches domains by substring and clears back', async () => {
    stubBackend();
    render(<AnalyticsPanel adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('analytics-domains')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByTestId('analytics-domain'), {
      target: { value: 'example1' },
    });
    fireEvent.click(screen.getByTestId('analytics-domain-search'));
    await waitFor(() => {
      expect(screen.getByTestId('analytics-domains')).toHaveTextContent('matching “example1”');
    });
    // example1, example10-example19 match; example0 filtered out.
    expect(screen.getByTestId('analytics-domains-list')).not.toHaveTextContent('example0.com');
    expect(screen.getByTestId('analytics-domains-list')).toHaveTextContent('example1.com');
    fireEvent.click(screen.getByTestId('analytics-domain-clear'));
    await waitFor(() => {
      expect(screen.getByTestId('analytics-domains-list')).toHaveTextContent('example0.com');
    });
  });

  it('pages through top domains with next and jump', async () => {
    stubBackend();
    render(<AnalyticsPanel adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('analytics-domains-list')).toHaveTextContent('example0.com');
    });
    expect(screen.getByTestId('analytics-domains-page')).toHaveTextContent('Page 1');
    expect(screen.getByTestId('analytics-domains-next')).not.toBeDisabled();
    fireEvent.click(screen.getByTestId('analytics-domains-next'));
    await waitFor(() => {
      expect(screen.getByTestId('analytics-domains-list')).toHaveTextContent('example10.com');
    });
    expect(screen.getByTestId('analytics-domains-page')).toHaveTextContent('Page 2');
    fireEvent.change(screen.getByTestId('analytics-domains-jump'), {
      target: { value: '3' },
    });
    fireEvent.click(screen.getByTestId('analytics-domains-go'));
    await waitFor(() => {
      expect(screen.getByTestId('analytics-domains-list')).toHaveTextContent('example20.com');
    });
    expect(screen.getByTestId('analytics-domains-page')).toHaveTextContent('Page 3');
  });
});
