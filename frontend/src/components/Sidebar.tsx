import type { UserFilterState } from '../api/filters.ts';
import type { ProviderPrefs } from '../api/types.ts';
import { AppliedFilters } from './AppliedFilters.tsx';
import { FilterPanel } from './FilterPanel.tsx';
import FilterToggle from './FilterToggle.tsx';
import { ProviderPrefsControl } from './ProviderPrefsControl.tsx';
import { Button } from './ui/Button.tsx';
import { Chip } from './ui/Chip.tsx';
import { GlassPanel } from './ui/GlassPanel.tsx';

export interface SidebarProps {
  /** Raw filter state, controlled by the shell. */
  filters: UserFilterState;
  /** Called with the next filter state on any filter edit or chip clear. */
  onFiltersChange: (next: UserFilterState) => void;
  /** Federation prefs, controlled by the shell; null primary means no preference. */
  prefs: ProviderPrefs;
  /** Called with the next prefs on every prefs edit. */
  onPrefsChange: (next: ProviderPrefs) => void;
  /** Provider names seen in this session, offered as suggestions. */
  knownProviders: string[];
  /** Backend-reported `applied_filters` attribution strings. */
  appliedFilters: string[];
  /** Disables every control while a search is in flight. */
  disabled?: boolean;
  /**
   * Collapsed state, controlled by the shell. When true the frosted panel
   * is hidden and only the toggle button renders — the mobile posture.
   * The shell should initialize this to true on narrow viewports.
   */
  collapsed: boolean;
  /** Called when the user toggles collapse. */
  onCollapsedChange: (collapsed: boolean) => void;
  /**
   * Optional hook fired after a chip clear with the cleared state, so the
   * shell can re-run the search. Chip clears only update state otherwise.
   */
  onChipRemove?: (cleared: UserFilterState) => void;
  /**
   * Optional hook opening the personal-library drawer, owned by the
   * shell. Filters stay owned here; the library affordance only
   * signals the shell to mount the drawer entry point.
   */
  onLibraryOpen?: () => void;
}

interface SidebarChip {
  key: string;
  label: string;
  clear: (filters: UserFilterState) => UserFilterState;
}

/**
 * User-set filters rendered as removable chips. Same set and labels as
 * the App-level chip row this sidebar replaces; clearing a chip edits
 * the matching `UserFilterState` field only.
 */
function sidebarChips(filters: UserFilterState): SidebarChip[] {
  const chips: SidebarChip[] = [];
  if (filters.excludeSponsored) {
    chips.push({
      key: 'excludeSponsored',
      label: 'Excludes sponsored',
      clear: (next) => ({ ...next, excludeSponsored: false }),
    });
  }
  if (filters.maxPrice.trim()) {
    chips.push({
      key: 'maxPrice',
      label: `Max price ${filters.maxPrice.trim()}`,
      clear: (next) => ({ ...next, maxPrice: '' }),
    });
  }
  if (filters.minRating.trim()) {
    chips.push({
      key: 'minRating',
      label: `Min rating ${filters.minRating.trim()}`,
      clear: (next) => ({ ...next, minRating: '' }),
    });
  }
  if (filters.location.trim()) {
    chips.push({
      key: 'location',
      label: `Location: ${filters.location.trim()}`,
      clear: (next) => ({ ...next, location: '' }),
    });
  }
  if (filters.facilities.trim()) {
    chips.push({
      key: 'facilities',
      label: `Facilities: ${filters.facilities.trim()}`,
      clear: (next) => ({ ...next, facilities: '' }),
    });
  }
  if (filters.brand.trim()) {
    chips.push({
      key: 'brand',
      label: `Brand: ${filters.brand.trim()}`,
      clear: (next) => ({ ...next, brand: '' }),
    });
  }
  if (filters.style.trim()) {
    chips.push({
      key: 'style',
      label: `Style: ${filters.style.trim()}`,
      clear: (next) => ({ ...next, style: '' }),
    });
  }
  if (filters.size.trim()) {
    chips.push({
      key: 'size',
      label: `Size: ${filters.size.trim()}`,
      clear: (next) => ({ ...next, size: '' }),
    });
  }
  if (filters.preferSuffix.trim()) {
    chips.push({
      key: 'preferSuffix',
      label: `Prefers ${filters.preferSuffix.trim()}`,
      clear: (next) => ({ ...next, preferSuffix: '' }),
    });
  }
  if (filters.blockedDomain.trim()) {
    chips.push({
      key: 'blockedDomain',
      label: `Blocked: ${filters.blockedDomain.trim()}`,
      clear: (next) => ({ ...next, blockedDomain: '' }),
    });
  }
  if (filters.allowedDomain.trim()) {
    chips.push({
      key: 'allowedDomain',
      label: `Only: ${filters.allowedDomain.trim()}`,
      clear: (next) => ({ ...next, allowedDomain: '' }),
    });
  }
  if (filters.mustNotTerm.trim()) {
    chips.push({
      key: 'mustNotTerm',
      label: `Excludes “${filters.mustNotTerm.trim()}”`,
      clear: (next) => ({ ...next, mustNotTerm: '' }),
    });
  }
  return chips;
}

