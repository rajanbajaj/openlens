import type { ErrorDetail } from '../api/types';
import { Card } from './ui/Card.tsx';
import { Skeleton } from './ui/Skeleton.tsx';

export type ErrorStateProps = ErrorDetail;

/**
 * Empty-result state shown when a successful search returns zero hits.
 * Solid card surface, matching the result cards it replaces.
 */
export function EmptyResults() {
  return (
    <Card>
      <p role="status" data-testid="empty-results">
        No results found. Try a different query or change the filters.
      </p>
    </Card>
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

/**
 * Error-envelope state: plain-language title plus the backend message,
 * on the same solid card surface as results.
 */
export function ErrorState({ code, message }: ErrorStateProps) {
  return (
    <Card role="alert" data-testid="error-state">
      <h3>{ERROR_TITLES[code] ?? 'Search failed'}</h3>
      <p>{message}</p>
      <p className="ol-result-meta" data-testid="error-code">
        Error code: {code}
      </p>
    </Card>
  );
}

/**
 * Loading state shown while a search request is in flight: a live
 * status line plus placeholder bars shaped like incoming cards.
 */
export function LoadingState() {
  return (
    <>
      <p role="status" data-testid="loading-state">
        Searching…
      </p>
      <Card>
        <Skeleton bars={6} />
      </Card>
    </>
  );
}
