import type { ReactNode } from 'react';
import type { CardProps } from './Card.tsx';

export type GlassPanelVariant = 'hero' | 'header' | 'sidebar' | 'rail';

export interface GlassPanelProps extends CardProps {
  children: ReactNode;
  /** Which frosted surface this panel renders as. Defaults to 'hero'. */
  variant?: GlassPanelVariant;
}

const VARIANT_CLASSES: Record<GlassPanelVariant, string> = {
  hero: 'ol-glass ol-glass-hero rounded-ol-l px-ol-l py-ol-l',
  header:
    'ol-glass ol-glass-header sticky top-0 z-10 rounded-none border-l-0 border-r-0 border-t-0',
  sidebar: 'ol-glass ol-glass-sidebar rounded-ol-m p-ol-m',
  rail: 'ol-glass ol-glass-rail rounded-ol-m px-ol-m py-ol-l',
};

/**
 * Flat panel for page chrome (hero, sticky header, filter sidebar,
 * diagnostics rail). Same contract as Card: a plain container that
 * forwards extra props to the div. The hero variant is transparent so
 * the landing page opens clean like google.com.
 */
export function GlassPanel({ children, variant = 'hero', className, ...rest }: GlassPanelProps) {
  const classes = [VARIANT_CLASSES[variant], className].filter(Boolean).join(' ');
  return (
    <div className={classes} {...rest}>
      {children}
    </div>
  );
}
