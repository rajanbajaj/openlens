export interface FilterToggleProps {
  /** Whether the filter is currently applied. */
  checked: boolean;
  /** Called with the new toggle state. */
  onChange: (checked: boolean) => void;
  /** Disables the toggle while a search is in flight. */
  disabled?: boolean;
}

/**
 * Single filter toggle. The parent maps the state into the request
 * `filters` dict via `buildFilters` (`api/filters.ts`), which emits
 * only backend-owned keys — the toggle itself knows no wire keys.
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
