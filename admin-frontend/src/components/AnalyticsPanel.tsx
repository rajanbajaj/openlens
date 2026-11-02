import { useEffect, useState } from 'react';
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import {
  fetchAnalyticsCounts,
  fetchAnalyticsSeries,
  fetchAnalyticsTop,
  fetchFeedbackSummary,
} from '../api/client.ts';
import type {
  AnalyticsCounts,
  AnalyticsSeries,
  AnalyticsTop,
  FeedbackSummary,
} from '../api/types.ts';

/** Text-sparkline blocks for the throughput strip (no chart dependency). */
const SPARK_LEVELS = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'];

/** Compact sparkline over per-bucket counts (empty when all zero). */
export function sparkline(counts: number[]): string {
  const peak = Math.max(0, ...counts);
  if (peak <= 0) return '';
  return counts
    .map(
      (count) =>
        SPARK_LEVELS[
          Math.min(SPARK_LEVELS.length - 1, Math.round((count / peak) * (SPARK_LEVELS.length - 1)))
        ],
    )
    .join('');
}
import { Badge } from './ui/Badge.tsx';
import { Button } from './ui/Button.tsx';
import { Card } from './ui/Card.tsx';
import { GlassPanel } from './ui/GlassPanel.tsx';
import { Toasts, useToasts } from './ui/Toast.tsx';

/** Trailing windows offered by the analytics view (backend caps at 365). */
const WINDOW_OPTIONS = [7, 30, 90];

/** Props for the analytics dashboard: the admin token comes from the app shell. */
export interface AnalyticsPanelProps {
  adminToken: string;
}

/**
 * Analytics dashboard: durable event counts from the analytics store
 * plus the in-process feedback summary. Both reads are admin-only;
 * every fetch failure degrades to an empty panel with an error toast
 * instead of crashing the page.
 */
