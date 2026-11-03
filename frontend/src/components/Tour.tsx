import { useCallback, useEffect, useRef, useState } from 'react';
import type { TourStep } from '../tourSteps.ts';

export interface TourProps {
  steps: TourStep[];
  runId: number;
  storageKey: string;
}

function resolveSteps(steps: TourStep[]): TourStep[] {
  return steps.filter(
    (step) =>
      typeof document !== 'undefined' &&
      document.querySelector(`[data-testid="${step.target}"]`) !== null,
  );
}

function prefersReducedMotion(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  );
}

export default function Tour({ steps, runId, storageKey }: TourProps) {
  const [activeSteps, setActiveSteps] = useState<TourStep[]>([]);
  const [index, setIndex] = useState<number | null>(null);
  const [position, setPosition] = useState<{ top: number; left: number }>({ top: 0, left: 0 });
  const nextRef = useRef<HTMLButtonElement>(null);
  const stateRef = useRef({ activeSteps, index });
  stateRef.current = { activeSteps, index };

  const exit = useCallback(
    (persist = true) => {
      const { activeSteps: current, index: currentIndex } = stateRef.current;
      const current_step = currentIndex !== null ? current[currentIndex] : undefined;
      if (current_step) {
        document
          .querySelector(`[data-testid="${current_step.target}"]`)
          ?.classList.remove('ol-tour-target');
      }
      if (persist) {
        try {
          window.localStorage.setItem(storageKey, 'done');
        } catch {
          /* storage unavailable: tour still exits */
        }
      }
      setIndex(null);
      setActiveSteps([]);
    },
    [storageKey],
  );

  const start = useCallback(() => {
    const resolved = resolveSteps(steps);
    if (resolved.length === 0) return;
    setActiveSteps(resolved);
    setIndex(0);
  }, [steps]);

  // Explicit replays via runId.
  useEffect(() => {
    if (runId === 0) return;
    start();
  }, [runId, start]);

  // Deep-link autostart: ?tour=1.
  useEffect(() => {
    if (typeof window === 'undefined') return;
    if (!window.location.search.includes('tour=1')) return;
    start();
  }, []);

  // Escape exits from anywhere while the tour is open.
  useEffect(() => {
    if (index === null) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') exit(true);
    }
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [index, exit]);

  // Highlight + scroll + tooltip placement + focus on step change.
  useEffect(() => {
    if (index === null) return;
    const step = activeSteps[index];
    if (!step) return;
    const el = document.querySelector(`[data-testid="${step.target}"]`);
    // Drop a stale highlight if the target vanished mid-tour.
    document.querySelectorAll('.ol-tour-target').forEach((node) => {
      if (node !== el) node.classList.remove('ol-tour-target');
    });
    if (!el) return;
    el.classList.add('ol-tour-target');
    try {
      el.scrollIntoView({
        block: 'nearest',
        behavior: prefersReducedMotion() ? 'auto' : 'smooth',
      });
    } catch {
      /* non-visual environments may not implement scrolling */
    }
    const rect = el.getBoundingClientRect();
    const gap = 12;
    const tooltipHeight = 220;
    const belowRoom = window.innerHeight - rect.bottom >= tooltipHeight + gap;
    const top = belowRoom ? rect.bottom + gap : Math.max(8, rect.top - tooltipHeight - gap);
    const left = Math.max(8, Math.min(rect.left, window.innerWidth - 340));
    setPosition({ top, left });
    nextRef.current?.focus();
    return () => {
      el.classList.remove('ol-tour-target');
    };
  }, [index, activeSteps]);

  if (index === null || activeSteps.length === 0) return null;
  const step = activeSteps[index];
  if (!step) return null;
  const isLast = index === activeSteps.length - 1;

  return (
    <>
      <div className="ol-tour-veil" data-testid="tour-veil" aria-hidden="true" />
      <div
        className="ol-tour-tooltip"
        role="dialog"
        aria-label={step.title}
        data-testid="tour-tooltip"
        style={{ top: position.top, left: position.left }}
      >
        <p className="ol-tour-title">{step.title}</p>
        <p className="ol-tour-body">{step.body}</p>
        <p className="ol-tour-progress">
          Step {index + 1} of {activeSteps.length}
        </p>
        <div className="ol-tour-controls">
          <button
            type="button"
            className="ol-button ol-button-secondary"
            disabled={index === 0}
            onClick={() => setIndex((i) => (i === null || i === 0 ? i : i - 1))}
          >
            Back
          </button>
          <button
            type="button"
            ref={nextRef}
            className="ol-button ol-button-primary"
            onClick={() => {
              if (isLast) exit(true);
              else setIndex((i) => (i === null ? i : i + 1));
            }}
          >
            {isLast ? 'Done' : 'Next'}
          </button>
          <button type="button" className="ol-button ol-button-ghost" onClick={() => exit(true)}>
            Skip tour
          </button>
        </div>
      </div>
    </>
  );
}
