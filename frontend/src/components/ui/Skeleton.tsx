export interface SkeletonProps {
  /** Number of placeholder bars. Defaults to 3. */
  bars?: number;
}

/**
 * Loading placeholder: decorative bars hidden from assistive tech.
 * Pair with a live-region status line (e.g. "Searching…") instead of
 * using this alone.
 */
export function Skeleton({ bars = 3 }: SkeletonProps) {
  return (
    <div
      className="ol-skeleton flex flex-col gap-ol-s my-ol-m"
      aria-hidden="true"
      data-testid="skeleton"
    >
      {Array.from({ length: Math.max(1, bars) }, (_, index) => (
        <div key={index} className="ol-skeleton-bar h-[0.9rem] rounded-ol-s bg-white/45" />
      ))}
    </div>
  );
}
