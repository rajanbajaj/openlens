import type { ErrorDetail } from '../api/types';
import { Skeleton } from './ui/Skeleton.tsx';

export type ErrorStateProps = ErrorDetail;

/** Empty-result state shown when a successful search returns zero hits. */
export function EmptyResults() {
  return (
    <p role="status" data-testid="empty-results">
      No results found. Try a different query or change the filters.
    </p>
  );
}

/**
 * Plain-language title per backend error code. The machine-readable
 * code stays visible for log correlation, but users read the title —
 * never raw JSON.
 */
export const ERROR_TITLES: Record<string, string> = {
  network_error: 'Could not reach OpenLens',
  invalid_response: 'OpenLens answered unexpectedly',
  validation_error: 'That search needs a tweak',
  config_error: 'OpenLens is misconfigured',
  plugin_error: 'A search step failed',
  provider_error: 'A search backend failed',
  unknown_error: 'Search failed',
};

/** Error-envelope state: plain-language title plus the backend message. */
export function ErrorState({ code, message }: ErrorStateProps) {
  return (
    <div role="alert" data-testid="error-state">
      <h3>{ERROR_TITLES[code] ?? 'Search failed'}</h3>
      <p>{message}</p>
      <p className="ol-result-meta" data-testid="error-code">
        Error code: {code}
      </p>
    </div>
  );
}

/** Loading state shown while a search request is in flight. */
export function LoadingState() {
  return (
    <>
      <p role="status" data-testid="loading-state">
        Searching…
      </p>
      <Skeleton bars={3} />
    </>
  );
}
