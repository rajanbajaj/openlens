import type { ReactNode } from 'react';

export interface BadgeProps {
  children: ReactNode;
}

/** Static label badge (provider names, status markers). */
export function Badge({ children }: BadgeProps) {
  return <span className="ol-badge">{children}</span>;
}
