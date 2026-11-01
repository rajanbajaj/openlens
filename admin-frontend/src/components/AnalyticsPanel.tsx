import { useEffect, useState } from 'react';
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import { fetchAnalyticsCounts, fetchFeedbackSummary } from '../api/client.ts';
import type { AnalyticsCounts, FeedbackSummary } from '../api/types.ts';
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

  const counts: AnalyticsCounts | null | undefined = countsQuery.data;
  const feedback: FeedbackSummary | null | undefined = feedbackQuery.data;

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
    const [countsResult, feedbackResult] = await Promise.all([
      countsQuery.refetch(),
      feedbackQuery.refetch(),
    ]);
    if (countsResult.isError || feedbackResult.isError) {
      pushToast('Refresh failed.', 'error');
    }
  }

  const loading = countsQuery.isPending || feedbackQuery.isPending;
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
