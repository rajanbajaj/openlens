import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { SearchResult } from '../api/types.ts';
import {
  LibraryDrawer,
  clearRecentSearches,
  clearSavedResults,
  loadRecentSearches,
  loadSavedResults,
  recordRecentSearch,
  removeSavedResult,
  saveResult,
} from './LibraryDrawer.tsx';

const RECENT_KEY = 'openlens.library.recent';
const SAVED_KEY = 'openlens.library.saved';

const SAVED_HIT: SearchResult = {
  id: 's1',
  title: 'Saved Headphones',
  snippet: 'Great sound',
  original_score: 1,
  final_score: 2,
  url: 'https://example.com/h',
  provider: 'custom_index',
  reasons: [],
  warnings: [],
  metadata: { price: 199.99, currency: 'USD' },
};

beforeEach(() => {
  window.localStorage.removeItem(RECENT_KEY);
  window.localStorage.removeItem(SAVED_KEY);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function openDrawer(onSelectSearch?: (query: string) => void) {
  render(<LibraryDrawer onSelectSearch={onSelectSearch} />);
  fireEvent.click(screen.getByTestId('library-toggle'));
}

describe('recent searches storage', () => {
  it('round-trips recorded queries newest-first with no duplicates', () => {
    expect(recordRecentSearch('  hotels  ')).toEqual(['hotels']);
    expect(recordRecentSearch('flights')).toEqual(['flights', 'hotels']);
    expect(recordRecentSearch('hotels')).toEqual(['hotels', 'flights']);
    expect(loadRecentSearches()).toEqual(['hotels', 'flights']);
  });

  it('caps the recent list and ignores blank queries', () => {
    for (let i = 0; i < 12; i += 1) recordRecentSearch(`q${i}`);
    const recent = loadRecentSearches();
    expect(recent).toHaveLength(10);
    expect(recent[0]).toBe('q11');
    recordRecentSearch('   ');
    expect(loadRecentSearches()).toHaveLength(10);
  });

  it('clears recent searches from storage', () => {
    recordRecentSearch('hotels');
    clearRecentSearches();
    expect(loadRecentSearches()).toEqual([]);
    expect(window.localStorage.getItem(RECENT_KEY)).toBeNull();
  });

  it('degrades to empty on malformed storage', () => {
    window.localStorage.setItem(RECENT_KEY, '{"not": "an array"}');
    expect(loadRecentSearches()).toEqual([]);
    window.localStorage.setItem(RECENT_KEY, '[1, null, "ok"]');
    expect(loadRecentSearches()).toEqual(['ok']);
  });
});

describe('saved results storage', () => {
  it('round-trips saved hits and replaces same-id entries', () => {
    expect(saveResult(SAVED_HIT)).toHaveLength(1);
    expect(loadSavedResults()[0]?.title).toBe('Saved Headphones');
    const updated = { ...SAVED_HIT, title: 'Updated' };
    expect(saveResult(updated)).toHaveLength(1);
    expect(loadSavedResults()[0]?.title).toBe('Updated');
  });

  it('removes one hit and clears the rest', () => {
    saveResult(SAVED_HIT);
    saveResult({ ...SAVED_HIT, id: 's2', title: 'Second' });
    expect(removeSavedResult('s1').map((hit) => hit.id)).toEqual(['s2']);
    clearSavedResults();
    expect(loadSavedResults()).toEqual([]);
  });

  it('drops malformed entries instead of throwing', () => {
    window.localStorage.setItem(SAVED_KEY, JSON.stringify([{ id: 'bad' }, SAVED_HIT, null]));
    expect(loadSavedResults().map((hit) => hit.id)).toEqual(['s1']);
  });
});

describe('LibraryDrawer', () => {
  it('renders stored recent searches and saved results after opening', () => {
    recordRecentSearch('harbor hotels');
    saveResult(SAVED_HIT);
    openDrawer();
    expect(screen.getByTestId('library-panel')).toBeInTheDocument();
    expect(screen.getByTestId('library-recent')).toHaveTextContent('harbor hotels');
    expect(screen.getByTestId('library-saved')).toHaveTextContent('Saved Headphones');
  });

  it('shows empty notes when the library is empty', () => {
    openDrawer();
    expect(screen.getByTestId('library-recent-empty')).toBeInTheDocument();
    expect(screen.getByTestId('library-saved-empty')).toBeInTheDocument();
  });

  it('re-runs a recent search through the selection callback', () => {
    const seen: string[] = [];
    recordRecentSearch('harbor hotels');
    openDrawer((query) => {
      seen.push(query);
    });
    fireEvent.click(within(screen.getByTestId('library-recent')).getByRole('button'));
    expect(seen).toEqual(['harbor hotels']);
  });

  it('clears each section from the UI and storage', () => {
    recordRecentSearch('harbor hotels');
    saveResult(SAVED_HIT);
    openDrawer();
    fireEvent.click(screen.getByTestId('library-clear-recent'));
    expect(screen.getByTestId('library-recent-empty')).toBeInTheDocument();
    expect(loadRecentSearches()).toEqual([]);
    fireEvent.click(screen.getByTestId('library-clear-saved'));
    expect(screen.getByTestId('library-saved-empty')).toBeInTheDocument();
    expect(loadSavedResults()).toEqual([]);
  });

  it('removes a single saved result from the UI and storage', () => {
    saveResult(SAVED_HIT);
    openDrawer();
    fireEvent.click(screen.getByTestId('library-remove-saved'));
    expect(screen.getByTestId('library-saved-empty')).toBeInTheDocument();
    expect(loadSavedResults()).toEqual([]);
  });

  it('works fully offline: renders storage content without any fetch', () => {
    const fetchSpy = vi.fn().mockRejectedValue(new Error('offline'));
    vi.stubGlobal('fetch', fetchSpy);
    window.localStorage.setItem(RECENT_KEY, JSON.stringify(['harbor hotels']));
    window.localStorage.setItem(SAVED_KEY, JSON.stringify([SAVED_HIT]));
    openDrawer();
    expect(screen.getByTestId('library-recent-item')).toHaveTextContent('harbor hotels');
    expect(screen.getByTestId('library-saved-item')).toHaveTextContent('Saved Headphones');
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('toggles closed with Escape-friendly disclosure semantics', () => {
    openDrawer();
    const toggle = screen.getByTestId('library-toggle');
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(toggle).toHaveAttribute('aria-controls', 'library-panel');
    fireEvent.click(toggle);
    expect(screen.queryByTestId('library-panel')).not.toBeInTheDocument();
  });
});
