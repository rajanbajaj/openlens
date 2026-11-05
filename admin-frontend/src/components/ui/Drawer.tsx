import { useEffect } from 'react';
import type { ReactNode } from 'react';
import { Button } from './Button.tsx';

export interface DrawerProps {
  /** Trigger button text, e.g. "Show diagnostics". */
  triggerLabel: string;
  /** Whether the panel is expanded. */
  open: boolean;
  /** Called when the trigger is activated. */
  onToggle: () => void;
  /** Id of the disclosed panel (wired to aria-controls). */
  panelId: string;
  /** Optional test hook for the panel. */
  panelTestId?: string;
  /** Explicit accessible name; defaults to the trigger minus Show/Hide. */
  label?: string;
  /** One-line summary rendered instead of the empty rail when closed. */
  closedSummary?: string;
  children: ReactNode;
}

/** Controlled disclosure panel: trigger button plus collapsible region. */
export function Drawer({
  triggerLabel,
  open,
  onToggle,
  panelId,
  panelTestId,
  label,
  closedSummary,
  children,
}: DrawerProps) {
  // Full Escape handling: the whole section no longer needs focus for
  // Escape to close the panel.
  useEffect(() => {
    if (!open) return;
    function onKeyDown(event: KeyboardEvent): void {
      if (event.key === 'Escape') onToggle();
    }
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open, onToggle]);

  return (
    <section
      aria-label={label ?? triggerLabel}
      className="ol-drawer mt-ol-l rounded-ol-l border border-ol-glass-border px-ol-m py-ol-l"
    >
      <Button
        variant="secondary"
        size="sm"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={onToggle}
      >
        {triggerLabel}
      </Button>
      {open ? (
        <div id={panelId} data-testid={panelTestId}>
          {children}
        </div>
      ) : closedSummary ? (
        <p className="mt-ol-s text-ol-s text-ol-ink-soft">{closedSummary}</p>
      ) : null}
    </section>
  );
}
