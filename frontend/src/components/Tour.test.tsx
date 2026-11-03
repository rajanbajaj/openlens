import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import Tour from './Tour.tsx';
import type { TourStep } from '../tourSteps.ts';

const STEPS: TourStep[] = [
  { target: 'alpha', title: 'Alpha title', body: 'Alpha body' },
  { target: 'beta', title: 'Beta title', body: 'Beta body' },
  { target: 'missing', title: 'Missing title', body: 'Missing body' },
];

function mountTargets() {
  const host = document.createElement('div');
  host.innerHTML =
    '<div data-testid="alpha">alpha</div><div data-testid="beta">beta</div><button data-testid="nav-chat">chat</button>';
  document.body.appendChild(host);
  return host;
}

afterEach(() => {
  cleanup();
  document.body.innerHTML = '';
  window.localStorage.clear();
});

beforeEach(() => {
  window.history.replaceState(null, '', '/');
  window.localStorage.clear();
  document.body.innerHTML = '';
});

describe('Tour', () => {
  it('starts on runId change and highlights the first existing target', () => {
    const host = mountTargets();
    const { rerender } = render(<Tour steps={STEPS} runId={0} storageKey="ol-tour-test" />);
    expect(screen.queryByTestId('tour-tooltip')).toBeNull();
    rerender(<Tour steps={STEPS} runId={1} storageKey="ol-tour-test" />);
    expect(screen.getByTestId('tour-tooltip')).toHaveAttribute('role', 'dialog');
    expect(screen.getByText('Alpha title')).toBeInTheDocument();
    expect(screen.getByText('Step 1 of 3')).toBeInTheDocument();
    expect(host.querySelector('[data-testid="alpha"]')).toHaveClass('ol-tour-target');
  });

  it('skips missing targets and navigates with Next and Back', () => {
    mountTargets();
    render(<Tour steps={STEPS} runId={1} storageKey="ol-tour-test" />);
    expect(screen.getByText('Step 1 of 3')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(screen.getByText('Beta title')).toBeInTheDocument();
    expect(screen.getByText('Step 2 of 3')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Back' })).not.toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    expect(screen.getByText('Alpha title')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Back' })).toBeDisabled();
  });

  it('Skip writes localStorage and unmounts the tooltip', () => {
    const host = mountTargets();
    render(<Tour steps={STEPS} runId={1} storageKey="ol-tour-test" />);
    fireEvent.click(screen.getByRole('button', { name: 'Skip tour' }));
    expect(window.localStorage.getItem('ol-tour-test')).toBe('done');
    expect(screen.queryByTestId('tour-tooltip')).toBeNull();
    expect(host.querySelector('.ol-tour-target')).toBeNull();
  });

  it('Escape exits the tour and persists completion', () => {
    mountTargets();
    render(<Tour steps={STEPS} runId={1} storageKey="ol-tour-test" />);
    expect(screen.getByTestId('tour-tooltip')).toBeInTheDocument();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByTestId('tour-tooltip')).toBeNull();
    expect(window.localStorage.getItem('ol-tour-test')).toBe('done');
  });

  it('autostarts when the URL carries ?tour=1', () => {
    mountTargets();
    window.history.replaceState(null, '', '/?tour=1');
    render(<Tour steps={STEPS} runId={0} storageKey="ol-tour-test" />);
    expect(screen.getByTestId('tour-tooltip')).toBeInTheDocument();
    expect(screen.getByText('Alpha title')).toBeInTheDocument();
  });

  it('does not start when no targets exist', () => {
    render(<Tour steps={STEPS} runId={1} storageKey="ol-tour-test" />);
    expect(screen.queryByTestId('tour-tooltip')).toBeNull();
  });

  it('starts at the first available target when earlier steps are missing', () => {
    const host = document.createElement('div');
    host.innerHTML = '<div data-testid="beta">beta</div>';
    document.body.appendChild(host);
    render(<Tour steps={STEPS} runId={1} storageKey="ol-tour-test" />);
    expect(screen.getByText('Beta title')).toBeInTheDocument();
    expect(screen.getByText('Step 2 of 3')).toBeInTheDocument();
  });

  it('ends the tour with Done on the last available target', () => {
    const host = document.createElement('div');
    host.innerHTML = '<div data-testid="beta">beta</div>';
    document.body.appendChild(host);
    render(<Tour steps={STEPS} runId={1} storageKey="ol-tour-test" />);
    expect(screen.getByRole('button', { name: 'Done' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    expect(screen.queryByTestId('tour-tooltip')).toBeNull();
    expect(window.localStorage.getItem('ol-tour-test')).toBe('done');
  });

  it('follows the user when the current target unmounts mid-tour', async () => {
    const host = mountTargets();
    render(<Tour steps={STEPS} runId={1} storageKey="ol-tour-test" />);
    expect(screen.getByText('Alpha title')).toBeInTheDocument();
    // Simulate switching to a view where the current target is gone.
    host.querySelector('[data-testid="alpha"]')?.remove();
    await waitFor(() => {
      expect(screen.getByText('Beta title')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('tour-tooltip')).toBeInTheDocument();
  });

  it('exits when every remaining target unmounts mid-tour', async () => {
    const host = document.createElement('div');
    host.innerHTML = '<div data-testid="beta">beta</div>';
    document.body.appendChild(host);
    render(<Tour steps={STEPS} runId={1} storageKey="ol-tour-test" />);
    expect(screen.getByText('Beta title')).toBeInTheDocument();
    host.querySelector('[data-testid="beta"]')?.remove();
    await waitFor(() => {
      expect(screen.queryByTestId('tour-tooltip')).toBeNull();
    });
  });
});
