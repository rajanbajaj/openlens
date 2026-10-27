import type { ErrorDetail } from '../api/types';

export type ErrorStateProps = ErrorDetail;

/** Empty-result state shown when a successful search returns zero hits. */
export function EmptyResults() {
  return (
    <p role="status" data-testid="empty-results">
      No results found. Try a different query or change the filters.
    </p>
  );
}

/** Error-envelope state rendering the API `{error: {code, message}}` body. */
export function ErrorState({ code, message }: ErrorStateProps) {
  return (
    <div role="alert" data-testid="error-state">
      <h3>Search failed</h3>
      <p>
        <strong>{code}</strong>: {message}
      </p>
    </div>
  );
}

/** Loading state shown while a search request is in flight. */
export function LoadingState() {
  return (
    <p role="status" data-testid="loading-state">
      Searching…
    </p>
  );
}
