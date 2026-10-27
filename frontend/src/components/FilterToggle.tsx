/** `filters`-dict key toggled by {@link FilterToggle}. */
export const EXCLUDE_SPONSORED_FILTER_KEY = 'exclude_sponsored';

export interface FilterToggleProps {
  /** Whether the filter is currently applied. */
  checked: boolean;
  /** Called with the new toggle state. */
  onChange: (checked: boolean) => void;
  /** Disables the toggle while a search is in flight. */
  disabled?: boolean;
}

/**
 * Map the "exclude sponsored" toggle into the request `filters` dict:
 * on → `{ [key]: true }` merged over existing filters; off → the key
 * removed, every other entry preserved.
 */
export function applyExcludeSponsoredFilter(
  filters: Record<string, unknown>,
  checked: boolean,
): Record<string, unknown> {
  if (checked) return { ...filters, [EXCLUDE_SPONSORED_FILTER_KEY]: true };
  const next = { ...filters };
  delete next[EXCLUDE_SPONSORED_FILTER_KEY];
  return next;
}

/**
 * Single filter toggle wired by the parent into the search request
 * `filters` dict via {@link applyExcludeSponsoredFilter}.
 */
export default function FilterToggle({ checked, onChange, disabled = false }: FilterToggleProps) {
  return (
    <label>
      <input
        type="checkbox"
        aria-label="Exclude sponsored"
        checked={checked}
        disabled={disabled}
        onChange={(event: React.ChangeEvent<HTMLInputElement>) => {
          onChange(event.target.checked);
        }}
      />
      Exclude sponsored
    </label>
  );
}
