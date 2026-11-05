import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ConfirmDialog } from './ConfirmDialog.tsx';

afterEach(() => {
  cleanup();
});

function renderDialog(overrides = {}) {
  return render(
    <ConfirmDialog
      open
      title="Pause crawling and clear the queue?"
      body="Permanently drops all queued URLs."
      confirmLabel="Pause & clear"
      tone="destructive"
      onConfirm={vi.fn()}
      onCancel={vi.fn()}
      {...overrides}
    />,
  );
}

describe('ConfirmDialog', () => {
  it('renders an alertdialog and confirms', () => {
    const onConfirm = vi.fn();
    renderDialog({ onConfirm });
    expect(screen.getByRole('alertdialog')).toHaveTextContent(
      'Pause crawling and clear the queue?',
    );
    fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it('cancels on cancel button and on Escape', () => {
    const onCancel = vi.fn();
    renderDialog({ onCancel });
    fireEvent.click(screen.getByTestId('confirm-dialog-cancel'));
    expect(onCancel).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onCancel).toHaveBeenCalledTimes(2);
  });

  it('autofocuses the destructive confirm action', () => {
    renderDialog({ tone: 'destructive' });
    expect(screen.getByTestId('confirm-dialog-confirm')).toHaveFocus();
  });

  it('autofocuses cancel for the default tone', () => {
    renderDialog({ tone: 'default', confirmLabel: 'Save' });
    expect(screen.getByTestId('confirm-dialog-cancel')).toHaveFocus();
  });

  it('renders nothing when closed', () => {
    renderDialog({ open: false });
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });
});
