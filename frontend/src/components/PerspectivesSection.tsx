import type { PerspectiveView, SearchResult } from '../api/types.ts';
import { Badge } from './ui/Badge.tsx';

export interface PerspectivesSectionProps {
  /** Story views from `readPerspectives`; renders nothing when empty. */
  views: PerspectiveView[];
  /** Ranked hits, used to resolve member titles and links by id. */
  results?: SearchResult[];
}

function stanceLabel(stance: string): string {
  if (stance === 'supports') return 'Supports';
  if (stance === 'refutes') return 'Refutes';
  if (stance === 'unrated') return 'Unrated';
  return 'Neutral';
}

function stanceTitle(stance: string): string {
  if (stance === 'supports') return 'This source backs the claim above';
  if (stance === 'refutes') return 'This source contradicts the claim above';
  if (stance === 'unrated') return 'No stance signal for this source';
  return 'This source takes no clear position on the claim above';
}

/**
 * Story views grouped by the backend: the claim, per-source stances
 * with evidence quotes, coverage notes, and trust caveats. Renders
 * nothing when `views` is empty (older envelopes, single-source
 * results). Member titles and links resolve from `results` by id and
 * degrade to the raw id when the hit is not on this page.
 */
export function PerspectivesSection({ views, results = [] }: PerspectivesSectionProps) {
  if (views.length === 0) return null;
  const byId = new Map(results.map((hit) => [hit.id, hit]));
  return (
    <section aria-label="Different perspectives" data-testid="perspectives-section">
      <h2 className="m-0 mb-ol-s text-ol-m">Different perspectives</h2>
      <p className="text-ol-s text-ol-ink-soft my-ol-s">
        Independent coverage of the same story, grouped automatically. Stances are a keyword
        heuristic, not a verdict.
      </p>
      {views.map((view, index) => (
        <article
          key={view.story_id || `perspective-${index}`}
          aria-label={view.claim ?? `Perspective ${index + 1}`}
          data-testid="perspective-view"
          className="mt-ol-s rounded-ol-s border border-ol-line px-ol-m py-ol-s"
        >
          {view.claim ? <h3 className="m-0 mb-ol-s text-ol-m">{view.claim}</h3> : null}
          <p className="ol-result-badges my-ol-s">
            {view.multi_source ? (
              <Badge title="Members come from at least two different websites">
                Multiple sources
              </Badge>
            ) : null}{' '}
            <Badge
              title="How many member sources back, contradict, or ignore the claim"
              testId="perspective-counts"
            >
              Supports {view.supports ?? 0} · Refutes {view.refutes ?? 0} · Neutral{' '}
              {view.neutral ?? 0}
            </Badge>
          </p>
          {view.stances && view.stances.length > 0 ? (
            <ul
              data-testid="perspective-stances"
              className="flex flex-col gap-ol-xs my-ol-s pl-ol-m"
            >
              {view.stances.map((stance) => {
                const hit = byId.get(stance.source_id);
                const label = hit?.title ?? stance.source_id;
                return (
                  <li key={stance.source_id}>
                    <Badge title={stanceTitle(stance.stance)}>{stanceLabel(stance.stance)}</Badge>{' '}
                    {hit?.url ? (
                      <a
                        href={hit.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        data-testid="perspective-source"
                      >
                        {label}
                      </a>
                    ) : (
                      <span data-testid="perspective-source">{label}</span>
                    )}
                    {stance.evidence ? (
                      <q data-testid="perspective-evidence">{stance.evidence}</q>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          ) : null}
          {view.missing_note ? (
            <p data-testid="perspective-missing" className="text-ol-s text-ol-ink-soft my-ol-s">
              <Badge title="One side of the story has no indexed source in this cluster">
                Coverage gap
              </Badge>{' '}
              {view.missing_note}
            </p>
          ) : null}
          {view.trust_caveats && view.trust_caveats.length > 0 ? (
            <div
              className="mt-ol-s rounded-ol-s border border-ol-warn bg-ol-warn-wash px-ol-m py-ol-s"
              data-testid="perspective-caveats"
            >
              <ul className="m-0 pl-ol-m">
                {view.trust_caveats.map((caveat) => (
                  <li key={caveat}>{caveat}</li>
                ))}
              </ul>
            </div>
          ) : null}
        </article>
      ))}
    </section>
  );
}
