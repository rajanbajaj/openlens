import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import {
  fetchAnalyticsCounts,
  fetchAnalyticsSeries,
  fetchAnalyticsTop,
} from '../api/client.ts';

interface AnalyticsPanelProps {
  /** Admin token for the admin-only analytics reads. */
  adminToken: string;
}

const WINDOW_OPTIONS = [7, 30, 90];

const TOP_DOMAINS_LIMIT = 10;

/** One row of the top-domains breakdown. */
interface DomainRow {
  value: string;
  count: number;
}

function readTopRows(top: { top?: DomainRow[] } | null | undefined): DomainRow[] {
  if (!top || !Array.isArray(top.top)) return [];
  return top.top.filter(
    (entry): entry is DomainRow =>
      typeof entry === 'object' &&
      entry !== null &&
      typeof entry.value === 'string' &&
      typeof entry.count === 'number',
  );
}

/**
 * Totomo-style analytics rows: key metrics, throughput, and ranked
 * breakdown lists — searchable by date range and domain, with paging
 * on the long domain lists. The window select covers trailing presets;
 * the From/To inputs replace it with an absolute ``YYYY-MM-DD`` range.
 * Reads degrade to skeletons on store trouble (fail-open).
 *
 * Owns its query client (same pattern as ``FrontierDrawer``) so the
 * panel works wherever it is mounted.
 */
export function AnalyticsPanel({ adminToken }: AnalyticsPanelProps) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { retry: false, refetchOnWindowFocus: false },
        },
      }),
  );
  return (
    <QueryClientProvider client={client}>
      <AnalyticsPanelView adminToken={adminToken} />
    </QueryClientProvider>
  );
}

