import type { ProviderPrefs } from '../api/types.ts';
import { CrawlRequestForm } from './CrawlRequestForm.tsx';
import { ProviderPrefsControl } from './ProviderPrefsControl.tsx';
import { Button } from './ui/Button.tsx';
import { GlassPanel } from './ui/GlassPanel.tsx';

export interface SidebarProps {
  /** Federation prefs, controlled by the shell; null primary means no preference. */
  prefs: ProviderPrefs;
  /** Called with the next prefs on every prefs edit. */
  onPrefsChange: (next: ProviderPrefs) => void;
  /** Provider names seen in this session, offered as suggestions. */
  knownProviders: string[];
  /** Disables every control while a search is in flight. */
  disabled?: boolean;
  /**
   * Optional hook opening the personal-library drawer, owned by the
   * shell. The library affordance only signals the shell to mount the
   * drawer entry point.
   */
  onLibraryOpen?: () => void;
}

/**
 * Search utilities rail: library shortcut, URL crawl requests, and
 * provider preferences. Filters intentionally live nowhere here — the
 * inline `QuickFilters` row above the results is the only filter
 * surface, so there is exactly one place to change a filter.
 */
export function Sidebar({
  prefs,
  onPrefsChange,
  knownProviders,
  disabled = false,
  onLibraryOpen,
}: SidebarProps) {
  return (
    <div data-testid="sidebar">
      {onLibraryOpen ? (
        <Button variant="secondary" onClick={onLibraryOpen} data-testid="library-open">
          Open library
        </Button>
      ) : null}
      <GlassPanel variant="sidebar" id="ol-sidebar-panel" data-testid="sidebar-panel">
        <div>
          <h3>Request a URL</h3>
          <CrawlRequestForm disabled={disabled} />
        </div>
        <ProviderPrefsControl
          value={prefs}
          onChange={onPrefsChange}
          knownProviders={knownProviders}
          disabled={disabled}
        />
      </GlassPanel>
    </div>
  );
}
