import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SearchInput } from './SearchInput.tsx';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('SearchInput', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  it('associates the label and debounces edits', () => {
    const onChange = vi.fn();
    render(
      <SearchInput
        value=""
        onChange={onChange}
        placeholder="Search…"
        ariaLabel="Search rows"
        debounceMs={150}
      />,
    );
    const field = screen.getByTestId('search-input-field');
    expect(field).toHaveAccessibleName('Search rows');
    fireEvent.change(field, { target: { value: 'exa' } });
    expect(onChange).not.toHaveBeenCalled();
    act(() => {
      vi.advanceTimersByTime(150);
    });
    expect(onChange).toHaveBeenCalledWith('exa');
  });

  it('clears synchronously and labels the clear button', () => {
    const onChange = vi.fn();
    render(
      <SearchInput
        value="example"
        onChange={onChange}
        placeholder="Search…"
        ariaLabel="Search rows"
        debounceMs={0}
      />,
    );
    const clear = screen.getByTestId('search-input-clear');
    expect(clear).toHaveAttribute('aria-label', 'Clear Search rows');
    fireEvent.click(clear);
    expect(onChange).toHaveBeenCalledWith('');
  });
});
