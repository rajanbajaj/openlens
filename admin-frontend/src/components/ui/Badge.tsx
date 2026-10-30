import type { ReactNode } from 'react';

export interface BadgeProps {
  children: ReactNode;
}

/** Static label badge (provider names, status markers). */
export function Badge({ children }: BadgeProps) {
  return (
    <span className="ol-badge inline-block text-ol-s font-semibold rounded-ol-s px-ol-s leading-[1.6] bg-white/30 border border-ol-glass-border text-ol-ink-soft">
      {children}
    </span>
  );
}
