import type { UserFilterState } from '../api/filters.ts';
import { Select } from './ui/Select.tsx';
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
 * `prefer_*`, `personalize`, `safety_preset`) plus the user-restrictions
 * block/allow indicators. Every field maps to a backend-owned wire key
 * via `buildFilters` — blank inputs send nothing.
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
          label="Min rating"
          id="filter-min-rating"
          name="min_rating"
          inputMode="decimal"
          placeholder="e.g. 4"
          value={value.minRating}
          disabled={disabled}
          onChange={(event) => set({ minRating: event.target.value })}
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
        <TextInput
          label="Brand"
          id="filter-brand"
          name="brand"
          placeholder="e.g. Acme"
          value={value.brand}
          disabled={disabled}
          onChange={(event) => set({ brand: event.target.value })}
        />
        <TextInput
          label="Style"
          id="filter-style"
          name="style"
          placeholder="e.g. casual"
          value={value.style}
          disabled={disabled}
          onChange={(event) => set({ style: event.target.value })}
        />
        <TextInput
          label="Size"
          id="filter-size"
          name="size"
          placeholder="e.g. M"
          value={value.size}
          disabled={disabled}
          onChange={(event) => set({ size: event.target.value })}
        />
        <TextInput
          label="Occasion"
          id="filter-occasion"
          name="occasion"
          placeholder="e.g. wedding"
          value={value.occasion}
          disabled={disabled}
          onChange={(event) => set({ occasion: event.target.value })}
        />
        <label className="ol-filter-toggle">
          <input
            type="checkbox"
            aria-label="Exclude over-budget results"
            checked={value.excludeOverBudget}
            disabled={disabled}
            onChange={(event) => onChange({ ...value, excludeOverBudget: event.target.checked })}
          />
          Exclude over-budget results
        </label>
      </div>
      <div>
        <h3>Books</h3>
        <TextInput
          label="License"
          id="filter-license"
          name="license"
          placeholder="e.g. CC-BY"
          value={value.license}
          disabled={disabled}
          onChange={(event) => set({ license: event.target.value })}
        />
        <TextInput
          label="Author"
          id="filter-author"
          name="author"
          placeholder="e.g. Jane Austen"
          value={value.author}
          disabled={disabled}
          onChange={(event) => set({ author: event.target.value })}
        />
      </div>
      <div>
        <h3>Food</h3>
        <TextInput
          label="Cuisine"
          id="filter-cuisine"
          name="cuisine"
          placeholder="e.g. Italian"
          value={value.cuisine}
          disabled={disabled}
          onChange={(event) => set({ cuisine: event.target.value })}
        />
        <TextInput
          label="Dietary needs"
          id="filter-dietary"
          name="dietary_tags"
          placeholder="e.g. vegan, gluten-free"
          value={value.dietaryTags}
          disabled={disabled}
          onChange={(event) => set({ dietaryTags: event.target.value })}
        />
        <TextInput
          label="Must-have ingredients"
          id="filter-ingredients"
          name="ingredients"
          placeholder="e.g. tomato, basil"
          value={value.ingredients}
          disabled={disabled}
          onChange={(event) => set({ ingredients: event.target.value })}
        />
        <TextInput
          label="Exclude allergens"
          id="filter-allergens"
          name="exclude_allergens"
          placeholder="e.g. peanuts, shellfish"
          value={value.excludeAllergens}
          disabled={disabled}
          onChange={(event) => set({ excludeAllergens: event.target.value })}
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
        <h3>Control</h3>
        <label className="ol-filter-toggle">
          <input
            type="checkbox"
            aria-label="Personalize with my preferences"
            checked={value.personalize}
            disabled={disabled}
            onChange={(event) => onChange({ ...value, personalize: event.target.checked })}
          />
          Personalize with my preferences
        </label>
        <Select
          label="Safety preset"
          id="filter-safety-preset"
          name="safety_preset"
          value={value.safetyPreset}
          disabled={disabled}
          onChange={(next) => set({ safetyPreset: next })}
          options={[
            { value: '', label: 'Backend default' },
            { value: 'standard', label: 'Standard' },
            { value: 'balanced', label: 'Balanced' },
            { value: 'strict', label: 'Strict' },
          ]}
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
