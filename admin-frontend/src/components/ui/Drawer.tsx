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
  children: ReactNode;
}

/** Controlled disclosure panel: trigger button plus collapsible region. */
export function Drawer({
  triggerLabel,
  open,
  onToggle,
  panelId,
  panelTestId,
  children,
}: DrawerProps) {
  return (
    <section
      aria-label={triggerLabel.replace(/^(Show|Hide)\s+/, '')}
      className="ol-drawer mt-ol-l rounded-ol-l border border-ol-glass-border px-ol-m py-ol-l shadow-[0_4px_30px_rgb(0_0_0/0.1)] backdrop-blur-[12px]"
      onKeyDown={(event: React.KeyboardEvent) => {
        if (event.key === 'Escape' && open) onToggle();
      }}
    >
      <Button variant="ghost" aria-expanded={open} aria-controls={panelId} onClick={onToggle}>
        {triggerLabel}
      </Button>
      {open ? (
        <div id={panelId} data-testid={panelTestId}>
          {children}
        </div>
      ) : null}
    </section>
  );
}
