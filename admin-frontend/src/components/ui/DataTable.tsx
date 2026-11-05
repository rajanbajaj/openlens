import { useMemo } from 'react';
import { EmptyState } from './EmptyState.tsx';
import { SearchInput } from './SearchInput.tsx';
import { Skeleton } from './Skeleton.tsx';

export interface DataTableColumn<T> {
  key: string;
  header: string;
  sortable?: boolean;
  render: (row: T) => React.ReactNode;
  sortValue?: (row: T) => string | number;
  align?: 'start' | 'end';
}

export interface DataTableSearch<T> {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  ariaLabel: string;
  keys: (keyof T | ((row: T) => string))[];
}

export interface DataTableSort {
  value: string;
  direction: 'asc' | 'desc';
  onChange: (key: string, direction: 'asc' | 'desc') => void;
}

export interface DataTablePagination {
  page: number;
  pageSize: number;
  /** Server-known total. Unknown → "Page N" + Next disabled on short page. */
  total?: number;
  onPage: (page: number) => void;
}

export interface DataTableSelection {
  selected: string[];
  onChange: (keys: string[]) => void;
  label: (count: number) => string;
}

export interface DataTableProps<T> {
  rows: T[];
  columns: DataTableColumn<T>[];
  rowKey: (row: T) => string;
  search?: DataTableSearch<T>;
  sort?: DataTableSort;
  pagination: DataTablePagination;
  selection?: DataTableSelection;
  empty?: { title: string; hint?: string; testId?: string };
  error?: string | null;
  onRetry?: () => void;
  loading?: boolean;
  /** Accessible name for the table plus every pager ("Failed domains pager"). */
  ariaLabel: string;
  /** Prefix for pager/selection testids (`${prefix}-prev`, …). */
  testIdPrefix?: string;
}

function searchText<T>(row: T, keys: DataTableSearch<T>['keys']): string {
  return keys
    .map((key) => (typeof key === 'function' ? key(row) : String(row[key] ?? '')))
    .join(' ')
    .toLowerCase();
}

/**
 * Shared table primitive: client-side search + sort over the fetched
 * page, server offset paging via `pagination.onPage`, and optional
 * row selection. Backend list endpoints take only limit/offset, so
 * search/sort stay client-side (server-side is a backend follow-up).
 * Loading renders `Skeleton` rows (never zeros); errors render an
 * inline panel with retry.
 */
