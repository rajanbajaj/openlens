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
  applied_filters: ['user-restrictions', 'safety-filter', 'privacy-filter'],
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

/**
 * Route-aware fetch stub: `/api/v1/suggest` answers with the given
 * completions (valid `{q, suggestions}` envelope), every other call
 * answers with the search payload.
 */
function mockSearchAndSuggest(searchPayload: unknown, suggestions: string[]) {
  const stub = vi.fn(async (input: unknown) => {
    if (String(input).includes('/api/v1/suggest')) {
      return { ok: true, json: () => Promise.resolve({ q: 'wireless', suggestions }) };
    }
    return { ok: true, json: () => Promise.resolve(searchPayload) };
  });
  vi.stubGlobal('fetch', stub);
  return stub;
}

async function runShoppingSearch() {
  render(<App />);
  // Google-style landing: hero mounted, exactly one search form, and
  // none of the workspace chrome (header, sidebar) exists yet.
  expect(screen.getByTestId('search-hero')).toBeInTheDocument();
  expect(screen.queryByTestId('search-header')).not.toBeInTheDocument();
  expect(screen.getAllByRole('search')).toHaveLength(1);
  expect(screen.queryByTestId('sidebar-mount')).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText(/search query/i), {
    target: { value: 'wireless headphones' },
  });
  fireEvent.change(screen.getByLabelText(/search mode/i), { target: { value: 'shopping' } });
  fireEvent.click(screen.getByRole('button', { name: /^search$/i }));
}

async function waitForResults() {
  await waitFor(() => {
    expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
  });
  // Post-search shell: sticky header replaces the hero; one form stays mounted.
  expect(screen.queryByTestId('search-hero')).not.toBeInTheDocument();
  expect(screen.getByTestId('search-header')).toHaveAttribute('role', 'banner');
  expect(screen.getAllByRole('search')).toHaveLength(1);
  expect(screen.getByTestId('sidebar-mount')).toBeInTheDocument();
}

