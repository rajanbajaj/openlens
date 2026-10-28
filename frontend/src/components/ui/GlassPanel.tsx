import type { ReactNode } from 'react';
import type { CardProps } from './Card.tsx';

export type GlassPanelVariant = 'hero' | 'header' | 'sidebar' | 'rail';

export interface GlassPanelProps extends CardProps {
  children: ReactNode;
  /** Which frosted surface this panel renders as. Defaults to 'hero'. */
  variant?: GlassPanelVariant;
}

const VARIANT_CLASSES: Record<GlassPanelVariant, string> = {
  hero: 'ol-glass ol-glass-hero',
  header: 'ol-glass ol-glass-header',
  sidebar: 'ol-glass ol-glass-sidebar',
  rail: 'ol-glass ol-glass-rail',
};

/**
 * Frosted-glass panel for selective chrome (hero, sticky header, filter
 * sidebar, diagnostics rail). Same contract as Card: a plain container
 * that forwards extra props to the div. Result bodies stay solid.
 */
export function GlassPanel({ children, variant = 'hero', className, ...rest }: GlassPanelProps) {
  const classes = [VARIANT_CLASSES[variant], className].filter(Boolean).join(' ');
  return (
    <div className={classes} {...rest}>
      {children}
    </div>
  );
}
