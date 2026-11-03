import { useCallback, useEffect, useRef, useState } from 'react';
import type { TourStep } from '../tourSteps.ts';

export interface TourProps {
  steps: TourStep[];
  runId: number;
  storageKey: string;
}

function targetExists(target: string): boolean {
  return (
    typeof document !== 'undefined' &&
    target !== '' &&
    document.querySelector(`[data-testid="${target}"]`) !== null
  );
}

/**
 * Whether step `i` can show right now. Gate steps (empty target) count
 * only while their `untilTarget` is still missing — once the awaited
 * view mounts, the gate hands off instead of showing.
 */
function stepAvailable(steps: TourStep[], i: number): boolean {
  const step = steps[i];
  if (!step) return false;
  if (step.target === '') return !step.untilTarget || !targetExists(step.untilTarget);
  return targetExists(step.target);
}

/** First step at or after `from` that can show right now. */
function nextAvailable(steps: TourStep[], from: number): number {
  for (let i = Math.max(0, from); i < steps.length; i += 1) {
    if (stepAvailable(steps, i)) return i;
  }
  return -1;
}

/** Last step at or before `from` that can show right now. */
function prevAvailable(steps: TourStep[], from: number): number {
  for (let i = Math.min(from, steps.length - 1); i >= 0; i -= 1) {
    if (stepAvailable(steps, i)) return i;
  }
  return -1;
}

function prefersReducedMotion(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  );
}

export default function Tour({ steps, runId, storageKey }: TourProps) {
  const [open, setOpen] = useState(false);
  const [index, setIndex] = useState(0);
  const [position, setPosition] = useState<{ top: number; left: number }>({ top: 0, left: 0 });
  const [rev, setRev] = useState(0);
  const nextRef = useRef<HTMLButtonElement>(null);
  const stateRef = useRef({ open, index });
  stateRef.current = { open, index };

  const exit = useCallback(
    (persist = true) => {
      document
        .querySelectorAll('.ol-tour-target')
        .forEach((node) => node.classList.remove('ol-tour-target'));
      if (persist) {
        try {
          window.localStorage.setItem(storageKey, 'done');
        } catch {
          /* storage unavailable: tour still exits */
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
    if (next === -1) exit(true);
    else setIndex(next);
  }, [steps, exit]);

  const goBack = useCallback(() => {
    const { open: isOpen, index: current } = stateRef.current;
    if (!isOpen) return;
    const prev = prevAvailable(steps, current - 1);
    if (prev !== -1) setIndex(prev);
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
  }, [start]);

  // Follow the user across views: tab switches mount/unmount targets
  // while the tour is open, so re-resolve on every DOM change.
  useEffect(() => {
    if (!open || typeof MutationObserver === 'undefined') return;
    const observer = new MutationObserver(() => {
      const { open: isOpen, index: current } = stateRef.current;
      if (!isOpen) return;
      const step = steps[current];
      if (!step) {
        exit(true);
        return;
      }
      if (step.target === '') {
        // Gate step: hand off as soon as the awaited view mounts.
        if (step.untilTarget && targetExists(step.untilTarget)) {
          const gated = nextAvailable(steps, current + 1);
          if (gated !== -1) setIndex(gated);
          else exit(true);
        } else {
          setRev((r) => r + 1);
        }
        return;
      }
      if (!targetExists(step.target)) {
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
        exit(true);
      } else {
        // Target moved: re-run placement below.
        setRev((r) => r + 1);
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [open, steps, exit]);

  // Escape exits from anywhere while the tour is open.
  useEffect(() => {
    if (!open) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') exit(true);
    }
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [open, exit]);

  // Highlight + scroll + tooltip placement + focus on step change.
  // A step whose target unmounted (tab switch) hands off to the next
  // mounted step instead of freezing on a stale highlight. Gate steps
  // (empty target) show centered with no highlight until their awaited
  // view mounts.
  useEffect(() => {
    if (!open) return;
    const step = steps[index];
    if (!step) {
      exit(true);
      return;
    }
    if (step.target === '') {
      document
        .querySelectorAll('.ol-tour-target')
        .forEach((node) => node.classList.remove('ol-tour-target'));
      if (step.untilTarget && targetExists(step.untilTarget)) {
        const forward = nextAvailable(steps, index + 1);
        if (forward !== -1) {
          setIndex(forward);
          return;
        }
        exit(true);
        return;
      }
      setPosition({
        top: Math.max(16, window.innerHeight * 0.25),
        left: Math.max(8, (window.innerWidth - 360) / 2),
      });
      nextRef.current?.focus();
      return;
    }
    if (!targetExists(step.target)) {
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
      exit(true);
      return;
    }
    const el = document.querySelector(`[data-testid="${step.target}"]`);
    // Drop a stale highlight if the target moved mid-tour.
    document.querySelectorAll('.ol-tour-target').forEach((node) => {
      if (node !== el) node.classList.remove('ol-tour-target');
    });
    if (!el) return;
    const disclosure = el.closest('details');
    if (disclosure instanceof HTMLDetailsElement && !disclosure.open) {
      // The tour opens accordions on demand instead of skipping them —
      // the forced reflow below then measures the opened layout.
      disclosure.open = true;
    }
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
  }, [open, index, steps, rev, exit]);

  if (!open) return null;
  const step = steps[index];
  if (!step || (step.target !== '' && !targetExists(step.target))) return null;
  const isLast = nextAvailable(steps, index + 1) === -1;
  const backDisabled = prevAvailable(steps, index - 1) === -1;

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
