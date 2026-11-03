import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App.tsx';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

const HEALTH_ENVELOPE = {
  query: 'flu symptoms',
  mode: 'health',
  results: [
    {
      id: 'h1',
      title: 'Flu symptoms and treatment options',
      snippet: 'What to do when flu season hits',
      original_score: 0.9,
      final_score: 0.95,
      url: 'https://www.cdc.gov/flu/symptoms',
      provider: 'mock',
      reasons: ['matches query'],
      warnings: [],
      metadata: { site_category: 'health', location: 'Boston', published: '2026-10-01' },
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
  metadata: {},
};

function mockFetchOnce(payload: unknown) {
  const stub = vi.fn().mockResolvedValue({
    ok: true,
    json: () => Promise.resolve(payload),
  });
  vi.stubGlobal('fetch', stub);
  return stub;
}

describe('App health mode', () => {
  it('sends mode "health" and renders the vertical highlight', async () => {
    const fetchStub = mockFetchOnce(HEALTH_ENVELOPE);
    render(<App />);

    fireEvent.change(screen.getByLabelText(/search query/i), {
      target: { value: 'flu symptoms' },
    });
    fireEvent.change(screen.getByLabelText(/search mode/i), { target: { value: 'health' } });
    fireEvent.click(screen.getByRole('button', { name: /^search$/i }));

    await waitFor(() => {
      expect(screen.getByText('Flu symptoms and treatment options')).toBeInTheDocument();
    });

    // The mode travels in the existing request body field; changing the
    // selector re-uses the same page-1 re-search path as other modes.
    const [, init] = fetchStub.mock.calls[0] as [string, RequestInit];
    expect((JSON.parse(init.body as string) as Record<string, unknown>).mode).toBe('health');

    expect(screen.getByTestId('result-vertical-health')).toHaveTextContent('Boston');
  });
});
