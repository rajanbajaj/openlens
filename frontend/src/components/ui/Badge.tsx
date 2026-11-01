import type { ReactNode } from 'react';

export interface BadgeProps {
  children: ReactNode;
  /** Native tooltip explaining the marker (provider, score, opinion...). */
  title?: string;
  /** Optional test hook (drives `result-opinion` and similar markers). */
  testId?: string;
}

/** Static label badge (provider names, status markers). */
export function Badge({ children, title, testId }: BadgeProps) {
  return (
    <span
      className="ol-badge inline-block text-ol-s font-semibold rounded-ol-s px-ol-s leading-[1.6] bg-ol-well border border-transparent text-ol-ink-soft"
      title={title}
      data-testid={testId}
    >
      {children}
    </span>
  );
}
