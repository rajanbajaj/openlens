import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App.tsx';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
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

async function submitQuery() {
  render(<App />);
  fireEvent.change(screen.getByLabelText(/search query/i), {
    target: { value: 'wireless headphones' },
  });
  fireEvent.click(screen.getByRole('button', { name: /^search$/i }));
}

function mockPagedEnvelope() {
  return mockFetchEnvelope({ ...BASE_ENVELOPE, total: 25 });
}

/**
 * Search-request bodies in call order. The header form also fires a
 * debounced `/api/v1/suggest` fetch, so tests count search calls only
 * instead of asserting exact global fetch totals.
 */
function searchBodies(stub: ReturnType<typeof mockFetchEnvelope>) {
  return stub.mock.calls
    .filter(([url]) => String(url).includes('/api/v1/search'))
    .map(([, init]) => JSON.parse((init as RequestInit).body as string));
}

describe('search workspace', () => {
  it('renders the quick-filter row and re-runs on quick date change', async () => {
    const fetchStub = mockFetchEnvelope(BASE_ENVELOPE);
    await submitQuery();
    await waitFor(() => {
      expect(screen.getByTestId('quick-filters')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByLabelText('Date'), { target: { value: 'past-day' } });
    await waitFor(() => {
      expect(searchBodies(fetchStub)).toHaveLength(2);
    });
    const body = searchBodies(fetchStub)[1] as Record<string, unknown>;
    const filters = body.filters as Record<string, unknown>;
    expect(typeof filters['date_from']).toBe('string');
    expect(typeof filters['date_to']).toBe('string');
  });

  it('renders the perspectives section in the right rail when the backend sends views', async () => {
    mockFetchEnvelope({
      ...BASE_ENVELOPE,
      perspectives: [
        {
          story_id: 's1',
          claim: 'Headphones sound great',
          representative_id: 'r1',
          member_ids: ['r1'],
          stances: [{ source_id: 'r1', stance: 'supports', evidence: 'Great sound' }],
          supports: 1,
          refutes: 0,
          neutral: 0,
          multi_source: false,
          missing_note: null,
          trust_caveats: [],
        },
      ],
    });
    await submitQuery();
    await waitFor(() => {
      expect(screen.getByTestId('perspectives-section')).toBeInTheDocument();
    });
    expect(screen.getByText('Headphones sound great')).toBeInTheDocument();
    // The section lives in the right rail (beside results on wide
    // screens, stacked below on narrow ones), not above the list.
    const rail = screen.getByTestId('results-rail');
    expect(rail).toContainElement(screen.getByTestId('perspectives-section'));
    expect(screen.getAllByTestId('result-card').length).toBeGreaterThan(0);
  });

  it('sends quick-row filters without a sidebar panel', async () => {
    const fetchStub = mockFetchEnvelope(BASE_ENVELOPE);
    render(<App />);
    // Google-style landing: filters only exist after the first search,
    // so search first, then refine. The sidebar carries no filter UI.
    fireEvent.change(screen.getByLabelText(/search query/i), {
      target: { value: 'wireless headphones' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^search$/i }));
    await waitFor(() => {
      expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('sidebar-panel')).not.toBeNull();
    expect(screen.queryByLabelText(/max price/i)).not.toBeInTheDocument();

    // Every quick-row edit re-runs the search immediately: each change
    // below issues its own request, no manual resubmit needed.
    // findBy: the quick-filter row remounts after every refetch, so
    // wait for it instead of assuming it survived the loading gap.
    fireEvent.click(await screen.findByLabelText(/exclude sponsored/i));
    await waitFor(() => {
      expect(searchBodies(fetchStub)).toHaveLength(2);
    });
    expect(searchBodies(fetchStub)[1]).toMatchObject({
      filters: { exclude_sponsored: true },
    });

    fireEvent.change(await screen.findByLabelText('Safe search'), {
      target: { value: 'strict' },
    });
    await waitFor(() => {
      expect(searchBodies(fetchStub)).toHaveLength(3);
    });
    const bodies = searchBodies(fetchStub);
    expect(bodies[2]?.filters).toEqual({ exclude_sponsored: true, safety_preset: 'strict' });
  });

  it('re-runs immediately on sort, sponsored, and mode changes', async () => {
    const fetchStub = mockFetchEnvelope(BASE_ENVELOPE);
    await submitQuery();
    await waitFor(() => {
      expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
    });

    fireEvent.change(await screen.findByLabelText('Sort by'), { target: { value: 'newest' } });
    await waitFor(() => {
      expect(searchBodies(fetchStub)).toHaveLength(2);
    });
    expect(searchBodies(fetchStub)[1]).toMatchObject({
      filters: { sort: 'newest' },
      offset: 0,
    });

    fireEvent.click(await screen.findByLabelText(/exclude sponsored/i));
    await waitFor(() => {
      expect(searchBodies(fetchStub)).toHaveLength(3);
    });
    expect(searchBodies(fetchStub)[2]).toMatchObject({
      filters: { sort: 'newest', exclude_sponsored: true },
    });

    fireEvent.change(screen.getByLabelText(/search mode/i), {
      target: { value: 'software' },
    });
    await waitFor(() => {
      expect(searchBodies(fetchStub)).toHaveLength(4);
    });
    const modeBody = searchBodies(fetchStub)[3] as Record<string, unknown>;
    expect(modeBody.mode).toBe('software');
    expect(modeBody).toMatchObject({
      filters: { sort: 'newest', exclude_sponsored: true },
    });
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
      expect(searchBodies(fetchStub)).toHaveLength(2);
    });
    const body = searchBodies(fetchStub)[1] as Record<string, unknown>;
    expect(body.mode).toBe('shopping');
  });

  it('sends provider prefs only when the user sets them', async () => {
    const fetchStub = mockFetchEnvelope(BASE_ENVELOPE);
    // Google-style landing: provider prefs live in the sidebar, which
    // only mounts after the first search — search, then set. The prefs
    // edit re-runs the search immediately, no resubmit needed.
    // (submitQuery mounts the app.)
    await submitQuery();
    await waitFor(() => {
      expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByLabelText(/primary provider/i), {
      target: { value: 'mock' },
    });
    await waitFor(() => {
      expect(searchBodies(fetchStub).length).toBeGreaterThanOrEqual(2);
    });
    const prefsBody = searchBodies(fetchStub)[1] as Record<string, unknown>;
    expect(prefsBody.provider_prefs).toEqual({ include: [], exclude: [], primary: 'mock' });
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
    vi.stubEnv('VITE_OPENLENS_DEV_MODE', 'true');
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

  it('mounts the shell grid: sidebar tools, results column, lazy rail', async () => {
    mockFetchEnvelope(BASE_ENVELOPE);
    render(<App />);
    // Google-style landing: no sidebar before the first search.
    expect(screen.queryByTestId('sidebar-mount')).not.toBeInTheDocument();
    await submitQueryRerun();
    const sidebar = screen.getByTestId('sidebar-mount');
    expect(sidebar).toHaveAttribute('aria-label', 'Search tools');
    expect(sidebar.tagName).toBe('ASIDE');
    expect(screen.getByTestId('sidebar-mount')).toBeInTheDocument();
    expect(screen.getByLabelText(/search results/i)).toBeInTheDocument();
    // The sidebar carries no filter UI: the quick row owns filters and
    // the sidebar carries no collapse toggle.
    expect(
      screen.queryByRole('button', { name: /show filters|hide filters/i }),
    ).not.toBeInTheDocument();
    // Diagnostics rail is dev-gated: hidden by default, even with a
    // diagnostics payload on the envelope.
    expect(screen.queryByRole('button', { name: /show diagnostics/i })).not.toBeInTheDocument();
  });

  it('paginates server-side with limit-bound offsets', async () => {
    const fetchStub = mockPagedEnvelope();
    await submitQuery();
    await waitFor(() => {
      expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
    });
    expect(screen.getByTestId('page-indicator')).toHaveTextContent('Page 1 of 3');
    expect(screen.getByRole('button', { name: /^previous$/i })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: /^next$/i }));
    await waitFor(() => {
      expect(screen.getByTestId('page-indicator')).toHaveTextContent('Page 2 of 3');
    });
    await waitFor(() => {
      expect(searchBodies(fetchStub)).toHaveLength(2);
    });
    expect(searchBodies(fetchStub)[1]).toMatchObject({ offset: 10, limit: 10 });
    expect(screen.getByTestId('result-announcer')).toHaveTextContent(
      '25 results for “wireless headphones”',
    );

    fireEvent.click(screen.getByRole('button', { name: /^previous$/i }));
    await waitFor(() => {
      expect(screen.getByTestId('page-indicator')).toHaveTextContent('Page 1 of 3');
    });
    await waitFor(() => {
      expect(searchBodies(fetchStub)).toHaveLength(3);
    });
    expect(searchBodies(fetchStub)[2]).toMatchObject({ offset: 0 });
  });

  it('header search box offers suggestions and Escape keeps results intact', async () => {
    mockSearchAndSuggest(BASE_ENVELOPE, ['wireless headphones', 'wireless earbuds']);
    await submitQuery();
    await waitFor(() => {
      expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
    });

    // The sticky-header form is the same wired SearchForm: typing
    // reopens the dropdown with a live announcement.
    const headerInput = screen.getByLabelText(/search query/i);
    headerInput.focus();
    fireEvent.change(headerInput, { target: { value: 'wireless' } });
    await screen.findByTestId('suggest-option-0');
    expect(screen.getByTestId('suggest-announcer')).toHaveTextContent('2 suggestions available');
    expect(headerInput).toHaveAttribute('aria-expanded', 'true');

    // Escape dismisses the dropdown; the result list is untouched.
    fireEvent.keyDown(headerInput, { key: 'Escape' });
    await waitFor(() => {
      expect(screen.queryByTestId('suggest-dropdown')).not.toBeInTheDocument();
    });
    expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
  });

  it('skips idle prefetch when every result fits on one page', async () => {
    mockSearchAndSuggest(BASE_ENVELOPE, ['wireless headphones']);
    const idleCallback = vi.fn((callback: () => void) => {
      callback();
      return 1;
    });
    Object.defineProperty(window, 'requestIdleCallback', {
      value: idleCallback,
      configurable: true,
    });
    try {
      await submitQuery();
      await waitFor(() => {
        expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
      });
      expect(screen.queryByTestId('page-indicator')).not.toBeInTheDocument();
      // Give the idle effect a chance to run, then confirm it stood down.
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(idleCallback).not.toHaveBeenCalled();
    } finally {
      Reflect.deleteProperty(window as unknown as Record<string, unknown>, 'requestIdleCallback');
    }
  });

  it('shows quick filters inline on narrow screens with no sidebar toggle', async () => {
    vi.stubGlobal(
      'matchMedia',
      vi.fn().mockImplementation((query: string) => ({
        matches: query.includes('max-width'),
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    );
    mockPagedEnvelope();
    await submitQuery();
    await waitFor(() => {
      expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
    });
    // No collapse toggle anywhere: the quick row is always visible and
    // the sidebar panel is always mounted.
    expect(
      screen.queryByRole('button', { name: /show filters|hide filters/i }),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId('quick-filters')).toBeVisible();
    expect(screen.getByTestId('sidebar-panel')).toBeVisible();
  });

  it('leaves focus on a live node (never a detached one) across result updates', async () => {
    mockPagedEnvelope();
    await submitQuery();
    await waitFor(() => {
      expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
    });
    const next = screen.getByRole('button', { name: /^next$/i });
    next.focus();
    fireEvent.click(next);
    await waitFor(() => {
      expect(screen.getByTestId('page-indicator')).toHaveTextContent('Page 2 of 3');
    });
    // The paged-out button unmounts mid-load; focus must land on the
    // document (or another mounted node), never on the removed button.
    expect(document.contains(document.activeElement)).toBe(true);
    expect(document.activeElement).not.toBe(next);
    expect(screen.getByTestId('result-announcer')).toHaveTextContent(
      '25 results for “wireless headphones”',
    );
  });
});

/** Fill the hero form and submit — used when the shell needs pre-search asserts first. */
async function submitQueryRerun() {
  fireEvent.change(screen.getByLabelText(/search query/i), {
    target: { value: 'wireless headphones' },
  });
  fireEvent.click(screen.getByRole('button', { name: /^search$/i }));
  await waitFor(() => {
    expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
  });
}
