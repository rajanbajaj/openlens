export interface ToastItem {
  id: string;
  text: string;
}

export interface ToastsProps {
  items: ToastItem[];
}

/** Non-blocking notifications announced politely to assistive tech. */
export function Toasts({ items }: ToastsProps) {
  if (items.length === 0) return null;
  return (
    <div
      className="fixed right-ol-l bottom-ol-l flex max-w-[min(24rem,calc(100vw_-_3rem))] flex-col gap-ol-s"
      role="status"
      aria-live="polite"
      data-testid="toasts"
    >
      {items.map((item) => (
        <p
          key={item.id}
          className="rounded-ol-m border border-white/20 bg-[#101828]/85 px-ol-m py-ol-s text-ol-paper text-ol-s shadow-ol"
        >
          {item.text}
        </p>
      ))}
    </div>
  );
}
