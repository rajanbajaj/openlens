import { pageItems } from '../api/client.ts';
import { Button } from './ui/Button.tsx';
import { Select } from './ui/Select.tsx';

export interface PaginationProps {
  /** Current 1-based page. */
  page: number;
  /** Total page count at the active page size. */
  totalPages: number;
  /** Active page size. */
  pageSize: number;
  /** Page-size choices offered (bounded by the backend `MAX_LIMIT`). */
  pageSizeOptions?: number[];
  /** Jump to a 1-based page. */
  onPage: (page: number) => void;
  /** Change the page size (caller re-runs from page 1). */
  onPageSize: (size: number) => void;
}

/**
 * Numbered pagination with previous/next, elided page windows, a
 * `Page X of Y` indicator, and a results-per-page selector.
 * Test ids and the indicator text match the historical contract.
 */
export function Pagination({
  page,
  totalPages,
  pageSize,
  pageSizeOptions = [10, 25, 50],
  onPage,
  onPageSize,
}: PaginationProps) {
  return (
    <nav aria-label="Pagination" className="ol-pagination mt-4 flex flex-wrap items-center gap-4">
      <Button disabled={page <= 1} onClick={() => onPage(page - 1)}>
        Previous
      </Button>
      {pageItems(page, totalPages).map((item, index) =>
        item === 'gap' ? (
          <span key={`gap-${index}`} aria-hidden="true" className="text-ol-ink-soft">
            …
          </span>
        ) : (
          <Button
            key={item}
            variant={item === page ? 'primary' : 'ghost'}
            aria-current={item === page ? 'page' : undefined}
            aria-label={`Go to page ${item}`}
            onClick={() => onPage(item)}
          >
            {item}
          </Button>
        ),
      )}
      <Button disabled={page >= totalPages} onClick={() => onPage(page + 1)}>
        Next
      </Button>
      <span data-testid="page-indicator">
        Page {page} of {totalPages}
      </span>
      <Select
        label="Results per page"
        data-testid="page-size"
        value={String(pageSize)}
        options={pageSizeOptions.map((size) => ({
          value: String(size),
          label: String(size),
        }))}
        onChange={(value) => onPageSize(Number(value))}
      />
    </nav>
  );
}
