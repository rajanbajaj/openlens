import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import type { SearchResult } from '../api/types.ts';
import { ResultCard } from './ResultCard.tsx';

afterEach(() => {
  cleanup();
});

const RICH_RESULT: SearchResult = {
  id: 'r1',
  title: 'Acme Headphones',
  snippet: 'Great sound',
  original_score: 2.04,
  final_score: 9.93,
  url: 'https://example.com/h',
  provider: 'custom_index',
  reasons: ['matches query'],
  warnings: ['trust: unknown domain'],
  metadata: {
    signals: { relevance: 1, trust: 0.55 },
    also_from: ['opensearch'],
    price: 199.99,
    currency: 'USD',
    location: 'Berlin',
    ai_summary: 'Two models compared.',
    ai_perspectives: ['Audiophiles prefer wired.'],
    ai_model: 'rule-based',
    ai_intent: 'shopping',
    ai_entities: ['headphones'],
  },
};

describe('ResultCard transparency', () => {
  it('shows provider badges, attributes, and the score breakdown', () => {
    render(<ResultCard result={RICH_RESULT} />);
    expect(screen.getByText('custom_index')).toBeInTheDocument();
    expect(screen.getByText('also from opensearch')).toBeInTheDocument();
    expect(screen.getByTestId('result-attributes')).toHaveTextContent('199.99 USD');
    expect(screen.getByTestId('result-attributes')).toHaveTextContent('Berlin');

    fireEvent.click(screen.getByRole('button', { name: /show score breakdown/i }));
    const breakdown = screen.getByTestId('result-breakdown');
    expect(breakdown).toHaveTextContent('2.04');
    expect(breakdown).toHaveTextContent('9.93');
    expect(breakdown).toHaveTextContent('trust');
  });

  it('renders the labelled AI block only when the backend produced one', () => {
    const { rerender } = render(<ResultCard result={RICH_RESULT} />);
    const ai = screen.getByTestId('result-ai');
    expect(ai).toHaveTextContent('Two models compared.');
    expect(ai).toHaveTextContent('rule-based');
    expect(ai).toHaveTextContent('AI-generated content');
    expect(ai).not.toHaveTextContent('Sources:');
    expect(screen.queryByTestId('result-ai-sources')).not.toBeInTheDocument();
    expect(screen.getByTestId('result-understanding')).toHaveTextContent('shopping');

    rerender(<ResultCard result={{ ...RICH_RESULT, metadata: {} }} />);
    expect(screen.queryByTestId('result-ai')).not.toBeInTheDocument();
    expect(screen.queryByTestId('result-ai-sources')).not.toBeInTheDocument();
    expect(screen.queryByTestId('result-understanding')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /show score breakdown/i }));
    expect(screen.getByTestId('result-breakdown')).toHaveTextContent('No scoring signals');
  });

  it('renders source links inside the AI block with correct hrefs', () => {
    const result: SearchResult = {
      ...RICH_RESULT,
      metadata: {
        ...RICH_RESULT.metadata,
        ai_citations: [
          { id: 'r1', title: 'Acme Headphones', url: 'https://example.com/h' },
          { id: 'r2', title: 'Unlinked Review', url: null },
        ],
      },
    };
    render(<ResultCard result={result} />);
    const sources = screen.getByTestId('result-ai-sources');
    expect(sources).toHaveTextContent('Acme Headphones');
    expect(sources).toHaveTextContent('Unlinked Review');
    const link = within(sources).getByRole('link', { name: 'Acme Headphones' });
    expect(link).toHaveAttribute('href', 'https://example.com/h');
    expect(
      within(sources).queryByRole('link', { name: 'Unlinked Review' }),
    ).not.toBeInTheDocument();
  });

  it('renders no sources chrome for absent or malformed citations', () => {
    const malformed: SearchResult = {
      ...RICH_RESULT,
      metadata: {
        ...RICH_RESULT.metadata,
        ai_citations: ['https://example.com/h', null, {}, { id: 'no-title' }],
      },
    };
    const { rerender } = render(<ResultCard result={malformed} />);
    expect(screen.getByTestId('result-ai')).toBeInTheDocument();
    expect(screen.queryByTestId('result-ai-sources')).not.toBeInTheDocument();

    rerender(
      <ResultCard
        result={{ ...RICH_RESULT, metadata: { ...RICH_RESULT.metadata, ai_citations: 'nope' } }}
      />,
    );
    expect(screen.getByTestId('result-ai')).toBeInTheDocument();
    expect(screen.queryByTestId('result-ai-sources')).not.toBeInTheDocument();
  });

  it('keeps trust warnings visually distinct from reasons', () => {
    render(<ResultCard result={RICH_RESULT} />);
    fireEvent.click(screen.getByRole('button', { name: /show warnings/i }));
    expect(screen.getByTestId('result-warnings-block')).toHaveClass('ol-warnings');
    expect(screen.getByTestId('result-warnings')).toHaveTextContent('trust: unknown domain');
  });
});

