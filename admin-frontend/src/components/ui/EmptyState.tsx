import type { ReactNode } from 'react';

export interface EmptyStateProps {
  title: string;
  hint?: string;
  action?: ReactNode;
}

/** Consistent empty panel replacing bare paragraphs and ad-hoc empties. */
export function EmptyState({ title, hint, action }: EmptyStateProps) {
  return (
    <div data-testid="empty-state" className="my-ol-s rounded-ol-m border border-ol-line p-ol-m">
      <p className="font-semibold">{title}</p>
      {hint ? <p className="mt-ol-xs text-ol-s text-ol-ink-soft">{hint}</p> : null}
      {action ? <div className="mt-ol-s">{action}</div> : null}
    </div>
  );
}