describe('App wiring', () => {
  it('shows only the centered search before the first search', () => {
    mockFetchOnce(SHOPPING_ENVELOPE);
    render(<App />);
    expect(screen.getByTestId('search-hero')).toBeInTheDocument();
    expect(screen.getAllByRole('search')).toHaveLength(1);
    expect(screen.queryByTestId('search-header')).not.toBeInTheDocument();
    expect(screen.queryByTestId('sidebar-mount')).not.toBeInTheDocument();
  });

  it('shopping-mode query renders results, applied filters, and diagnostics', async () => {
    const fetchStub = mockFetchOnce(SHOPPING_ENVELOPE);
    await runShoppingSearch();

    await waitForResults();
    expect(screen.getByText(/showing 1 of 1 result/i)).toBeInTheDocument();
    expect(screen.getByTestId('applied-filters')).toHaveTextContent(
      'Applied filters: user-restrictions, safety-filter, privacy-filter',
    );

    // Filters exist only after the first search (Google-style landing):
    // toggle in the now-mounted sidebar, resubmit, assert the wire call.
    // The sponsored toggle sends the backend `exclude_sponsored`
    // reserved flag (hard-excludes crawler-truth sponsored hits) plus
    // the legacy owned term filter for older backends.
    fireEvent.click(screen.getByLabelText(/exclude sponsored/i));
    fireEvent.click(screen.getByRole('button', { name: /^search$/i }));
    await waitFor(() => {
      expect(fetchStub).toHaveBeenCalledTimes(2);
    });
    const [, init] = fetchStub.mock.calls[1] as [string, RequestInit];
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(body.mode).toBe('shopping');
    expect(body.filters).toEqual({
      exclude_sponsored: true,
      must_not_terms: ['sponsored'],
    });
    expect(body.offset).toBe(0);

    // Reasons expand and diagnostics drawer render.
    fireEvent.click(screen.getByRole('button', { name: /show reasons/i }));
    expect(screen.getByText('matches query')).toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: /show diagnostics/i }));
    expect(screen.getByTestId('diagnostics-panel')).toBeInTheDocument();
    expect(screen.getByTestId('diagnostics-fallback')).toHaveTextContent('Live');
  });

  it('renders the sticky header (not the hero) once the first search runs', async () => {
    mockFetchOnce(SHOPPING_ENVELOPE);
    await runShoppingSearch();
    await waitForResults();
    // The header keeps the query + mode visible while results scroll beneath.
    expect(screen.getByLabelText(/search query/i)).toHaveDisplayValue('wireless headphones');
    expect(screen.getByLabelText(/search mode/i)).toHaveValue('shopping');
    expect(screen.getByTestId('result-announcer')).toHaveTextContent(
      '1 result for “wireless headphones”',
    );
  });

  it('renders the error envelope instead of crashing when the backend fails', async () => {
    mockFetchOnce({ error: { code: 'validation_error', message: 'query must not be blank' } });
    await runShoppingSearch();

    await waitFor(() => {
      expect(screen.getByTestId('error-state')).toBeInTheDocument();
    });
    expect(screen.getByTestId('error-state')).toHaveTextContent('validation_error');
    expect(screen.queryByLabelText(/search results/i)).not.toBeInTheDocument();
    // The failed search still collapses the hero into the sticky header.
    expect(screen.queryByTestId('search-hero')).not.toBeInTheDocument();
    expect(screen.getByTestId('search-header')).toHaveAttribute('role', 'banner');
  });

  it('renders an error state when the backend is unreachable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Failed to fetch')));
    await runShoppingSearch();

    await waitFor(() => {
      expect(screen.getByTestId('error-state')).toBeInTheDocument();
    });
    expect(screen.getByTestId('error-state')).toHaveTextContent('network_error');
  });

  it('selecting a dropdown suggestion runs the search with the picked query', async () => {
    const fetchStub = mockSearchAndSuggest(SHOPPING_ENVELOPE, ['wireless headphones']);
    render(<App />);
    const input = screen.getByLabelText(/search query/i);
    fireEvent.change(input, { target: { value: 'wireless' } });
    await screen.findByTestId('suggest-option-0');
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.keyDown(input, { key: 'Enter' });

    await waitForResults();
    const searchCall = fetchStub.mock.calls.find(([url]) =>
      String(url).includes('/api/v1/search'),
    ) as unknown as [string, RequestInit] | undefined;
    expect(searchCall).toBeDefined();
    const [, init] = searchCall as [string, RequestInit];
    expect((JSON.parse(init.body as string) as Record<string, unknown>).query).toBe(
      'wireless headphones',
    );
  });

  it('opens and closes the personal library from the sidebar', async () => {
    mockFetchOnce(SHOPPING_ENVELOPE);
    await runShoppingSearch();
    await waitForResults();

    const open = screen.getByRole('button', { name: /open library/i });
    open.focus();
    expect(document.activeElement).toBe(open);
    fireEvent.click(open);
    expect(screen.getByTestId('library-drawer')).toBeInTheDocument();

    const close = screen.getByRole('button', { name: /close library/i });
    close.focus();
    expect(document.activeElement).toBe(close);
    fireEvent.click(close);
    expect(screen.queryByTestId('library-drawer')).not.toBeInTheDocument();
  });

  it('exposes no frontier or admin UI and never calls frontier endpoints', async () => {
    const fetchStub = mockFetchOnce(SHOPPING_ENVELOPE);
    await runShoppingSearch();
    await waitForResults();

    // URL-frontier + configuration editing live in the admin app:
    // no admin-token input, no frontier drawer, no frontier fetch.
    expect(screen.queryByTestId('frontier-admin-token')).not.toBeInTheDocument();
    expect(screen.queryByText(/frontier/i)).not.toBeInTheDocument();
    expect(fetchStub.mock.calls.some(([url]) => String(url).includes('/api/v1/frontier/'))).toBe(
      false,
    );
  });

  it('prefetches the next page while idle once results land', async () => {
    mockFetchOnce({ ...SHOPPING_ENVELOPE, total: 25 });
    const idleCallback = vi.fn((callback: () => void) => {
      callback();
      return 1;
    });
    Object.defineProperty(window, 'requestIdleCallback', {
      value: idleCallback,
      configurable: true,
    });
    try {
      await runShoppingSearch();
      await waitForResults();
      await waitFor(() => {
        expect(idleCallback).toHaveBeenCalled();
      });
      // The prefetch requests page 2 server-side with the page-1 body.
      fireEvent.click(screen.getByRole('button', { name: /^next$/i }));
      await waitFor(() => {
        expect(screen.getByTestId('page-indicator')).toHaveTextContent('Page 2 of 3');
      });
    } finally {
      Reflect.deleteProperty(window as unknown as Record<string, unknown>, 'requestIdleCallback');
    }
  });
});