/**
 * Collapsible filter sidebar: removable user-filter chips, the sponsored
 * toggle, the advanced panel, provider prefs, and backend
 * `applied_filters` attribution inside one frosted panel.
 *
 * Shell contract (integrate wave): render `<Sidebar>` in the
 * `.ol-shell-sidebar` region of `.ol-app-shell` and lift `filters`,
 * `prefs`, `knownProviders`, `appliedFilters`, `disabled`, and
 * `collapsed` from App state. `collapsed=true` keeps the toggle button
 * visible for in-region expand; the shell-level
 * `.ol-app-shell-collapsed` class instead hides the whole region and
 * needs its own expand control in the results column. Wire keys are
 * unchanged — the shell still sends `buildFilters(filters)` plus prefs.
 */
export function Sidebar({
  filters,
  onFiltersChange,
  prefs,
  onPrefsChange,
  knownProviders,
  appliedFilters,
  disabled = false,
  collapsed,
  onCollapsedChange,
  onChipRemove,
  onLibraryOpen,
}: SidebarProps) {
  const chips = sidebarChips(filters);

  function removeChip(chip: SidebarChip) {
    const cleared = chip.clear(filters);
    onFiltersChange(cleared);
    onChipRemove?.(cleared);
  }

  return (
    <div data-testid="sidebar">
      <Button
        variant="secondary"
        aria-expanded={!collapsed}
        aria-controls="ol-sidebar-panel"
        disabled={disabled}
        onClick={() => onCollapsedChange(!collapsed)}
      >
        {collapsed ? 'Show filters' : 'Hide filters'}
      </Button>
      {onLibraryOpen ? (
        <Button variant="secondary" onClick={onLibraryOpen} data-testid="library-open">
          Open library
        </Button>
      ) : null}
      <GlassPanel
        variant="sidebar"
        id="ol-sidebar-panel"
        data-testid="sidebar-panel"
        hidden={collapsed || undefined}
      >
        {chips.length > 0 ? (
          <div data-testid="user-filter-chips">
            {chips.map((chip) => (
              <Chip
                key={chip.key}
                onRemove={() => removeChip(chip)}
                removeLabel={`Remove ${chip.label}`}
              >
                {chip.label}
              </Chip>
            ))}
          </div>
        ) : null}
        <FilterToggle
          checked={filters.excludeSponsored}
          onChange={(checked) => onFiltersChange({ ...filters, excludeSponsored: checked })}
          disabled={disabled}
        />
        <FilterPanel value={filters} onChange={onFiltersChange} disabled={disabled} />
        <ProviderPrefsControl
          value={prefs}
          onChange={onPrefsChange}
          knownProviders={knownProviders}
          disabled={disabled}
        />
        <AppliedFilters appliedFilters={appliedFilters} />
      </GlassPanel>
    </div>
  );
}
