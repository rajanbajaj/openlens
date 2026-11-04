import { useEffect, useRef, useState } from 'react';
import { FILTER_DEBOUNCE_MS } from '../api/client.ts';
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
 * only (library, URL requests, provider prefs, applied filters). Edits
 * re-run page 1 through the parent handler. The custom date inputs
 * only render for the custom preset.
 *
 * Selects and the toggle apply immediately (one discrete change is
 * already polite). The custom date fields edit a local draft so
 * typing stays instant, and propagate after FILTER_DEBOUNCE_MS of
 * quiet — without the delay every typed digit would re-run the
 * search. The draft re-syncs when the parent state changes
 * from elsewhere.
 */
export function QuickFilters({ value, onChange, disabled = false }: QuickFiltersProps) {
  const set = (patch: Partial<UserFilterState>) => onChange({ ...value, ...patch });

  const [draftDates, setDraftDates] = useState({ from: value.dateFrom, to: value.dateTo });
  const parentDates = `${value.dateFrom}|${value.dateTo}`;
  const [syncedDates, setSyncedDates] = useState(parentDates);
  if (parentDates !== syncedDates) {
    setSyncedDates(parentDates);
    setDraftDates({ from: value.dateFrom, to: value.dateTo });
  }

  const latestRef = useRef({ value, onChange });
  latestRef.current = { value, onChange };

  useEffect(() => {
    const current = latestRef.current;
    if (draftDates.from === current.value.dateFrom && draftDates.to === current.value.dateTo) {
      return;
    }
    const timer = window.setTimeout(() => {
      const next = latestRef.current;
      next.onChange({ ...next.value, dateFrom: draftDates.from, dateTo: draftDates.to });
    }, FILTER_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [draftDates]);

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
            value={draftDates.from}
            disabled={disabled}
            onChange={(event) => setDraftDates({ ...draftDates, from: event.target.value })}
          />
          <TextInput
            label="To"
            id="quickfilter-date-to"
            name="quick_date_to"
            type="date"
            value={draftDates.to}
            disabled={disabled}
            onChange={(event) => setDraftDates({ ...draftDates, to: event.target.value })}
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
