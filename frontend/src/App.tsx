import { Suspense, lazy, useState } from 'react';
import {
  DEFAULT_LIMIT,
  buildSearchBody,
  offsetForPage,
  searchLens,
  SearchApiError,
  totalPages,
  validateQuery,
} from './api/client.ts';
import { EMPTY_FILTER_STATE, buildFilters, type UserFilterState } from './api/filters.ts';
import type { ErrorDetail, ProviderPrefs, SearchMode, SearchResponse } from './api/types.ts';
import { AppliedFilters } from './components/AppliedFilters.tsx';
import FilterToggle from './components/FilterToggle.tsx';
import { FilterPanel } from './components/FilterPanel.tsx';
import { Button } from './components/ui/Button.tsx';
import { Chip } from './components/ui/Chip.tsx';
import ModeSelector from './components/ModeSelector.tsx';
import { ProviderPrefsControl } from './components/ProviderPrefsControl.tsx';
import { ResultList } from './components/ResultList.tsx';
import SearchForm from './components/SearchForm.tsx';
import { ErrorState, LoadingState } from './components/States.tsx';

const DiagnosticsRail = lazy(() =>
  import('./components/DiagnosticsDrawer.tsx').then((module) => ({
    default: module.DiagnosticsDrawer,
  })),
);

const PAGE_SIZE = DEFAULT_LIMIT;

const EMPTY_PREFS: ProviderPrefs = { include: [], exclude: [], primary: null };

interface ActiveChip {
  key: string;
  label: string;
  clear: (filters: UserFilterState) => UserFilterState;
}

/** User-set filters rendered as removable chips with per-filter attribution. */
function activeChips(filters: UserFilterState): ActiveChip[] {
  const chips: ActiveChip[] = [];
  if (filters.excludeSponsored) {
    chips.push({
      key: 'excludeSponsored',
      label: 'Excludes sponsored',
      clear: (next) => ({ ...next, excludeSponsored: false }),
    });
  }
  if (filters.maxPrice.trim()) {
    chips.push({
      key: 'maxPrice',
      label: `Max price ${filters.maxPrice.trim()}`,
      clear: (next) => ({ ...next, maxPrice: '' }),
    });
  }
  if (filters.location.trim()) {
    chips.push({
      key: 'location',
      label: `Location: ${filters.location.trim()}`,
      clear: (next) => ({ ...next, location: '' }),
    });
  }
  if (filters.facilities.trim()) {
    chips.push({
      key: 'facilities',
      label: `Facilities: ${filters.facilities.trim()}`,
      clear: (next) => ({ ...next, facilities: '' }),
    });
  }
  if (filters.preferSuffix.trim()) {
    chips.push({
      key: 'preferSuffix',
      label: `Prefers ${filters.preferSuffix.trim()}`,
      clear: (next) => ({ ...next, preferSuffix: '' }),
    });
  }
  if (filters.blockedDomain.trim()) {
    chips.push({
      key: 'blockedDomain',
      label: `Blocked: ${filters.blockedDomain.trim()}`,
      clear: (next) => ({ ...next, blockedDomain: '' }),
    });
  }
  if (filters.allowedDomain.trim()) {
    chips.push({
      key: 'allowedDomain',
      label: `Only: ${filters.allowedDomain.trim()}`,
      clear: (next) => ({ ...next, allowedDomain: '' }),
    });
  }
  if (filters.mustNotTerm.trim()) {
    chips.push({
      key: 'mustNotTerm',
      label: `Excludes “${filters.mustNotTerm.trim()}”`,
      clear: (next) => ({ ...next, mustNotTerm: '' }),
    });
  }
  return chips;
}

function prefsActive(prefs: ProviderPrefs): boolean {
  return prefs.include.length > 0 || prefs.exclude.length > 0 || prefs.primary !== null;
}

/**
 * Root search experience: query form, mode selector, filter panel, and
 * provider preferences wired into `POST /api/v1/search`, rendering
 * results with transparency surfaces, pagination, and the lazily
 * loaded diagnostics rail.
 */
