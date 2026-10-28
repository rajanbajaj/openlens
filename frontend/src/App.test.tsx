import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App.tsx';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const SHOPPING_ENVELOPE = {
  query: 'wireless headphones',
  mode: 'shopping',
  results: [
    {
      id: 'r1',
      title: 'Acme Headphones',
      snippet: 'Great sound, noise cancelling',
      original_score: 0.9,
      final_score: 0.95,
      url: 'https://example.com/h',
      provider: 'mock',
      reasons: ['matches query'],
      warnings: ['demo warning'],
      metadata: {},
    },
  ],
  total: 1,
  applied_filters: ['exclude_sponsored'],
  diagnostics: {
    fallback_used: false,
    provider_latencies_ms: { mock: 12.5 },
    plugin_executions: [],
    errors: [],
  },
};

function mockFetchOnce(payload: unknown, ok = true) {
  const stub = vi.fn().mockResolvedValue({
    ok,
    json: () => Promise.resolve(payload),
  });
  vi.stubGlobal('fetch', stub);
  return stub;
}

async function runShoppingSearch() {
  render(<App />);
  fireEvent.change(screen.getByLabelText(/search query/i), {
    target: { value: 'wireless headphones' },
  });
  fireEvent.change(screen.getByLabelText(/search mode/i), { target: { value: 'shopping' } });
  fireEvent.click(screen.getByLabelText(/exclude sponsored/i));
  fireEvent.click(screen.getByRole('button', { name: /^search$/i }));
}

describe('App wiring', () => {
  it('shopping-mode query renders results, applied filters, and diagnostics', async () => {
    const fetchStub = mockFetchOnce(SHOPPING_ENVELOPE);
    await runShoppingSearch();

    await waitFor(() => {
      expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
    });
    expect(screen.getByText(/showing 1 of 1 result/i)).toBeInTheDocument();
    expect(screen.getByTestId('applied-filters')).toHaveTextContent(
      'Applied filters: exclude_sponsored',
    );

    // Request shaping: shopping mode + exclude_sponsored filter + page 1 offset.
    const [, init] = fetchStub.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(body.mode).toBe('shopping');
    expect(body.filters).toEqual({ exclude_sponsored: true });
    expect(body.offset).toBe(0);

    // Reasons expand and diagnostics drawer render.
    fireEvent.click(screen.getByRole('button', { name: /show reasons/i }));
    expect(screen.getByText('matches query')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /show diagnostics/i }));
    expect(screen.getByTestId('diagnostics-panel')).toBeInTheDocument();
    expect(screen.getByTestId('diagnostics-fallback')).toHaveTextContent('no');
  });

  it('renders the error envelope instead of crashing when the backend fails', async () => {
    mockFetchOnce({ error: { code: 'validation_error', message: 'query must not be blank' } });
    await runShoppingSearch();

    await waitFor(() => {
      expect(screen.getByTestId('error-state')).toBeInTheDocument();
    });
    expect(screen.getByTestId('error-state')).toHaveTextContent('validation_error');
    expect(screen.queryByLabelText(/search results/i)).not.toBeInTheDocument();
  });

  it('renders an error state when the backend is unreachable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Failed to fetch')));
    await runShoppingSearch();

    await waitFor(() => {
      expect(screen.getByTestId('error-state')).toBeInTheDocument();
    });
    expect(screen.getByTestId('error-state')).toHaveTextContent('network_error');
  });
});
