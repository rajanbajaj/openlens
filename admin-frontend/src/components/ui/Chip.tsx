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
    <span className="ol-chip inline-block text-ol-s font-semibold rounded-ol-s px-ol-s leading-[1.6] bg-ol-accent-wash border border-ol-accent text-ol-accent-strong mr-ol-xs">
      {children}
      {onRemove ? (
        <button
          type="button"
          aria-label={removeLabel}
          onClick={onRemove}
          className="font-inherit text-inherit bg-transparent border-0 cursor-pointer pl-ol-xs underline"
        >
          ×
        </button>
      ) : null}
    </span>
  );
}
