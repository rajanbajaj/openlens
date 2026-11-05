import { useQuery } from '@tanstack/react-query';
import { fetchFeedbackSummary } from '../../api/client.ts';
import { Skeleton } from '../ui/Skeleton.tsx';

export interface FeedbackCardProps {
  adminToken: string;
  /** Analytics-counts fallback text shown when the summary endpoint 404s. */
  fallbackText?: string;
}

/**
 * Admin feedback overview wired to `GET /admin/feedback/summary` — the
 * previously unused surface. Totals by action; when the endpoint is
 * absent (404) or fails, falls back to the analytics-counts text so
 * the card still says something true.
 */
export function FeedbackCard({ adminToken, fallbackText }: FeedbackCardProps) {
  const summaryQuery = useQuery({
    queryKey: ['admin', 'feedback-summary', adminToken],
    queryFn: () => fetchFeedbackSummary(adminToken),
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });

  return (
    <div className="bg-white border border-ol-line rounded-ol-m p-ol-m">
      <h3 className="mb-ol-s text-ol-m" data-testid="analytics-feedback">
        Feedback
      </h3>
      {summaryQuery.isPending ? (
        <div aria-live="polite">
          <Skeleton bars={2} />
        </div>
      ) : summaryQuery.data == null ? (
        <p data-testid="analytics-feedback-empty" className="my-ol-s">
          {fallbackText ?? 'No feedback summary yet.'}
        </p>
      ) : (
        <ul data-testid="analytics-feedback-card" className="flex flex-col gap-ol-s">
          {Object.entries(summaryQuery.data.by_action).map(([action, count]) => (
            <li key={action}>
              <div className="flex justify-between gap-ol-s">
                <span className="truncate">{action}</span>
                <strong className="tabular-nums">{count.toLocaleString()}</strong>
              </div>
            </li>
          ))}
          <li>
            <div className="flex justify-between gap-ol-s">
              <span className="truncate">Total</span>
              <strong className="tabular-nums">{summaryQuery.data.total.toLocaleString()}</strong>
            </div>
          </li>
        </ul>
      )}
    </div>
  );
}
