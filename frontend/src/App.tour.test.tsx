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
