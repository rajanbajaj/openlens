import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { ResultCard } from '../components/ResultCard.tsx';
import type { SearchResult } from './types.ts';
import { buildFilters, EMPTY_FILTER_STATE, isKnownFilterKey } from './filters.ts';
import { resultBook, resultFood } from './enrichments.ts';

afterEach(() => {
  cleanup();
});

/**
 * Vertical filters (travel/books/food): every new panel field maps to a
 * backend-known wire key, and the book/food readers degrade gracefully.
 */
describe('vertical filter mapper', () => {
  it('emits the budget gate only when opted in', () => {
    expect(buildFilters({ ...EMPTY_FILTER_STATE, maxPrice: '250' })).toEqual({
      max_price: 250,
    });
    expect(
      buildFilters({ ...EMPTY_FILTER_STATE, maxPrice: '250', excludeOverBudget: true }),
    ).toEqual({ max_price: 250, exclude_over_budget: true });
  });

  it('emits book and food keys for a fully populated vertical state', () => {
    const filters = buildFilters({
      ...EMPTY_FILTER_STATE,
      occasion: 'wedding',
      license: 'CC-BY',
      author: 'Jane Austen',
      cuisine: 'Italian',
      dietaryTags: 'vegan, gluten-free',
      ingredients: 'tomato, basil',
      excludeAllergens: 'peanuts',
    });
    expect(filters).toEqual({
      occasion: 'wedding',
      license: 'CC-BY',
      author: 'Jane Austen',
      cuisine: 'Italian',
      dietary_tags: 'vegan, gluten-free',
      ingredients: 'tomato, basil',
      exclude_allergens: 'peanuts',
    });
    for (const key of Object.keys(filters)) {
      expect(isKnownFilterKey(key)).toBe(true);
    }
  });

  it('drops blank vertical inputs', () => {
    expect(buildFilters({ ...EMPTY_FILTER_STATE, license: '   ', cuisine: '' })).toEqual({});
  });
});

describe('ResultCard vertical lines', () => {
  const base: SearchResult = {
    id: 'v1',
    title: 'Margherita Recipe',
    snippet: 'Classic pie',
    original_score: 1,
    final_score: 1,
    url: 'https://example.com/recipe',
    provider: 'custom_index',
    reasons: [],
    warnings: [],
    metadata: {},
  };

  it('renders book and food attribute lines when present', () => {
    render(
      <ResultCard
        result={{
          ...base,
          metadata: {
            author: 'Jane Austen',
            license: 'CC-BY',
            cuisine: 'Italian',
            dietary_tags: ['vegan'],
            allergens: ['peanuts'],
          },
        }}
      />,
    );
    const attrs = screen.getByTestId('result-attributes');
    expect(attrs).toHaveTextContent('By Jane Austen');
    expect(attrs).toHaveTextContent('License: CC-BY');
    expect(attrs).toHaveTextContent('Italian cuisine');
    expect(attrs).toHaveTextContent('vegan');
    expect(attrs).toHaveTextContent('Contains: peanuts');
  });

  it('renders no attribute line for bare metadata', () => {
    render(<ResultCard result={base} />);
    expect(screen.queryByTestId('result-attributes')).not.toBeInTheDocument();
  });
});

describe('book and food readers', () => {
  it('reads book attributes with graceful fallbacks', () => {
    expect(
      resultBook({ license: 'CC-BY', author: 'Jane Austen', isbn: '', publisher: 42 }),
    ).toEqual({ license: 'CC-BY', author: 'Jane Austen', isbn: null, publisher: null });
    expect(resultBook({})).toEqual({
      license: null,
      author: null,
      isbn: null,
      publisher: null,
    });
  });

  it('reads food attributes with graceful fallbacks', () => {
    expect(
      resultFood({
        cuisine: 'Italian',
        ingredients: ['tomato', 'basil'],
        dietary_tags: 'vegan',
        allergens: ['peanuts', 7],
      }),
    ).toEqual({
      cuisine: 'Italian',
      ingredients: ['tomato', 'basil'],
      dietaryTags: ['vegan'],
      allergens: ['peanuts'],
    });
    expect(resultFood({})).toEqual({
      cuisine: null,
      ingredients: [],
      dietaryTags: [],
      allergens: [],
    });
  });
});
