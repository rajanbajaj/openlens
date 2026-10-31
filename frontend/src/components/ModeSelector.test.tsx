import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ModeSelector, { SEARCH_MODES } from './ModeSelector.tsx';

afterEach(() => {
  cleanup();
});

describe('ModeSelector', () => {
  it('offers the agent mode alongside the existing modes', () => {
    expect(SEARCH_MODES).toContain('agent');
    render(<ModeSelector mode="default" onChange={() => {}} />);

    const select = screen.getByLabelText(/search mode/i);
    const values = Array.from(select.querySelectorAll('option')).map(
      (option) => (option as HTMLOptionElement).value,
    );
    expect(values).toEqual(['default', 'child-safe', 'news', 'shopping', 'travel', 'agent']);
  });

  it('forwards the agent selection to the parent re-search path', () => {
    const onChange = vi.fn();
    render(<ModeSelector mode="default" onChange={onChange} />);

    fireEvent.change(screen.getByLabelText(/search mode/i), { target: { value: 'agent' } });
    expect(onChange).toHaveBeenCalledWith('agent');
  });

  it('ignores unknown selections instead of calling onChange', () => {
    const onChange = vi.fn();
    render(<ModeSelector mode="default" onChange={onChange} />);

    fireEvent.change(screen.getByLabelText(/search mode/i), { target: { value: 'turbo' } });
    expect(onChange).not.toHaveBeenCalled();
  });
});
