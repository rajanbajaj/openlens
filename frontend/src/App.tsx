import { Suspense, lazy, useEffect, useRef, useState } from 'react';
import {
  DEFAULT_LIMIT,
  buildSearchBody,
  offsetForPage,
  prefetchPage,
  searchLens,
  SearchApiError,
  totalPages,
  validateQuery,
} from './api/client.ts';
import { EMPTY_FILTER_STATE, buildFilters, type UserFilterState } from './api/filters.ts';
import type {
  ErrorDetail,
  ProviderPrefs,
  SearchMode,
  SearchRequest,
  SearchResponse,
  SearchResult,
} from './api/types.ts';
import { readRelated, readSpelling } from './api/guard.ts';
import {
  loadSavedResults,
  recordRecentSearch,
  removeSavedResult,
  saveResult,
} from './components/libraryStore.ts';
import { Button } from './components/ui/Button.tsx';
import { GlassPanel } from './components/ui/GlassPanel.tsx';
import ModeSelector from './components/ModeSelector.tsx';
import { Pagination } from './components/Pagination.tsx';
import { ResultList } from './components/ResultList.tsx';
import { Sidebar } from './components/Sidebar.tsx';
import SearchForm from './components/SearchForm.tsx';
import { ErrorState, LoadingState } from './components/States.tsx';

const DiagnosticsRail = lazy(() =>
  import('./components/DiagnosticsDrawer.tsx').then((module) => ({
    default: module.DiagnosticsDrawer,
  })),
);

// Personal library stays out of the main chunk: same lazy pattern as the
// diagnostics rail, so opening the library never costs first-search bytes.
const LibraryDrawer = lazy(() =>
  import('./components/LibraryDrawer.tsx').then((module) => ({
    default: module.LibraryDrawer,
  })),
);

const PAGE_SIZE = DEFAULT_LIMIT;

const EMPTY_PREFS: ProviderPrefs = { include: [], exclude: [], primary: null };

function prefsActive(prefs: ProviderPrefs): boolean {
  return prefs.include.length > 0 || prefs.exclude.length > 0 || prefs.primary !== null;
}

/**
 * Root search experience, Google-style: a centered hero glass panel
 * (brand + `SearchForm` + `ModeSelector`, the only thing rendered) until
 * the first search runs, then a compact sticky frosted header plus the
 * `.ol-app-shell` grid — collapsible filter sidebar (chips, toggle,
 * panel, provider prefs, applied filters), results column (announcer,
 * loading/error branches, suggestion chip, results, pagination), and
 * the lazily loaded diagnostics rail. Both forms reuse the same
 * components (exactly one mounted at a time). Search logic,
 * announcements, and `data-testid` hooks are unchanged.
 */
