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
    <div className="ol-toasts" role="status" aria-live="polite" data-testid="toasts">
      {items.map((item) => (
        <p key={item.id} className="ol-toast">
          {item.text}
        </p>
      ))}
    </div>
  );
}
