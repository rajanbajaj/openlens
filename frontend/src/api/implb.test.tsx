import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import type { SearchResult } from '../api/types.ts';
import {
  resultKeyClaims,
  resultOpinion,
  resultRating,
  resultShoppingDetails,
  resultSponsored,
} from '../api/enrichments.ts';
import { ResultCard } from '../components/ResultCard.tsx';

afterEach(() => {
  cleanup();
});

describe('IMPL-B shopping/news readers', () => {
  it('reads sponsored, rating, details, opinion, and claims', () => {
    const metadata: Record<string, unknown> = {
      sponsored: true,
      rating: 4.5,
      rating_count: 12,
      brand: 'Acme',
      availability: 'in_stock',
      booking_url: 'https://hotel.example/book/1',
      style: 'Blue',
      size: 'M',
      occasion: 'casual',
      opinion: 'opinion',
      key_claims: ['The council approved the budget 5-2.'],
    };
    expect(resultSponsored(metadata)).toBe(true);
    expect(resultRating(metadata)).toEqual({ rating: 4.5, ratingCount: 12 });
    expect(resultShoppingDetails(metadata)).toEqual({
      brand: 'Acme',
      availability: 'in_stock',
      bookingUrl: 'https://hotel.example/book/1',
      style: 'Blue',
      size: 'M',
      occasion: 'casual',
    });
    expect(resultOpinion(metadata)).toBe('opinion');
    expect(resultKeyClaims(metadata)).toEqual(['The council approved the budget 5-2.']);
    expect(resultSponsored({})).toBe(false);
    expect(resultRating({})).toEqual({ rating: null, ratingCount: null });
    expect(resultOpinion({})).toBeNull();
    expect(resultKeyClaims({ key_claims: 'nope' })).toEqual([]);
  });

  it('renders sponsored badge, rating, booking link, opinion, and claims', () => {
    const result: SearchResult = {
      id: 'r9',
      title: 'Acme Hotel',
      snippet: 'Seaside rooms',
      original_score: 1,
      final_score: 2,
      url: 'https://hotel.example/rooms',
      provider: 'custom_index',
      reasons: [],
      warnings: [],
      metadata: {
        sponsored: true,
        rating: 4.5,
        rating_count: 12,
        brand: 'Acme',
        booking_url: 'https://hotel.example/book/1',
        style: 'Blue',
        size: 'M',
        opinion: 'opinion',
        key_claims: ['The council approved the budget 5-2.'],
      },
    };
    render(<ResultCard result={result} />);
    expect(screen.getByText('Sponsored')).toBeInTheDocument();
    expect(screen.getByTestId('result-attributes')).toHaveTextContent('Rated 4.5 (12 reviews)');
    expect(screen.getByTestId('result-attributes')).toHaveTextContent('Acme');
    expect(screen.getByTestId('result-attributes')).toHaveTextContent('size M');
    expect(screen.getByTestId('result-booking')).toHaveTextContent('Book now');
    expect(screen.getByTestId('result-opinion')).toHaveTextContent('Opinion');
    expect(screen.getByTestId('result-claims')).toHaveTextContent('council approved');
  });
});
