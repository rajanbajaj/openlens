import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useState } from 'react';
import SearchForm from './SearchForm.tsx';
import SuggestDropdown, { moveSuggestHighlight } from './SuggestDropdown.tsx';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function suggestResponse(q: string, suggestions: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve({ q, suggestions }),
  } as unknown as Response;
}

function stubFetch(impl: (url: unknown, init?: RequestInit) => Promise<Response>) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown, init?: RequestInit) => impl(url, init)),
  );
}

function renderForm(
  options: {
    initialValue?: string;
    onSuggestSelect?: (suggestion: string) => void;
  } = {},
) {
  const onSubmit = vi.fn();
  const onChange = vi.fn();
  const onSuggestSelect = options.onSuggestSelect ? vi.fn(options.onSuggestSelect) : undefined;
  function Harness() {
    const [value, setValue] = useState(options.initialValue ?? '');
    return (
      <SearchForm
        value={value}
        onChange={(next) => {
          onChange(next);
          setValue(next);
        }}
        onSubmit={onSubmit}
        onSuggestSelect={onSuggestSelect}
      />
    );
  }
  render(<Harness />);
  const input = screen.getByLabelText('Search query');
  return { input, onSubmit, onChange, onSuggestSelect };
}

function typeQuery(input: HTMLElement, query: string) {
  fireEvent.change(input, { target: { value: query } });
}

describe('moveSuggestHighlight', () => {
  it('lands ArrowDown on the first option from no highlight', () => {
    expect(moveSuggestHighlight(-1, 'ArrowDown', 3)).toBe(0);
  });

  it('lands ArrowUp on the last option from no highlight', () => {
    expect(moveSuggestHighlight(-1, 'ArrowUp', 3)).toBe(2);
  });

  it('wraps at both ends', () => {
    expect(moveSuggestHighlight(2, 'ArrowDown', 3)).toBe(0);
    expect(moveSuggestHighlight(0, 'ArrowUp', 3)).toBe(2);
  });

  it('steps through the middle without wrapping', () => {
    expect(moveSuggestHighlight(0, 'ArrowDown', 3)).toBe(1);
    expect(moveSuggestHighlight(2, 'ArrowUp', 3)).toBe(1);
  });

  it('stays unhighlighted when there are no options', () => {
    expect(moveSuggestHighlight(-1, 'ArrowDown', 0)).toBe(-1);
    expect(moveSuggestHighlight(1, 'ArrowUp', 0)).toBe(-1);
  });
});

describe('SuggestDropdown silence', () => {
  it('renders nothing when empty and idle', () => {
    render(
      <SuggestDropdown suggestions={[]} loading={false} listId="suggest" onSelect={() => {}} />,
    );
    expect(screen.queryByTestId('suggest-dropdown')).not.toBeInTheDocument();
  });

  it('renders the loading line while fetching with no suggestions yet', () => {
    render(<SuggestDropdown suggestions={[]} loading listId="suggest" onSelect={() => {}} />);
    expect(screen.getByTestId('suggest-dropdown')).toBeInTheDocument();
    expect(screen.getByTestId('suggest-loading')).toHaveTextContent('Loading suggestions…');
  });
});

describe('keyboard map', () => {
  it('ArrowDown/ArrowUp move the highlight and expose it on the input', async () => {
    stubFetch(async () =>
      suggestResponse('wireless', ['wireless headphones', 'wireless charger', 'wireless earbuds']),
    );
    const { input } = renderForm();
    typeQuery(input, 'wireless');
    await screen.findByTestId('suggest-option-2');

    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(input).toHaveAttribute('aria-activedescendant', 'openlens-suggest-listbox-option-0');
    expect(screen.getByTestId('suggest-option-0')).toHaveAttribute('aria-selected', 'true');

    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(input).toHaveAttribute('aria-activedescendant', 'openlens-suggest-listbox-option-1');

    fireEvent.keyDown(input, { key: 'ArrowUp' });
    expect(input).toHaveAttribute('aria-activedescendant', 'openlens-suggest-listbox-option-0');
  });

  it('ArrowUp from no highlight wraps to the last option', async () => {
    stubFetch(async () => suggestResponse('wireless', ['alpha', 'beta']));
    const { input } = renderForm();
    typeQuery(input, 'wireless');
    await screen.findByTestId('suggest-option-1');

    fireEvent.keyDown(input, { key: 'ArrowUp' });
    expect(input).toHaveAttribute('aria-activedescendant', 'openlens-suggest-listbox-option-1');
    expect(screen.getByTestId('suggest-option-1')).toHaveAttribute('aria-selected', 'true');
  });

  it('Enter picks the highlighted suggestion and submits it', async () => {
    stubFetch(async () => suggestResponse('wireless', ['wireless headphones', 'wireless charger']));
    const { input, onSubmit } = renderForm();
    typeQuery(input, 'wireless');
    await screen.findByTestId('suggest-option-1');

    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onSubmit).toHaveBeenCalledWith('wireless headphones');
    // The picked query becomes the input value, so suggestions refetch
    // for it; the submit itself already fired with the picked text.
    await screen.findByTestId('suggest-option-1');
  });

  it('Enter without a highlight submits the typed query directly', async () => {
    stubFetch(async () => suggestResponse('wireless', ['wireless headphones']));
    const { input, onSubmit } = renderForm();
    typeQuery(input, 'wireless');
    await screen.findByTestId('suggest-option-0');

    // No suggestion is highlighted, so the key handler leaves the
    // event alone and the native form submit carries the typed value
    // (jsdom needs the explicit submit for the native half).
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.submit(screen.getByRole('search'));
    expect(onSubmit).toHaveBeenCalledWith('wireless');
  });

  it('Escape closes the dropdown without submitting', async () => {
    stubFetch(async () => suggestResponse('wireless', ['wireless headphones']));
    const { input, onSubmit } = renderForm();
    typeQuery(input, 'wireless');
    await screen.findByTestId('suggest-option-0');

    fireEvent.keyDown(input, { key: 'Escape' });
    expect(screen.queryByTestId('suggest-dropdown')).not.toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('routes picks through onSuggestSelect when the hook is provided', async () => {
    stubFetch(async () => suggestResponse('wireless', ['wireless headphones']));
    const { input, onSubmit, onSuggestSelect } = renderForm({ onSuggestSelect: () => {} });
    typeQuery(input, 'wireless');
    await screen.findByTestId('suggest-option-0');

    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onSuggestSelect).toHaveBeenCalledWith('wireless headphones');
    expect(onSubmit).not.toHaveBeenCalled();
  });
});