describe('ResultCard hierarchy', () => {
  it('demotes commerce and understanding lines to secondary meta text', () => {
    render(<ResultCard result={RICH_RESULT} />);
    expect(screen.getByTestId('result-snippet')).not.toHaveClass('ol-result-meta');
    expect(screen.getByTestId('result-attributes')).toHaveClass('ol-result-meta');
    expect(screen.getByTestId('result-understanding')).toHaveClass('ol-result-meta');
    expect(screen.getByTestId('result-meta')).toHaveClass('ol-result-meta');
  });

  it('renders signals in display order with labelled bars', () => {
    const result: SearchResult = {
      ...RICH_RESULT,
      metadata: { ...RICH_RESULT.metadata, signals: { trust: 0.55, relevance: 1 } },
    };
    render(<ResultCard result={result} />);
    fireEvent.click(screen.getByRole('button', { name: /show score breakdown/i }));
    const breakdown = screen.getByTestId('result-breakdown');
    const items = within(breakdown)
      .getAllByRole('term')
      .map((term) => term.textContent);
    expect(items.indexOf('relevance')).toBeLessThan(items.indexOf('trust'));
    expect(within(breakdown).getByRole('img', { name: 'relevance signal 1' })).toBeInTheDocument();
    expect(within(breakdown).getByRole('img', { name: 'trust signal 0.55' })).toBeInTheDocument();
  });

  it('renders no AI chrome at all without an AI summary', () => {
    render(<ResultCard result={{ ...RICH_RESULT, metadata: {} }} />);
    const card = screen.getByTestId('result-card');
    expect(card).not.toHaveTextContent('AI summary');
    expect(card).not.toHaveTextContent('Sources:');
    expect(card).not.toHaveTextContent('AI-generated content');
    expect(card).not.toHaveTextContent('Understood as');
  });

  it('keeps the reasons disclosure free of warning styling', () => {
    render(<ResultCard result={RICH_RESULT} />);
    fireEvent.click(screen.getByRole('button', { name: /show reasons/i }));
    const reasons = screen.getByTestId('result-reasons');
    expect(reasons).toHaveTextContent('matches query');
    expect(reasons.parentElement).not.toHaveClass('ol-warnings');
  });
});

describe('ResultCard disclosures', () => {
  it.each([
    [/show score breakdown/i, 'result-breakdown'],
    [/show reasons/i, 'result-reasons'],
    [/show warnings/i, 'result-warnings'],
  ] as const)('exposes %s as a keyboard-operable disclosure for %s', (name, testId) => {
    render(<ResultCard result={RICH_RESULT} />);
    const toggle = screen.getByRole('button', { name });
    expect(toggle.tagName).toBe('BUTTON');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    const controls = toggle.getAttribute('aria-controls');
    expect(controls).not.toBeNull();

    toggle.focus();
    expect(document.activeElement).toBe(toggle);
    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByTestId(testId)).toHaveAttribute('id', controls);
  });
});