function AnalyticsPanelView({ adminToken }: AnalyticsPanelProps) {
  const [days, setDays] = useState(30);
  const [fromDate, setFromDate] = useState('');
  const [toDate, setToDate] = useState('');
  const [domainDraft, setDomainDraft] = useState('');
  const [domain, setDomain] = useState('');
  const [domainPage, setDomainPage] = useState(1);
  const [failingDomainPage, setFailingDomainPage] = useState(1);

  const rangeActive = fromDate !== '' && toDate !== '';
  const start = rangeActive ? fromDate : undefined;
  const end = rangeActive ? toDate : undefined;
  const appliedDomain = domain.trim() === '' ? undefined : domain.trim();
  const windowLabel = rangeActive ? `${fromDate} → ${toDate}` : `in the last ${days} days`;
  const rangeSpanDays = rangeActive
    ? Math.max(
        0,
        Math.round(
          (new Date(`${toDate}T00:00:00Z`).getTime() -
            new Date(`${fromDate}T00:00:00Z`).getTime()) /
            86400000,
        ),
      )
    : undefined;
  const effectiveSpan = rangeSpanDays ?? days;

  const countsQuery = useQuery({
    queryKey: ['admin', 'analytics', 'counts', days, fromDate, toDate],
    queryFn: () => fetchAnalyticsCounts(days, adminToken, start, end),
  });
  const seriesQuery = useQuery({
    queryKey: ['admin', 'analytics', 'series', days, fromDate, toDate],
    queryFn: () =>
      fetchAnalyticsSeries(
        'crawl.page',
        effectiveSpan <= 7 ? 'hour' : 'day',
        days,
        adminToken,
        start,
        end,
      ),
  });
  const crawlDomainsQuery = useQuery({
    queryKey: [
      'admin',
      'analytics',
      'top-domains',
      days,
      fromDate,
      toDate,
      appliedDomain,
      domainPage,
    ],
    queryFn: () =>
      fetchAnalyticsTop(
        'crawl.page',
        'domain',
        days,
        TOP_DOMAINS_LIMIT,
        adminToken,
        undefined,
        undefined,
        (domainPage - 1) * TOP_DOMAINS_LIMIT,
        appliedDomain,
        start,
        end,
      ),
  });
  const failingDomainsQuery = useQuery({
    queryKey: [
      'admin',
      'analytics',
      'failing-domains',
      days,
      fromDate,
      toDate,
      failingDomainPage,
    ],
    queryFn: () =>
      fetchAnalyticsTop(
        'crawl.page',
        'domain',
        days,
        TOP_DOMAINS_LIMIT,
        adminToken,
        'outcome',
        'failed',
        (failingDomainPage - 1) * TOP_DOMAINS_LIMIT,
        undefined,
        start,
        end,
      ),
  });
  const failingReasonsQuery = useQuery({
    queryKey: ['admin', 'analytics', 'failing-reasons', days, fromDate, toDate],
    queryFn: () =>
      fetchAnalyticsTop(
        'crawl.page',
        'reason',
        days,
        10,
        adminToken,
        'outcome',
        'failed',
        undefined,
        undefined,
        start,
        end,
      ),
  });
  const outcomesQuery = useQuery({
    queryKey: ['admin', 'analytics', 'outcomes', days, fromDate, toDate],
    queryFn: () =>
      fetchAnalyticsTop(
        'crawl.page',
        'outcome',
        days,
        10,
        adminToken,
        undefined,
        undefined,
        undefined,
        undefined,
        start,
        end,
      ),
  });

  const counts = countsQuery.data ?? null;
  const series = seriesQuery.data?.points ?? [];
  const domains = readTopRows(crawlDomainsQuery.data);
  const failingDomains = readTopRows(failingDomainsQuery.data);
  const failingReasons = readTopRows(failingReasonsQuery.data);
  const outcomes = readTopRows(outcomesQuery.data);
  const totals = counts?.by_name ?? {};
  const fetched = totals['crawl.fetched'] ?? 0;
  const failed = totals['crawl.failed'] ?? 0;
  const skipped = totals['crawl.skipped'] ?? 0;
  const feedbackHelpful = totals['search.feedback.helpful'] ?? 0;
  const feedbackTotal =
    feedbackHelpful + (totals['search.feedback.not_helpful'] ?? 0);
  const anyLoading =
    countsQuery.isLoading ||
    seriesQuery.isLoading ||
    crawlDomainsQuery.isLoading ||
    failingDomainsQuery.isLoading ||
    failingReasonsQuery.isLoading ||
    outcomesQuery.isLoading;
  const failedQuery =
    countsQuery.isError ||
    seriesQuery.isError ||
    crawlDomainsQuery.isError ||
    failingDomainsQuery.isError ||
    failingReasonsQuery.isError ||
    outcomesQuery.isError;

  function applyDomainSearch() {
    setDomain(domainDraft.trim());
    setDomainPage(1);
  }

  function clearDomainSearch() {
    setDomainDraft('');
    setDomain('');
    setDomainPage(1);
  }

  function changeWindow(next: number) {
    setDays(next);
    setDomainPage(1);
    setFailingDomainPage(1);
  }

  function changeFromDate(next: string) {
    setFromDate(next);
    setDomainPage(1);
    setFailingDomainPage(1);
  }

  function changeToDate(next: string) {
    setToDate(next);
    setDomainPage(1);
    setFailingDomainPage(1);
  }

  function renderDomainPager(
    page: number,
    setPage: (next: number) => void,
    rows: DomainRow[],
    prefix: string,
  ) {
    return (
      <div className="mt-ol-s flex flex-wrap items-center gap-ol-s text-ol-s">
        <button
          type="button"
          data-testid={`${prefix}-prev`}
          className="ol-button-ghost"
          disabled={page <= 1}
          onClick={() => setPage(Math.max(1, page - 1))}
        >
          Previous
        </button>
        <span data-testid={`${prefix}-page`}>Page {page}</span>
        <button
          type="button"
          data-testid={`${prefix}-next`}
          className="ol-button-ghost"
          disabled={rows.length < TOP_DOMAINS_LIMIT}
          onClick={() => setPage(page + 1)}
        >
          Next
        </button>
        <label className="flex items-center gap-ol-xs">
          Go to
          <input
            data-testid={`${prefix}-jump`}
            type="number"
            min={1}
            defaultValue={page}
            key={`${prefix}-jump-${page}`}
            className="w-16 font-normal text-ol-ink bg-white/55 border border-ol-glass-border rounded-ol-m px-ol-xs py-ol-xs"
            onKeyDown={(event) => {
              if (event.key !== 'Enter') return;
              const next = Number((event.target as HTMLInputElement).value);
              if (Number.isInteger(next) && next >= 1) setPage(next);
            }}
          />
        </label>
        <button
          type="button"
          data-testid={`${prefix}-go`}
          className="ol-button-ghost"
          onClick={() => {
            const input = document.querySelector<HTMLInputElement>(
              `[data-testid="${prefix}-jump"]`,
            );
            const next = Number(input?.value);
            if (Number.isInteger(next) && next >= 1) setPage(next);
          }}
        >
          Go
        </button>
      </div>
    );
  }

  return (
    <section aria-label="Analytics" className="flex flex-col gap-ol-m text-ol-s">
      {failedQuery ? (
        <p role="alert" className="my-ol-s">
          Analytics unavailable right now.
        </p>
      ) : null}
      <div className="flex flex-wrap items-end gap-ol-m">
        <label className="flex flex-col gap-ol-xs font-semibold">
          Window
          <select
            data-testid="analytics-window"
            value={days}
            onChange={(event) => changeWindow(Number(event.target.value))}
            className="font-normal text-ol-ink bg-white/55 border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
          >
            {WINDOW_OPTIONS.map((option) => (
              <option key={option} value={option}>
                Last {option} days
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-ol-xs font-semibold">
          From
          <input
            data-testid="analytics-from"
            type="date"
            value={fromDate}
            max={toDate === '' ? undefined : toDate}
            onChange={(event) => changeFromDate(event.target.value)}
            className="font-normal text-ol-ink bg-white/55 border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
          />
        </label>
        <label className="flex flex-col gap-ol-xs font-semibold">
          To
          <input
            data-testid="analytics-to"
            type="date"
            value={toDate}
            min={fromDate === '' ? undefined : fromDate}
            onChange={(event) => changeToDate(event.target.value)}
            className="font-normal text-ol-ink bg-white/55 border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
          />
        </label>
        {rangeActive ? (
          <p data-testid="analytics-range-note" className="my-ol-xs">
            Showing absolute range; the window preset is ignored.
          </p>
        ) : null}
      </div>
      <div className="flex flex-wrap items-end gap-ol-m">
        <label className="flex flex-col gap-ol-xs font-semibold">
          Domain contains
          <input
            data-testid="analytics-domain"
            type="text"
            value={domainDraft}
            onChange={(event) => setDomainDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') applyDomainSearch();
            }}
            placeholder="e.g. wikipedia"
            className="font-normal text-ol-ink bg-white/55 border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
          />
        </label>
        <button
          type="button"
          data-testid="analytics-domain-search"
          className="ol-button-ghost"
          onClick={applyDomainSearch}
        >
          Search
        </button>
        {appliedDomain !== undefined ? (
          <button
            type="button"
            data-testid="analytics-domain-clear"
            className="ol-button-ghost"
            onClick={clearDomainSearch}
          >
            Clear
          </button>
        ) : null}
      </div>
      {anyLoading ? <p className="my-ol-s">Loading analytics…</p> : null}
      <div>
        <h4 className="mt-ol-m mb-ol-s text-ol-m">Key events {windowLabel}</h4>
        <ul className="flex flex-col gap-ol-xs">
          <li>
            Pages fetched: <strong>{fetched}</strong>
          </li>
          <li>
            Pages failed: <strong>{failed}</strong>
          </li>
          <li>
            Pages skipped: <strong>{skipped}</strong>
          </li>
          <li>
            Feedback helpful: <strong>{feedbackHelpful}</strong> of {feedbackTotal}
          </li>
        </ul>
      </div>
      <div>
        <h4 className="mt-ol-m mb-ol-s text-ol-m">Pages per day {windowLabel}</h4>
        {series.length === 0 ? (
          <p className="my-ol-s">No throughput yet.</p>
        ) : (
          <ul className="flex flex-col gap-ol-xs">
            {series.map((point) => (
              <li key={point.t}>
                {point.t}: <strong>{point.count}</strong>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <h4 className="mt-ol-m mb-ol-s text-ol-m" data-testid="analytics-domains">
          Top domains {windowLabel}
          {appliedDomain !== undefined ? ` matching “${appliedDomain}”` : ''}
        </h4>
        {domains.length === 0 ? (
          <p data-testid="analytics-domains-empty" className="my-ol-s">
            No domains{domainPage > 1 ? ' on this page' : ''}.
          </p>
        ) : (
          <ul data-testid="analytics-domains-list" className="flex flex-col gap-ol-xs">
            {domains.map((entry) => (
              <li key={entry.value}>
                {entry.value}: <strong>{entry.count}</strong>
              </li>
            ))}
          </ul>
        )}
        {renderDomainPager(domainPage, setDomainPage, domains, 'analytics-domains')}
      </div>
      <div>
        <h4 className="mt-ol-m mb-ol-s text-ol-m" data-testid="analytics-failing-domains">
          Top failing domains {windowLabel}
        </h4>
        {failingDomains.length === 0 ? (
          <p data-testid="analytics-failing-domains-empty" className="my-ol-s">
            No failed pages{failingDomainPage > 1 ? ' on this page' : ''}.
          </p>
        ) : (
          <ul data-testid="analytics-failing-domains-list" className="flex flex-col gap-ol-xs">
            {failingDomains.map((entry) => (
              <li key={entry.value}>
                {entry.value}: <strong>{entry.count}</strong>
              </li>
            ))}
          </ul>
        )}
        {renderDomainPager(
          failingDomainPage,
          setFailingDomainPage,
          failingDomains,
          'analytics-failing-domains',
        )}
      </div>
      <div>
        <h4 className="mt-ol-m mb-ol-s text-ol-m" data-testid="analytics-reasons">
          Top failing reasons {windowLabel}
        </h4>
        {failingReasons.length === 0 ? (
          <p className="my-ol-s">No failed pages.</p>
        ) : (
          <ul data-testid="analytics-reasons-list" className="flex flex-col gap-ol-xs">
            {failingReasons.map((entry) => (
              <li key={entry.value}>
                {entry.value}: <strong>{entry.count}</strong>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <h4 className="mt-ol-m mb-ol-s text-ol-m">Page outcomes {windowLabel}</h4>
        {outcomes.length === 0 ? (
          <p className="my-ol-s">No crawl activity.</p>
        ) : (
          <ul className="flex flex-col gap-ol-xs">
            {outcomes.map((entry) => (
              <li key={entry.value}>
                {entry.value}: <strong>{entry.count}</strong>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
