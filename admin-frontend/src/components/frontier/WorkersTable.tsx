import type { FrontierWorker } from '../../api/types.ts';
import { Badge } from '../ui/Badge.tsx';

/** Format a per-minute rate; em dash when the worker has no sample yet. */
export function formatRate(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return `${value >= 10 ? Math.round(value) : value.toFixed(1)}/min`;
}

/** Sum helper that skips missing rates. */
function sumRates(
  workers: FrontierWorker[],
  key: 'pages_per_min' | 'tokens_per_min',
): number | null {
  let total = 0;
  let seen = false;
  for (const worker of workers) {
    const value = worker[key];
    if (typeof value === 'number' && !Number.isNaN(value)) {
      total += value;
      seen = true;
    }
  }
  return seen ? total : null;
}

/**
 * Structured workers list replacing the concatenated `break-all` line:
 * one row per worker with aligned numeric columns (tabular numerals).
 */
export function WorkersTable({ workers }: { workers: FrontierWorker[] }) {
  return (
    <details className="ol-disclosure" aria-label="Workers">
      <summary className="ol-disclosure-title">Workers ({workers.length})</summary>
      {workers.length === 0 ? (
        <p className="my-ol-s text-ol-s">No workers heartbeating</p>
      ) : (
        <>
          {workers.some((worker) => worker.stats) && (
            <p data-testid="frontier-throughput" className="my-ol-s text-ol-s">
              Throughput: {formatRate(sumRates(workers, 'pages_per_min'))} pages ·{' '}
              {formatRate(sumRates(workers, 'tokens_per_min'))} tokens ·{' '}
              {workers.reduce((sum, worker) => sum + (worker.stats?.pages ?? 0), 0)} pages ·{' '}
              {workers.reduce((sum, worker) => sum + (worker.stats?.tokens ?? 0), 0)} tokens total
            </p>
          )}
          <table aria-label="Frontier workers" data-testid="frontier-workers" className="ol-table">
            <thead>
              <tr>
                <th scope="col">Status</th>
                <th scope="col">Worker</th>
                <th scope="col" className="ol-table-num">
                  Claimed
                </th>
                <th scope="col">Crawl</th>
                <th scope="col" className="ol-table-num">
                  Rate
                </th>
                <th scope="col" className="ol-table-num">
                  Totals
                </th>
                <th scope="col">Discovery</th>
              </tr>
            </thead>
            <tbody>
              {workers.map((worker) => (
                <tr key={worker.worker_id}>
                  <td>
                    <Badge>{worker.status}</Badge>
                  </td>
                  <td className="ol-table-id">{worker.worker_id}</td>
                  <td className="ol-table-num">{worker.claimed}</td>
                  <td className="ol-table-id">{worker.crawl_id}</td>
                  <td className="ol-table-num">
                    {worker.stats
                      ? `${formatRate(worker.pages_per_min)} pages, ${formatRate(worker.tokens_per_min)} tokens`
                      : '—'}
                  </td>
                  <td className="ol-table-num">
                    {worker.stats
                      ? `${worker.stats.pages} pages · ${worker.stats.tokens} tokens`
                      : '—'}
                  </td>
                  <td>
                    {worker.discovery
                      ? `${worker.discovery.feeds_checked} feeds, ${worker.discovery.new_urls} new, ${worker.discovery.refreshed} refreshed`
                      : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </details>
  );
}