export default function App() {
  const [query, setQuery] = useState('');
  const [mode, setMode] = useState<SearchMode>('default');
  const [userFilters, setUserFilters] = useState(EMPTY_FILTER_STATE);
  const [prefs, setPrefs] = useState<ProviderPrefs>(EMPTY_PREFS);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(PAGE_SIZE);
  const [response, setResponse] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<ErrorDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);
  const [announcement, setAnnouncement] = useState('');
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [savedIds, setSavedIds] = useState<string[]>(() =>
    loadSavedResults().map((entry) => entry.id),
  );
  const lastBody = useRef<SearchRequest | null>(null);

  async function runSearch(
    nextPage: number,
    searchQuery: string,
    searchMode: SearchMode,
    filters: UserFilterState,
    providerPrefs: ProviderPrefs,
    size: number = pageSize,
  ) {
    const message = validateQuery(searchQuery);
    if (message !== null) return;
    setLoading(true);
    setError(null);
    try {
      const body = buildSearchBody({
        query: searchQuery,
        mode: searchMode,
        limit: size,
        offset: offsetForPage(nextPage, size),
        filters: buildFilters(filters),
        providerPrefs: prefsActive(providerPrefs) ? providerPrefs : null,
      });
      const payload = await searchLens(body);
      lastBody.current = body;
      setResponse(payload);
      setPage(nextPage);
      setHasSearched(true);
      // Local-only recent searches: deduped + capped inside the helper,
      // never sent to the backend. Pagination re-records are harmless.
      recordRecentSearch(searchQuery);
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

  const pages = response ? totalPages(response.total, pageSize) : 0;

  // Prefetch the next page while idle once results land. Failures resolve
  // to null inside `prefetchPage`, so a miss only means "not prefetched".
  useEffect(() => {
    if (!response || loading || error) return;
    if (page >= totalPages(response.total, pageSize)) return;
    const body = lastBody.current;
    if (!body) return;
    const nextPage = page + 1;
    let cancelled = false;
    const idle = window as unknown as {
      requestIdleCallback?: (callback: () => void) => number;
      cancelIdleCallback?: (id: number) => void;
    };
    if (typeof idle.requestIdleCallback === 'function') {
      const id = idle.requestIdleCallback(() => {
        if (!cancelled) void prefetchPage(body, nextPage);
      });
      return () => {
        cancelled = true;
        idle.cancelIdleCallback?.(id);
      };
    }
    const timer = window.setTimeout(() => {
      if (!cancelled) void prefetchPage(body, nextPage);
    }, 1000);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [response, page, loading, error]);

  function goToPage(nextPage: number) {
    void runSearch(nextPage, query, mode, userFilters, prefs);
  }

  // Every filter, mode, or provider-prefs edit takes effect immediately:
  // once results exist, the edit re-runs page 1 with the next state.
  // Before the first search there is nothing to refine, so edits only
  // update state for the upcoming submit.
  function handleFiltersChange(next: UserFilterState) {
    setUserFilters(next);
    if (hasSearched) void runSearch(1, query, mode, next, prefs);
  }

  function handlePrefsChange(next: ProviderPrefs) {
    setPrefs(next);
    if (hasSearched) void runSearch(1, query, mode, userFilters, next);
  }

  function handleModeChange(next: SearchMode) {
    setMode(next);
    if (hasSearched) void runSearch(1, query, next, userFilters, prefs);
  }

  function handlePageSize(size: number) {
    if (!Number.isFinite(size) || size <= 0 || size === pageSize) return;
    setPageSize(size);
    void runSearch(1, query, mode, userFilters, prefs, size);
  }

  function submitFirstPage(searchQuery: string) {
    void runSearch(1, searchQuery, mode, userFilters, prefs);
  }

  // Suggestion pick (keyboard Enter or click): sync the controlled input
  // to the picked completion, then run the same first-page search submit
  // would run. Both hero and sticky-header forms share this handler.
  function handleSuggestSelect(picked: string) {
    setQuery(picked);
    submitFirstPage(picked);
  }

  // Related-search chip and library recent-search re-runs.
  function handleAssistedSelect(nextQuery: string) {
    setQuery(nextQuery);
    void runSearch(1, nextQuery, mode, userFilters, prefs);
  }

  // Per-card library toggle: persist locally, then reflect saved state.
  function handleSaveResult(hit: SearchResult) {
    if (savedIds.includes(hit.id)) {
      setSavedIds(removeSavedResult(hit.id).map((entry) => entry.id));
    } else {
      setSavedIds(saveResult(hit).map((entry) => entry.id));
    }
  }

  // Chip clears flow through `handleFiltersChange`, which already re-runs
  // the search — no second request is issued here.

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

  // Google-style two states: before the first search only the centered
  // hero (brand + search + mode) renders — the filter sidebar, results,
  // and diagnostics rail do not exist yet. Submitting flips to the
  // workspace: sticky header search + shell. Loading counts as the
  // workspace so the skeleton shows in the results column.
  const showLanding = !hasSearched && !loading;

  return (
    <div className="ol-shell flex min-h-screen flex-col">
      {showLanding ? (
        <main className="ol-landing grid min-h-screen place-items-center p-6">
          <GlassPanel variant="hero" data-testid="search-hero">
            <h1 className="ol-brand m-0 text-2xl font-bold tracking-tight">OpenLens</h1>
            <p className="ol-brand-sub text-ol-ink-soft m-0 text-sm">Plug-and-play search</p>
            <SearchForm
              value={query}
              onChange={setQuery}
              onSubmit={submitFirstPage}
              onSuggestSelect={handleSuggestSelect}
              disabled={loading}
            />
            <ModeSelector mode={mode} onChange={handleModeChange} disabled={loading} />
          </GlassPanel>
        </main>
      ) : (
        <>
          <GlassPanel variant="header" role="banner" data-testid="search-header">
            <div className="ol-header-inner mx-auto flex w-full max-w-4xl items-baseline gap-4 px-6 py-4">
              <h1 className="ol-brand m-0 text-2xl font-bold tracking-tight">OpenLens</h1>
              <SearchForm
                value={query}
                onChange={setQuery}
                onSubmit={submitFirstPage}
                onSuggestSelect={handleSuggestSelect}
                disabled={loading}
              />
              <ModeSelector mode={mode} onChange={handleModeChange} disabled={loading} />
            </div>
          </GlassPanel>
          <main>
            <div className="ol-app-shell mx-auto grid w-full max-w-4xl items-start gap-4 p-6">
              <aside
                className="ol-shell-sidebar min-w-0"
                data-testid="sidebar-mount"
                aria-label="Search filters"
              >
                <Sidebar
                  filters={userFilters}
                  onFiltersChange={handleFiltersChange}
                  prefs={prefs}
                  onPrefsChange={handlePrefsChange}
                  knownProviders={knownProviders}
                  appliedFilters={response?.applied_filters ?? []}
                  disabled={loading}
                  collapsed={sidebarCollapsed}
                  onCollapsedChange={setSidebarCollapsed}
                  onLibraryOpen={() => setLibraryOpen(true)}
                />
                {libraryOpen ? (
                  <section aria-label="Personal library" data-testid="library-drawer">
                    <h2>Library</h2>
                    <Suspense fallback={<p>Loading library…</p>}>
                      <LibraryDrawer onSelectSearch={handleAssistedSelect} />
                    </Suspense>
                    <Button variant="secondary" onClick={() => setLibraryOpen(false)}>
                      Close library
                    </Button>
                  </section>
                ) : null}
              </aside>
              <div className="ol-shell-results min-w-0">
                <p aria-live="polite" data-testid="result-announcer" className="ol-sr-only sr-only">
                  {announcement}
                </p>
                {loading ? <LoadingState /> : null}
                {!loading && error ? (
                  <ErrorState code={error.code} message={error.message} />
                ) : null}
                {!loading && !error && response ? (
                  <section aria-label="Search output">
                    {showSuggestion ? (
                      <p data-testid="mode-suggestion">
                        <Button
                          variant="ghost"
                          onClick={() => {
                            setMode(showSuggestion as SearchMode);
                            void runSearch(
                              1,
                              query,
                              showSuggestion as SearchMode,
                              userFilters,
                              prefs,
                            );
                          }}
                        >
                          Try {showSuggestion} mode
                        </Button>
                      </p>
                    ) : null}
                    <ResultList
                      results={response.results}
                      total={response.total}
                      mode={mode}
                      spelling={readSpelling(response)}
                      related={readRelated(response)}
                      onRelatedSelect={handleAssistedSelect}
                      savedIds={savedIds}
                      onSaveResult={handleSaveResult}
                    />
                    {pages > 1 ? (
                      <Pagination
                        page={page}
                        totalPages={pages}
                        pageSize={pageSize}
                        onPage={goToPage}
                        onPageSize={handlePageSize}
                      />
                    ) : null}
                  </section>
                ) : null}
              </div>
              <div className="ol-shell-rail min-w-0">
                {!loading && !error && response ? (
                  <Suspense fallback={<p>Loading diagnostics…</p>}>
                    <DiagnosticsRail diagnostics={response.diagnostics} />
                  </Suspense>
                ) : null}
              </div>
            </div>
          </main>
        </>
      )}
    </div>
  );
}
