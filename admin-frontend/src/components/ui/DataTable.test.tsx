import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DataTable } from './DataTable.tsx';
import type { DataTableColumn } from './DataTable.tsx';

afterEach(() => {
  cleanup();
});

interface Row {
  id: string;
  url: string;
  attempts: number;
}

const ROWS: Row[] = [
  { id: 'a', url: 'https://example.com/a', attempts: 3 },
  { id: 'b', url: 'https://other.example/b', attempts: 1 },
  { id: 'c', url: 'https://example.com/c', attempts: 9 },
];

const COLUMNS: DataTableColumn<Row>[] = [
  {
    key: 'url',
    header: 'URL',
    sortable: true,
    sortValue: (row) => row.url,
    render: (row) => <span>{row.url}</span>,
  },
  {
    key: 'attempts',
    header: 'Tries',
    sortable: true,
    sortValue: (row) => row.attempts,
    render: (row) => <span>{row.attempts}</span>,
  },
];

function Harness({
  sort,
  onSort,
  search,
  onSearch,
  page,
  pageSize,
  total,
  selection,
  onSelect,
  rows = ROWS,
  error = null,
  onRetry,
}: {
  sort: { value: string; direction: 'asc' | 'desc' } | undefined;
  onSort: (key: string, dir: 'asc' | 'desc') => void;
  search: string;
  onSearch: (value: string) => void;
  page: number;
  pageSize: number;
  total?: number;
  selection: string[];
  onSelect: (keys: string[]) => void;
  rows?: Row[];
  error?: string | null;
  onRetry?: () => void;
}) {
  return (
    <DataTable<Row>
      rows={rows}
      columns={COLUMNS}
      rowKey={(row) => row.id}
      ariaLabel="Test rows"
      testIdPrefix="test-rows"
      search={{
        value: search,
        onChange: onSearch,
        placeholder: 'Search…',
        ariaLabel: 'Search rows',
        keys: ['url'],
      }}
      sort={sort ? { ...sort, onChange: onSort } : undefined}
      pagination={{ page, pageSize, total, onPage: () => {} }}
      selection={{ selected: selection, onChange: onSelect, label: (n) => `${n} selected` }}
      error={error}
      onRetry={onRetry}
    />
  );
}

describe('DataTable', () => {
  it('sorts by a sortable column and marks aria-sort', () => {
    const onSort = vi.fn();
    render(
      <Harness
        sort={{ value: 'attempts', direction: 'desc' }}
        onSort={onSort}
        search=""
        onSearch={() => {}}
        page={0}
        pageSize={10}
        selection={[]}
        onSelect={() => {}}
      />,
    );
    expect(screen.getByRole('columnheader', { name: /Tries/ })).toHaveAttribute(
      'aria-sort',
      'descending',
    );
    expect(screen.getAllByRole('row')[1]).toHaveTextContent('example.com/c');

    fireEvent.click(screen.getByRole('button', { name: 'Sort by URL' }));
    expect(onSort).toHaveBeenCalledWith('url', 'asc');
  });

  it('filters rows through the search box', async () => {
    const onSearch = vi.fn();
    const { rerender } = render(
      <Harness
        sort={undefined}
        onSort={() => {}}
        search=""
        onSearch={onSearch}
        page={0}
        pageSize={10}
        selection={[]}
        onSelect={() => {}}
      />,
    );
    fireEvent.change(screen.getByTestId('search-input-field'), {
      target: { value: 'other.example' },
    });
    await waitFor(() => {
      expect(onSearch).toHaveBeenCalledWith('other.example');
    });
    rerender(
      <Harness
        sort={undefined}
        onSort={() => {}}
        search="other.example"
        onSearch={onSearch}
        page={0}
        pageSize={10}
        selection={[]}
        onSelect={() => {}}
      />,
    );
    await waitFor(() => {
      expect(screen.getByTestId('test-rows')).not.toHaveTextContent('example.com/a');
    });
    expect(screen.getByTestId('test-rows')).toHaveTextContent('other.example/b');
  });

  it('pages client-side when no total is known', () => {
    render(
      <Harness
        sort={undefined}
        onSort={() => {}}
        search=""
        onSearch={() => {}}
        page={0}
        pageSize={2}
        selection={[]}
        onSelect={() => {}}
      />,
    );
    expect(screen.getByTestId('test-rows-page')).toHaveTextContent('Page 1');
    expect(screen.getByTestId('test-rows')).toHaveTextContent('example.com/a');
    expect(screen.getByTestId('test-rows')).not.toHaveTextContent('example.com/c');
    expect(screen.getByTestId('test-rows-next')).not.toBeDisabled();
  });

  it('shows server totals and disables Next on the last page', () => {
    render(
      <Harness
        sort={undefined}
        onSort={() => {}}
        search=""
        onSearch={() => {}}
        page={1}
        pageSize={2}
        total={3}
        selection={[]}
        onSelect={() => {}}
      />,
    );
    expect(screen.getByTestId('test-rows-page')).toHaveTextContent('Page 2 of 2');
    expect(screen.getByTestId('test-rows-next')).toBeDisabled();
    expect(screen.getByTestId('test-rows-prev')).not.toBeDisabled();
  });

  it('selects rows and reports the count label', () => {
    const onSelect = vi.fn();
    render(
      <Harness
        sort={undefined}
        onSort={() => {}}
        search=""
        onSearch={() => {}}
        page={0}
        pageSize={10}
        selection={['a']}
        onSelect={onSelect}
      />,
    );
    expect(screen.getByText('1 selected')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('test-rows-select-b'));
    expect(onSelect).toHaveBeenCalledWith(['a', 'b']);
    fireEvent.click(screen.getByTestId('test-rows-select-all'));
    expect(onSelect).toHaveBeenCalledWith(['a', 'b', 'c']);
  });

  it('renders the empty state and the error panel with retry', () => {
    const onRetry = vi.fn();
    const { rerender } = render(
      <Harness
        sort={undefined}
        onSort={() => {}}
        search=""
        onSearch={() => {}}
        page={0}
        pageSize={10}
        selection={[]}
        onSelect={() => {}}
        rows={[]}
      />,
    );
    expect(screen.getByTestId('empty-state')).toBeInTheDocument();
    rerender(
      <Harness
        sort={undefined}
        onSort={() => {}}
        search=""
        onSearch={() => {}}
        page={0}
        pageSize={10}
        selection={[]}
        onSelect={() => {}}
        error="Boom"
        onRetry={onRetry}
      />,
    );
    expect(screen.getByRole('alert')).toHaveTextContent('Boom');
  });
});
