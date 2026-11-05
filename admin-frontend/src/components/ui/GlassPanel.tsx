import type { ReactNode } from 'react';
import type { CardProps } from './Card.tsx';

export type GlassPanelVariant = 'hero' | 'rail';

export interface GlassPanelProps extends CardProps {
  children: ReactNode;
  /** Which frosted surface this panel renders as. Defaults to 'hero'. */
  variant?: GlassPanelVariant;
}

const VARIANT_CLASSES: Record<GlassPanelVariant, string> = {
  hero: 'ol-glass ol-glass-hero rounded-ol-l px-ol-l py-ol-l',
  rail: 'ol-glass ol-glass-rail rounded-ol-m px-ol-m py-ol-l',
};

/**
 * Flat panel for selective chrome (admin hero, frontier rail). Same
 * contract as Card: a plain container that forwards extra props to
 * the div. Result bodies use `.ol-card`.
 */
export function GlassPanel({ children, variant = 'hero', className, ...rest }: GlassPanelProps) {
  const classes = [VARIANT_CLASSES[variant], className].filter(Boolean).join(' ');
  return (
    <div className={classes} {...rest}>
      {children}
    </div>
  );
}
