import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { PerspectivesSection } from './PerspectivesSection.tsx';
import type { PerspectiveView, SearchResult } from '../api/types.ts';

afterEach(() => {
  cleanup();
});

const RESULTS: SearchResult[] = [
  {
    id: 'a',
    title: 'Bridge reopens Monday',
    snippet: 'Open.',
    original_score: 1,
    final_score: 1,
    url: 'https://one.example/x',
    provider: 'mock',
    reasons: [],
    warnings: [],
    metadata: {},
  },
];

const VIEWS: PerspectiveView[] = [
  {
    story_id: 's1',
    claim: 'The bridge reopened on Monday',
    representative_id: 'a',
    member_ids: ['a', 'b'],
    stances: [
      { source_id: 'a', stance: 'supports', evidence: 'The bridge reopened Monday.' },
      { source_id: 'b', stance: 'refutes', evidence: '' },
    ],
    supports: 1,
    refutes: 1,
    neutral: 0,
    multi_source: true,
    missing_note: null,
    trust_caveats: ['b: low credibility signal (0.20)'],
  },
];

describe('PerspectivesSection', () => {
  it('renders nothing when views are empty', () => {
    const { container } = render(<PerspectivesSection views={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders claims, stance badges, evidence, and caveats', () => {
    render(<PerspectivesSection views={VIEWS} results={RESULTS} />);
    expect(screen.getByTestId('perspectives-section')).toBeInTheDocument();
    expect(screen.getByText('Different perspectives')).toBeInTheDocument();
    expect(screen.getByText('The bridge reopened on Monday')).toBeInTheDocument();
    expect(screen.getByTestId('perspective-counts')).toHaveTextContent('Supports 1');
    expect(screen.getByText('Supports')).toBeInTheDocument();
    expect(screen.getByText('Refutes')).toBeInTheDocument();
    expect(screen.getByTestId('perspective-evidence')).toHaveTextContent(
      'The bridge reopened Monday.',
    );
    const sources = screen.getAllByTestId('perspective-source');
    expect(sources).toHaveLength(2);
    expect(sources[0]).toHaveTextContent('Bridge reopens Monday');
    expect(sources[1]).toHaveTextContent('b');
    expect(screen.getByTestId('perspective-caveats')).toHaveTextContent('low credibility');
  });

  it('renders coverage gaps and degrades unknown stances to neutral', () => {
    render(
      <PerspectivesSection
        views={[
          {
            story_id: 's2',
            claim: 'Council approved the budget',
            member_ids: ['x'],
            stances: [{ source_id: 'x', stance: 'bogus', evidence: '' }],
            missing_note: 'no refuting source in this cluster',
          },
        ]}
      />,
    );
    expect(screen.getByTestId('perspective-missing')).toHaveTextContent('no refuting source');
    expect(screen.getByText('Neutral')).toBeInTheDocument();
    // No results passed: member renders as a raw id, not a link.
    expect(screen.getByTestId('perspective-source')).toHaveTextContent('x');
    expect(screen.getByTestId('perspective-source').tagName).toBe('SPAN');
  });
});
