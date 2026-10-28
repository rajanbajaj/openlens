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
import { Button } from './components/ui/Button.tsx';
import { GlassPanel } from './components/ui/GlassPanel.tsx';
import ModeSelector from './components/ModeSelector.tsx';
import { ResultList } from './components/ResultList.tsx';
import { Sidebar } from './components/Sidebar.tsx';
import SearchForm from './components/SearchForm.tsx';
import { ErrorState, LoadingState } from './components/States.tsx';

const DiagnosticsRail = lazy(() =>
  import('./components/DiagnosticsDrawer.tsx').then((module) => ({
    default: module.DiagnosticsDrawer,
  })),
);

const PAGE_SIZE = DEFAULT_LIMIT;

const EMPTY_PREFS: ProviderPrefs = { include: [], exclude: [], primary: null };

function prefsActive(prefs: ProviderPrefs): boolean {
  return prefs.include.length > 0 || prefs.exclude.length > 0 || prefs.primary !== null;
}

/**
 * Root search experience: an empty-state hero glass panel, then a compact
 * sticky frosted header once the first search runs — both reuse the same
 * `SearchForm` + `ModeSelector` (exactly one mounted at a time). Below sits
 * the `.ol-app-shell` grid: collapsible filter sidebar (chips, toggle,
 * panel, provider prefs, applied filters), results column (announcer,
 * loading/error branches, suggestion chip, results, pagination), and the
 * lazily loaded diagnostics rail. Search logic, announcements, and
 * `data-testid` hooks are unchanged.
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
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

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

  function submitFirstPage(searchQuery: string) {
    void runSearch(1, searchQuery, mode, userFilters, prefs);
  }

  function handleChipRemove(cleared: UserFilterState) {
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
      {hasSearched ? (
        <GlassPanel variant="header" role="banner" data-testid="search-header">
          <div className="ol-header-inner">
            <h1 className="ol-brand">OpenLens</h1>
            <SearchForm
              value={query}
              onChange={setQuery}
              onSubmit={submitFirstPage}
              disabled={loading}
            />
            <ModeSelector mode={mode} onChange={setMode} disabled={loading} />
          </div>
        </GlassPanel>
      ) : null}
      <main>
        {!hasSearched ? (
          <div className="ol-workspace">
            <GlassPanel variant="hero" data-testid="search-hero">
              <h1 className="ol-brand">OpenLens</h1>
              <p className="ol-brand-sub">Plug-and-play search</p>
              <SearchForm
                value={query}
                onChange={setQuery}
                onSubmit={submitFirstPage}
                disabled={loading}
              />
              <ModeSelector mode={mode} onChange={setMode} disabled={loading} />
            </GlassPanel>
          </div>
        ) : null}
        <div className="ol-app-shell">
          <aside
            className="ol-shell-sidebar"
            data-testid="sidebar-mount"
            aria-label="Search filters"
          >
            <Sidebar
              filters={userFilters}
              onFiltersChange={setUserFilters}
              prefs={prefs}
              onPrefsChange={setPrefs}
              knownProviders={knownProviders}
              appliedFilters={response?.applied_filters ?? []}
              disabled={loading}
              collapsed={sidebarCollapsed}
              onCollapsedChange={setSidebarCollapsed}
              onChipRemove={handleChipRemove}
            />
          </aside>
          <div className="ol-shell-results">
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
              </section>
            ) : null}
            {!loading && !error && !hasSearched ? <p>Enter a query to search.</p> : null}
          </div>
          <div className="ol-shell-rail">
            {!loading && !error && response ? (
              <Suspense fallback={<p>Loading diagnostics…</p>}>
                <DiagnosticsRail diagnostics={response.diagnostics} />
              </Suspense>
            ) : null}
          </div>
        </div>
      </main>
    </div>
  );
}
