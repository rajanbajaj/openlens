import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AnalyticsPanel } from './AnalyticsPanel.tsx';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const COUNTS = {
  days: 30,
  total: 9,
  by_name: { 'crawl.fetched': 2, 'crawl.failed': 1, 'crawl.skipped': 1, 'search.served': 5 },
};

const TOP_MODES = {
  name: 'search.served',
  prop: 'mode',
  days: 30,
  total: 5,
  top: [
    { value: 'default', count: 3 },
    { value: 'news', count: 2 },
  ],
};

const TOP_RESULT_COUNTS = {
  name: 'search.served',
  prop: 'result_count',
  days: 30,
  total: 5,
  top: [
    { value: '8', count: 3 },
    { value: '0', count: 2 },
  ],
};

const PREV_TOP_RESULT_COUNTS = {
  name: 'search.served',
  prop: 'result_count',
  days: 30,
  total: 4,
  top: [
    { value: '8', count: 3 },
    { value: '0', count: 1 },
  ],
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

const PREV_COUNTS = {
  days: 30,
  total: 6,
  by_name: { 'crawl.fetched': 1, 'crawl.failed': 1, 'crawl.skipped': 0, 'search.served': 4 },
};

const FAILED_SERIES = {
  name: 'crawl.page',
  bucket: 'day',
  days: 30,
  points: [{ t: '2026-10-27', count: 1 }],
};

const EMPTY_SERIES = {
  name: 'crawl.page',
  bucket: 'day',
  days: 30,
  points: [],
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
      if (text.includes('/api/v1/admin/analytics/counts') && text.includes('start=')) {
        payload = PREV_COUNTS;
      } else if (text.includes('/api/v1/admin/analytics/series')) {
        payload = SERIES;
        if (text.includes('filter_value=failed')) payload = FAILED_SERIES;
        if (text.includes('filter_value=skipped')) payload = EMPTY_SERIES;
      } else if (text.includes('/api/v1/admin/analytics/top')) {
        payload = TOP_OUTCOME;
        if (text.includes('prop=reason')) payload = TOP_REASON;
        if (text.includes('name=search.served') && text.includes('prop=mode')) {
          payload = TOP_MODES;
        }
        if (text.includes('name=search.served') && text.includes('prop=result_count')) {
          payload = TOP_RESULT_COUNTS;
          if (text.includes('start=')) payload = PREV_TOP_RESULT_COUNTS;
        }
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

  it('renders KPI cards with prior-period deltas', async () => {
    stubBackend();
    render(<AnalyticsPanel adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('analytics-kpi-fetched')).toHaveTextContent('+100.0%');
    });
    // Current window: 2 fetched of 4 attempted = 50.0% success.
    expect(screen.getByTestId('analytics-kpi-fetched')).toHaveTextContent('2');
    expect(screen.getByTestId('analytics-kpi-success')).toHaveTextContent('50.0%');
    // Prior window: 1 fetched of 2 attempted, so fetched doubles.
    expect(screen.getByTestId('analytics-kpi-fetched')).toHaveTextContent(
      '+100.0% vs prior 30 days',
    );
    // Success rate is flat at 50 points vs 50 points.
    expect(screen.getByTestId('analytics-kpi-success')).toHaveTextContent(
      '±0.0 pp vs prior 30 days',
    );
    // Skipped goes 1 vs 0 history: new.
    expect(screen.getByTestId('analytics-kpi-skipped')).toHaveTextContent('new vs prior 30 days');
  });

  it('stacks fetched, failed, and skipped throughput per bucket', async () => {
    stubBackend();
    render(<AnalyticsPanel adminToken="" />);
    await waitFor(() => {
      expect(screen.getByRole('img', { name: /stacked pages per bucket/i })).toBeInTheDocument();
    });
    const chart = screen.getByRole('img', { name: /stacked pages per bucket/i });
    expect(chart).toHaveAttribute(
      'aria-label',
      'Stacked pages per bucket: 4 fetched, 1 failed, 0 skipped',
    );
    const panel = screen.getByTestId('analytics-throughput');
    expect(panel).toHaveTextContent('Fetched 4');
    expect(panel).toHaveTextContent('Failed 1');
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

  it('renders skeletons instead of zeros while loading', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(() => new Promise(() => {})),
    );
    render(<AnalyticsPanel adminToken="" />);
    const card = screen.getByTestId('analytics-kpi-fetched');
    expect(card.querySelector('[data-testid="skeleton"]')).toBeInTheDocument();
    expect(card).toHaveTextContent('—');
    expect(card).not.toHaveTextContent('0');
  });

  it('keeps per-card errors local instead of a global banner', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        const text = String(url);
        let payload: unknown = COUNTS;
        let status = 200;
        if (text.includes('/api/v1/admin/analytics/top') && text.includes('prop=domain')) {
          status = 500;
          payload = { error: { code: 'unavailable', message: 'store down' } };
        } else if (text.includes('/api/v1/admin/analytics/series')) {
          payload = SERIES;
        } else if (text.includes('/api/v1/admin/analytics/top')) {
          payload = TOP_OUTCOME;
        }
        return Promise.resolve({
          ok: status >= 200 && status < 300,
          status,
          headers: new Headers(),
          text: () => Promise.resolve(JSON.stringify(payload)),
          json: () => Promise.resolve(payload),
        });
      }),
    );
    render(<AnalyticsPanel adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('analytics-reasons-list')).toBeInTheDocument();
    });
    expect(screen.getByText('Top domains unavailable.')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('toggles the throughput table fallback', async () => {
    stubBackend();
    render(<AnalyticsPanel adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('analytics-throughput')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('analytics-view-table'));
    expect(screen.getByTestId('analytics-view-table')).toHaveAttribute('aria-pressed', 'true');
    const table = await screen.findByTestId('analytics-throughput-table');
    expect(table).toHaveTextContent('2026-10-27');
    fireEvent.click(screen.getByTestId('analytics-view-chart'));
    expect(screen.getByTestId('analytics-throughput')).toBeInTheDocument();
  });

  it('clears the window when a range is picked and vice versa', async () => {
    stubBackend();
    render(<AnalyticsPanel adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('analytics-domains-list')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByTestId('analytics-from'), {
      target: { value: '2026-10-01' },
    });
    fireEvent.change(screen.getByTestId('analytics-to'), {
      target: { value: '2026-10-10' },
    });
    await waitFor(() => {
      expect(screen.getByTestId('analytics-range-note')).toBeInTheDocument();
    });
    expect(screen.getByTestId('analytics-window')).toHaveValue('custom');
    fireEvent.change(screen.getByTestId('analytics-window'), { target: { value: '7' } });
    await waitFor(() => {
      expect(screen.queryByTestId('analytics-range-note')).not.toBeInTheDocument();
    });
  });

  it('flags an inverted range inline', async () => {
    stubBackend();
    render(<AnalyticsPanel adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('analytics-domains-list')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByTestId('analytics-from'), {
      target: { value: '2026-10-10' },
    });
    fireEvent.change(screen.getByTestId('analytics-to'), {
      target: { value: '2026-10-01' },
    });
    expect(await screen.findByTestId('analytics-range')).toHaveTextContent(
      'Start must be before end.',
    );
  });

  it('sorts ranked lists without refetching', async () => {
    stubBackend();
    render(<AnalyticsPanel adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('analytics-domains-list')).toHaveTextContent('example0.com');
    });
    fireEvent.change(screen.getByTestId('analytics-domains-sort'), {
      target: { value: 'value-asc' },
    });
    await waitFor(() => {
      const items = screen
        .getByTestId('analytics-domains-list')
        .textContent?.match(/example\d+\.com/g);
      expect(items?.[0]).toBe('example0.com');
      expect(items?.[1]).toBe('example1.com');
    });
  });

  it('renders search traffic: served KPI, no-result KPI, and by-mode list', async () => {
    stubBackend();
    render(<AnalyticsPanel adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('analytics-search-modes-list')).toHaveTextContent('default');
    });
    // 5 served now vs 4 prior => +25.0%.
    expect(screen.getByTestId('analytics-kpi-searches')).toHaveTextContent('5');
    expect(screen.getByTestId('analytics-kpi-searches')).toHaveTextContent('+25.0%');
    // 2 no-result now vs 1 prior => +100.0%, flagged bad (up is bad).
    expect(screen.getByTestId('analytics-kpi-noresults')).toHaveTextContent('2');
    expect(screen.getByTestId('analytics-kpi-noresults')).toHaveTextContent('+100.0%');
    expect(screen.getByTestId('analytics-search-modes-list')).toHaveTextContent('news');
    fireEvent.change(screen.getByTestId('analytics-search-modes-sort'), {
      target: { value: 'value-asc' },
    });
    await waitFor(() => {
      const items = screen
        .getByTestId('analytics-search-modes-list')
        .textContent?.match(/default|news/g);
      expect(items?.[0]).toBe('default');
      expect(items?.[1]).toBe('news');
    });
  });
});
