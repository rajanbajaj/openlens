import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { FILTER_DEBOUNCE_MS } from '../api/client.ts';
import { ProviderPrefsControl } from './ProviderPrefsControl.tsx';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const EMPTY = { include: [], exclude: [], primary: null };

describe('ProviderPrefsControl debounce', () => {
  it('keeps typing instant but propagates once after the delay', () => {
    vi.useFakeTimers();
    const onChange = vi.fn();
    render(
      <ProviderPrefsControl value={EMPTY} onChange={onChange} knownProviders={['custom_index']} />,
    );
    const input = screen.getByLabelText(/Only these providers/);
    fireEvent.change(input, { target: { value: 'c' } });
    expect(input).toHaveValue('c');
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { value: 'cu' } });
    fireEvent.change(input, { target: { value: 'cus' } });
    expect(input).toHaveValue('cus');
    vi.advanceTimersByTime(FILTER_DEBOUNCE_MS);
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith({ include: ['cus'], exclude: [], primary: null });
  });

  it('parses comma-separated lists and strips control characters', () => {
    vi.useFakeTimers();
    const onChange = vi.fn();
    render(
      <ProviderPrefsControl value={EMPTY} onChange={onChange} knownProviders={['custom_index']} />,
    );
    fireEvent.change(screen.getByLabelText(/Exclude these providers/), {
      target: { value: `a${String.fromCharCode(7)}, b ` },
    });
    vi.advanceTimersByTime(FILTER_DEBOUNCE_MS);
    expect(onChange).toHaveBeenCalledWith({ include: [], exclude: ['a', 'b'], primary: null });
  });

  it('re-syncs the draft when the parent value changes elsewhere', () => {
    vi.useFakeTimers();
    const onChange = vi.fn();
    const { rerender } = render(
      <ProviderPrefsControl value={EMPTY} onChange={onChange} knownProviders={[]} />,
    );
    const input = screen.getByLabelText(/Primary provider/);
    fireEvent.change(input, { target: { value: 'typed' } });
    rerender(
      <ProviderPrefsControl
        value={{ include: [], exclude: [], primary: 'server-side' }}
        onChange={onChange}
        knownProviders={[]}
      />,
    );
    expect(input).toHaveValue('server-side');
    vi.advanceTimersByTime(FILTER_DEBOUNCE_MS * 2);
    expect(onChange).not.toHaveBeenCalled();
  });
});
