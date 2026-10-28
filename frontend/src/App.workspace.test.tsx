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

describe('search workspace', () => {
  it('sends advanced panel filters and offers removable chips', async () => {
    const fetchStub = mockFetchEnvelope(BASE_ENVELOPE);
    render(<App />);
    // Google-style landing: the filter panel only exists after the
    // first search, so search first, then refine and resubmit.
    fireEvent.change(screen.getByLabelText(/search query/i), {
      target: { value: 'wireless headphones' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^search$/i }));
    await waitFor(() => {
      expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText(/max price/i), { target: { value: '250' } });
    fireEvent.change(screen.getByLabelText(/location/i), { target: { value: 'Berlin' } });
    fireEvent.click(screen.getByRole('button', { name: /^search$/i }));
    await waitFor(() => {
      expect(fetchStub).toHaveBeenCalledTimes(2);
    });
    const [, init] = fetchStub.mock.calls[1] as [string, RequestInit];
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(body.filters).toEqual({ max_price: 250, location: 'Berlin' });
    expect(screen.getByTestId('user-filter-chips')).toHaveTextContent('Max price 250');

    fireEvent.click(screen.getByRole('button', { name: /remove max price 250/i }));
    await waitFor(() => {
      expect(fetchStub).toHaveBeenCalledTimes(3);
    });
    const [, retryInit] = fetchStub.mock.calls[2] as [string, RequestInit];
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
    // Google-style landing: provider prefs live in the sidebar, which
    // only mounts after the first search — search, set, resubmit.
    // (submitQuery mounts the app.)
    await submitQuery();
    await waitFor(() => {
      expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByLabelText(/primary provider/i), {
      target: { value: 'mock' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^search$/i }));
    await waitFor(() => {
      expect(fetchStub.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
    const [, init] = fetchStub.mock.calls[1] as [string, RequestInit];
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

  it('mounts the shell grid: sidebar filters, results column, lazy rail', async () => {
    mockFetchEnvelope(BASE_ENVELOPE);
    render(<App />);
    // Google-style landing: no sidebar before the first search.
    expect(screen.queryByTestId('sidebar-mount')).not.toBeInTheDocument();
    await submitQueryRerun();
    const sidebar = screen.getByTestId('sidebar-mount');
    expect(sidebar).toHaveAttribute('aria-label', 'Search filters');
    expect(sidebar.tagName).toBe('ASIDE');
    expect(screen.getByTestId('sidebar-mount')).toBeInTheDocument();
    expect(screen.getByLabelText(/search results/i)).toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: /show diagnostics/i }));
    expect(screen.getByTestId('diagnostics-panel')).toBeInTheDocument();
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
    const [, nextInit] = fetchStub.mock.calls[1] as [string, RequestInit];
    const nextBody = JSON.parse(nextInit.body as string) as Record<string, unknown>;
    expect(nextBody.offset).toBe(10);
    expect(nextBody.limit).toBe(10);
    expect(screen.getByTestId('result-announcer')).toHaveTextContent(
      '25 results for “wireless headphones”',
    );

    fireEvent.click(screen.getByRole('button', { name: /^previous$/i }));
    await waitFor(() => {
      expect(screen.getByTestId('page-indicator')).toHaveTextContent('Page 1 of 3');
    });
    const [, prevInit] = fetchStub.mock.calls[2] as [string, RequestInit];
    const prevBody = JSON.parse(prevInit.body as string) as Record<string, unknown>;
    expect(prevBody.offset).toBe(0);
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
