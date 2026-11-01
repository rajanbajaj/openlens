import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App.tsx';
import { isSearchResponse } from './api/guard.ts';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const EVENTS_ENVELOPE = {
  query: 'jazz festival',
  mode: 'events',
  results: [
    {
      id: 'e1',
      title: 'Harbor Jazz Festival',
      snippet: 'Three days by the bay',
      original_score: 0.9,
      final_score: 0.95,
      url: 'https://example.com/e1',
      provider: 'mock',
      reasons: ['matches query'],
      warnings: [],
      metadata: { location: 'Lisbon', published: '2026-11-15' },
    },
  ],
  total: 1,
  applied_filters: [],
  diagnostics: {
    fallback_used: false,
    provider_latencies_ms: { mock: 12.5 },
    plugin_executions: [],
    errors: [],
  },
};

function mockFetchOnce(payload: unknown) {
  const stub = vi.fn().mockResolvedValue({
    ok: true,
    json: () => Promise.resolve(payload),
  });
  vi.stubGlobal('fetch', stub);
  return stub;
}

describe('App events mode', () => {
  it('parses an events envelope through the shared success guard', () => {
    // The guard validates mode as an open string, so a new mode needs
    // no guard change to degrade gracefully.
    expect(isSearchResponse(EVENTS_ENVELOPE)).toBe(true);
  });

  it('sends mode "events" and renders the events vertical line', async () => {
    const fetchStub = mockFetchOnce(EVENTS_ENVELOPE);
    render(<App />);

    fireEvent.change(screen.getByLabelText(/search query/i), {
      target: { value: 'jazz festival' },
    });
    fireEvent.change(screen.getByLabelText(/search mode/i), { target: { value: 'events' } });
    fireEvent.click(screen.getByRole('button', { name: /^search$/i }));

    await waitFor(() => {
      expect(screen.getByText('Harbor Jazz Festival')).toBeInTheDocument();
    });

    // The mode travels in the existing request body field; changing the
    // selector re-uses the same page-1 re-search path as other modes.
    const [, init] = fetchStub.mock.calls[0] as [string, RequestInit];
    expect((JSON.parse(init.body as string) as Record<string, unknown>).mode).toBe('events');

    const vertical = screen.getByTestId('result-vertical-events');
    expect(vertical).toHaveTextContent('Lisbon');
    expect(vertical).toHaveTextContent('2026-11-15');
  });
});
