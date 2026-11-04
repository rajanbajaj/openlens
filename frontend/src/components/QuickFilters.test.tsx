import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { FILTER_DEBOUNCE_MS } from '../api/client.ts';
import { EMPTY_FILTER_STATE } from '../api/filters.ts';
import { QuickFilters } from './QuickFilters.tsx';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('QuickFilters', () => {
  it('emits the next filter state for date, safety, and sort edits', () => {
    const onChange = vi.fn();
    render(<QuickFilters value={EMPTY_FILTER_STATE} onChange={onChange} />);
    fireEvent.change(screen.getByLabelText('Date'), { target: { value: 'past-week' } });
    expect(onChange).toHaveBeenCalledWith({ ...EMPTY_FILTER_STATE, datePreset: 'past-week' });
    fireEvent.change(screen.getByLabelText('Safe search'), { target: { value: 'strict' } });
    expect(onChange).toHaveBeenCalledWith({ ...EMPTY_FILTER_STATE, safetyPreset: 'strict' });
    fireEvent.change(screen.getByLabelText('Sort by'), { target: { value: 'newest' } });
    expect(onChange).toHaveBeenCalledWith({ ...EMPTY_FILTER_STATE, sortPreset: 'newest' });
  });

  it('reveals custom date inputs only for the custom preset', () => {
    const onChange = vi.fn();
    const { rerender } = render(<QuickFilters value={EMPTY_FILTER_STATE} onChange={onChange} />);
    expect(screen.queryByLabelText('From')).not.toBeInTheDocument();
    rerender(
      <QuickFilters value={{ ...EMPTY_FILTER_STATE, datePreset: 'custom' }} onChange={onChange} />,
    );
    expect(screen.getByLabelText('From')).toBeInTheDocument();
  });

  it('debounces custom date typing into a single change', () => {
    vi.useFakeTimers();
    try {
      const onChange = vi.fn();
      render(
        <QuickFilters
          value={{ ...EMPTY_FILTER_STATE, datePreset: 'custom' }}
          onChange={onChange}
        />,
      );
      const from = screen.getByLabelText('From');
      fireEvent.change(from, { target: { value: '2026-10-01' } });
      expect(from).toHaveValue('2026-10-01');
      expect(onChange).not.toHaveBeenCalled();
      fireEvent.change(from, { target: { value: '2026-10-02' } });
      vi.advanceTimersByTime(FILTER_DEBOUNCE_MS);
      expect(onChange).toHaveBeenCalledTimes(1);
      expect(onChange).toHaveBeenCalledWith({
        ...EMPTY_FILTER_STATE,
        datePreset: 'custom',
        dateFrom: '2026-10-02',
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it('disables every field while a search is in flight', () => {
    render(<QuickFilters value={EMPTY_FILTER_STATE} onChange={vi.fn()} disabled />);
    expect(screen.getByLabelText('Date')).toBeDisabled();
    expect(screen.getByLabelText('Safe search')).toBeDisabled();
    expect(screen.getByLabelText('Sort by')).toBeDisabled();
  });
});
