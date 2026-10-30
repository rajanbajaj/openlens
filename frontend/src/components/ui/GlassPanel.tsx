import type { ReactNode } from 'react';
import type { CardProps } from './Card.tsx';

export type GlassPanelVariant = 'hero' | 'header' | 'sidebar' | 'rail';

export interface GlassPanelProps extends CardProps {
  children: ReactNode;
  /** Which frosted surface this panel renders as. Defaults to 'hero'. */
  variant?: GlassPanelVariant;
}

const VARIANT_CLASSES: Record<GlassPanelVariant, string> = {
  hero: 'ol-glass ol-glass-hero rounded-ol-l px-ol-l py-ol-l shadow-[0_8px_40px_rgb(0_0_0/0.15),0_0_32px_rgb(255_255_255/0.25)] backdrop-blur-[16px]',
  header:
    'ol-glass ol-glass-header sticky top-0 z-10 rounded-none border-l-0 border-r-0 border-t-0 backdrop-blur-[8px]',
  sidebar: 'ol-glass ol-glass-sidebar rounded-ol-m p-ol-m backdrop-blur-[12px]',
  rail: 'ol-glass ol-glass-rail rounded-ol-m px-ol-m py-ol-l backdrop-blur-[12px]',
};

/**
 * Frosted-glass panel for selective chrome (hero, sticky header, filter
 * sidebar, diagnostics rail). Same contract as Card: a plain container
 * that forwards extra props to the div. Result bodies use the lighter
 * glass weight on `.ol-card`.
 */
export function GlassPanel({ children, variant = 'hero', className, ...rest }: GlassPanelProps) {
  const classes = [VARIANT_CLASSES[variant], className].filter(Boolean).join(' ');
  return (
    <div className={classes} {...rest}>
      {children}
    </div>
  );
}
