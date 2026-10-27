import { useState } from 'react';
import type { SearchResult } from '../api/types';

export interface ResultCardProps {
  result: SearchResult;
}

/** One search hit: title link, snippet, final score, expandable reasons/warnings. */
export function ResultCard({ result }: ResultCardProps) {
  const [showReasons, setShowReasons] = useState(false);
  const [showWarnings, setShowWarnings] = useState(false);

  return (
    <article data-testid="result-card" aria-label={result.title}>
      <h3>
        {result.url ? (
          <a href={result.url} target="_blank" rel="noopener noreferrer">
            {result.title}
          </a>
        ) : (
          <span>{result.title}</span>
        )}
      </h3>
      <p data-testid="result-snippet">{result.snippet}</p>
      <p data-testid="result-meta">
        <span>Score: {result.final_score}</span>
        {result.provider ? <span> · {result.provider}</span> : null}
      </p>
      {result.reasons.length > 0 ? (
        <div>
          <button
            type="button"
            aria-expanded={showReasons}
            aria-controls={`reasons-${result.id}`}
            onClick={() => setShowReasons((v) => !v)}
          >
            {showReasons ? 'Hide reasons' : `Show reasons (${result.reasons.length})`}
          </button>
          {showReasons ? (
            <ul id={`reasons-${result.id}`} data-testid="result-reasons">
              {result.reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
      {result.warnings.length > 0 ? (
        <div>
          <button
            type="button"
            aria-expanded={showWarnings}
            aria-controls={`warnings-${result.id}`}
            onClick={() => setShowWarnings((v) => !v)}
          >
            {showWarnings ? 'Hide warnings' : `Show warnings (${result.warnings.length})`}
          </button>
          {showWarnings ? (
            <ul id={`warnings-${result.id}`} data-testid="result-warnings">
              {result.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}
