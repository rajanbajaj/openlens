import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
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

  it('keeps first-seen order across interleaved stories', () => {
    const groups = groupByStory([
      hit('a', 'story-1'),
      hit('b', 'story-2'),
      hit('c', 'story-1'),
      hit('d', 'story-2'),
    ]);
    expect(groups.map((group) => group.key)).toEqual(['story-1', 'story-2']);
    expect(groups.map((group) => group.members.map((member) => member.id))).toEqual([
      ['a', 'c'],
      ['b', 'd'],
    ]);
  });

  it('renders group headers only for multi-hit stories', () => {
    render(<ResultList results={[hit('a', 'story-1'), hit('b'), hit('c', 'story-1')]} total={3} />);
    const headers = screen.getAllByTestId('story-group');
    expect(headers).toHaveLength(1);
    expect(headers[0]).toHaveTextContent('Related coverage (2)');
    expect(screen.getByText('Title b')).toBeInTheDocument();
  });

  it('renders group members under their header', () => {
    render(<ResultList results={[hit('a', 'story-1'), hit('c', 'story-1')]} total={2} />);
    const group = screen.getByTestId('story-group');
    expect(within(group).getByText('Title a')).toBeInTheDocument();
    expect(within(group).getByText('Title c')).toBeInTheDocument();
  });

  it('renders no group chrome without story ids', () => {
    render(<ResultList results={[hit('a'), hit('b')]} total={2} />);
    expect(screen.queryByTestId('story-group')).not.toBeInTheDocument();
    expect(screen.getByText('Title a')).toBeInTheDocument();
  });

  it('renders no group chrome for a single-hit story', () => {
    render(<ResultList results={[hit('a', 'story-1'), hit('b')]} total={2} />);
    expect(screen.queryByTestId('story-group')).not.toBeInTheDocument();
    expect(screen.getByText('Title a')).toBeInTheDocument();
  });
});

describe('result count', () => {
  it('shows the page size against the pre-pagination total', () => {
    render(<ResultList results={[hit('a'), hit('b')]} total={42} />);
    expect(screen.getByTestId('result-count')).toHaveTextContent('Showing 2 of 42 results');
  });

  it('uses the singular for a single total', () => {
    render(<ResultList results={[hit('a')]} total={1} />);
    expect(screen.getByTestId('result-count')).toHaveTextContent('Showing 1 of 1 result');
  });
});

describe('empty results', () => {
  it('renders the empty state without result chrome', () => {
    render(<ResultList results={[]} total={0} />);
    expect(screen.getByTestId('empty-results')).toBeInTheDocument();
    expect(screen.queryByTestId('result-count')).not.toBeInTheDocument();
    expect(screen.queryByTestId('result-card')).not.toBeInTheDocument();
  });
});

describe('spelling hint', () => {
  it('offers a clickable correction only when there are no results', () => {
    const onRelatedSelect = vi.fn();
    const { rerender } = render(
      <ResultList
        results={[]}
        total={0}
        spelling="running shoes"
        onRelatedSelect={onRelatedSelect}
      />,
    );
    const button = screen.getByTestId('spelling-suggestion');
    expect(button).toHaveTextContent('running shoes');
    fireEvent.click(button);
    expect(onRelatedSelect).toHaveBeenCalledWith('running shoes');
    rerender(
      <ResultList results={[]} total={0} spelling={null} onRelatedSelect={onRelatedSelect} />,
    );
    expect(screen.queryByTestId('spelling-hint')).not.toBeInTheDocument();
  });

  it('never shows the hint alongside results', () => {
    render(<ResultList results={[hit('a')]} total={3} spelling="running shoes" />);
    expect(screen.queryByTestId('spelling-hint')).not.toBeInTheDocument();
  });
});