describe('stale-abort', () => {
  it('a slow first fetch loses to the latest query', async () => {
    const resolvers: Array<(response: Response) => void> = [];
    const signals: Array<AbortSignal | undefined> = [];
    stubFetch(async (_url, init) => {
      signals.push(init?.signal as AbortSignal | undefined);
      return new Promise<Response>((resolve) => {
        resolvers.push(resolve);
      });
    });

    const { input } = renderForm();
    typeQuery(input, 'wireless');
    // Let the debounce elapse so the first fetch is in flight.
    await waitFor(() => {
      expect(resolvers).toHaveLength(1);
    });

    typeQuery(input, 'wireless headphones');
    await waitFor(() => {
      expect(resolvers).toHaveLength(2);
    });

    // The stale request is aborted when the fresh one starts.
    expect(signals[0]?.aborted).toBe(true);

    // Resolve out of order: fresh first, then the slow stale one.
    resolvers[1]?.(suggestResponse('wireless headphones', ['fresh match']));
    await screen.findByText('fresh match');
    resolvers[0]?.(suggestResponse('wireless', ['stale match']));
    // Give the stale response a chance to clobber the list.
    await screen.findByText('fresh match');
    expect(screen.queryByText('stale match')).not.toBeInTheDocument();
    expect(screen.getByTestId('suggest-option-0')).toHaveTextContent('fresh match');
  });
});

describe('backend-absent degradation', () => {
  it('a 404 suggest backend hides the dropdown but search still submits', async () => {
    stubFetch(async () => suggestResponse('wireless', {}, 404));
    const { input, onSubmit } = renderForm();
    typeQuery(input, 'wireless');
    // Wait past the debounce so the (failed) fetch has settled.
    await waitFor(
      () => {
        expect(vi.mocked(fetch)).toHaveBeenCalled();
      },
      { timeout: 2000 },
    );
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(screen.queryByTestId('suggest-dropdown')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    expect(onSubmit).toHaveBeenCalledWith('wireless');
  });

  it('a malformed suggest payload degrades to silence', async () => {
    stubFetch(async () => suggestResponse('wireless', ['ok', 42]));
    const { input } = renderForm();
    typeQuery(input, 'wireless');
    await waitFor(
      () => {
        expect(vi.mocked(fetch)).toHaveBeenCalled();
      },
      { timeout: 2000 },
    );
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(screen.queryByTestId('suggest-dropdown')).not.toBeInTheDocument();
  });

  it('short queries never fetch and render nothing', async () => {
    const fetchMock = vi.fn(async () => suggestResponse('w', ['w']));
    vi.stubGlobal('fetch', fetchMock);
    const { input } = renderForm();
    typeQuery(input, 'w');
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.queryByTestId('suggest-dropdown')).not.toBeInTheDocument();
  });
});

describe('announcer', () => {
  it('names the result count once suggestions arrive', async () => {
    stubFetch(async () => suggestResponse('wireless', ['wireless headphones', 'wireless charger']));
    const { input } = renderForm();
    typeQuery(input, 'wireless');
    await screen.findByTestId('suggest-option-1');
    expect(screen.getByTestId('suggest-announcer')).toHaveTextContent('2 suggestions available');
  });

  it('uses the singular for a single suggestion', async () => {
    stubFetch(async () => suggestResponse('wireless', ['wireless headphones']));
    const { input } = renderForm();
    typeQuery(input, 'wireless');
    await screen.findByTestId('suggest-option-0');
    expect(screen.getByTestId('suggest-announcer')).toHaveTextContent('1 suggestion available');
  });

  it('announces loading while the fetch is in flight', async () => {
    let resolveFetch!: (response: Response) => void;
    stubFetch(
      async () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        }),
    );
    const { input } = renderForm();
    typeQuery(input, 'wireless');
    // Wait past the debounce so the fetch is actually in flight.
    await waitFor(() => {
      expect(vi.mocked(fetch)).toHaveBeenCalled();
    });
    await screen.findByTestId('suggest-loading');
    expect(screen.getByTestId('suggest-announcer')).toHaveTextContent('Loading suggestions…');
    resolveFetch(suggestResponse('wireless', ['wireless headphones']));
    await screen.findByTestId('suggest-option-0');
  });
});
