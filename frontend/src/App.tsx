import { useState } from 'react';
import {
  DEFAULT_LIMIT,
  buildSearchBody,
  offsetForPage,
  searchLens,
  SearchApiError,
  totalPages,
  validateQuery,
} from './api/client.ts';
import type { ErrorDetail, SearchMode, SearchResponse } from './api/types.ts';
import { AppliedFilters } from './components/AppliedFilters.tsx';
import { DiagnosticsDrawer } from './components/DiagnosticsDrawer.tsx';
import FilterToggle, { applyExcludeSponsoredFilter } from './components/FilterToggle.tsx';
import ModeSelector from './components/ModeSelector.tsx';
import { ResultList } from './components/ResultList.tsx';
import SearchForm from './components/SearchForm.tsx';
import { ErrorState, LoadingState } from './components/States.tsx';

const PAGE_SIZE = DEFAULT_LIMIT;

/**
 * Root search experience: wires the query form, mode selector, and
 * filter toggle into `POST /api/v1/search` via the typed fetch
 * client, and renders results, applied filters, pagination,
 * diagnostics, and the error envelope.
 */
export default function App() {
  const [query, setQuery] = useState('');
  const [mode, setMode] = useState<SearchMode>('default');
  const [excludeSponsored, setExcludeSponsored] = useState(false);
  const [page, setPage] = useState(1);
  const [response, setResponse] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<ErrorDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);

  async function runSearch(
    nextPage: number,
    searchQuery: string,
    searchMode: SearchMode,
    sponsored: boolean,
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
        filters: applyExcludeSponsoredFilter({}, sponsored),
      });
      const payload = await searchLens(body);
      setResponse(payload);
      setPage(nextPage);
      setHasSearched(true);
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
    void runSearch(nextPage, query, mode, excludeSponsored);
  }

  return (
    <main>
      <h1>OpenLens</h1>
      <SearchForm
        value={query}
        onChange={setQuery}
        onSubmit={(q) => {
          void runSearch(1, q, mode, excludeSponsored);
        }}
        disabled={loading}
      />
      <ModeSelector mode={mode} onChange={setMode} disabled={loading} />
      <FilterToggle checked={excludeSponsored} onChange={setExcludeSponsored} disabled={loading} />
      {loading ? <LoadingState /> : null}
      {!loading && error ? <ErrorState code={error.code} message={error.message} /> : null}
      {!loading && !error && response ? (
        <section aria-label="Search output">
          <AppliedFilters appliedFilters={response.applied_filters} />
          <ResultList results={response.results} total={response.total} />
          {pages > 1 ? (
            <nav aria-label="Pagination">
              <button type="button" disabled={page <= 1} onClick={() => goToPage(page - 1)}>
                Previous
              </button>
              <span data-testid="page-indicator">
                Page {page} of {pages}
              </span>
              <button type="button" disabled={page >= pages} onClick={() => goToPage(page + 1)}>
                Next
              </button>
            </nav>
          ) : null}
          <DiagnosticsDrawer diagnostics={response.diagnostics} />
        </section>
      ) : null}
      {!loading && !error && !hasSearched ? <p>Enter a query to search.</p> : null}
    </main>
  );
}
