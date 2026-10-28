export interface AppliedFiltersProps {
  appliedFilters: string[];
}

/**
 * One-line summary of the backend-reported applied filters. Rendered at
 * the foot of the sidebar next to the user-filter chips; the text
 * contract (`No filters applied` / `Applied filters: …`) is stable.
 */
export function AppliedFilters({ appliedFilters }: AppliedFiltersProps) {
  return (
    <p data-testid="applied-filters">
      {appliedFilters.length === 0
        ? 'No filters applied'
        : `Applied filters: ${appliedFilters.join(', ')}`}
    </p>
  );
}
