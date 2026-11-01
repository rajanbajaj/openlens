import type { UserFilterState } from '../api/filters.ts';
import { DATE_PRESET_OPTIONS, SAFETY_PRESETS } from '../api/filters.ts';
import FilterToggle from './FilterToggle.tsx';
import { Select } from './ui/Select.tsx';
import { TextInput } from './ui/TextInput.tsx';

export interface QuickFiltersProps {
  /** Raw filter state, controlled by the parent. */
  value: UserFilterState;
  /** Called with the next state on every field edit. */
  onChange: (next: UserFilterState) => void;
  /** Disables every field while a search is in flight. */
  disabled?: boolean;
}

/**
 * DuckDuckGo-style inline filter row: date, safe-search, sort, and
 * the sponsored toggle directly under the results header. This row is
 * the only filter surface; the sidebar carries non-filter utilities
 * only (library, URL requests, provider prefs). Edits re-run page 1
 * through the parent handler. The custom date inputs only render for
 * the custom preset.
 */
export function QuickFilters({ value, onChange, disabled = false }: QuickFiltersProps) {
  const set = (patch: Partial<UserFilterState>) => onChange({ ...value, ...patch });
  return (
    <div aria-label="Quick filters" data-testid="quick-filters" className="ol-quickfilters">
      <Select
        label="Date"
        id="quickfilter-date"
        name="quick_date_preset"
        value={value.datePreset}
        disabled={disabled}
        onChange={(next) => set({ datePreset: next })}
        options={DATE_PRESET_OPTIONS.map((option) => ({
          value: option.value,
          label: option.label,
        }))}
      />
      {value.datePreset === 'custom' && (
        <>
          <TextInput
            label="From"
            id="quickfilter-date-from"
            name="quick_date_from"
            type="date"
            value={value.dateFrom}
            disabled={disabled}
            onChange={(event) => set({ dateFrom: event.target.value })}
          />
          <TextInput
            label="To"
            id="quickfilter-date-to"
            name="quick_date_to"
            type="date"
            value={value.dateTo}
            disabled={disabled}
            onChange={(event) => set({ dateTo: event.target.value })}
          />
        </>
      )}
      <Select
        label="Safe search"
        id="quickfilter-safety"
        name="quick_safety_preset"
        value={value.safetyPreset}
        disabled={disabled}
        onChange={(next) => set({ safetyPreset: next })}
        options={[
          { value: '', label: 'Backend default' },
          ...SAFETY_PRESETS.map((preset) => ({
            value: preset,
            label: preset.charAt(0).toUpperCase() + preset.slice(1),
          })),
        ]}
      />
      <Select
        label="Sort by"
        id="quickfilter-sort"
        name="quick_sort"
        value={value.sortPreset}
        disabled={disabled}
        onChange={(next) => set({ sortPreset: next })}
        options={[
          { value: '', label: 'Best match' },
          { value: 'newest', label: 'Newest' },
          { value: 'most-trusted', label: 'Most trusted' },
        ]}
      />
      <FilterToggle
        checked={value.excludeSponsored}
        onChange={(checked) => set({ excludeSponsored: checked })}
        disabled={disabled}
      />
    </div>
  );
}
