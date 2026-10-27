export interface AppliedFiltersProps {
  appliedFilters: string[];
}

/** One-line summary of the backend-reported applied filters. */
export function AppliedFilters({ appliedFilters }: AppliedFiltersProps) {
  return (
    <p data-testid="applied-filters">
      {appliedFilters.length === 0
        ? 'No filters applied'
        : `Applied filters: ${appliedFilters.join(', ')}`}
    </p>
  );
}
