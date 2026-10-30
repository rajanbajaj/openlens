import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { pageItems } from '../api/client.ts';
import { Pagination } from './Pagination.tsx';

afterEach(() => {
  cleanup();
});

describe('pageItems', () => {
  it('lists small totals fully', () => {
    expect(pageItems(1, 1)).toEqual([1]);
    expect(pageItems(2, 3)).toEqual([1, 2, 3]);
  });

  it('elides middle ranges with gaps', () => {
    expect(pageItems(1, 10)).toEqual([1, 2, 'gap', 10]);
    expect(pageItems(5, 10)).toEqual([1, 'gap', 4, 5, 6, 'gap', 10]);
    expect(pageItems(10, 10)).toEqual([1, 'gap', 9, 10]);
  });

  it('clamps out-of-range pages and empties', () => {
    expect(pageItems(0, 0)).toEqual([]);
    expect(pageItems(99, 4)).toEqual([1, 'gap', 3, 4]);
  });
});

function renderPagination(page: number, totalPages: number) {
  const onPage = vi.fn();
  const onPageSize = vi.fn();
  render(
    <Pagination
      page={page}
      totalPages={totalPages}
      pageSize={10}
      onPage={onPage}
      onPageSize={onPageSize}
    />,
  );
  return { onPage, onPageSize };
}

describe('Pagination', () => {
  it('marks the current page and jumps on number click', () => {
    const { onPage } = renderPagination(5, 10);
    expect(screen.getByRole('button', { name: 'Go to page 5' })).toHaveAttribute(
      'aria-current',
      'page',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Go to page 6' }));
    expect(onPage).toHaveBeenCalledWith(6);
  });

  it('disables Previous on the first page and Next on the last', () => {
    renderPagination(1, 3);
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled();
    cleanup();
    renderPagination(3, 3);
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
  });

  it('keeps the indicator text and reports size changes', () => {
    const { onPageSize } = renderPagination(2, 3);
    expect(screen.getByTestId('page-indicator')).toHaveTextContent('Page 2 of 3');
    fireEvent.change(screen.getByTestId('page-size'), { target: { value: '25' } });
    expect(onPageSize).toHaveBeenCalledWith(25);
  });
});
