import axe from 'axe-core';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App.tsx';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const ENVELOPE = {
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
      warnings: ['demo warning'],
      metadata: {},
    },
  ],
  total: 1,
  applied_filters: ['user-restrictions'],
  diagnostics: {
    fallback_used: false,
    provider_latencies_ms: { mock: 12.5 },
    provider_result_counts: { mock: 1 },
    provider_costs_estimated: { mock: 0 },
    plugin_executions: [],
    errors: [],
    request_id: 'abc123',
  },
};

const PAGED_ENVELOPE = { ...ENVELOPE, total: 25 };

function mockFetch(payload: unknown) {
  const stub = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(payload) });
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

/** Stub `window.matchMedia` with a fixed `prefers-reduced-motion` verdict. */
function mockMatchMedia(reduceMatches: boolean) {
  const matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: query.includes('prefers-reduced-motion: reduce') ? reduceMatches : false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
  vi.stubGlobal('matchMedia', matchMedia);
  return matchMedia;
}

async function runSearchFlow() {
  mockFetch(ENVELOPE);
  const rendered = render(<App />);
  fireEvent.change(screen.getByLabelText(/search query/i), {
    target: { value: 'wireless headphones' },
  });
  fireEvent.click(screen.getByRole('button', { name: /^search$/i }));
  await waitFor(() => {
    expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
  });
  fireEvent.click(await screen.findByRole('button', { name: /show diagnostics/i }));
  await waitFor(() => {
    expect(screen.getByTestId('diagnostics-panel')).toBeInTheDocument();
  });
  return rendered;
}

async function expectZeroCriticalViolations(root: Element) {
  const results = await axe.run(root);
  const critical = results.violations.filter((violation) => violation.impact === 'critical');
  expect(critical).toEqual([]);
}

