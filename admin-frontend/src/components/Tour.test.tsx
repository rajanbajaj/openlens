import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { Tour } from './Tour.tsx';
import type { TourStep } from '../tourSteps.ts';

const STEPS: TourStep[] = [
  { target: 'tour-a', title: 'First stop', body: 'First body.' },
  { target: 'tour-missing', title: 'Skipped stop', body: 'Never rendered.' },
  { target: 'tour-b', title: 'Second stop', body: 'Second body.' },
];

function renderTargets(): void {
  const host = document.createElement('div');
  host.innerHTML = '<p data-testid="tour-a">aaa</p><section data-testid="tour-b">bbb</section>';
  document.body.append(host);
}

afterEach(() => {
  cleanup();
  document.body.innerHTML = '';
  localStorage.clear();
  window.history.replaceState(null, '', window.location.pathname);
});

describe('Tour guided onboarding', () => {
  it('starts when runId changes and highlights the first target', () => {
    renderTargets();
    const { rerender } = render(<Tour steps={STEPS} runId={0} storageKey="ol-tour-test" />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    rerender(<Tour steps={STEPS} runId={1} storageKey="ol-tour-test" />);
    expect(screen.getByRole('dialog', { name: 'First stop' })).toBeInTheDocument();
    expect(screen.getByText('Step 1 of 2')).toBeInTheDocument();
    expect(document.querySelector('[data-testid="tour-a"]')).toHaveClass('ol-tour-target');
  });

  it('does not start when no targets exist', () => {
    const { rerender } = render(<Tour steps={STEPS} runId={0} storageKey="ol-tour-test" />);
    rerender(<Tour steps={STEPS} runId={1} storageKey="ol-tour-test" />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('skips missing targets and navigates with Next and Back', () => {
    renderTargets();
    const { rerender } = render(<Tour steps={STEPS} runId={0} storageKey="ol-tour-test" />);
    rerender(<Tour steps={STEPS} runId={1} storageKey="ol-tour-test" />);
    expect(screen.getByText('Step 1 of 2')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Next'));
    expect(screen.getByRole('dialog', { name: 'Second stop' })).toBeInTheDocument();
    expect(screen.getByText('Step 2 of 2')).toBeInTheDocument();
    expect(screen.getByText('Done')).toBeInTheDocument();
    expect(document.querySelector('[data-testid="tour-b"]')).toHaveClass('ol-tour-target');
    expect(document.querySelector('[data-testid="tour-a"]')).not.toHaveClass('ol-tour-target');

    fireEvent.click(screen.getByText('Back'));
    expect(screen.getByRole('dialog', { name: 'First stop' })).toBeInTheDocument();
    expect(screen.getByText('Next')).toBeInTheDocument();
    expect(screen.getByText('Back')).toBeDisabled();
  });

  it('Skip writes localStorage and unmounts the tour', () => {
    renderTargets();
    const { rerender } = render(<Tour steps={STEPS} runId={0} storageKey="ol-tour-test" />);
    rerender(<Tour steps={STEPS} runId={1} storageKey="ol-tour-test" />);
    fireEvent.click(screen.getByText('Skip tour'));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(localStorage.getItem('ol-tour-test')).toBe('done');
    expect(document.querySelector('.ol-tour-target')).not.toBeInTheDocument();
  });

  it('Escape exits the tour and marks it done', () => {
    renderTargets();
    const { rerender } = render(<Tour steps={STEPS} runId={0} storageKey="ol-tour-test" />);
    rerender(<Tour steps={STEPS} runId={1} storageKey="ol-tour-test" />);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(localStorage.getItem('ol-tour-test')).toBe('done');
  });

  it('autostarts when the URL carries ?tour=1', () => {
    renderTargets();
    window.history.replaceState(null, '', '/?tour=1');
    render(<Tour steps={STEPS} runId={0} storageKey="ol-tour-test" />);
    expect(screen.getByRole('dialog', { name: 'First stop' })).toBeInTheDocument();
    expect(screen.getByText('Step 1 of 2')).toBeInTheDocument();
  });
});
