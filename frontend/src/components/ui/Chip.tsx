import type { ReactNode } from 'react';

export interface ChipProps {
  children: ReactNode;
  /** When set, renders a remove control labelled `removeLabel`. */
  onRemove?: () => void;
  removeLabel?: string;
}

/** Removable (or static) filter chip for applied-filter lists. */
export function Chip({ children, onRemove, removeLabel = 'Remove filter' }: ChipProps) {
  return (
    <span className="ol-chip">
      {children}
      {onRemove ? (
        <button type="button" aria-label={removeLabel} onClick={onRemove}>
          ×
        </button>
      ) : null}
    </span>
  );
}
