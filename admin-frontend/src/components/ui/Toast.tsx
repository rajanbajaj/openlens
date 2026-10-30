import { useCallback, useEffect, useRef, useState } from 'react';

/** Toast severity: success/info fade out, errors persist until dismissed. */
export type ToastKind = 'success' | 'error' | 'info';

export interface ToastItem {
  id: string;
  text: string;
  kind: ToastKind;
}

export interface ToastsProps {
  items: ToastItem[];
  onDismiss?: (id: string) => void;
}

/** Non-blocking notifications anchored bottom-right of the viewport. */
export function Toasts({ items, onDismiss }: ToastsProps) {
  if (items.length === 0) return null;
  return (
    <div
      className="fixed right-ol-l bottom-ol-l z-50 flex max-w-[min(24rem,calc(100vw_-_3rem))] flex-col gap-ol-s"
      data-testid="toasts"
    >
      {items.map((item) => (
        <p
          key={item.id}
          role={item.kind === 'error' ? 'alert' : 'status'}
          aria-live={item.kind === 'error' ? 'assertive' : 'polite'}
          data-testid={`toast-${item.kind}`}
          className={
            item.kind === 'error'
              ? 'rounded-ol-m border border-red-300/50 bg-[#101828]/85 px-ol-m py-ol-s text-ol-paper text-ol-s shadow-ol'
              : 'rounded-ol-m border border-white/20 bg-[#101828]/85 px-ol-m py-ol-s text-ol-paper text-ol-s shadow-ol'
          }
        >
          <span className="mr-ol-s inline-flex w-full items-start justify-between gap-ol-s">
            <span>{item.text}</span>
            <button
              type="button"
              aria-label="Dismiss notification"
              onClick={() => onDismiss?.(item.id)}
              className="shrink-0 cursor-pointer rounded-ol-s px-ol-xs text-ol-paper/70 hover:text-ol-paper"
            >
              ×
            </button>
          </span>
        </p>
      ))}
    </div>
  );
}

/** Auto-dismiss delay for success/info toasts (errors persist). */
export const TOAST_TTL_MS = 6000;

let toastSeq = 0;

/** Toast state for one form: push success/error/info, auto-fading. */
export function useToasts() {
  const [items, setItems] = useState<ToastItem[]>([]);
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>());

  const dismiss = useCallback((id: string) => {
    const timer = timers.current.get(id);
    if (timer !== undefined) {
      clearTimeout(timer);
      timers.current.delete(id);
    }
    setItems((current) => current.filter((item) => item.id !== id));
  }, []);

  const push = useCallback(
    (text: string, kind: ToastKind = 'info') => {
      toastSeq += 1;
      const id = `toast-${toastSeq}`;
      setItems((current) => [...current, { id, text, kind }]);
      if (kind !== 'error') {
        const timer = setTimeout(() => dismiss(id), TOAST_TTL_MS);
        timers.current.set(id, timer);
      }
    },
    [dismiss],
  );

  useEffect(() => {
    const pending = timers.current;
    return () => {
      for (const timer of pending.values()) clearTimeout(timer);
      pending.clear();
    };
  }, []);

  return { items, push, dismiss };
}
