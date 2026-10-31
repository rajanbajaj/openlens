import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App.tsx';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const AGENT_ENVELOPE = {
  query: 'cheap headphones',
  mode: 'agent',
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
      warnings: [],
      metadata: {},
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
  metadata: {
    agent_trace: {
      rewritten_query: 'cheap bluetooth headphones under $50',
      filters: { max_price: 50 },
      steps: [
        { kind: 'rewrite', detail: 'expanded with bluetooth' },
        { kind: 'retrieve', detail: 'queried mock provider' },
      ],
      fallbacks: ['reranker timed out, kept retrieval order'],
      model_calls: 2,
    },
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

describe('App agent mode', () => {
  it('sends mode "agent" and renders the returned trace in diagnostics', async () => {
    const fetchStub = mockFetchOnce(AGENT_ENVELOPE);
    render(<App />);

    fireEvent.change(screen.getByLabelText(/search query/i), {
      target: { value: 'cheap headphones' },
    });
    fireEvent.change(screen.getByLabelText(/search mode/i), { target: { value: 'agent' } });
    fireEvent.click(screen.getByRole('button', { name: /^search$/i }));

    await waitFor(() => {
      expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
    });

    // The mode travels in the existing request body field; changing the
    // selector re-uses the same page-1 re-search path as other modes.
    const [, init] = fetchStub.mock.calls[0] as [string, RequestInit];
    expect((JSON.parse(init.body as string) as Record<string, unknown>).mode).toBe('agent');

    fireEvent.click(await screen.findByRole('button', { name: /show diagnostics/i }));
    expect(screen.getByTestId('diagnostics-agent-trace')).toBeInTheDocument();
    expect(screen.getByTestId('diagnostics-agent-rewritten')).toHaveTextContent(
      'cheap bluetooth headphones under $50',
    );
    expect(screen.getByTestId('diagnostics-agent-steps')).toHaveTextContent('rewrite');
    expect(screen.getByTestId('diagnostics-agent-fallbacks')).toHaveTextContent(
      'reranker timed out, kept retrieval order',
    );
    expect(screen.getByTestId('diagnostics-agent-model-calls')).toHaveTextContent('Model calls: 2');
  });

  it('renders no trace section when the backend omits agent_trace', async () => {
    mockFetchOnce({ ...AGENT_ENVELOPE, metadata: undefined });
    render(<App />);

    fireEvent.change(screen.getByLabelText(/search query/i), {
      target: { value: 'cheap headphones' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^search$/i }));

    await waitFor(() => {
      expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
    });

    fireEvent.click(await screen.findByRole('button', { name: /show diagnostics/i }));
    expect(screen.getByTestId('diagnostics-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('diagnostics-agent-trace')).not.toBeInTheDocument();
  });
});
