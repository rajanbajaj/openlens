import { useCallback, useEffect, useRef, useState } from 'react';
import type { TourStep } from '../tourSteps.ts';

export interface TourProps {
  steps: TourStep[];
  runId: number;
  storageKey: string;
}

const TARGET_CLASS = 'ol-tour-target';
const GAP = 12;

function resolveSteps(steps: TourStep[]): TourStep[] {
  return steps.filter((step) => document.querySelector(`[data-testid="${step.target}"]`));
}

function reducedMotion(): boolean {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
}

/** Controlled guided tour: highlights one data-testid target at a time. */
export function Tour({ steps, runId, storageKey }: TourProps) {
  const [active, setActive] = useState<TourStep[] | null>(null);
  const [index, setIndex] = useState(0);
  const [anchor, setAnchor] = useState<{
    top: number;
    left: number;
    width: number;
    above: boolean;
  } | null>(null);
  const nextRef = useRef<HTMLButtonElement>(null);

  const stop = useCallback(
    (markDone: boolean) => {
      document.querySelectorAll(`.${TARGET_CLASS}`).forEach((el) => {
        el.classList.remove(TARGET_CLASS);
      });
      if (markDone) {
        try {
          localStorage.setItem(storageKey, 'done');
        } catch {
          /* private-mode storage: tour simply ends */
        }
      }
      setActive(null);
      setIndex(0);
    },
    [storageKey],
  );

  const start = useCallback(() => {
    const resolved = resolveSteps(steps);
    if (resolved.length === 0) return;
    setActive(resolved);
    setIndex(0);
  }, [steps]);

  // Button-triggered runs.
  useEffect(() => {
    if (runId === 0) return;
    start();
  }, [runId, start]);

  // Autostart via ?tour=1.
  useEffect(() => {
    if (new URLSearchParams(window.location.search).get('tour') === '1') {
      start();
    }
  }, [start]);

  // Highlight + position on step change.
  useEffect(() => {
    if (!active) return;
    const step = active[index];
    if (!step) return;
    const el = document.querySelector(`[data-testid="${step.target}"]`);
    document.querySelectorAll(`.${TARGET_CLASS}`).forEach((node) => {
      node.classList.remove(TARGET_CLASS);
    });
    if (el) {
      el.classList.add(TARGET_CLASS);
      if (typeof el.scrollIntoView === 'function') {
        el.scrollIntoView({ block: 'nearest', behavior: reducedMotion() ? 'auto' : 'smooth' });
      }
      const rect = el.getBoundingClientRect();
      const belowRoom = window.innerHeight - rect.bottom > 200;
      setAnchor({
        top: belowRoom ? rect.bottom + GAP : Math.max(GAP, rect.top - GAP),
        left: Math.min(Math.max(GAP, rect.left), Math.max(GAP, window.innerWidth - 340)),
        width: rect.width,
        above: !belowRoom,
      });
    }
    nextRef.current?.focus();
  }, [active, index]);

  // Cleanup highlight on unmount.
  useEffect(
    () => () => {
      document.querySelectorAll(`.${TARGET_CLASS}`).forEach((node) => {
        node.classList.remove(TARGET_CLASS);
      });
    },
    [],
  );

  // Escape exits.
  useEffect(() => {
    if (!active) return;
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') stop(true);
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
    };
  }, [active, stop]);

  if (!active || active.length === 0) return null;
  const step = active[index];
  if (!step) return null;
  const last = index === active.length - 1;
  const style = anchor ? { top: anchor.top, left: anchor.left } : { top: 16, left: 16 };

  return (
    <>
      <div className="ol-tour-veil" aria-hidden="true" />
      <div
        role="dialog"
        aria-label={step.title}
        className={`ol-tour-tooltip${anchor?.above ? ' ol-tour-tooltip-above' : ''}`}
        style={{ position: 'fixed', ...style }}
      >
        <p className="ol-tour-title">{step.title}</p>
        <p className="ol-tour-body">{step.body}</p>
        <p className="ol-tour-progress">
          Step {index + 1} of {active.length}
        </p>
        <div className="ol-tour-controls">
          <button
            type="button"
            className="ol-button ol-button-secondary"
            disabled={index === 0}
            onClick={() => {
              setIndex((i) => Math.max(0, i - 1));
            }}
          >
            Back
          </button>
          <button
            type="button"
            ref={nextRef}
            className="ol-button ol-button-primary"
            onClick={() => {
              if (last) {
                stop(true);
              } else {
                setIndex((i) => i + 1);
              }
            }}
          >
            {last ? 'Done' : 'Next'}
          </button>
          <button
            type="button"
            className="ol-button ol-button-ghost"
            onClick={() => {
              stop(true);
            }}
          >
            Skip tour
          </button>
        </div>
      </div>
    </>
  );
}
