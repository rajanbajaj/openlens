import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App.tsx';

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  window.history.replaceState(null, '', '/');
  vi.unstubAllGlobals();
});

const SEARCH_ENVELOPE = {
  query: 'dating advice',
  mode: 'default',
  results: [
    {
      id: 'r1',
      title: 'Relationship Advice for Couples',
      snippet: 'What to discuss early on',
      original_score: 0.9,
      final_score: 0.95,
      url: 'https://example.com/love',
      provider: 'mock',
      reasons: ['matches query'],
      warnings: [],
      metadata: {},
    },
  ],
  total: 1,
  applied_filters: ['user-restrictions', 'safety-filter', 'privacy-filter'],
  diagnostics: {
    fallback_used: false,
    provider_latencies_ms: { mock: 12.5 },
    provider_result_counts: { mock: 1 },
    provider_costs_estimated: { mock: 0 },
    plugin_executions: [],
    errors: [],
  },
  perspectives: [],
};

function mockSearch() {
  const stub = vi.fn(async (input: unknown) => {
    if (String(input).includes('/api/v1/suggest')) {
      return { ok: true, json: () => Promise.resolve({ q: 'dating', suggestions: [] }) };
    }
    return { ok: true, json: () => Promise.resolve(SEARCH_ENVELOPE) };
  });
  vi.stubGlobal('fetch', stub);
}

async function runDatingSearch() {
  mockSearch();
  render(<App />);
  fireEvent.change(screen.getByLabelText(/search query/i), {
    target: { value: 'dating advice' },
  });
  fireEvent.click(screen.getByRole('button', { name: /^search$/i }));
  await waitFor(() => {
    expect(screen.getByText('Relationship Advice for Couples')).toBeInTheDocument();
  });
}

describe('App guided tour wiring', () => {
  it('takes the tour on the search page after a real search', async () => {
    await runDatingSearch();
    fireEvent.click(screen.getByRole('button', { name: 'Take a tour' }));
    await waitFor(() => {
      expect(screen.getByTestId('tour-tooltip')).toBeInTheDocument();
    });
    // search-header is the first step available in the workspace.
    expect(screen.getByText('Same box, sticky')).toBeInTheDocument();
  });

  it('runs the example search and chat from the tour itself', async () => {
    const stub = vi.fn(async (input: unknown) => {
      const url = String(input);
      if (url.includes('/api/v1/suggest')) {
        return { ok: true, json: () => Promise.resolve({ q: 'dating', suggestions: [] }) };
      }
      if (url.includes('/api/v1/agent/chat')) {
        return {
          ok: true,
          json: () =>
            Promise.resolve({
              answer: 'This index covers relationships [1].',
              citations: [
                {
                  ref: 1,
                  title: 'Relationship Advice for Couples',
                  url: 'https://example.com/love',
                },
              ],
              results: SEARCH_ENVELOPE.results,
              trace: {
                rewritten_query: 'dating advice',
                filters: {},
                steps: [{ kind: 'retrieve', detail: 'queried mock' }],
                fallbacks: [],
                model_calls: 1,
              },
              notice: null,
            }),
        };
      }
      return { ok: true, json: () => Promise.resolve(SEARCH_ENVELOPE) };
    });
    vi.stubGlobal('fetch', stub);
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: 'Take a tour' }));
    // Landing step, then the search gate.
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(screen.getByText('Your turn: run a search')).toBeInTheDocument();
    // The tour runs the search itself; the gate hands off to results.
    fireEvent.click(screen.getByRole('button', { name: 'Run an example search' }));
    await waitFor(() => {
      expect(screen.getByText('Relationship Advice for Couples')).toBeInTheDocument();
    });
    await waitFor(() => {
      expect(screen.getByText('Same box, sticky')).toBeInTheDocument();
    });
    // Walk forward to the chat gate (missing-target steps skip by).
    for (let i = 0; i < 12; i += 1) {
      if (screen.queryByText('Your turn: open AI chat') !== null) break;
      const next = screen.queryByRole('button', { name: 'Next' });
      if (next === null) break;
      fireEvent.click(next);
    }
    expect(screen.getByText('Your turn: open AI chat')).toBeInTheDocument();
    // The tour opens chat itself and lands on the chat-view step.
    fireEvent.click(screen.getByRole('button', { name: 'Open AI chat for me' }));
    await waitFor(() => {
      expect(screen.getByText('Cited answers, live')).toBeInTheDocument();
    });
    // The tour asks the example question itself; the answer renders
    // (the [1] citation is its own chip, so match the container text).
    fireEvent.click(screen.getByRole('button', { name: 'Ask an example question' }));
    await waitFor(() => {
      expect(screen.getByTestId('chat-answer')).toBeInTheDocument();
    });
    expect(screen.getByTestId('chat-answer')).toHaveTextContent('This index covers relationships');
    const chatCalls = stub.mock.calls.filter((call) =>
      String(call[0]).includes('/api/v1/agent/chat'),
    );
    expect(chatCalls.length).toBeGreaterThan(0);
  });

  it('follows the user into AI chat and tours the chat view', async () => {
    await runDatingSearch();
    fireEvent.click(screen.getByRole('button', { name: 'Take a tour' }));
    await waitFor(() => {
      expect(screen.getByTestId('tour-tooltip')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('nav-chat'));
    await waitFor(() => {
      expect(screen.getByTestId('chat-view')).toBeInTheDocument();
    });
    // Advance past the search-only steps: the tour skips unmounted
    // targets and lands on the chat steps instead of freezing.
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => {
      expect(screen.getByTestId('tour-tooltip')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('tour-tooltip')).not.toBeNull();
  });
});
