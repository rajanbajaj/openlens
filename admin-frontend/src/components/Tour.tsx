import { useCallback, useEffect, useRef, useState } from 'react';
import type { TourStep } from '../tourSteps.ts';

export interface TourProps {
  steps: TourStep[];
  runId: number;
  storageKey: string;
}

const TARGET_CLASS = 'ol-tour-target';
const GAP = 12;
/** Tooltip width cap (22rem) used for viewport clamping. */
const TOOLTIP_WIDTH = 352;

function targetExists(target: string): boolean {
  return target !== '' && document.querySelector(`[data-testid="${target}"]`) !== null;
}

/**
 * Mounted element backing a step: the target itself, or the healthy
 * fallback when the target is absent (e.g. auto-triage on a fleet with
 * nothing to triage). Empty when neither is mounted.
 */
function resolvedTarget(step: TourStep): string {
  if (step.target !== '') {
    if (targetExists(step.target)) return step.target;
    if (step.healthyFallback && targetExists(step.healthyFallback)) {
      return step.healthyFallback;
    }
    return '';
  }
  return '';
}

function stepAvailable(steps: TourStep[], i: number): boolean {
  const step = steps[i];
  if (!step) return false;
  if (step.target === '') return !step.untilTarget || !targetExists(step.untilTarget);
  return resolvedTarget(step) !== '';
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
  const tooltipRef = useRef<HTMLDivElement>(null);
  const stateRef = useRef({ open, index });
  stateRef.current = { open, index };
  const resolvedRef = useRef<string | null>(null);
  const [anchorEpoch, setAnchorEpoch] = useState(0);

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
  }, [steps, stop]);

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
      if (!step) {
        stop(true);
        return;
      }
      if (step.target === '') {
        if (step.untilTarget && targetExists(step.untilTarget)) {
          const gated = nextAvailable(steps, current + 1);
          if (gated !== -1) setIndex(gated);
          else stop(true);
        }
        return;
      }
      const resolved = resolvedTarget(step);
      if (resolved === '') {
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
      } else if (resolved !== resolvedRef.current) {
        // The target resolved (possibly to its healthy fallback) after
        // a DOM change: re-anchor without moving the step.
        resolvedRef.current = resolved;
        setAnchorEpoch((epoch) => epoch + 1);
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [open, steps, stop]);

  // Highlight + position on step change. A step whose target unmounted
  // (tab switch) hands off to the next mounted step instead of freezing.
  // Gate steps (empty target) show centered until their awaited view mounts.
  useEffect(() => {
    if (!open) return;
    const step = steps[index];
    if (!step) {
      stop(true);
      return;
    }
    if (step.target === '') {
      document.querySelectorAll(`.${TARGET_CLASS}`).forEach((node) => {
        node.classList.remove(TARGET_CLASS);
      });
      if (step.untilTarget && targetExists(step.untilTarget)) {
        const forward = nextAvailable(steps, index + 1);
        if (forward !== -1) {
          setIndex(forward);
          return;
        }
        stop(true);
        return;
      }
      const width = Math.min(TOOLTIP_WIDTH, Math.max(0, window.innerWidth - GAP * 2));
      setAnchor({
        top: Math.min(
          Math.max(GAP, window.innerHeight * 0.25),
          Math.max(GAP, window.innerHeight - GAP - 80),
        ),
        left: Math.min(
          Math.max(GAP, (window.innerWidth - width) / 2),
          Math.max(GAP, window.innerWidth - width - GAP),
        ),
        width: 0,
        above: false,
      });
      nextRef.current?.focus();
      return;
    }
    const resolved = resolvedTarget(step);
    if (resolved === '') {
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
    const el = document.querySelector(`[data-testid="${resolved}"]`);
    resolvedRef.current = resolved;
    document.querySelectorAll(`.${TARGET_CLASS}`).forEach((node) => {
      node.classList.remove(TARGET_CLASS);
    });
    if (el) {
      const disclosure = el.closest('details');
      if (disclosure instanceof HTMLDetailsElement && !disclosure.open) {
        // The tour opens accordions on demand instead of skipping them.
        disclosure.open = true;
      }
      el.classList.add(TARGET_CLASS);
      if (typeof el.scrollIntoView === 'function') {
        el.scrollIntoView({ block: 'nearest', behavior: reducedMotion() ? 'auto' : 'smooth' });
      }
      const rect = el.getBoundingClientRect();
      const width = Math.min(TOOLTIP_WIDTH, Math.max(0, window.innerWidth - GAP * 2));
      const belowRoom = window.innerHeight - rect.bottom > 200;
      const top = belowRoom ? rect.bottom + GAP : Math.max(GAP, rect.top - GAP);
      setAnchor({
        top: Math.min(top, Math.max(GAP, window.innerHeight - GAP - 80)),
        left: Math.min(Math.max(GAP, rect.left), Math.max(GAP, window.innerWidth - width - GAP)),
        width: rect.width,
        above: !belowRoom,
      });
    }
    nextRef.current?.focus();
  }, [open, index, steps, stop, anchorEpoch]);

  // Cleanup highlight on unmount.
  useEffect(
    () => () => {
      document.querySelectorAll(`.${TARGET_CLASS}`).forEach((node) => {
        node.classList.remove(TARGET_CLASS);
      });
    },
    [],
  );

  // Escape exits; Tab stays trapped inside the tooltip while open.
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') {
        stop(true);
        return;
      }
      if (event.key !== 'Tab' || !tooltipRef.current) return;
      const focusables = tooltipRef.current.querySelectorAll<HTMLElement>('button:not([disabled])');
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
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
    };
  }, [open, stop]);

  if (!open) return null;
  const step = steps[index];
  const resolved = step && step.target !== '' ? resolvedTarget(step) : 'gate';
  if (!step || resolved === '') return null;
  const last = nextAvailable(steps, index + 1) === -1;
  const backDisabled = prevAvailable(steps, index - 1) === -1;
  const style = anchor ? { top: anchor.top, left: anchor.left } : { top: 16, left: 16 };
  // Honest progress: count only steps that can show right now.
  const visibleTotal = steps.filter((_, i) => stepAvailable(steps, i)).length;
  const visibleIndex = steps
    .map((entry, i) => ({ entry, i }))
    .filter(({ i }) => stepAvailable(steps, i))
    .findIndex(({ i }) => i === index);

  return (
    <>
      <div className="ol-tour-veil" aria-hidden="true" />
      <div
        role="dialog"
        aria-label={step.title}
        data-testid="tour-tooltip"
        ref={tooltipRef}
        className={`ol-tour-tooltip${anchor?.above ? ' ol-tour-tooltip-above' : ''}`}
        style={{ position: 'fixed', ...style }}
      >
        <p className="ol-tour-title">{step.title}</p>
        <p className="ol-tour-body">{step.body}</p>
        <p className="ol-tour-progress">
          Step {visibleIndex + 1} of {visibleTotal}
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
