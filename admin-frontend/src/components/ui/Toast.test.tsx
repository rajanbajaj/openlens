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

  it('coalesces repeats and caps the queue', () => {
    function MultiHarness() {
      const { items, push, dismiss } = useToasts();
      return (
        <>
          <button type="button" onClick={() => push('Saved.', 'success')}>
            save
          </button>
          <button type="button" onClick={() => push(`Note ${Math.random()}`, 'info')}>
            note
          </button>
          <Toasts items={items} onDismiss={dismiss} />
        </>
      );
    }
    render(<MultiHarness />);
    fireEvent.click(screen.getByText('save'));
    fireEvent.click(screen.getByText('save'));
    fireEvent.click(screen.getByText('save'));
    expect(screen.getByTestId('toast-success')).toHaveTextContent('Saved. ×3');
    fireEvent.click(screen.getByText('note'));
    fireEvent.click(screen.getByText('note'));
    fireEvent.click(screen.getByText('note'));
    fireEvent.click(screen.getByText('note'));
    expect(screen.getAllByTestId('toast-info')).toHaveLength(3);
  });

  it('uses a single live region with an opaque surface', () => {
    render(<Harness text="Saved." kind="success" />);
    fireEvent.click(screen.getByText('push'));
    const region = screen.getByTestId('toasts');
    expect(region).toHaveAttribute('aria-live', 'polite');
    expect(screen.getByTestId('toast-success').className).toContain('bg-[#101828]');
    expect(screen.getByTestId('toast-success').className).not.toContain('/85');
  });
});