function AnalyticsPanelView({ adminToken }: AnalyticsPanelProps) {
  const [days, setDays] = useState<number>(30);
  const { items: toasts, push: pushToast, dismiss: dismissToast } = useToasts();

  const countsQuery = useQuery({
    queryKey: ['admin', 'analytics-counts', days, adminToken],
    queryFn: () => fetchAnalyticsCounts(days, adminToken),
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });
  const feedbackQuery = useQuery({
    queryKey: ['admin', 'feedback-summary', adminToken],
    queryFn: () => fetchFeedbackSummary(adminToken),
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });
  const crawlOutcomeQuery = useQuery({
    queryKey: ['admin', 'analytics-top', 'crawl.page', 'outcome', days, adminToken],
    queryFn: () => fetchAnalyticsTop('crawl.page', 'outcome', days, 10, adminToken),
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });
  const crawlReasonQuery = useQuery({
    queryKey: ['admin', 'analytics-top', 'crawl.page', 'reason', days, adminToken],
    queryFn: () => fetchAnalyticsTop('crawl.page', 'reason', days, 10, adminToken),
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });
  const crawlDomainQuery = useQuery({
    queryKey: ['admin', 'analytics-top', 'crawl.page', 'domain', days, adminToken],
    queryFn: () => fetchAnalyticsTop('crawl.page', 'domain', days, 10, adminToken),
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });
  const crawlFailingDomainQuery = useQuery({
    queryKey: ['admin', 'analytics-top', 'crawl.page', 'domain', 'failed', days, adminToken],
    queryFn: () =>
      fetchAnalyticsTop('crawl.page', 'domain', days, 10, adminToken, 'outcome', 'failed'),
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });
  const crawlBucket = days <= 7 ? 'hour' : 'day';
  const crawlSeriesQuery = useQuery({
    queryKey: ['admin', 'analytics-series', 'crawl.page', crawlBucket, days, adminToken],
    queryFn: () => fetchAnalyticsSeries('crawl.page', crawlBucket, days, adminToken),
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });

  const counts: AnalyticsCounts | null | undefined = countsQuery.data;
  const feedback: FeedbackSummary | null | undefined = feedbackQuery.data;
  const crawlOutcomes: AnalyticsTop | null | undefined = crawlOutcomeQuery.data;
  const crawlReasons: AnalyticsTop | null | undefined = crawlReasonQuery.data;
  const crawlDomains: AnalyticsTop | null | undefined = crawlDomainQuery.data;
  const crawlFailingDomains: AnalyticsTop | null | undefined = crawlFailingDomainQuery.data;
  const crawlSeries: AnalyticsSeries | null | undefined = crawlSeriesQuery.data;

  useEffect(() => {
    if (countsQuery.isError || (countsQuery.isSuccess && countsQuery.data === null)) {
      pushToast('Analytics counts unavailable.', 'error');
    }
  }, [countsQuery.isError, countsQuery.isSuccess, countsQuery.data]);
  useEffect(() => {
    if (feedbackQuery.isError || (feedbackQuery.isSuccess && feedbackQuery.data === null)) {
      pushToast('Feedback summary unavailable.', 'error');
    }
  }, [feedbackQuery.isError, feedbackQuery.isSuccess, feedbackQuery.data]);

  async function refresh() {
    const results = await Promise.all([
      countsQuery.refetch(),
      feedbackQuery.refetch(),
      crawlOutcomeQuery.refetch(),
      crawlReasonQuery.refetch(),
      crawlDomainQuery.refetch(),
      crawlFailingDomainQuery.refetch(),
      crawlSeriesQuery.refetch(),
    ]);
    if (results.some((result) => result.isError)) {
      pushToast('Refresh failed.', 'error');
    }
  }

  const loading =
    countsQuery.isPending ||
    feedbackQuery.isPending ||
    crawlOutcomeQuery.isPending ||
    crawlReasonQuery.isPending ||
    crawlDomainQuery.isPending ||
    crawlFailingDomainQuery.isPending ||
    crawlSeriesQuery.isPending;
  const eventEntries = counts ? Object.entries(counts.by_name).sort((a, b) => b[1] - a[1]) : [];
  const actionEntries = feedback
    ? Object.entries(feedback.by_action).sort((a, b) => b[1] - a[1])
    : [];
  const reasonEntries = feedback
    ? Object.entries(feedback.by_reason).sort((a, b) => b[1] - a[1])
    : [];

  return (
    <GlassPanel variant="rail">
      <div className="flex flex-wrap gap-ol-s my-ol-s">
        <label className="flex items-center gap-ol-xs text-ol-s font-semibold">
          Window
          <select
            data-testid="analytics-window"
            value={days}
            onChange={(event) => setDays(Number(event.target.value))}
            className="font-normal text-ol-ink bg-white/55 border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
          >
            {WINDOW_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {option} days
              </option>
            ))}
          </select>
        </label>
        <Button variant="ghost" onClick={() => void refresh()}>
          Refresh
        </Button>
      </div>
      {loading ? (
        <p>Loading analytics…</p>
      ) : (
        <>
          <h4 className="mt-ol-m mb-ol-s text-ol-m">Event counts</h4>
          {counts === null || counts === undefined ? (
            <p data-testid="analytics-empty" className="my-ol-s text-ol-s">
              No analytics data — the analytics store is unreachable or empty.
            </p>
          ) : (
            <Card data-testid="analytics-counts">
              <p data-testid="analytics-total" className="my-ol-s">
                <Badge>{counts.total} events</Badge> in the last {counts.days} days
              </p>
              {eventEntries.length === 0 ? (
                <p className="my-ol-s text-ol-s">No events recorded in this window.</p>
              ) : (
                <ul
                  data-testid="analytics-by-name"
                  className="flex flex-col gap-ol-xs my-ol-s pl-ol-m"
                >
                  {eventEntries.map(([name, count]) => (
                    <li key={name} className="break-all">
                      <Badge>{count}</Badge> {name}
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          )}

          <h4 className="mt-ol-m mb-ol-s text-ol-m">Crawler</h4>
          {crawlOutcomes == null &&
          crawlReasons == null &&
          crawlDomains == null &&
          crawlFailingDomains == null &&
          crawlSeries == null ? (
            <p data-testid="crawler-empty" className="my-ol-s text-ol-s">
              No crawler activity — workers have not reported page outcomes yet.
            </p>
          ) : (
            <Card data-testid="crawler-summary">
              {(() => {
                const outcomeCounts: Record<string, number> = {};
                for (const entry of crawlOutcomes?.top ?? []) {
                  outcomeCounts[entry.value] = entry.count;
                }
                const total =
                  (outcomeCounts.fetched ?? 0) +
                  (outcomeCounts.failed ?? 0) +
                  (outcomeCounts.skipped ?? 0);
                const successRate =
                  total > 0
                    ? `${Math.round(((outcomeCounts.fetched ?? 0) / total) * 100)}% fetched (${outcomeCounts.fetched ?? 0}/${total})`
                    : null;
                const seriesCounts = (crawlSeries?.points ?? []).map((point) => point.count);
                const seriesPeak = Math.max(0, ...seriesCounts);
                return (
                  <>
                    {crawlOutcomes != null && crawlOutcomes.top.length > 0 && (
                      <ul
                        data-testid="crawler-by-outcome"
                        className="flex flex-col gap-ol-xs my-ol-s pl-ol-m"
                      >
                        {crawlOutcomes.top.map((entry) => (
                          <li key={entry.value} className="break-all">
                            <Badge>{entry.count}</Badge> {entry.value}
                          </li>
                        ))}
                      </ul>
                    )}
                    {successRate != null && (
                      <p data-testid="crawler-success-rate" className="my-ol-s text-ol-s">
                        Success rate: {successRate}
                      </p>
                    )}
                    {seriesCounts.length > 0 && seriesPeak > 0 && (
                      <p data-testid="crawler-throughput" className="my-ol-s">
                        <span aria-hidden="true">{sparkline(seriesCounts)}</span>{' '}
                        <span className="text-ol-s">
                          {crawlSeries?.bucket === 'hour' ? 'hourly' : 'daily'} pages · peak{' '}
                          {seriesPeak}/{crawlSeries?.bucket === 'hour' ? 'hour' : 'day'}
                        </span>
                      </p>
                    )}
                    {crawlDomains != null && crawlDomains.top.length > 0 && (
                      <ul
                        data-testid="crawler-by-domain"
                        className="flex flex-col gap-ol-xs my-ol-s pl-ol-m"
                      >
                        {crawlDomains.top.map((entry) => (
                          <li key={entry.value} className="break-all">
                            <Badge>{entry.count}</Badge> {entry.value}
                          </li>
                        ))}
                      </ul>
                    )}
                    {crawlFailingDomains != null && crawlFailingDomains.top.length > 0 && (
                      <p data-testid="crawler-failing-domains" className="my-ol-s text-ol-s">
                        Top failing domains:{' '}
                        {crawlFailingDomains.top
                          .map((entry) => `${entry.value} (${entry.count})`)
                          .join(', ')}
                      </p>
                    )}
                    {crawlReasons != null && crawlReasons.top.length > 0 && (
                      <p data-testid="crawler-by-reason" className="my-ol-s text-ol-s">
                        Top reasons:{' '}
                        {crawlReasons.top
                          .map((entry) => `${entry.value} (${entry.count})`)
                          .join(', ')}
                      </p>
                    )}
                    {crawlOutcomes != null &&
                      crawlOutcomes.top.length === 0 &&
                      (crawlReasons == null || crawlReasons.top.length === 0) &&
                      (crawlDomains == null || crawlDomains.top.length === 0) && (
                        <p className="my-ol-s text-ol-s">
                          No page outcomes recorded in this window.
                        </p>
                      )}
                  </>
                );
              })()}
            </Card>
          )}

          <h4 className="mt-ol-m mb-ol-s text-ol-m">Feedback summary</h4>
          {feedback === null || feedback === undefined ? (
            <p data-testid="feedback-empty" className="my-ol-s text-ol-s">
              No feedback summary — the backend did not return one.
            </p>
          ) : (
            <Card data-testid="feedback-summary">
              <p data-testid="feedback-total" className="my-ol-s">
                <Badge>{feedback.total} signals</Badge> total
              </p>
              {actionEntries.length > 0 && (
                <ul
                  data-testid="feedback-by-action"
                  className="flex flex-col gap-ol-xs my-ol-s pl-ol-m"
                >
                  {actionEntries.map(([action, count]) => (
                    <li key={action} className="break-all">
                      <Badge>{count}</Badge> {action}
                    </li>
                  ))}
                </ul>
              )}
              {reasonEntries.length > 0 && (
                <p data-testid="feedback-by-reason" className="my-ol-s text-ol-s">
                  Reasons:{' '}
                  {reasonEntries.map(([reason, count]) => `${reason} (${count})`).join(', ')}
                </p>
              )}
              {feedback.top_docs.length > 0 && (
                <ul
                  data-testid="feedback-top-docs"
                  className="flex flex-col gap-ol-xs my-ol-s pl-ol-m"
                >
                  {feedback.top_docs.slice(0, 10).map((doc) => (
                    <li key={doc.doc_id} className="break-all">
                      <Badge>{doc.total}</Badge> {doc.doc_id}
                      {doc.top_reason ? ` · ${doc.top_reason}` : null}
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          )}
        </>
      )}
      <Toasts items={toasts} onDismiss={dismissToast} />
    </GlassPanel>
  );
}

/**
 * Self-contained Query provider so the panel works wherever it is
 * mounted (admin page, tests) without app-wide wiring yet.
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
