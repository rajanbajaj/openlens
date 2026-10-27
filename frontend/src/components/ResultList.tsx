import type { SearchResult } from '../api/types';
import { ResultCard } from './ResultCard';
import { EmptyResults } from './States';

export interface ResultListProps {
  results: SearchResult[];
  total: number;
}

/** Ordered result list with a pre-pagination total; empty state when no hits. */
export function ResultList({ results, total }: ResultListProps) {
  if (results.length === 0) {
    return <EmptyResults />;
  }
  return (
    <section aria-label="Search results">
      <p data-testid="result-count">
        Showing {results.length} of {total} result{total === 1 ? '' : 's'}
      </p>
      <ol>
        {results.map((result) => (
          <li key={result.id}>
            <ResultCard result={result} />
          </li>
        ))}
      </ol>
    </section>
  );
}