export default function App() {
  const [query, setQuery] = useState('');
  const [mode, setMode] = useState<SearchMode>('default');
  const [userFilters, setUserFilters] = useState(EMPTY_FILTER_STATE);
  const [prefs, setPrefs] = useState<ProviderPrefs>(EMPTY_PREFS);
  const [page, setPage] = useState(1);
  const [response, setResponse] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<ErrorDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);
  const [announcement, setAnnouncement] = useState('');

  async function runSearch(
    nextPage: number,
    searchQuery: string,
    searchMode: SearchMode,
    filters: UserFilterState,
    providerPrefs: ProviderPrefs,
  ) {
    const message = validateQuery(searchQuery);
    if (message !== null) return;
    setLoading(true);
    setError(null);
    try {
      const body = buildSearchBody({
        query: searchQuery,
        mode: searchMode,
        limit: PAGE_SIZE,
        offset: offsetForPage(nextPage, PAGE_SIZE),
        filters: buildFilters(filters),
        providerPrefs: prefsActive(providerPrefs) ? providerPrefs : null,
      });
      const payload = await searchLens(body);
      setResponse(payload);
      setPage(nextPage);
      setHasSearched(true);
      setAnnouncement(
        `${payload.total} result${payload.total === 1 ? '' : 's'} for “${searchQuery}”`,
      );
    } catch (err) {
      if (err instanceof SearchApiError) {
        setError({ code: err.code, message: err.message });
      } else {
        setError({ code: 'unknown_error', message: 'Unexpected search failure.' });
      }
      setHasSearched(true);
    } finally {
      setLoading(false);
    }
  }

  const pages = response ? totalPages(response.total, PAGE_SIZE) : 0;

  function goToPage(nextPage: number) {
    void runSearch(nextPage, query, mode, userFilters, prefs);
  }

  function removeChip(chip: ActiveChip) {
    const cleared = chip.clear(userFilters);
    setUserFilters(cleared);
    if (hasSearched) void runSearch(1, query, mode, cleared, prefs);
  }

  const suggestion = response?.diagnostics.query?.suggested_mode;
  const showSuggestion =
    !loading && !error && response && suggestion && suggestion !== mode ? suggestion : null;

  const knownProviders = response
    ? Array.from(
        new Set([
          ...Object.keys(response.diagnostics.provider_latencies_ms),
          ...response.results.map((result) => result.provider),
        ]),
      ).sort()
    : [];

  return (
    <div className="ol-shell">
      <header className="ol-header">
        <div className="ol-header-inner">
          <h1 className="ol-brand">OpenLens</h1>
          <p className="ol-brand-sub">Plug-and-play search</p>
        </div>
      </header>
      <main className="ol-workspace">
        <SearchForm
          value={query}
          onChange={setQuery}
          onSubmit={(q) => {
            void runSearch(1, q, mode, userFilters, prefs);
          }}
          disabled={loading}
        />
        <ModeSelector mode={mode} onChange={setMode} disabled={loading} />
        <FilterToggle
          checked={userFilters.excludeSponsored}
          onChange={(checked) => setUserFilters({ ...userFilters, excludeSponsored: checked })}
          disabled={loading}
        />
        <FilterPanel value={userFilters} onChange={setUserFilters} disabled={loading} />
        <ProviderPrefsControl
          value={prefs}
          onChange={setPrefs}
          knownProviders={knownProviders}
          disabled={loading}
        />
        <p aria-live="polite" data-testid="result-announcer" className="ol-sr-only">
          {announcement}
        </p>
        {loading ? <LoadingState /> : null}
        {!loading && error ? <ErrorState code={error.code} message={error.message} /> : null}
        {!loading && !error && response ? (
          <section aria-label="Search output">
            {showSuggestion ? (
              <p data-testid="mode-suggestion">
                <Button
                  variant="ghost"
                  onClick={() => {
                    setMode(showSuggestion as SearchMode);
                    void runSearch(1, query, showSuggestion as SearchMode, userFilters, prefs);
                  }}
                >
                  Try {showSuggestion} mode
                </Button>
              </p>
            ) : null}
            {activeChips(userFilters).length > 0 ? (
              <p data-testid="user-filter-chips">
                {activeChips(userFilters).map((chip) => (
                  <Chip
                    key={chip.key}
                    onRemove={() => removeChip(chip)}
                    removeLabel={`Remove ${chip.label}`}
                  >
                    {chip.label}
                  </Chip>
                ))}
              </p>
            ) : null}
            <AppliedFilters appliedFilters={response.applied_filters} />
            <ResultList results={response.results} total={response.total} />
            {pages > 1 ? (
              <nav aria-label="Pagination" className="ol-pagination">
                <Button disabled={page <= 1} onClick={() => goToPage(page - 1)}>
                  Previous
                </Button>
                <span data-testid="page-indicator">
                  Page {page} of {pages}
                </span>
                <Button disabled={page >= pages} onClick={() => goToPage(page + 1)}>
                  Next
                </Button>
              </nav>
            ) : null}
            <Suspense fallback={<p>Loading diagnostics…</p>}>
              <DiagnosticsRail diagnostics={response.diagnostics} />
            </Suspense>
          </section>
        ) : null}
        {!loading && !error && !hasSearched ? <p>Enter a query to search.</p> : null}
      </main>
    </div>
  );
}
