import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Toasts, TOAST_TTL_MS, useToasts } from './Toast.tsx';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function Harness({ text, kind }: { text: string; kind: 'success' | 'error' | 'info' }) {
  const { items, push, dismiss } = useToasts();
  return (
    <>
      <button type="button" onClick={() => push(text, kind)}>
        push
      </button>
      <Toasts items={items} onDismiss={dismiss} />
    </>
  );
}

describe('Toasts', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  it('announces success politely and auto-dismisses', () => {
    render(<Harness text="Saved." kind="success" />);
    fireEvent.click(screen.getByText('push'));
    const toast = screen.getByTestId('toast-success');
    expect(toast).toHaveTextContent('Saved.');
    expect(toast).toHaveAttribute('role', 'status');
    act(() => {
      vi.advanceTimersByTime(TOAST_TTL_MS);
    });
    expect(screen.queryByTestId('toasts')).not.toBeInTheDocument();
  });

  it('announces errors assertively and keeps them until dismissed', () => {
    render(<Harness text="Save failed." kind="error" />);
    fireEvent.click(screen.getByText('push'));
    const toast = screen.getByTestId('toast-error');
    expect(toast).toHaveTextContent('Save failed.');
    expect(toast).toHaveAttribute('role', 'alert');
    act(() => {
      vi.advanceTimersByTime(TOAST_TTL_MS * 10);
    });
    expect(screen.getByTestId('toast-error')).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText('Dismiss notification'));
    expect(screen.queryByTestId('toasts')).not.toBeInTheDocument();
  });
});
