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

function stubBackend(counts: unknown = COUNTS, feedback: unknown = FEEDBACK): void {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation((url: string) =>
      Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers(),
        text: () =>
          Promise.resolve(
            JSON.stringify(
              String(url).includes('/api/v1/admin/feedback/summary') ? feedback : counts,
            ),
          ),
        json: () =>
          Promise.resolve(
            String(url).includes('/api/v1/admin/feedback/summary') ? feedback : counts,
          ),
      }),
    ),
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

  it('sends the admin token on both reads', async () => {
    stubBackend();
    const seen: string[] = [];
    const impl = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const headers = new Headers(init?.headers);
      seen.push(headers.get('X-Admin-Token') ?? '');
      const payload = String(url).includes('/api/v1/admin/feedback/summary') ? FEEDBACK : COUNTS;
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
    await waitFor(() => expect(impl).toHaveBeenCalledTimes(2));
    expect(seen).toEqual(['s3cret', 's3cret']);
  });

  it('degrades to empty panels when the store is unavailable', async () => {
    stubBackend(null, null);
    render(<AnalyticsPanel adminToken="" />);
    expect(await screen.findByTestId('analytics-empty')).toBeInTheDocument();
    expect(screen.getByTestId('feedback-empty')).toBeInTheDocument();
  });
});
