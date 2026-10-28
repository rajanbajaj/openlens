import type { UserFilterState } from '../api/filters.ts';
import { TextInput } from './ui/TextInput.tsx';

export interface FilterPanelProps {
  /** Raw panel state, controlled by the parent. */
  value: UserFilterState;
  /** Called with the next state on every field edit. */
  onChange: (next: UserFilterState) => void;
  /** Disables every field while a search is in flight. */
  disabled?: boolean;
}

/**
 * Advanced-filters panel generated from the backend T10 filter schema:
 * reserved consumer keys (`max_price`, `location`, `facilities`,
 * `prefer_*`) plus the user-restrictions block/allow indicators. Every
 * field maps to a backend-owned wire key via `buildFilters` — blank
 * inputs send nothing.
 *
 * Grouped into short sidebar sections (commerce, preference, domains);
 * labels, input ids/names, and the section accessible name are stable
 * so existing queries keep working.
 */
export function FilterPanel({ value, onChange, disabled = false }: FilterPanelProps) {
  const set = (patch: Partial<UserFilterState>) => onChange({ ...value, ...patch });
  return (
    <section aria-label="Advanced filters">
      <h2>Filters</h2>
      <div>
        <h3>Commerce</h3>
        <TextInput
          label="Max price"
          id="filter-max-price"
          name="max_price"
          inputMode="decimal"
          placeholder="e.g. 250"
          value={value.maxPrice}
          disabled={disabled}
          onChange={(event) => set({ maxPrice: event.target.value })}
        />
        <TextInput
          label="Location"
          id="filter-location"
          name="location"
          placeholder="e.g. Berlin"
          value={value.location}
          disabled={disabled}
          onChange={(event) => set({ location: event.target.value })}
        />
        <TextInput
          label="Facilities"
          id="filter-facilities"
          name="facilities"
          placeholder="e.g. wifi, pool"
          value={value.facilities}
          disabled={disabled}
          onChange={(event) => set({ facilities: event.target.value })}
        />
      </div>
      <div>
        <h3>Preference</h3>
        <TextInput
          label="Prefer (preference keyword)"
          id="filter-prefer"
          name="prefer"
          placeholder="e.g. trust"
          value={value.preferSuffix}
          disabled={disabled}
          onChange={(event) => set({ preferSuffix: event.target.value })}
        />
      </div>
      <div>
        <h3>Domains and excluded terms</h3>
        <TextInput
          label="Blocked domain"
          id="filter-blocked-domain"
          name="blocked_domain"
          placeholder="e.g. example.com"
          value={value.blockedDomain}
          disabled={disabled}
          onChange={(event) => set({ blockedDomain: event.target.value })}
        />
        <TextInput
          label="Allowed domain"
          id="filter-allowed-domain"
          name="allowed_domain"
          placeholder="e.g. trusted.example"
          value={value.allowedDomain}
          disabled={disabled}
          onChange={(event) => set({ allowedDomain: event.target.value })}
        />
        <TextInput
          label="Exclude term"
          id="filter-must-not-term"
          name="must_not_term"
          placeholder="e.g. advertisement"
          value={value.mustNotTerm}
          disabled={disabled}
          onChange={(event) => set({ mustNotTerm: event.target.value })}
        />
      </div>
    </section>
  );
}
