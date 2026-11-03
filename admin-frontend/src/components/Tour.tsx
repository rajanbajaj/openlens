import { useCallback, useEffect, useRef, useState } from 'react';
import type { TourStep } from '../tourSteps.ts';

export interface TourProps {
  steps: TourStep[];
  runId: number;
  storageKey: string;
}

const TARGET_CLASS = 'ol-tour-target';
const GAP = 12;

function targetExists(target: string): boolean {
  return document.querySelector(`[data-testid="${target}"]`) !== null;
}

/** First step at or after `from` whose target is currently mounted. */
function nextAvailable(steps: TourStep[], from: number): number {
  for (let i = Math.max(0, from); i < steps.length; i += 1) {
    if (targetExists(steps[i].target)) return i;
  }
  return -1;
}

/** Last step at or before `from` whose target is currently mounted. */
function prevAvailable(steps: TourStep[], from: number): number {
  for (let i = Math.min(from, steps.length - 1); i >= 0; i -= 1) {
    if (targetExists(steps[i].target)) return i;
  }
  return -1;
}

function reducedMotion(): boolean {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
}

/** Controlled guided tour: highlights one data-testid target at a time. */
export function Tour({ steps, runId, storageKey }: TourProps) {
  const [open, setOpen] = useState(false);
  const [index, setIndex] = useState(0);
  const [anchor, setAnchor] = useState<{
    top: number;
    left: number;
    width: number;
    above: boolean;
  } | null>(null);
  const nextRef = useRef<HTMLButtonElement>(null);
  const stateRef = useRef({ open, index });
  stateRef.current = { open, index };

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
      setOpen(false);
    },
    [storageKey],
  );

  const start = useCallback(() => {
    const first = nextAvailable(steps, 0);
    if (first === -1) return;
    setIndex(first);
    setOpen(true);
  }, [steps]);

  const goNext = useCallback(() => {
    const { open: isOpen, index: current } = stateRef.current;
    if (!isOpen) return;
    const next = nextAvailable(steps, current + 1);
    if (next === -1) stop(true);
    else setIndex(next);
  }, [steps, stop]);

  const goBack = useCallback(() => {
    const { open: isOpen, index: current } = stateRef.current;
    if (!isOpen) return;
    const prev = prevAvailable(steps, current - 1);
    if (prev !== -1) setIndex(prev);
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

  // Follow the user across tabs: tab switches mount/unmount targets
  // while the tour is open, so re-resolve on every DOM change.
  useEffect(() => {
    if (!open || typeof MutationObserver === 'undefined') return;
    const observer = new MutationObserver(() => {
      const { open: isOpen, index: current } = stateRef.current;
      if (!isOpen) return;
      const step = steps[current];
      if (!step || !targetExists(step.target)) {
        const forward = nextAvailable(steps, current + 1);
        if (forward !== -1) {
          setIndex(forward);
          return;
        }
        const backward = prevAvailable(steps, current - 1);
        if (backward !== -1) {
          setIndex(backward);
          return;
        }
        stop(true);
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [open, steps, stop]);

  // Highlight + position on step change. A step whose target unmounted
  // (tab switch) hands off to the next mounted step instead of freezing.
  useEffect(() => {
    if (!open) return;
    const step = steps[index];
    if (!step || !targetExists(step.target)) {
      const forward = nextAvailable(steps, index + 1);
      if (forward !== -1) {
        setIndex(forward);
        return;
      }
      const backward = prevAvailable(steps, index - 1);
      if (backward !== -1) {
        setIndex(backward);
        return;
      }
      stop(true);
      return;
    }
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
  }, [open, index, steps, stop]);

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
    if (!open) return;
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') stop(true);
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
    };
  }, [open, stop]);

  if (!open) return null;
  const step = steps[index];
  if (!step || !targetExists(step.target)) return null;
  const last = nextAvailable(steps, index + 1) === -1;
  const backDisabled = prevAvailable(steps, index - 1) === -1;
  const style = anchor ? { top: anchor.top, left: anchor.left } : { top: 16, left: 16 };

  return (
    <>
      <div className="ol-tour-veil" aria-hidden="true" />
      <div
        role="dialog"
        aria-label={step.title}
        data-testid="tour-tooltip"
        className={`ol-tour-tooltip${anchor?.above ? ' ol-tour-tooltip-above' : ''}`}
        style={{ position: 'fixed', ...style }}
      >
        <p className="ol-tour-title">{step.title}</p>
        <p className="ol-tour-body">{step.body}</p>
        <p className="ol-tour-progress">
          Step {index + 1} of {steps.length}
        </p>
        <div className="ol-tour-controls">
          <button
            type="button"
            className="ol-button ol-button-secondary"
            disabled={backDisabled}
            onClick={goBack}
          >
            Back
          </button>
          <button
            type="button"
            ref={nextRef}
            className="ol-button ol-button-primary"
            onClick={goNext}
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
