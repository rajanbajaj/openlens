import { cleanup, fireEvent, render, screen } from '@testing-library/react';
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
    expect(screen.getByTestId('result-understanding')).toHaveTextContent('shopping');

    rerender(<ResultCard result={{ ...RICH_RESULT, metadata: {} }} />);
    expect(screen.queryByTestId('result-ai')).not.toBeInTheDocument();
    expect(screen.queryByTestId('result-understanding')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /show score breakdown/i }));
    expect(screen.getByTestId('result-breakdown')).toHaveTextContent('No scoring signals');
  });

  it('keeps trust warnings visually distinct from reasons', () => {
    render(<ResultCard result={RICH_RESULT} />);
    fireEvent.click(screen.getByRole('button', { name: /show warnings/i }));
    expect(screen.getByTestId('result-warnings-block')).toHaveClass('ol-warnings');
    expect(screen.getByTestId('result-warnings')).toHaveTextContent('trust: unknown domain');
  });
});
