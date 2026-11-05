import { useCallback, useEffect, useRef, useState } from 'react';

/** Toast severity: success/info fade out, errors persist until dismissed. */
export type ToastKind = 'success' | 'error' | 'info';

export interface ToastItem {
  id: string;
  text: string;
  kind: ToastKind;
  /** Repeat count when coalesced; rendered as "text ×N". */
  count?: number;
}

export interface ToastsProps {
  items: ToastItem[];
  onDismiss?: (id: string) => void;
}

/**
 * Non-blocking notifications anchored bottom-right of the viewport. One
 * `aria-live="polite"` region for the queue; errors carry `role="alert"`
 * inside it. The surface is opaque so text stays legible over content.
 */
export function Toasts({ items, onDismiss }: ToastsProps) {
  if (items.length === 0) return null;
  return (
    <div
      aria-live="polite"
      className="fixed right-ol-l bottom-ol-l z-50 flex max-w-[min(24rem,calc(100vw_-_3rem))] flex-col gap-ol-s"
      data-testid="toasts"
    >
      {items.map((item) => (
        <p
          key={item.id}
          role={item.kind === 'error' ? 'alert' : 'status'}
          data-testid={`toast-${item.kind}`}
          className={
            item.kind === 'error'
              ? 'rounded-ol-m border border-red-300 bg-[#101828] px-ol-m py-ol-s text-ol-paper text-ol-s shadow-ol'
              : 'rounded-ol-m border border-white/20 bg-[#101828] px-ol-m py-ol-s text-ol-paper text-ol-s shadow-ol'
          }
        >
          <span className="mr-ol-s inline-flex w-full items-start justify-between gap-ol-s">
            <span>
              {item.text}
              {item.count && item.count > 1 ? ` ×${item.count}` : ''}
            </span>
            <button
              type="button"
              aria-label="Dismiss notification"
              onClick={() => onDismiss?.(item.id)}
              className="shrink-0 cursor-pointer rounded-ol-s px-ol-xs text-ol-paper hover:text-ol-paper"
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

/** Maximum queued toasts; repeats coalesce, oldest drops past the cap. */
export const TOAST_MAX_ITEMS = 3;

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
      setItems((current) => {
        const repeat = current.find((item) => item.text === text && item.kind === kind);
        if (repeat) {
          return current.map((item) =>
            item.id === repeat.id ? { ...item, count: (item.count ?? 1) + 1 } : item,
          );
        }
        const next = [...current, { id, text, kind }];
        return next.length > TOAST_MAX_ITEMS ? next.slice(next.length - TOAST_MAX_ITEMS) : next;
      });
      if (kind !== 'error') {
        // Repeats reuse the live toast's own timer; this arm is a no-op
        // dismiss for the unused id and cleans itself up on fire.
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