export function DataTable<T>({
  rows,
  columns,
  rowKey,
  search,
  sort,
  pagination,
  selection,
  empty,
  error,
  onRetry,
  loading,
  ariaLabel,
  testIdPrefix,
}: DataTableProps<T>) {
  const query = (search?.value ?? '').trim().toLowerCase();
  const filtered = useMemo(() => {
    if (!search || query === '') return rows;
    return rows.filter((row) => searchText(row, search.keys).includes(query));
  }, [rows, search, query]);

  const sorted = useMemo(() => {
    if (!sort) return filtered;
    const column = columns.find((entry) => entry.key === sort.value && entry.sortable);
    if (!column?.sortValue) return filtered;
    const get = column.sortValue;
    const direction = sort.direction === 'desc' ? -1 : 1;
    return [...filtered].sort((a, b) => {
      const left = get(a);
      const right = get(b);
      if (typeof left === 'number' && typeof right === 'number') {
        return (left - right) * direction;
      }
      return String(left).localeCompare(String(right)) * direction;
    });
  }, [filtered, sort, columns]);

  const { page, pageSize, total, onPage } = pagination;
  const serverMode = total !== undefined;
  const pageCount = serverMode ? Math.max(1, Math.ceil(total / pageSize)) : 1;
  const visible = serverMode ? sorted : sorted.slice(page * pageSize, page * pageSize + pageSize);
  const nextDisabled = serverMode ? page >= pageCount - 1 : sorted.length <= pageSize;

  const selectedSet = useMemo(() => new Set(selection?.selected ?? []), [selection]);
  const visibleKeys = visible.map(rowKey);
  const allVisibleSelected =
    visibleKeys.length > 0 && visibleKeys.every((key) => selectedSet.has(key));

  function toggleRow(key: string): void {
    if (!selection) return;
    const next = selectedSet.has(key)
      ? selection.selected.filter((entry) => entry !== key)
      : [...selection.selected, key];
    selection.onChange(next);
  }

  function toggleAll(): void {
    if (!selection) return;
    selection.onChange(
      allVisibleSelected
        ? selection.selected.filter((key) => !visibleKeys.includes(key))
        : [...selection.selected, ...visibleKeys.filter((key) => !selectedSet.has(key))],
    );
  }

  function handleSort(column: DataTableColumn<T>): void {
    if (!column.sortable || !sort) return;
    if (sort.value === column.key) {
      sort.onChange(column.key, sort.direction === 'asc' ? 'desc' : 'asc');
    } else {
      sort.onChange(column.key, 'asc');
    }
  }

  const pagerLabel = `${ariaLabel} pager`;
  const tid = (suffix: string): string | undefined =>
    testIdPrefix ? `${testIdPrefix}-${suffix}` : undefined;

  return (
    <div className="ol-table-wrap">
      {search ? (
        <div className="my-ol-s">
          <SearchInput
            value={search.value}
            onChange={search.onChange}
            placeholder={search.placeholder}
            ariaLabel={search.ariaLabel}
          />
        </div>
      ) : null}
      {selection && selection.selected.length > 0 ? (
        <p className="my-ol-s text-ol-s" role="status">
          {selection.label(selection.selected.length)}
        </p>
      ) : null}
      {error ? (
        <div
          role="alert"
          className="my-ol-s rounded-ol-m border border-ol-error bg-ol-error-wash p-ol-m text-ol-s"
        >
          <p>{error}</p>
          {onRetry ? (
            <button
              type="button"
              onClick={onRetry}
              className="ol-button ol-button-secondary mt-ol-s text-ol-s font-semibold rounded-ol-s border border-transparent px-ol-s py-ol-s cursor-pointer"
            >
              Retry
            </button>
          ) : null}
        </div>
      ) : loading ? (
        <div aria-live="polite">
          <Skeleton bars={5} />
        </div>
      ) : sorted.length === 0 ? (
        <div data-testid={empty?.testId}>
          <EmptyState title={empty?.title ?? 'No rows'} hint={empty?.hint} />
        </div>
      ) : (
        <table aria-label={ariaLabel} data-testid={testIdPrefix} className="ol-table">
          <thead>
            <tr>
              {selection ? (
                <th scope="col">
                  <input
                    type="checkbox"
                    checked={allVisibleSelected}
                    onChange={toggleAll}
                    aria-label={`Select all rows in ${ariaLabel}`}
                    data-testid={tid('select-all')}
                  />
                </th>
              ) : null}
              {columns.map((column) => (
                <th
                  key={column.key}
                  scope="col"
                  aria-sort={
                    sort && column.sortable
                      ? sort.value === column.key
                        ? sort.direction === 'asc'
                          ? 'ascending'
                          : 'descending'
                        : 'none'
                      : undefined
                  }
                  className={column.align === 'end' ? 'ol-table-num' : undefined}
                >
                  {column.sortable && sort ? (
                    <button
                      type="button"
                      onClick={() => handleSort(column)}
                      aria-label={`Sort by ${column.header}`}
                      className="cursor-pointer underline"
                    >
                      {column.header}
                      {sort.value === column.key ? (sort.direction === 'asc' ? ' ▲' : ' ▼') : ''}
                    </button>
                  ) : (
                    column.header
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visible.map((row) => {
              const key = rowKey(row);
              return (
                <tr key={key}>
                  {selection ? (
                    <td>
                      <input
                        type="checkbox"
                        checked={selectedSet.has(key)}
                        onChange={() => toggleRow(key)}
                        aria-label={`Select row ${key}`}
                        data-testid={tid(`select-${key}`)}
                      />
                    </td>
                  ) : null}
                  {columns.map((column) => (
                    <td
                      key={column.key}
                      className={column.align === 'end' ? 'ol-table-num' : undefined}
                    >
                      {column.render(row)}
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      <div
        className="mt-ol-s flex flex-wrap items-center gap-ol-s text-ol-s"
        aria-label={pagerLabel}
      >
        <button
          type="button"
          data-testid={tid('prev')}
          disabled={page <= 0}
          onClick={() => onPage(Math.max(0, page - 1))}
          className="ol-button ol-button-ghost"
        >
          Previous
        </button>
        <span data-testid={tid('page')}>
          {serverMode ? `Page ${page + 1} of ${pageCount}` : `Page ${page + 1}`}
        </span>
        <button
          type="button"
          data-testid={tid('next')}
          disabled={nextDisabled}
          onClick={() => onPage(page + 1)}
          className="ol-button ol-button-ghost"
        >
          Next
        </button>
        {serverMode && pageCount > 1 ? (
          <label className="flex items-center gap-ol-xs">
            Go to
            <input
              data-testid={tid('jump')}
              type="number"
              min={1}
              max={pageCount}
              defaultValue={page + 1}
              key={`dt-jump-${page}-${pageCount}`}
              className="w-16 font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-xs py-ol-xs"
              onKeyDown={(event) => {
                if (event.key !== 'Enter') return;
                const next = Number((event.target as HTMLInputElement).value);
                if (Number.isInteger(next) && next >= 1 && next <= pageCount) {
                  onPage(next - 1);
                }
              }}
            />
          </label>
        ) : null}
        {serverMode && pageCount > 1 ? (
          <button
            type="button"
            data-testid={tid('go')}
            className="ol-button ol-button-ghost"
            onClick={() => {
              const input = document.querySelector<HTMLInputElement>(
                `[data-testid="${testIdPrefix}-jump"]`,
              );
              const next = Number(input?.value);
              if (Number.isInteger(next) && next >= 1 && next <= pageCount) {
                onPage(next - 1);
              }
            }}
          >
            Go
          </button>
        ) : null}
      </div>
    </div>
  );
}
