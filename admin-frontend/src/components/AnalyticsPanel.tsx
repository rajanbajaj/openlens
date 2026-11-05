import { createAdminQueryClient } from '../api/queryClient.tsx';
import { QueryClientProvider, useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { fetchAnalyticsCounts, fetchAnalyticsSeries, fetchAnalyticsTop } from '../api/client.ts';
import { FeedbackCard } from './analytics/FeedbackCard.tsx';
import { capBuckets, ThroughputTable } from './analytics/ThroughputTable.tsx';
import type { StackedPoint } from './analytics/ThroughputTable.tsx';
import { Button } from './ui/Button.tsx';
import { FieldError } from './ui/FieldError.tsx';
import { Skeleton } from './ui/Skeleton.tsx';

interface AnalyticsPanelProps {
  /** Admin token for the admin-only analytics reads. */
  adminToken: string;
}

const WINDOW_OPTIONS = [7, 30, 90];

const TOP_DOMAINS_LIMIT = 10;

type ListSort = 'count-desc' | 'count-asc' | 'value-asc' | 'value-desc';

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

function sortRows(rows: DomainRow[], sort: ListSort): DomainRow[] {
  const copy = [...rows];
  switch (sort) {
    case 'count-asc':
      return copy.sort((a, b) => a.count - b.count);
    case 'value-asc':
      return copy.sort((a, b) => a.value.localeCompare(b.value));
    case 'value-desc':
      return copy.sort((a, b) => b.value.localeCompare(a.value));
    case 'count-desc':
    default:
      return copy.sort((a, b) => b.count - a.count);
  }
}

/** Shift a ``YYYY-MM-DD`` date by whole days, returning ``YYYY-MM-DD``. */
function shiftDate(iso: string, deltaDays: number): string {
  const day = new Date(`${iso}T00:00:00Z`);
  day.setUTCDate(day.getUTCDate() + deltaDays);
  return day.toISOString().slice(0, 10);
}

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

type DeltaTone = 'good' | 'bad' | 'flat';

interface Delta {
  text: string;
  tone: DeltaTone;
}

/**
 * Period-over-period delta for a KPI. ``invert`` marks metrics where a
 * drop is the good direction (failures, skips). Deltas render as a
 * percent change, or as percentage points when ``unit`` is ``pp``
 * (ratios such as the success rate). Zero history on both sides
 * renders an em dash; activity with no history renders "new".
 */
function periodDelta(
  current: number,
  previous: number,
  priorLabel: string,
  invert = false,
  unit: 'pct' | 'pp' = 'pct',
): Delta {
  if (previous === 0 && current === 0) return { text: `— ${priorLabel}`, tone: 'flat' };
  if (previous === 0) return { text: `new ${priorLabel}`, tone: 'flat' };
  const change = unit === 'pp' ? current - previous : ((current - previous) / previous) * 100;
  const suffix = unit === 'pp' ? ' pp' : '%';
  if (Math.abs(change) < 0.05) return { text: `±0.0${suffix} ${priorLabel}`, tone: 'flat' };
  const signed = `${change >= 0 ? '+' : ''}${change.toFixed(1)}${suffix}`;
  const good = invert ? change < 0 : change > 0;
  return { text: `${signed} ${priorLabel}`, tone: good ? 'good' : 'bad' };
}

const DELTA_TONE_CLASS: Record<DeltaTone, string> = {
  good: 'text-ol-ok',
  bad: 'text-ol-error',
  flat: 'text-ol-ink-soft',
};

const DELTA_MARKER: Record<DeltaTone, string> = {
  good: '▲',
  bad: '▼',
  flat: '—',
};

const DELTA_DIRECTION: Record<DeltaTone, string> = {
  good: 'up',
  bad: 'down',
  flat: 'flat',
};

/** One KPI card: label, headline value, and prior-period delta. */
function KpiCard({
  label,
  value,
  delta,
  testId,
  loading,
}: {
  label: string;
  value: string;
  delta: Delta;
  testId: string;
  loading: boolean;
}) {
  return (
    <div
      data-testid={testId}
      className="bg-white border border-ol-line rounded-ol-m px-ol-m py-ol-m"
    >
      <p className="font-semibold">{label}</p>
      {loading ? (
        <div aria-live="polite">
          <Skeleton bars={1} />
          <p className="mt-ol-xs text-ol-ink-soft">—</p>
        </div>
      ) : (
        <>
          <p className="text-ol-xl font-bold tabular-nums mt-ol-xs">{value}</p>
          <p
            className={`mt-ol-xs ${DELTA_TONE_CLASS[delta.tone]}`}
            aria-label={`${DELTA_DIRECTION[delta.tone]} ${delta.text}`}
          >
            <span aria-hidden="true">{DELTA_MARKER[delta.tone]} </span>
            {delta.text}
          </p>
        </>
      )}
    </div>
  );
}

/**
 * Stacked throughput bars (fetched / failed / skipped) rendered as
 * plain divs so no chart dependency is needed. Bars cap at 48 buckets
 * (see `capBuckets`) and stay keyboard-focusable with per-bucket
 * labels; a table fallback carries the same data for screen readers.
 */
function ThroughputChart({ points }: { points: StackedPoint[] }) {
  if (points.length === 0) return <p className="my-ol-s">No throughput yet.</p>;
  const max = Math.max(1, ...points.map((p) => p.fetched + p.failed + p.skipped));
  const totals = points.reduce(
    (acc, p) => ({
      fetched: acc.fetched + p.fetched,
      failed: acc.failed + p.failed,
      skipped: acc.skipped + p.skipped,
    }),
    { fetched: 0, failed: 0, skipped: 0 },
  );
  const labelAt = (index: number) => {
    const t = points[index].t;
    return t.length > 10 ? t.slice(5, 13) : t.slice(5);
  };
  const axis =
    points.length <= 5
      ? points.map((_, index) => index)
      : [0, 1, 2, 3, 4].map((slot) =>
          Math.min(points.length - 1, Math.round((slot * (points.length - 1)) / 4)),
        );
  return (
    <div data-testid="analytics-throughput">
      <div
        role="img"
        aria-label={`Stacked pages per bucket: ${totals.fetched} fetched, ${totals.failed} failed, ${totals.skipped} skipped`}
        className="flex items-stretch gap-[2px] h-40"
      >
        {points.map((point) => {
          const total = point.fetched + point.failed + point.skipped;
          return (
            <div
              key={point.t}
              tabIndex={0}
              aria-label={`${point.t}: ${total} pages (${point.fetched} fetched, ${point.failed} failed, ${point.skipped} skipped)`}
              title={`${point.t}: ${total} pages (${point.fetched} fetched, ${point.failed} failed, ${point.skipped} skipped)`}
              className="flex-1 flex flex-col justify-end min-w-0 focus-visible:outline-2"
            >
              <div
                className="bg-ol-error rounded-t-[2px]"
                style={{ height: `${(point.failed / max) * 100}%` }}
              />
              <div className="bg-ol-warn" style={{ height: `${(point.skipped / max) * 100}%` }} />
              <div
                className="bg-ol-accent rounded-b-[2px] min-h-[2px]"
                style={{ height: `${(point.fetched / max) * 100}%` }}
              />
            </div>
          );
        })}
      </div>
      <div className="flex justify-between mt-ol-xs text-ol-ink-soft">
        {axis.map((index) => (
          <span key={index}>{labelAt(index)}</span>
        ))}
      </div>
      <ul className="flex flex-wrap gap-ol-m mt-ol-xs">
        <li>
          <span
            className="inline-block w-3 h-3 bg-ol-accent rounded-[2px] mr-ol-xs"
            aria-hidden="true"
          />
          Fetched <strong>{totals.fetched}</strong>
        </li>
        <li>
          <span
            className="inline-block w-3 h-3 bg-ol-error rounded-[2px] mr-ol-xs"
            aria-hidden="true"
          />
          Failed <strong>{totals.failed}</strong>
        </li>
        <li>
          <span
            className="inline-block w-3 h-3 bg-ol-warn rounded-[2px] mr-ol-xs"
            aria-hidden="true"
          />
          Skipped <strong>{totals.skipped}</strong>
        </li>
      </ul>
    </div>
  );
}

/**
 * Enterprise crawl-health dashboard: KPI cards with prior-period
 * deltas, a stacked fetched/failed/skipped throughput chart, and
 * ranked breakdown lists — searchable by date range and domain, with
 * jump paging on the long domain lists. The window select covers
 * trailing presets; the From/To inputs replace it with an absolute
 * ``YYYY-MM-DD`` range. Reads degrade to notices on store trouble
 * (fail-open).
 *
 * Owns its query client (same pattern as ``FrontierDrawer``) so the
 * panel works wherever it is mounted.
 */
export function AnalyticsPanel({ adminToken }: AnalyticsPanelProps) {
  const [client] = useState(createAdminQueryClient);
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
  const [failingDomainDraft, setFailingDomainDraft] = useState('');
  const [failingDomain, setFailingDomain] = useState('');
  const [failingDomainPage, setFailingDomainPage] = useState(1);
  const [failingReasonsPage, setFailingReasonsPage] = useState(1);
  const [outcomesPage, setOutcomesPage] = useState(1);
  const [domainsSort, setDomainsSort] = useState<ListSort>('count-desc');
  const [failingSort, setFailingSort] = useState<ListSort>('count-desc');
  const [reasonsSort, setReasonsSort] = useState<ListSort>('count-desc');
  const [outcomesSort, setOutcomesSort] = useState<ListSort>('count-desc');
  const [throughputView, setThroughputView] = useState<'chart' | 'table'>('chart');

  const rangeActive = fromDate !== '' && toDate !== '';
  const rangeInvalid = fromDate !== '' && toDate !== '' && fromDate > toDate;
  const start = rangeActive && !rangeInvalid ? fromDate : undefined;
  const end = rangeActive && !rangeInvalid ? toDate : undefined;
  const appliedDomain = domain.trim() === '' ? undefined : domain.trim();
  const appliedFailingDomain = failingDomain.trim() === '' ? undefined : failingDomain.trim();
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
  // Prior window of the same length, for KPI deltas. Trailing mode
  // looks back 2×days; range mode takes the span right before From.
  const spanDays = rangeActive ? Math.max(1, (rangeSpanDays ?? 0) + 1) : days;
  const prevEnd = rangeActive ? shiftDate(fromDate, -1) : shiftDate(todayIso(), -days);
  const prevStart = shiftDate(prevEnd, -(spanDays - 1));
  const priorLabel = `vs prior ${spanDays} day${spanDays === 1 ? '' : 's'}`;
  const bucket = effectiveSpan <= 7 ? 'hour' : 'day';

  const countsQuery = useQuery({
    queryKey: ['admin', 'analytics', 'counts', days, fromDate, toDate],
    queryFn: () => fetchAnalyticsCounts(days, adminToken, start, end),
  });
  const prevCountsQuery = useQuery({
    queryKey: ['admin', 'analytics', 'counts-prev', days, prevStart, prevEnd],
    queryFn: () => fetchAnalyticsCounts(days, adminToken, prevStart, prevEnd),
  });
  function seriesFor(outcome: string) {
    return {
      queryKey: ['admin', 'analytics', 'series', outcome, days, fromDate, toDate],
      queryFn: () =>
        fetchAnalyticsSeries(
          'crawl.page',
          bucket,
          days,
          adminToken,
          start,
          end,
          'outcome',
          outcome,
        ),
    };
  }
  const fetchedSeriesQuery = useQuery(seriesFor('fetched'));
  const failedSeriesQuery = useQuery(seriesFor('failed'));
  const skippedSeriesQuery = useQuery(seriesFor('skipped'));
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
      appliedFailingDomain,
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
        appliedFailingDomain,
        start,
        end,
      ),
  });
  const failingReasonsQuery = useQuery({
    queryKey: ['admin', 'analytics', 'failing-reasons', days, fromDate, toDate, failingReasonsPage],
    queryFn: () =>
      fetchAnalyticsTop(
        'crawl.page',
        'reason',
        days,
        10,
        adminToken,
        'outcome',
        'failed',
        (failingReasonsPage - 1) * 10,
        undefined,
        start,
        end,
      ),
  });
  const outcomesQuery = useQuery({
    queryKey: ['admin', 'analytics', 'outcomes', days, fromDate, toDate, outcomesPage],
    queryFn: () =>
      fetchAnalyticsTop(
        'crawl.page',
        'outcome',
        days,
        10,
        adminToken,
        undefined,
        undefined,
        (outcomesPage - 1) * 10,
        undefined,
        start,
        end,
      ),
  });

  const counts = countsQuery.data ?? null;
  const prevTotals = prevCountsQuery.data?.by_name ?? {};
  const totals = counts?.by_name ?? {};
  const fetched = totals['crawl.fetched'] ?? 0;
  const failed = totals['crawl.failed'] ?? 0;
  const skipped = totals['crawl.skipped'] ?? 0;
  const prevFetched = prevTotals['crawl.fetched'] ?? 0;
  const prevFailed = prevTotals['crawl.failed'] ?? 0;
  const prevSkipped = prevTotals['crawl.skipped'] ?? 0;
  const attempted = fetched + failed + skipped;
  const prevAttempted = prevFetched + prevFailed + prevSkipped;
  const successRate = attempted === 0 ? 0 : (fetched / attempted) * 100;
  const prevSuccessRate = prevAttempted === 0 ? 0 : (prevFetched / prevAttempted) * 100;
  const avgPerDay = attempted / spanDays;
  const prevAvgPerDay = prevAttempted / spanDays;
  const feedbackHelpful = totals['search.feedback.helpful'] ?? 0;
  const feedbackTotal = feedbackHelpful + (totals['search.feedback.not_helpful'] ?? 0);
  const prevFeedbackHelpful = prevTotals['search.feedback.helpful'] ?? 0;
  const prevFeedbackTotal = prevFeedbackHelpful + (prevTotals['search.feedback.not_helpful'] ?? 0);
  const feedbackRate = feedbackTotal === 0 ? 0 : (feedbackHelpful / feedbackTotal) * 100;
  const prevFeedbackRate =
    prevFeedbackTotal === 0 ? 0 : (prevFeedbackHelpful / prevFeedbackTotal) * 100;

  function seriesPoints(query: {
    data?: { points?: { t: string; count: number }[] } | null;
  }): Map<string, number> {
    const map = new Map<string, number>();
    for (const point of query.data?.points ?? []) map.set(point.t, point.count);
    return map;
  }
  const fetchedByBucket = seriesPoints(fetchedSeriesQuery);
  const failedByBucket = seriesPoints(failedSeriesQuery);
  const skippedByBucket = seriesPoints(skippedSeriesQuery);
  const bucketKeys = Array.from(
    new Set([...fetchedByBucket.keys(), ...failedByBucket.keys(), ...skippedByBucket.keys()]),
  ).sort();
  const stacked: StackedPoint[] = capBuckets(
    bucketKeys.map((t) => ({
      t,
      fetched: fetchedByBucket.get(t) ?? 0,
      failed: failedByBucket.get(t) ?? 0,
      skipped: skippedByBucket.get(t) ?? 0,
    })),
  );

  const domains = sortRows(readTopRows(crawlDomainsQuery.data), domainsSort);
  const failingDomains = sortRows(readTopRows(failingDomainsQuery.data), failingSort);
  const failingReasons = sortRows(readTopRows(failingReasonsQuery.data), reasonsSort);
  const outcomes = sortRows(readTopRows(outcomesQuery.data), outcomesSort);
  const anyLoading =
    countsQuery.isLoading ||
    prevCountsQuery.isLoading ||
    fetchedSeriesQuery.isLoading ||
    failedSeriesQuery.isLoading ||
    skippedSeriesQuery.isLoading ||
    crawlDomainsQuery.isLoading ||
    failingDomainsQuery.isLoading ||
    failingReasonsQuery.isLoading ||
    outcomesQuery.isLoading;
  // The generated client resolves HTTP failures to `null` (fail-open)
  // instead of throwing, so a settled query with no data failed.
  function queryFailed(query: { isError: boolean; isPending: boolean; data: unknown }): boolean {
    return query.isError || (!query.isPending && query.data == null);
  }

  const anyCountsError = queryFailed(countsQuery);
  const anySeriesError =
    queryFailed(fetchedSeriesQuery) &&
    queryFailed(failedSeriesQuery) &&
    queryFailed(skippedSeriesQuery);
  // A global banner only when the headline numbers *and* the throughput
  // both fail; single-card failures stay on their own card.
  const failedQuery = anyCountsError && anySeriesError;

  function resetPages() {
    setDomainPage(1);
    setFailingDomainPage(1);
    setFailingReasonsPage(1);
    setOutcomesPage(1);
  }

  function applyDomainSearch() {
    setDomain(domainDraft.trim());
    setDomainPage(1);
  }

  function clearDomainSearch() {
    setDomainDraft('');
    setDomain('');
    setDomainPage(1);
  }

  function applyFailingDomainSearch() {
    setFailingDomain(failingDomainDraft.trim());
    setFailingDomainPage(1);
  }

  function clearFailingDomainSearch() {
    setFailingDomainDraft('');
    setFailingDomain('');
    setFailingDomainPage(1);
  }

  function changeWindow(next: string) {
    if (next === 'custom') return;
    setDays(Number(next));
    setFromDate('');
    setToDate('');
    resetPages();
  }

  function changeFromDate(next: string) {
    setFromDate(next);
    resetPages();
  }

  function changeToDate(next: string) {
    setToDate(next);
    resetPages();
  }

  function renderSortControl(
    testId: string,
    label: string,
    value: ListSort,
    onChange: (next: ListSort) => void,
  ) {
    return (
      <label className="flex items-center gap-ol-xs text-ol-s font-semibold">
        {label}
        <select
          data-testid={testId}
          value={value}
          onChange={(event) => onChange(event.target.value as ListSort)}
          className="font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-xs py-ol-xs"
        >
          <option value="count-desc">Count ↓</option>
          <option value="count-asc">Count ↑</option>
          <option value="value-asc">Value A–Z</option>
          <option value="value-desc">Value Z–A</option>
        </select>
      </label>
    );
  }

  /** Ranked rows with a proportional share bar under each value. */
  function renderShareRows(
    rows: DomainRow[],
    barClass: string,
    listTestId: string,
    emptyTestId: string | null,
    emptyText: string,
    sort: ListSort,
    setSort: (next: ListSort) => void,
    sortTestId: string,
  ) {
    if (rows.length === 0) {
      return (
        <p data-testid={emptyTestId ?? undefined} className="my-ol-s">
          {emptyText}
        </p>
      );
    }
    const max = Math.max(1, ...rows.map((row) => row.count));
    return (
      <>
        {renderSortControl(sortTestId, 'Sort', sort, setSort)}
        <ul data-testid={listTestId} className="flex flex-col gap-ol-s mt-ol-s">
          {rows.map((entry) => (
            <li key={entry.value}>
              <div className="flex justify-between gap-ol-s">
                <span className="truncate">{entry.value}</span>
                <strong className="tabular-nums">{entry.count.toLocaleString()}</strong>
              </div>
              <div className="h-1 bg-ol-well rounded-ol-s mt-ol-xs" aria-hidden="true">
                <div
                  className={`h-1 rounded-ol-s ${barClass}`}
                  style={{ width: `${Math.max(2, (entry.count / max) * 100)}%` }}
                />
              </div>
            </li>
          ))}
        </ul>
      </>
    );
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
            className="w-16 font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-xs py-ol-xs"
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
      <h2 className="ol-dash-title">Analytics</h2>
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
            value={rangeActive ? 'custom' : days}
            onChange={(event) => changeWindow(event.target.value)}
            className="font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
          >
            {WINDOW_OPTIONS.map((option) => (
              <option key={option} value={option}>
                Last {option} days
              </option>
            ))}
            {rangeActive ? <option value="custom">(custom range)</option> : null}
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
            className="font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
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
            className="font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
          />
        </label>
        {rangeActive ? (
          <p data-testid="analytics-range-note" className="my-ol-xs">
            Showing absolute range; the window preset is ignored.
          </p>
        ) : null}
        {rangeInvalid ? (
          <FieldError id="analytics-range" message="Start must be before end." />
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
            className="font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
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
      <div>
        <h3 className="mt-ol-m mb-ol-s text-ol-m">Overview {windowLabel}</h3>
        <div className="grid gap-ol-m grid-cols-2 sm:grid-cols-3">
          <KpiCard
            label="Pages fetched"
            value={fetched.toLocaleString()}
            delta={periodDelta(fetched, prevFetched, priorLabel)}
            testId="analytics-kpi-fetched"
            loading={anyLoading}
          />
          <KpiCard
            label="Success rate"
            value={`${successRate.toFixed(1)}%`}
            delta={periodDelta(successRate, prevSuccessRate, priorLabel, false, 'pp')}
            testId="analytics-kpi-success"
            loading={anyLoading}
          />
          <KpiCard
            label="Avg pages / day"
            value={avgPerDay.toFixed(1)}
            delta={periodDelta(avgPerDay, prevAvgPerDay, priorLabel)}
            testId="analytics-kpi-pace"
            loading={anyLoading}
          />
          <KpiCard
            label="Pages failed"
            value={failed.toLocaleString()}
            delta={periodDelta(failed, prevFailed, priorLabel, true)}
            testId="analytics-kpi-failed"
            loading={anyLoading}
          />
          <KpiCard
            label="Pages skipped"
            value={skipped.toLocaleString()}
            delta={periodDelta(skipped, prevSkipped, priorLabel, true)}
            testId="analytics-kpi-skipped"
            loading={anyLoading}
          />
          <KpiCard
            label="Feedback helpful"
            value={`${feedbackRate.toFixed(0)}% (${feedbackHelpful}/${feedbackTotal})`}
            delta={periodDelta(feedbackRate, prevFeedbackRate, priorLabel, false, 'pp')}
            testId="analytics-kpi-feedback"
            loading={anyLoading}
          />
        </div>
        <p className="mt-ol-xs text-ol-s text-ol-ink-soft">
          Deltas: pp = percentage points, % = percent change.
        </p>
      </div>
      <div className="bg-white border border-ol-line rounded-ol-m p-ol-m">
        <div className="flex flex-wrap items-center justify-between gap-ol-s mb-ol-s">
          <h3 className="text-ol-m">Pages per bucket {windowLabel}</h3>
          <div className="flex gap-ol-xs" role="group" aria-label="Throughput view">
            <Button
              variant={throughputView === 'chart' ? 'primary' : 'secondary'}
              size="sm"
              data-testid="analytics-view-chart"
              aria-pressed={throughputView === 'chart'}
              onClick={() => setThroughputView('chart')}
            >
              Chart
            </Button>
            <Button
              variant={throughputView === 'table' ? 'primary' : 'secondary'}
              size="sm"
              data-testid="analytics-view-table"
              aria-pressed={throughputView === 'table'}
              onClick={() => setThroughputView('table')}
            >
              Table
            </Button>
          </div>
        </div>
        {throughputView === 'chart' ? (
          <ThroughputChart points={stacked} />
        ) : (
          <ThroughputTable points={stacked} />
        )}
      </div>
      <div className="grid gap-ol-m lg:grid-cols-2">
        <div className="bg-white border border-ol-line rounded-ol-m p-ol-m">
          <h3 className="mb-ol-s text-ol-m" data-testid="analytics-domains">
            Top domains {windowLabel}
            {appliedDomain !== undefined ? ` matching “${appliedDomain}”` : ''}
          </h3>
          {queryFailed(crawlDomainsQuery) ? (
            <p className="my-ol-s">Top domains unavailable.</p>
          ) : (
            renderShareRows(
              domains,
              'bg-ol-accent',
              'analytics-domains-list',
              'analytics-domains-empty',
              `No domains${domainPage > 1 ? ' on this page' : ''}.`,
              domainsSort,
              setDomainsSort,
              'analytics-domains-sort',
            )
          )}
          {renderDomainPager(domainPage, setDomainPage, domains, 'analytics-domains')}
        </div>
        <div className="bg-white border border-ol-line rounded-ol-m p-ol-m">
          <h3 className="mb-ol-s text-ol-m" data-testid="analytics-failing-domains">
            Top failing domains {windowLabel}
            {appliedFailingDomain !== undefined ? ` matching “${appliedFailingDomain}”` : ''}
          </h3>
          <div className="flex flex-wrap items-end gap-ol-s mb-ol-s">
            <label className="flex flex-col gap-ol-xs font-semibold">
              Domain contains
              <input
                data-testid="analytics-failing-domain"
                type="text"
                value={failingDomainDraft}
                onChange={(event) => setFailingDomainDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') applyFailingDomainSearch();
                }}
                placeholder="e.g. example"
                className="font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-xs py-ol-xs"
              />
            </label>
            <button
              type="button"
              data-testid="analytics-failing-domain-search"
              className="ol-button-ghost"
              onClick={applyFailingDomainSearch}
            >
              Search
            </button>
            {appliedFailingDomain !== undefined ? (
              <button
                type="button"
                data-testid="analytics-failing-domain-clear"
                className="ol-button-ghost"
                onClick={clearFailingDomainSearch}
              >
                Clear
              </button>
            ) : null}
          </div>
          {queryFailed(failingDomainsQuery) ? (
            <p className="my-ol-s">Failing domains unavailable.</p>
          ) : (
            renderShareRows(
              failingDomains,
              'bg-ol-error',
              'analytics-failing-domains-list',
              'analytics-failing-domains-empty',
              `No failed pages${failingDomainPage > 1 ? ' on this page' : ''}.`,
              failingSort,
              setFailingSort,
              'analytics-failing-domains-sort',
            )
          )}
          {renderDomainPager(
            failingDomainPage,
            setFailingDomainPage,
            failingDomains,
            'analytics-failing-domains',
          )}
        </div>
        <div className="bg-white border border-ol-line rounded-ol-m p-ol-m">
          <h3 className="mb-ol-s text-ol-m" data-testid="analytics-reasons">
            Top failing reasons {windowLabel}
          </h3>
          {queryFailed(failingReasonsQuery) ? (
            <p className="my-ol-s">Failing reasons unavailable.</p>
          ) : (
            renderShareRows(
              failingReasons,
              'bg-ol-error',
              'analytics-reasons-list',
              null,
              failingReasonsPage > 1 ? 'No rows on this page.' : 'No failed pages.',
              reasonsSort,
              setReasonsSort,
              'analytics-reasons-sort',
            )
          )}
          {renderDomainPager(
            failingReasonsPage,
            setFailingReasonsPage,
            failingReasons,
            'analytics-reasons',
          )}
        </div>
        <div className="bg-white border border-ol-line rounded-ol-m p-ol-m">
          <h3 className="mb-ol-s text-ol-m">Page outcomes {windowLabel}</h3>
          {queryFailed(outcomesQuery) ? (
            <p className="my-ol-s">Page outcomes unavailable.</p>
          ) : (
            renderShareRows(
              outcomes,
              'bg-ol-accent',
              'analytics-outcomes-list',
              null,
              outcomesPage > 1 ? 'No rows on this page.' : 'No crawl activity.',
              outcomesSort,
              setOutcomesSort,
              'analytics-outcomes-sort',
            )
          )}
          {renderDomainPager(outcomesPage, setOutcomesPage, outcomes, 'analytics-outcomes')}
        </div>
        <FeedbackCard
          adminToken={adminToken}
          fallbackText={`Feedback helpful ${feedbackRate.toFixed(0)}% (${feedbackHelpful}/${feedbackTotal}) from analytics counts.`}
        />
      </div>
    </section>
  );
}
