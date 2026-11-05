import type { ReactNode } from 'react';

export type StatusTone = 'running' | 'paused' | 'live' | 'stale' | 'ok' | 'error' | 'neutral';

export interface StatusBadgeProps {
  tone: StatusTone;
  children: ReactNode;
  /** Optional CSS dot marker (shape only, never emoji). Defaults to none. */
  icon?: 'dot';
}

const TONE_CLASSES: Record<StatusTone, string> = {
  running: 'bg-ol-ok-wash text-ol-ok border-ol-ok',
  paused: 'bg-ol-warn-wash text-ol-warn border-ol-warn',
  live: 'bg-ol-ok-wash text-ol-ok border-ol-ok',
  stale: 'bg-ol-warn-wash text-ol-warn border-ol-warn',
  ok: 'bg-ol-ok-wash text-ol-ok border-ol-ok',
  error: 'bg-ol-error-wash text-ol-error border-ol-error',
  neutral: 'bg-ol-well text-ol-ink-soft border-ol-glass-border',
};

/**
 * Semantic state badge (Running/Paused/Live/Stale/…). Tones reuse the
 * existing `--color-ol-*` tokens — no new hues. Counts and provider
 * names stay on the monochrome `Badge`; state moves here.
 */
export function StatusBadge({ tone, children, icon }: StatusBadgeProps) {
  return (
    <span
      className={`inline-block text-ol-s font-semibold rounded-ol-s px-ol-s leading-[1.6] border ${TONE_CLASSES[tone]}`}
    >
      {icon === 'dot' ? (
        <span
          aria-hidden="true"
          className="mr-ol-xs inline-block h-2 w-2 rounded-full bg-current"
        />
      ) : null}
      {children}
    </span>
  );
}
