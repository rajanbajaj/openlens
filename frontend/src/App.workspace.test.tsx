import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App.tsx';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const BASE_ENVELOPE = {
  query: 'wireless headphones',
  mode: 'shopping',
  results: [
    {
      id: 'r1',
      title: 'Acme Headphones',
      snippet: 'Great sound',
      original_score: 0.9,
      final_score: 0.95,
      url: 'https://example.com/h',
      provider: 'mock',
      reasons: ['matches query'],
      warnings: [],
      metadata: {},
    },
  ],
  total: 1,
  applied_filters: ['user-restrictions'],
  diagnostics: {
    fallback_used: false,
    provider_latencies_ms: { mock: 12.5 },
    plugin_executions: [],
    errors: [],
  },
};

function mockFetchEnvelope(payload: unknown) {
  const stub = vi.fn().mockResolvedValue({
    ok: true,
    json: () => Promise.resolve(payload),
  });
  vi.stubGlobal('fetch', stub);
  return stub;
}

async function submitQuery() {
  render(<App />);
  fireEvent.change(screen.getByLabelText(/search query/i), {
    target: { value: 'wireless headphones' },
  });
  fireEvent.click(screen.getByRole('button', { name: /^search$/i }));
}

describe('search workspace', () => {
  it('sends advanced panel filters and offers removable chips', async () => {
    const fetchStub = mockFetchEnvelope(BASE_ENVELOPE);
    render(<App />);
    fireEvent.change(screen.getByLabelText(/max price/i), { target: { value: '250' } });
    fireEvent.change(screen.getByLabelText(/location/i), { target: { value: 'Berlin' } });
    fireEvent.change(screen.getByLabelText(/search query/i), {
      target: { value: 'wireless headphones' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^search$/i }));

    await waitFor(() => {
      expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
    });
    const [, init] = fetchStub.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(body.filters).toEqual({ max_price: 250, location: 'Berlin' });
    expect(screen.getByTestId('user-filter-chips')).toHaveTextContent('Max price 250');

    fireEvent.click(screen.getByRole('button', { name: /remove max price 250/i }));
    await waitFor(() => {
      expect(fetchStub).toHaveBeenCalledTimes(2);
    });
    const [, retryInit] = fetchStub.mock.calls[1] as [string, RequestInit];
    const retryBody = JSON.parse(retryInit.body as string) as Record<string, unknown>;
    expect(retryBody.filters).toEqual({ location: 'Berlin' });
  });

  it('offers the T9 mode suggestion without overriding the explicit mode', async () => {
    const fetchStub = mockFetchEnvelope({
      ...BASE_ENVELOPE,
      diagnostics: {
        ...BASE_ENVELOPE.diagnostics,
        query: {
          normalized: 'wireless headphones',
          truncated: false,
          intent: 'shopping',
          suggested_mode: 'shopping',
          explicit_mode: 'default',
          effective_mode: 'default',
          base_terms: ['wireless', 'headphones'],
          added_terms: [],
          provenance: {},
          reasons: [],
        },
      },
    });
    await submitQuery();
    await waitFor(() => {
      expect(screen.getByTestId('mode-suggestion')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: /try shopping mode/i }));
    await waitFor(() => {
      expect(fetchStub).toHaveBeenCalledTimes(2);
    });
    const [, init] = fetchStub.mock.calls[1] as [string, RequestInit];
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(body.mode).toBe('shopping');
  });

  it('sends provider prefs only when the user sets them', async () => {
    const fetchStub = mockFetchEnvelope(BASE_ENVELOPE);
    render(<App />);
    fireEvent.change(screen.getByLabelText(/primary provider/i), {
      target: { value: 'mock' },
    });
    fireEvent.change(screen.getByLabelText(/search query/i), {
      target: { value: 'wireless headphones' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^search$/i }));
    await waitFor(() => {
      expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
    });
    const [, init] = fetchStub.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(body.provider_prefs).toEqual({ include: [], exclude: [], primary: 'mock' });
  });

  it('omits provider prefs and announces results on a plain search', async () => {
    const fetchStub = mockFetchEnvelope(BASE_ENVELOPE);
    await submitQuery();
    await waitFor(() => {
      expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
    });
    const [, init] = fetchStub.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect('provider_prefs' in body).toBe(false);
    expect(screen.getByTestId('result-announcer')).toHaveTextContent(
      '1 result for “wireless headphones”',
    );
  });

  it('degrades gracefully on envelopes without the new diagnostic keys', async () => {
    mockFetchEnvelope(BASE_ENVELOPE);
    await submitQuery();
    await waitFor(() => {
      expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
    });
    fireEvent.click(await screen.findByRole('button', { name: /show diagnostics/i }));
    expect(screen.getByTestId('diagnostics-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('diagnostics-request-id')).not.toBeInTheDocument();
    expect(screen.queryByTestId('diagnostics-query')).not.toBeInTheDocument();
  });
});
