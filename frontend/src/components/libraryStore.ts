import type { SearchResult } from '../api/types.ts';
import { isSearchResult } from '../api/guard.ts';

const RECENT_KEY = 'openlens.library.recent';
const SAVED_KEY = 'openlens.library.saved';
const MAX_RECENT = 10;

function readKey(key: string): unknown {
  try {
    const raw = window.localStorage.getItem(key);
    if (raw === null) return null;
    return JSON.parse(raw) as unknown;
  } catch {
    return null;
  }
}

function writeKey(key: string, value: unknown): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Storage full or unavailable (private mode): library stays in memory only.
  }
}

/** Recent search queries, newest first; `[]` when absent or malformed. */
export function loadRecentSearches(): string[] {
  const raw = readKey(RECENT_KEY);
  if (!Array.isArray(raw)) return [];
  return raw.filter((item): item is string => typeof item === 'string' && item !== '');
}

/**
 * Record a query at the front of the recent list (deduplicated, capped).
 * Returns the updated list. Pure localStorage — never touches the network.
 */
export function recordRecentSearch(query: string): string[] {
  const trimmed = query.trim();
  if (trimmed === '') return loadRecentSearches();
  const next = [trimmed, ...loadRecentSearches().filter((item) => item !== trimmed)].slice(
    0,
    MAX_RECENT,
  );
  writeKey(RECENT_KEY, next);
  return next;
}

/** Drop every stored recent search. */
export function clearRecentSearches(): void {
  try {
    window.localStorage.removeItem(RECENT_KEY);
  } catch {
    // Unavailable storage: nothing to clear.
  }
}

/** Saved results; malformed entries are dropped, never thrown. */
export function loadSavedResults(): SearchResult[] {
  const raw = readKey(SAVED_KEY);
  if (!Array.isArray(raw)) return [];
  return raw.filter(isSearchResult);
}

/** Persist the full saved list (local-only). */
function storeSavedResults(results: SearchResult[]): void {
  writeKey(SAVED_KEY, results);
}

/**
 * Save a hit (replacing any entry with the same id). Returns the list.
 */
export function saveResult(result: SearchResult): SearchResult[] {
  const next = [result, ...loadSavedResults().filter((item) => item.id !== result.id)];
  storeSavedResults(next);
  return next;
}

/** Remove one saved hit by id. Returns the remaining list. */
export function removeSavedResult(id: string): SearchResult[] {
  const next = loadSavedResults().filter((item) => item.id !== id);
  storeSavedResults(next);
  return next;
}

/** Drop every saved result. */
export function clearSavedResults(): void {
  try {
    window.localStorage.removeItem(SAVED_KEY);
  } catch {
    // Unavailable storage: nothing to clear.
  }
}
