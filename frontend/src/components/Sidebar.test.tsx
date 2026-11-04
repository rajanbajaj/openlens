import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { FILTER_DEBOUNCE_MS } from '../api/client.ts';
import type { ProviderPrefs } from '../api/types.ts';
import { Sidebar } from './Sidebar.tsx';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const EMPTY_PREFS: ProviderPrefs = { include: [], exclude: [], primary: null };

function setup(
  overrides: {
    prefs?: ProviderPrefs;
    knownProviders?: string[];
    disabled?: boolean;
    onLibraryOpen?: () => void;
  } = {},
) {
  const onPrefsChange = vi.fn();
  const onLibraryOpen = overrides.onLibraryOpen ?? vi.fn();
  render(
    <Sidebar
      prefs={overrides.prefs ?? EMPTY_PREFS}
      onPrefsChange={onPrefsChange}
      knownProviders={overrides.knownProviders ?? ['mock']}
      disabled={overrides.disabled}
      onLibraryOpen={onLibraryOpen}
    />,
  );
  return { onPrefsChange, onLibraryOpen };
}

describe('Sidebar utilities rail', () => {
  it('renders library shortcut, URL requests, and provider prefs without filters', () => {
    setup({ prefs: { include: [], exclude: [], primary: 'mock' } });

    expect(screen.getByTestId('sidebar')).toBeInTheDocument();
    expect(screen.getByTestId('sidebar-panel')).toBeInTheDocument();
    expect(screen.getByTestId('library-open')).toHaveTextContent('Open library');
    // No filter surface lives here anymore: QuickFilters owns filters.
    expect(screen.queryByTestId('sidebar-toggle')).not.toBeInTheDocument();
    expect(screen.queryByTestId('sidebar-panel')).not.toBeNull();
    expect(screen.queryByLabelText(/exclude sponsored/i)).not.toBeInTheDocument();
    expect(screen.queryByTestId('user-filter-chips')).not.toBeInTheDocument();
    expect(screen.queryByTestId('applied-filters')).not.toBeInTheDocument();
    // Provider prefs + URL request affordances remain.
    expect(screen.getByLabelText(/only these providers/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/exclude these providers/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/primary provider/i)).toHaveValue('mock');
    expect(screen.getByTestId('crawl-request-form')).toBeInTheDocument();
    expect(screen.getByTestId('crawl-request-submit')).toHaveTextContent('Request crawl');
  });

  it('opens the library through the shell hook', () => {
    const onLibraryOpen = vi.fn();
    setup({ onLibraryOpen });

    fireEvent.click(screen.getByTestId('library-open'));
    expect(onLibraryOpen).toHaveBeenCalledTimes(1);
  });

  it('prefs edits flow through the change callback after the debounce', () => {
    vi.useFakeTimers();
    try {
      const { onPrefsChange } = setup();
      const input = screen.getByLabelText(/primary provider/i);
      fireEvent.change(input, { target: { value: 'mock' } });
      expect(input).toHaveValue('mock');
      expect(onPrefsChange).not.toHaveBeenCalled();
      vi.advanceTimersByTime(FILTER_DEBOUNCE_MS);
      expect(onPrefsChange).toHaveBeenCalledTimes(1);
      expect(onPrefsChange).toHaveBeenCalledWith({
        include: [],
        exclude: [],
        primary: 'mock',
      });
    } finally {
      vi.useRealTimers();
    }
  });
});