describe('accessibility', () => {
  it('search flow reports zero critical axe violations', async () => {
    const { container } = await runSearchFlow();
    await expectZeroCriticalViolations(container);
  });

  it('hero region reports zero critical axe violations', async () => {
    mockFetch(ENVELOPE);
    render(<App />);
    await expectZeroCriticalViolations(screen.getByTestId('search-hero'));
  });

  it('header, results, sidebar, and rail each report zero critical violations', async () => {
    await runSearchFlow();
    await expectZeroCriticalViolations(screen.getByTestId('search-header'));
    await expectZeroCriticalViolations(screen.getByLabelText(/search results/i));
    await expectZeroCriticalViolations(screen.getByTestId('sidebar-mount'));
    await expectZeroCriticalViolations(screen.getByTestId('diagnostics-panel'));
  });

  it('Escape closes the open diagnostics drawer', async () => {
    await runSearchFlow();
    const panel = screen.getByTestId('diagnostics-panel');
    fireEvent.keyDown(panel, { key: 'Escape' });
    await waitFor(() => {
      expect(screen.queryByTestId('diagnostics-panel')).not.toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: /show diagnostics/i })).toHaveAttribute(
      'aria-expanded',
      'false',
    );
  });

  it('announces result counts through a live region', async () => {
    await runSearchFlow();
    const announcer = screen.getByTestId('result-announcer');
    expect(announcer).toHaveAttribute('aria-live', 'polite');
    expect(announcer).toHaveTextContent('1 result for “wireless headphones”');
  });

  it('completes a keyboard-only walkthrough: search, filter, paginate, rail, Escape', async () => {
    const fetchStub = mockFetch(PAGED_ENVELOPE);
    render(<App />);

    // Search: focus the query input, type, submit with the form (Enter).
    const query = screen.getByLabelText(/search query/i);
    query.focus();
    expect(document.activeElement).toBe(query);
    fireEvent.change(query, { target: { value: 'wireless headphones' } });
    fireEvent.submit(screen.getByRole('search'));
    await waitFor(() => {
      expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
    });
    expect(screen.getByTestId('result-announcer')).toHaveTextContent(
      '25 results for “wireless headphones”',
    );

    // Filter: keyboard-toggle the sponsored checkbox; the chip appears at once.
    const sponsored = screen.getByLabelText(/exclude sponsored/i);
    sponsored.focus();
    expect(document.activeElement).toBe(sponsored);
    fireEvent.click(sponsored); // Space-key activation equivalent on a checkbox.
    expect(sponsored).toBeChecked();
    expect(screen.getByTestId('user-filter-chips')).toHaveTextContent('Excludes sponsored');

    // Paginate: focus Next and activate; page 2 is requested server-side.
    const next = screen.getByRole('button', { name: /^next$/i });
    next.focus();
    expect(document.activeElement).toBe(next);
    fireEvent.click(next); // Enter-key activation equivalent on a button.
    await waitFor(() => {
      expect(screen.getByTestId('page-indicator')).toHaveTextContent('Page 2 of 3');
    });
    const [, nextInit] = fetchStub.mock.calls[1] as [string, RequestInit];
    expect((JSON.parse(nextInit.body as string) as Record<string, unknown>).offset).toBe(10);

    // Rail: focus the trigger and open; Escape closes it again.
    const railTrigger = await screen.findByRole('button', { name: /show diagnostics/i });
    railTrigger.focus();
    expect(document.activeElement).toBe(railTrigger);
    fireEvent.click(railTrigger);
    await waitFor(() => {
      expect(screen.getByTestId('diagnostics-panel')).toBeInTheDocument();
    });
    fireEvent.keyDown(screen.getByTestId('diagnostics-panel'), { key: 'Escape' });
    await waitFor(() => {
      expect(screen.queryByTestId('diagnostics-panel')).not.toBeInTheDocument();
    });
    // The persistent trigger keeps focus across the open/close update.
    expect(document.activeElement).toBe(screen.getByRole('button', { name: /show diagnostics/i }));
  });

  it('keeps every primary control keyboard-focusable in the workspace', async () => {
    mockFetch(ENVELOPE);
    render(<App />);
    fireEvent.change(screen.getByLabelText(/search query/i), {
      target: { value: 'wireless headphones' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^search$/i }));
    await waitFor(() => {
      expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
    });
    // Tab-order walk: each stop must accept programmatic focus.
    const stops = [
      screen.getByLabelText(/search query/i),
      screen.getByRole('button', { name: /^search$/i }),
      screen.getByLabelText(/search mode/i),
      screen.getByLabelText(/exclude sponsored/i),
      screen.getByLabelText(/max price/i),
      screen.getByRole('button', { name: /show score breakdown/i }),
      await screen.findByRole('button', { name: /show diagnostics/i }),
    ];
    for (const stop of stops) {
      (stop as HTMLElement).focus();
      expect(document.activeElement).toBe(stop);
    }
  });

  it('search flow works identically with prefers-reduced-motion enabled', async () => {
    mockMatchMedia(true);
    mockFetch(ENVELOPE);
    render(<App />);
    fireEvent.change(screen.getByLabelText(/search query/i), {
      target: { value: 'wireless headphones' },
    });
    fireEvent.submit(screen.getByRole('search'));
    await waitFor(() => {
      expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
    });
    // Instant state changes: disclosures, chips, announcer, rail all respond.
    fireEvent.click(screen.getByRole('button', { name: /show reasons/i }));
    expect(screen.getByText('matches query')).toBeInTheDocument();
    expect(screen.getByTestId('result-announcer')).toHaveTextContent(
      '1 result for “wireless headphones”',
    );
    fireEvent.click(await screen.findByRole('button', { name: /show diagnostics/i }));
    expect(screen.getByTestId('diagnostics-panel')).toBeInTheDocument();
  });

  it('search flow works identically with prefers-reduced-motion disabled', async () => {
    mockMatchMedia(false);
    mockFetch(ENVELOPE);
    render(<App />);
    fireEvent.change(screen.getByLabelText(/search query/i), {
      target: { value: 'wireless headphones' },
    });
    fireEvent.submit(screen.getByRole('search'));
    await waitFor(() => {
      expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: /show reasons/i }));
    expect(screen.getByText('matches query')).toBeInTheDocument();
    expect(screen.getByTestId('result-announcer')).toHaveTextContent(
      '1 result for “wireless headphones”',
    );
  });

  it('open suggestion dropdown reports zero critical axe violations', async () => {
    mockSearchAndSuggest(ENVELOPE, ['wireless headphones', 'wireless earbuds']);
    render(<App />);
    fireEvent.change(screen.getByLabelText(/search query/i), {
      target: { value: 'wireless' },
    });
    const dropdown = await screen.findByTestId('suggest-dropdown');
    // The open dropdown wires the combobox contract while visible.
    const input = screen.getByLabelText(/search query/i);
    expect(input).toHaveAttribute('aria-expanded', 'true');
    expect(input).toHaveAttribute('aria-controls', 'openlens-suggest-listbox');
    await expectZeroCriticalViolations(dropdown);
    // The hero stays clean while the dropdown is open (the sidebar
    // only mounts after the first search, Google-style landing).
    await expectZeroCriticalViolations(screen.getByTestId('search-hero'));
    expect(screen.queryByTestId('sidebar-mount')).not.toBeInTheDocument();
  });

  it('dropdown arrow-highlight, Enter-select, and Escape all work by keyboard', async () => {
    mockSearchAndSuggest(ENVELOPE, ['wireless headphones', 'wireless earbuds']);
    render(<App />);
    const input = screen.getByLabelText(/search query/i);
    input.focus();
    fireEvent.change(input, { target: { value: 'wireless' } });
    await screen.findByTestId('suggest-option-0');

    // ArrowDown highlights the first option and names it on the input.
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(screen.getByTestId('suggest-option-0')).toHaveAttribute('aria-selected', 'true');
    expect(input).toHaveAttribute('aria-activedescendant', 'openlens-suggest-listbox-option-0');

    // ArrowUp wraps to the last option.
    fireEvent.keyDown(input, { key: 'ArrowUp' });
    expect(screen.getByTestId('suggest-option-1')).toHaveAttribute('aria-selected', 'true');

    // Escape closes the dropdown without submitting.
    fireEvent.keyDown(input, { key: 'Escape' });
    await waitFor(() => {
      expect(screen.queryByTestId('suggest-dropdown')).not.toBeInTheDocument();
    });
    expect(input).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('Acme Headphones')).not.toBeInTheDocument();
  });

  it('full keyboard walkthrough: type, arrow, select, paginate, library, Escape, rail', async () => {
    const fetchStub = mockSearchAndSuggest(PAGED_ENVELOPE, [
      'wireless headphones',
      'wireless earbuds',
    ]);
    render(<App />);

    // Type: the dropdown offers completions with a live announcement.
    const heroInput = screen.getByLabelText(/search query/i);
    heroInput.focus();
    fireEvent.change(heroInput, { target: { value: 'wireless' } });
    await screen.findByTestId('suggest-option-0');
    expect(screen.getByTestId('suggest-announcer')).toHaveTextContent('2 suggestions available');

    // Arrow + select: highlight the first option, Enter runs the search
    // (App submits the picked suggestion through the onSubmit fallback).
    fireEvent.keyDown(heroInput, { key: 'ArrowDown' });
    fireEvent.keyDown(heroInput, { key: 'Enter' });
    await waitFor(() => {
      expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
    });
    expect(screen.getByTestId('result-announcer')).toHaveTextContent(
      '25 results for “wireless headphones”',
    );
    const searchCall = fetchStub.mock.calls.find(([url]) =>
      String(url).includes('/api/v1/search'),
    ) as unknown as [string, RequestInit] | undefined;
    expect(searchCall).toBeDefined();
    const [, searchInit] = searchCall as [string, RequestInit];
    expect((JSON.parse(searchInit.body as string) as Record<string, unknown>).query).toBe(
      'wireless headphones',
    );

    // Paginate by keyboard: focus Next, activate, page 2 is requested.
    const next = screen.getByRole('button', { name: /^next$/i });
    next.focus();
    expect(document.activeElement).toBe(next);
    fireEvent.click(next);
    await waitFor(() => {
      expect(screen.getByTestId('page-indicator')).toHaveTextContent('Page 2 of 3');
    });

    // Library: the sidebar affordance opens the drawer by keyboard and
    // the drawer closes again. Per-card save toggles are not asserted
    // here: App does not yet wire onSaveResult into ResultList, so no
    // save button renders (integrate owns that wiring).
    const libraryOpen = screen.getByRole('button', { name: /open library/i });
    libraryOpen.focus();
    expect(document.activeElement).toBe(libraryOpen);
    fireEvent.click(libraryOpen);
    expect(screen.getByTestId('library-drawer')).toBeInTheDocument();

    // Escape closes the diagnostics rail; the rail trigger keeps focus.
    const railTrigger = await screen.findByRole('button', { name: /show diagnostics/i });
    railTrigger.focus();
    fireEvent.click(railTrigger);
    await waitFor(() => {
      expect(screen.getByTestId('diagnostics-panel')).toBeInTheDocument();
    });
    fireEvent.keyDown(screen.getByTestId('diagnostics-panel'), { key: 'Escape' });
    await waitFor(() => {
      expect(screen.queryByTestId('diagnostics-panel')).not.toBeInTheDocument();
    });
    expect(document.activeElement).toBe(screen.getByRole('button', { name: /show diagnostics/i }));

    // Zero critical violations on every region at the end of the flow.
    await expectZeroCriticalViolations(screen.getByTestId('search-header'));
    await expectZeroCriticalViolations(screen.getByLabelText(/search results/i));
    await expectZeroCriticalViolations(screen.getByTestId('sidebar-mount'));
  });

  it('needs no matchMedia at all: state changes are synchronous either way', async () => {
    // Motion lives in CSS (read-only wave0 territory) behind
    // `prefers-reduced-motion: no-preference`; the React tree applies every
    // state change instantly, so the flow works with matchMedia absent.
    vi.stubGlobal('matchMedia', undefined);
    mockFetch(ENVELOPE);
    render(<App />);
    fireEvent.change(screen.getByLabelText(/search query/i), {
      target: { value: 'wireless headphones' },
    });
    fireEvent.submit(screen.getByRole('search'));
    await waitFor(() => {
      expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
    });
    expect(screen.getByTestId('result-announcer')).toHaveTextContent(
      '1 result for “wireless headphones”',
    );
  });
});
