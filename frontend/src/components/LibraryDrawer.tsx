import { useState } from 'react';
import type { SearchResult } from '../api/types';
import {
  clearRecentSearches,
  clearSavedResults,
  loadRecentSearches,
  loadSavedResults,
  removeSavedResult,
} from './libraryStore.ts';
import { Button } from './ui/Button.tsx';

// Storage helpers live in `./libraryStore.ts` so the shell can persist
// recents/saves without eagerly importing this drawer component.
// Re-exported here so existing `./LibraryDrawer.tsx` importers keep working.
export {
  clearRecentSearches,
  clearSavedResults,
  loadRecentSearches,
  loadSavedResults,
  recordRecentSearch,
  removeSavedResult,
  saveResult,
} from './libraryStore.ts';

export interface LibraryDrawerProps {
  /** Called when a recent search is re-run. */
  onSelectSearch?: (query: string) => void;
}

/**
 * Personal library: recent searches plus saved results, both persisted
 * in `localStorage` only. Fully offline — no fetch, no identifiers, and
 * nothing is ever sent to the backend. Both sections are clearable and
 * every section degrades to an empty note when storage is unavailable
 * or holds malformed data.
 */
export function LibraryDrawer({ onSelectSearch }: LibraryDrawerProps) {
  const [open, setOpen] = useState(false);
  const [recent, setRecent] = useState<string[]>(() => loadRecentSearches());
  const [saved, setSaved] = useState<SearchResult[]>(() => loadSavedResults());

  function toggle() {
    if (!open) {
      // Re-read on open so saves recorded elsewhere show up.
      setRecent(loadRecentSearches());
      setSaved(loadSavedResults());
    }
    setOpen((v) => !v);
  }

  function handleClearRecent() {
    clearRecentSearches();
    setRecent([]);
  }

  function handleClearSaved() {
    clearSavedResults();
    setSaved([]);
  }

  function handleRemoveSaved(id: string) {
    setSaved(removeSavedResult(id));
  }

  return (
    <section
      aria-label="Library"
      className="ol-drawer mt-ol-l rounded-ol-l border border-ol-line bg-white px-ol-m py-ol-l"
    >
      <Button
        variant="ghost"
        data-testid="library-toggle"
        aria-expanded={open}
        aria-controls="library-panel"
        onClick={toggle}
      >
        {open ? 'Hide library' : 'Show library'}
      </Button>
      {open ? (
        <div id="library-panel" data-testid="library-panel">
          <h4 className="mt-ol-m mb-ol-s text-ol-m">Recent searches</h4>
          {recent.length === 0 ? (
            <p data-testid="library-recent-empty" className="my-ol-s text-ol-s">
              No recent searches yet.
            </p>
          ) : (
            <>
              <ul
                data-testid="library-recent"
                className="flex flex-col gap-ol-xs my-ol-s p-0 list-none"
              >
                {recent.map((query) => (
                  <li key={query} data-testid="library-recent-item">
                    <Button variant="ghost" onClick={() => onSelectSearch?.(query)}>
                      {query}
                    </Button>
                  </li>
                ))}
              </ul>
              <Button
                variant="ghost"
                data-testid="library-clear-recent"
                onClick={handleClearRecent}
              >
                Clear recent searches
              </Button>
            </>
          )}
          <h4 className="mt-ol-m mb-ol-s text-ol-m">Saved results ({saved.length})</h4>
          {saved.length === 0 ? (
            <p data-testid="library-saved-empty" className="my-ol-s text-ol-s">
              No saved results yet.
            </p>
          ) : (
            <>
              <ul
                data-testid="library-saved"
                className="flex flex-col gap-ol-xs my-ol-s p-0 list-none"
              >
                {saved.map((result) => (
                  <li key={result.id} data-testid="library-saved-item">
                    <span>{result.title}</span>{' '}
                    <Button
                      variant="ghost"
                      data-testid="library-remove-saved"
                      aria-label={`Remove ${result.title} from library`}
                      onClick={() => handleRemoveSaved(result.id)}
                    >
                      Remove
                    </Button>
                  </li>
                ))}
              </ul>
              <Button variant="ghost" data-testid="library-clear-saved" onClick={handleClearSaved}>
                Clear saved results
              </Button>
            </>
          )}
        </div>
      ) : null}
    </section>
  );
}
