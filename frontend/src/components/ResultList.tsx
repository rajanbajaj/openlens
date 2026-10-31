import type { SearchResult } from '../api/types';
import { resultStoryId } from '../api/enrichments.ts';
import { ResultCard } from './ResultCard';
import { EmptyResults } from './States';
import { Button } from './ui/Button.tsx';

export interface ResultListProps {
  results: SearchResult[];
  total: number;
  /** Vertical mode forwarded to each card; defaults to "default". */
  mode?: string;
  /** Corrected spelling hint; the hint row renders only when non-null. */
  spelling?: string | null;
  /** Related queries; the chip row renders only when non-empty. */
  related?: string[];
  /** Called when a related-search chip is activated. */
  onRelatedSelect?: (query: string) => void;
  /** Ids already in the personal library (for the per-card save toggle). */
  savedIds?: readonly string[];
  /** Called when a card save toggle is activated; omit to hide save buttons. */
  onSaveResult?: (result: SearchResult) => void;
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
 *
 * Query-assistance rows (spelling hint, related-search chips) render
 * only when the backend supplied them; absent or malformed assistance
 * renders nothing. Cards inherit the list `mode` for vertical regroups.
 */
export function ResultList({
  results,
  total,
  mode = 'default',
  spelling = null,
  related = [],
  onRelatedSelect,
  savedIds = [],
  onSaveResult,
}: ResultListProps) {
  const relatedQueries = related.filter(
    (query): query is string => typeof query === 'string' && query !== '',
  );
  const assistance =
    spelling || relatedQueries.length > 0 ? (
      <>
        {spelling && total === 0 ? (
          <p data-testid="spelling-hint" className="ol-spelling-hint">
            No results. Did you mean{' '}
            <Button
              variant="ghost"
              data-testid="spelling-suggestion"
              onClick={() => onRelatedSelect?.(spelling)}
            >
              &ldquo;{spelling}&rdquo;
            </Button>
            ?
          </p>
        ) : null}
        {relatedQueries.length > 0 ? (
          <ul data-testid="related-searches" className="ol-related-searches">
            {relatedQueries.map((query) => (
              <li key={query}>
                <Button
                  variant="ghost"
                  data-testid="related-chip"
                  onClick={() => onRelatedSelect?.(query)}
                >
                  {query}
                </Button>
              </li>
            ))}
          </ul>
        ) : null}
      </>
    ) : null;

  if (results.length === 0) {
    return (
      <>
        {assistance}
        <EmptyResults />
      </>
    );
  }
  return (
    <section aria-label="Search results">
      {assistance}
      <p data-testid="result-count" className="text-ol-s text-ol-ink-soft">
        Showing {results.length} of {total} result{total === 1 ? '' : 's'}
      </p>
      <ol className="my-ol-m flex list-none flex-col gap-ol-m p-0">
        {groupByStory(results).map((group) =>
          group.heading ? (
            <li key={group.key}>
              <section
                aria-label={group.heading}
                data-testid="story-group"
                className="border-t-2 border-ol-line pt-ol-s"
              >
                <h2 className="m-0 mb-ol-s text-ol-m">{group.heading}</h2>
                <ol className="my-ol-m flex list-none flex-col gap-ol-m p-0">
                  {group.members.map((result) => (
                    <li key={result.id}>
                      <ResultCard
                        result={result}
                        mode={mode}
                        saved={savedIds.includes(result.id)}
                        onSave={onSaveResult}
                      />
                    </li>
                  ))}
                </ol>
              </section>
            </li>
          ) : (
            group.members.map((result) => (
              <li key={result.id}>
                <ResultCard
                  result={result}
                  mode={mode}
                  saved={savedIds.includes(result.id)}
                  onSave={onSaveResult}
                />
              </li>
            ))
          ),
        )}
      </ol>
    </section>
  );
}
