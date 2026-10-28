import type { SearchResult } from '../api/types';
import { resultStoryId } from '../api/enrichments.ts';
import { ResultCard } from './ResultCard';
import { EmptyResults } from './States';

export interface ResultListProps {
  results: SearchResult[];
  total: number;
}

interface StoryGroup {
  key: string;
  heading: string | null;
  members: SearchResult[];
}

/** Group hits by `story_id` (T12), preserving first-seen order. */
export function groupByStory(results: SearchResult[]): StoryGroup[] {
  const groups: StoryGroup[] = [];
  const byKey = new Map<string, StoryGroup>();
  for (const result of results) {
    const storyId = resultStoryId(result.metadata);
    if (storyId === null) {
      groups.push({ key: `single-${result.id}`, heading: null, members: [result] });
      continue;
    }
    let group = byKey.get(storyId);
    if (!group) {
      group = { key: storyId, heading: null, members: [] };
      byKey.set(storyId, group);
      groups.push(group);
    }
    group.members.push(result);
  }
  for (const group of groups) {
    if (group.members.length > 1) {
      group.heading = `Related coverage (${group.members.length})`;
    }
  }
  return groups;
}

/**
 * Ordered result list with a pre-pagination total and story-group
 * headers for multi-hit stories. Single hits render as plain cards —
 * no group chrome — and group order follows first appearance.
 */
export function ResultList({ results, total }: ResultListProps) {
  if (results.length === 0) {
    return <EmptyResults />;
  }
  return (
    <section aria-label="Search results">
      <p data-testid="result-count" className="ol-result-count">
        Showing {results.length} of {total} result{total === 1 ? '' : 's'}
      </p>
      <ol className="ol-result-list">
        {groupByStory(results).map((group) =>
          group.heading ? (
            <li key={group.key}>
              <section
                aria-label={group.heading}
                data-testid="story-group"
                className="ol-story-group"
              >
                <h2>{group.heading}</h2>
                <ol className="ol-result-list">
                  {group.members.map((result) => (
                    <li key={result.id}>
                      <ResultCard result={result} />
                    </li>
                  ))}
                </ol>
              </section>
            </li>
          ) : (
            group.members.map((result) => (
              <li key={result.id}>
                <ResultCard result={result} />
              </li>
            ))
          ),
        )}
      </ol>
    </section>
  );
}
