import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import type { SearchResult } from '../api/types.ts';
import { ResultList, groupByStory } from './ResultList.tsx';

afterEach(() => {
  cleanup();
});

function hit(id: string, storyId?: string): SearchResult {
  return {
    id,
    title: `Title ${id}`,
    snippet: `Snippet ${id}`,
    original_score: 1,
    final_score: 1,
    url: null,
    provider: 'mock',
    reasons: [],
    warnings: [],
    metadata: storyId ? { story_id: storyId } : {},
  };
}

describe('story grouping', () => {
  it('groups shared story ids under one header, preserving order', () => {
    const groups = groupByStory([hit('a', 'story-1'), hit('b'), hit('c', 'story-1')]);
    expect(groups.map((group) => group.key)).toEqual(['story-1', 'single-b']);
    expect(groups[0]?.heading).toBe('Related coverage (2)');
    expect(groups[1]?.heading).toBeNull();
  });

  it('renders group headers only for multi-hit stories', () => {
    render(<ResultList results={[hit('a', 'story-1'), hit('b'), hit('c', 'story-1')]} total={3} />);
    const headers = screen.getAllByTestId('story-group');
    expect(headers).toHaveLength(1);
    expect(headers[0]).toHaveTextContent('Related coverage (2)');
    expect(screen.getByText('Title b')).toBeInTheDocument();
  });

  it('renders no group chrome without story ids', () => {
    render(<ResultList results={[hit('a'), hit('b')]} total={2} />);
    expect(screen.queryByTestId('story-group')).not.toBeInTheDocument();
    expect(screen.getByText('Title a')).toBeInTheDocument();
  });
});
