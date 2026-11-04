import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { fetchAnalyticsCounts, fetchAnalyticsSeries, fetchAnalyticsTop } from '../api/client.ts';

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

/** One KPI card: label, headline value, and prior-period delta. */
function KpiCard({
  label,
  value,
  delta,
  testId,
}: {
  label: string;
  value: string;
  delta: Delta;
  testId: string;
}) {
  return (
    <div
      data-testid={testId}
      className="bg-white border border-ol-line rounded-ol-m px-ol-m py-ol-s"
    >
      <p className="font-semibold">{label}</p>
      <p className="text-ol-xl font-bold tabular-nums mt-ol-xs">{value}</p>
      <p className={`mt-ol-xs ${DELTA_TONE_CLASS[delta.tone]}`}>{delta.text}</p>
    </div>
  );
}

interface StackedPoint {
  t: string;
  fetched: number;
  failed: number;
  skipped: number;
}

/**
 * Stacked throughput bars (fetched / failed / skipped) rendered as
 * plain divs so no chart dependency is needed. Shows the first,
 * middle, and last bucket labels to stay readable on long windows.
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
  const axis = points.length === 1 ? [0] : [0, Math.floor(points.length / 2), points.length - 1];
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
              title={`${point.t}: ${total} pages (${point.fetched} fetched, ${point.failed} failed, ${point.skipped} skipped)`}
              className="flex-1 flex flex-col justify-end min-w-0"
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
          <span className="inline-block w-3 h-3 bg-ol-accent rounded-[2px] mr-ol-xs" />
          Fetched <strong>{totals.fetched}</strong>
        </li>
        <li>
          <span className="inline-block w-3 h-3 bg-ol-error rounded-[2px] mr-ol-xs" />
          Failed <strong>{totals.failed}</strong>
        </li>
        <li>
          <span className="inline-block w-3 h-3 bg-ol-warn rounded-[2px] mr-ol-xs" />
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
    queryKey: ['admin', 'analytics', 'failing-domains', days, fromDate, toDate, failingDomainPage],
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
  const stacked: StackedPoint[] = bucketKeys.map((t) => ({
    t,
    fetched: fetchedByBucket.get(t) ?? 0,
    failed: failedByBucket.get(t) ?? 0,
    skipped: skippedByBucket.get(t) ?? 0,
  }));

  const domains = readTopRows(crawlDomainsQuery.data);
  const failingDomains = readTopRows(failingDomainsQuery.data);
  const failingReasons = readTopRows(failingReasonsQuery.data);
  const outcomes = readTopRows(outcomesQuery.data);
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
  const failedQuery =
    countsQuery.isError ||
    fetchedSeriesQuery.isError ||
    failedSeriesQuery.isError ||
    skippedSeriesQuery.isError ||
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

  /** Ranked rows with a proportional share bar under each value. */
  function renderShareRows(
    rows: DomainRow[],
    barClass: string,
    listTestId: string,
    emptyTestId: string | null,
    emptyText: string,
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
      <ul data-testid={listTestId} className="flex flex-col gap-ol-s">
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
            className="font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
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
      {anyLoading ? <p className="my-ol-s">Loading analytics…</p> : null}
      <div>
        <h4 className="mt-ol-m mb-ol-s text-ol-m">Overview {windowLabel}</h4>
        <div className="grid gap-ol-m grid-cols-2 lg:grid-cols-3">
          <KpiCard
            label="Pages fetched"
            value={fetched.toLocaleString()}
            delta={periodDelta(fetched, prevFetched, priorLabel)}
            testId="analytics-kpi-fetched"
          />
          <KpiCard
            label="Success rate"
            value={`${successRate.toFixed(1)}%`}
            delta={periodDelta(successRate, prevSuccessRate, priorLabel, false, 'pp')}
            testId="analytics-kpi-success"
          />
          <KpiCard
            label="Avg pages / day"
            value={avgPerDay.toFixed(1)}
            delta={periodDelta(avgPerDay, prevAvgPerDay, priorLabel)}
            testId="analytics-kpi-pace"
          />
          <KpiCard
            label="Pages failed"
            value={failed.toLocaleString()}
            delta={periodDelta(failed, prevFailed, priorLabel, true)}
            testId="analytics-kpi-failed"
          />
          <KpiCard
            label="Pages skipped"
            value={skipped.toLocaleString()}
            delta={periodDelta(skipped, prevSkipped, priorLabel, true)}
            testId="analytics-kpi-skipped"
          />
          <KpiCard
            label="Feedback helpful"
            value={`${feedbackRate.toFixed(0)}% (${feedbackHelpful}/${feedbackTotal})`}
            delta={periodDelta(feedbackRate, prevFeedbackRate, priorLabel, false, 'pp')}
            testId="analytics-kpi-feedback"
          />
        </div>
      </div>
      <div className="bg-white border border-ol-line rounded-ol-m p-ol-m">
        <h4 className="mb-ol-s text-ol-m">Pages per bucket {windowLabel}</h4>
        <ThroughputChart points={stacked} />
      </div>
      <div className="grid gap-ol-m lg:grid-cols-2">
        <div className="bg-white border border-ol-line rounded-ol-m p-ol-m">
          <h4 className="mb-ol-s text-ol-m" data-testid="analytics-domains">
            Top domains {windowLabel}
            {appliedDomain !== undefined ? ` matching “${appliedDomain}”` : ''}
          </h4>
          {renderShareRows(
            domains,
            'bg-ol-accent',
            'analytics-domains-list',
            'analytics-domains-empty',
            `No domains${domainPage > 1 ? ' on this page' : ''}.`,
          )}
          {renderDomainPager(domainPage, setDomainPage, domains, 'analytics-domains')}
        </div>
        <div className="bg-white border border-ol-line rounded-ol-m p-ol-m">
          <h4 className="mb-ol-s text-ol-m" data-testid="analytics-failing-domains">
            Top failing domains {windowLabel}
          </h4>
          {renderShareRows(
            failingDomains,
            'bg-ol-error',
            'analytics-failing-domains-list',
            'analytics-failing-domains-empty',
            `No failed pages${failingDomainPage > 1 ? ' on this page' : ''}.`,
          )}
          {renderDomainPager(
            failingDomainPage,
            setFailingDomainPage,
            failingDomains,
            'analytics-failing-domains',
          )}
        </div>
        <div className="bg-white border border-ol-line rounded-ol-m p-ol-m">
          <h4 className="mb-ol-s text-ol-m" data-testid="analytics-reasons">
            Top failing reasons {windowLabel}
          </h4>
          {renderShareRows(
            failingReasons,
            'bg-ol-error',
            'analytics-reasons-list',
            null,
            'No failed pages.',
          )}
        </div>
        <div className="bg-white border border-ol-line rounded-ol-m p-ol-m">
          <h4 className="mb-ol-s text-ol-m">Page outcomes {windowLabel}</h4>
          {renderShareRows(
            outcomes,
            'bg-ol-accent',
            'analytics-outcomes-list',
            null,
            'No crawl activity.',
          )}
        </div>
      </div>
    </section>
  );
}
