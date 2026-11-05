import { useEffect, useRef } from 'react';

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  body: string;
  confirmLabel: string;
  cancelLabel?: string;
  tone: 'destructive' | 'default';
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * Accessible replacement for `window.confirm`: a focus-trapped
 * `alertdialog` where Escape cancels. The destructive confirm button
 * autofocuses; otherwise focus lands on cancel so the safe choice wins
 * keyboard races.
 */
export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel,
  cancelLabel = 'Cancel',
  tone,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    (tone === 'destructive' ? confirmRef : cancelRef).current?.focus();
  }, [open, tone]);

  useEffect(() => {
    if (!open) return;
    function onKeyDown(event: KeyboardEvent): void {
      if (event.key === 'Escape') {
        event.stopPropagation();
        onCancel();
        return;
      }
      if (event.key !== 'Tab' || !dialogRef.current) return;
      const focusables = dialogRef.current.querySelectorAll<HTMLElement>('button:not([disabled])');
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener('keydown', onKeyDown, true);
    return () => {
      document.removeEventListener('keydown', onKeyDown, true);
    };
  }, [open, onCancel]);

  if (!open) return null;

  return (
    <div
      role="alertdialog"
      aria-modal="true"
      aria-label={title}
      data-testid="confirm-dialog"
      ref={dialogRef}
      className="fixed inset-0 z-50 grid place-items-center bg-[rgb(16_24_40/0.45)] p-ol-m"
    >
      <div className="w-full max-w-[min(24rem,calc(100vw_-_2rem))] rounded-ol-m border border-ol-glass-border bg-white p-ol-m shadow-ol-l">
        <p className="text-ol-m font-bold">{title}</p>
        <p className="my-ol-s text-ol-s text-ol-ink-soft">{body}</p>
        <div className="mt-ol-m flex flex-wrap justify-end gap-ol-s">
          <button
            type="button"
            ref={cancelRef}
            data-testid="confirm-dialog-cancel"
            onClick={onCancel}
            className="ol-button ol-button-secondary text-ol-s font-semibold rounded-ol-s border border-transparent px-ol-s py-ol-s cursor-pointer"
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            ref={confirmRef}
            data-testid="confirm-dialog-confirm"
            onClick={onConfirm}
            className={
              tone === 'destructive'
                ? 'ol-button ol-button-destructive text-ol-s font-semibold rounded-ol-s border border-transparent px-ol-s py-ol-s cursor-pointer'
                : 'ol-button ol-button-primary text-ol-s font-semibold rounded-ol-s border border-transparent px-ol-s py-ol-s cursor-pointer'
            }
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
