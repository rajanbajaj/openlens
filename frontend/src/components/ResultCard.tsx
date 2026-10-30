import { useState } from 'react';
import type { SearchResult } from '../api/types';
import { sendFeedback } from '../api/client.ts';
import {
  resultAiDigest,
  resultAlsoFrom,
  resultBook,
  resultCommerce,
  resultFood,
  resultKeyClaims,
  resultOpinion,
  resultRating,
  resultShoppingDetails,
  resultConfidence,
  resultSignals,
  resultSponsored,
  resultUnderstanding,
} from '../api/enrichments.ts';
import { Badge } from './ui/Badge.tsx';
import { Button } from './ui/Button.tsx';
import { Card } from './ui/Card.tsx';

/** Anonymous report actions a reader can file against one hit. */
export type ReportAction = 'report_incorrect' | 'report_suspicious';

export interface ResultCardProps {
  result: SearchResult;
  /** Vertical mode regrouping which readers get prominence. Defaults to "default". */
  mode?: string;
  /** Whether this hit is already in the personal library. */
  saved?: boolean;
  /** Called when the save toggle is activated; omit to hide the button. */
  onSave?: (result: SearchResult) => void;
  /**
   * Called when a report button is activated. Defaults to a
   * fire-and-forget `sendFeedback` post (anonymous doc id + action
   * only); pass a spy in tests to avoid the network.
   */
  onReport?: (action: ReportAction, result: SearchResult) => void;
}

function formatPrice(price: number | null, currency: string | null): string | null {
  if (price === null) return null;
  return currency ? `${price.toFixed(2)} ${currency}` : price.toFixed(2);
}

interface VerticalHighlight {
  kind: 'news' | 'shopping' | 'travel';
  text: string;
}

/**
 * Mode-specific prominence line built only from fields the backend
 * already returns (via the shared enrichment readers — no new data).
 * News foregrounds date + source, shopping foregrounds price,
 * travel foregrounds location + facilities. Unknown modes (including
 * "default" and "child-safe") return null so the default layout is kept.
 */
function verticalHighlight(result: SearchResult, mode: string): VerticalHighlight | null {
  const commerce = resultCommerce(result.metadata);
  if (mode === 'news') {
    const parts: string[] = [];
    if (commerce.published) parts.push(commerce.published);
    parts.push(`Source: ${result.provider}`);
    return { kind: 'news', text: parts.join(' · ') };
  }
  if (mode === 'shopping') {
    const price = formatPrice(commerce.price, commerce.currency);
    if (price === null) return null;
    return { kind: 'shopping', text: price };
  }
  if (mode === 'travel') {
    const parts: string[] = [];
    if (commerce.location) parts.push(commerce.location);
    if (commerce.facilities.length > 0) parts.push(commerce.facilities.join(', '));
    if (parts.length === 0) return null;
    return { kind: 'travel', text: parts.join(' · ') };
  }
  return null;
}

/**
 * One search hit on a subtle glass card (the vibrant backdrop does the
 * refracting; the card itself stays near-opaque for readability): title row,
 * provider + federation badges, snippet,
 * secondary commerce/understanding lines, score with an expandable
 * breakdown (original vs final, T11 signals in display order), the
 * labelled AI block (rendered only when the backend produced one), and
 * reasons/warnings disclosures — trust warnings visually distinct.
 *
 * News/shopping/travel modes add one prominent vertical line regrouping
 * the same readers (date/source, price, location/facilities); every
 * other line keeps its `data-testid` and default placement.
 */
export function ResultCard({
  result,
  mode = 'default',
  saved = false,
  onSave,
  onReport,
}: ResultCardProps) {
  const [showBreakdown, setShowBreakdown] = useState(false);
  const [showReasons, setShowReasons] = useState(false);
  const [showWarnings, setShowWarnings] = useState(false);

  const signals = resultSignals(result.metadata);
  const alsoFrom = resultAlsoFrom(result.metadata);
  const commerce = resultCommerce(result.metadata);
  const digest = resultAiDigest(result.metadata);
  const understanding = resultUnderstanding(result.metadata);
  const sponsored = resultSponsored(result.metadata);
  const rating = resultRating(result.metadata);
  const details = resultShoppingDetails(result.metadata);
  const book = resultBook(result.metadata);
  const food = resultFood(result.metadata);
  const opinion = resultOpinion(result.metadata);
  const keyClaims = resultKeyClaims(result.metadata);
  const confidence = resultConfidence(result.metadata);
  const price = formatPrice(commerce.price, commerce.currency);
  const attributes: string[] = [];
  if (price) attributes.push(price);
  if (rating.rating !== null) {
    attributes.push(
      rating.ratingCount !== null
        ? `Rated ${rating.rating} (${rating.ratingCount} reviews)`
        : `Rated ${rating.rating}`,
    );
  }
  if (details.brand) attributes.push(details.brand);
  if (details.availability) attributes.push(details.availability);
  if (commerce.location) attributes.push(commerce.location);
  if (commerce.facilities.length > 0) attributes.push(commerce.facilities.join(', '));
  const styleBits: string[] = [];
  if (details.style) styleBits.push(details.style);
  if (details.size) styleBits.push(`size ${details.size}`);
  if (details.occasion) styleBits.push(details.occasion);
  if (styleBits.length > 0) attributes.push(styleBits.join(' · '));
  if (book.author) attributes.push(`By ${book.author}`);
  if (book.license) attributes.push(`License: ${book.license}`);
  if (food.cuisine) attributes.push(`${food.cuisine} cuisine`);
  if (food.dietaryTags.length > 0) attributes.push(food.dietaryTags.join(', '));
  if (food.allergens.length > 0) attributes.push(`Contains: ${food.allergens.join(', ')}`);
  if (commerce.published) attributes.push(commerce.published);
  const highlight = verticalHighlight(result, mode);

  return (
    <Card>
      <article aria-label={result.title} data-testid="result-card">
        <header>
          <h3>
            {result.url ? (
              <a href={result.url} target="_blank" rel="noopener noreferrer">
                {result.title}
              </a>
            ) : (
              <span>{result.title}</span>
            )}
          </h3>
          <p>
            <Badge>{result.provider}</Badge>{' '}
            {alsoFrom.map((provider) => (
              <Badge key={provider}>also from {provider}</Badge>
            ))}{' '}
            {sponsored ? <Badge>Sponsored</Badge> : null}
            {onSave ? (
              <>
                {' '}
                <Button
                  variant="ghost"
                  data-testid="result-save"
                  aria-pressed={saved}
                  aria-label={
                    saved
                      ? `Remove ${result.title} from library`
                      : `Save ${result.title} to library`
                  }
                  onClick={() => onSave(result)}
                >
                  {saved ? 'Saved' : 'Save'}
                </Button>
              </>
            ) : null}
          </p>
        </header>
        {highlight ? (
          <p
            data-testid={`result-vertical-${highlight.kind}`}
            className="ol-result-vertical text-ol-s text-ol-ink-soft"
          >
            {highlight.text}
          </p>
        ) : null}
        <p data-testid="result-snippet">{result.snippet}</p>
        {attributes.length > 0 ? (
          <p data-testid="result-attributes" className="text-ol-s text-ol-ink-soft">
            {attributes.join(' · ')}
          </p>
        ) : null}
        {details.bookingUrl ? (
          <p className="text-ol-s text-ol-ink-soft">
            <a
              href={details.bookingUrl}
              target="_blank"
              rel="noopener noreferrer"
              data-testid="result-booking"
            >
              Book now
            </a>
          </p>
        ) : null}
        {opinion ? (
          <p data-testid="result-opinion" className="text-ol-s text-ol-ink-soft">
            {opinion === 'opinion' ? 'Opinion / commentary' : `Reads as ${opinion}`}
          </p>
        ) : null}
        {keyClaims.length > 0 ? (
          <div>
            <p className="text-ol-s text-ol-ink-soft">Key claims:</p>
            <ul data-testid="result-claims">
              {keyClaims.map((claim) => (
                <li key={claim}>{claim}</li>
              ))}
            </ul>
          </div>
        ) : null}
        {understanding ? (
          <p data-testid="result-understanding" className="text-ol-s text-ol-ink-soft">
            Understood as {understanding.intent}
            {understanding.entities.length > 0
              ? ` (${understanding.entities.join(', ')})`
              : ''} · {understanding.model === 'rule-based' ? 'rule-based' : understanding.model}
          </p>
        ) : null}
        <p data-testid="result-meta" className="text-ol-s text-ol-ink-soft">
          <span>Score: {result.final_score}</span>
          {confidence !== null ? (
            <span data-testid="result-confidence"> · Confidence: {confidence}</span>
          ) : null}
        </p>
        <div>
          <Button
            variant="ghost"
            aria-expanded={showBreakdown}
            aria-controls={`breakdown-${result.id}`}
            onClick={() => setShowBreakdown((v) => !v)}
          >
            {showBreakdown ? 'Hide score breakdown' : 'Show score breakdown'}
          </Button>
          {showBreakdown ? (
            <dl id={`breakdown-${result.id}`} data-testid="result-breakdown">
              <div>
                <dt>Provider score</dt>
                <dd>{result.original_score}</dd>
              </div>
              <div>
                <dt>Final score</dt>
                <dd>{result.final_score}</dd>
              </div>
              {signals.map(([name, score]) => (
                <div key={name}>
                  <dt>{name}</dt>
                  <dd>
                    <span
                      className="inline-block h-2 w-24 overflow-hidden rounded-ol-s border border-ol-glass-border bg-white/35 align-middle"
                      role="img"
                      aria-label={`${name} signal ${score}`}
                    >
                      <span
                        className="block h-full bg-ol-accent"
                        style={{ width: `${Math.round(score * 100)}%` }}
                      />
                    </span>{' '}
                    {score}
                  </dd>
                </div>
              ))}
              {signals.length === 0 ? (
                <div>
                  <dt>Signals</dt>
                  <dd>No scoring signals reported</dd>
                </div>
              ) : null}
            </dl>
          ) : null}
        </div>
        {digest ? (
          <section aria-label="AI summary" data-testid="result-ai">
            <h4>AI summary</h4>
            <p>{digest.summary}</p>
            {digest.perspectives.length > 0 ? (
              <ul>
                {digest.perspectives.map((perspective) => (
                  <li key={perspective}>{perspective}</li>
                ))}
              </ul>
            ) : null}
            <p className="text-ol-s text-ol-ink-soft">
              Generated by {digest.model} · AI-generated content
            </p>
            {digest.citations.length > 0 ? (
              <div>
                <p className="text-ol-s text-ol-ink-soft">Sources:</p>
                <ul data-testid="result-ai-sources">
                  {digest.citations.map((citation) => (
                    <li key={citation.id}>
                      {citation.url ? (
                        <a href={citation.url} target="_blank" rel="noopener noreferrer">
                          {citation.title}
                        </a>
                      ) : (
                        <span>{citation.title}</span>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </section>
        ) : null}
        {result.reasons.length > 0 ? (
          <div>
            <Button
              variant="ghost"
              aria-expanded={showReasons}
              aria-controls={`reasons-${result.id}`}
              onClick={() => setShowReasons((v) => !v)}
            >
              {showReasons ? 'Hide reasons' : `Show reasons (${result.reasons.length})`}
            </Button>
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
          <div
            className="mt-ol-s rounded-ol-s border border-ol-warn bg-ol-warn-wash px-ol-m py-ol-s"
            data-testid="result-warnings-block"
          >
            <Button
              variant="ghost"
              aria-expanded={showWarnings}
              aria-controls={`warnings-${result.id}`}
              onClick={() => setShowWarnings((v) => !v)}
            >
              {showWarnings ? 'Hide warnings' : `Show warnings (${result.warnings.length})`}
            </Button>
            {showWarnings ? (
              <ul id={`warnings-${result.id}`} data-testid="result-warnings">
                {result.warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}
        <p className="text-ol-s text-ol-ink-soft">
          <Button
            variant="ghost"
            data-testid="result-report-incorrect"
            aria-label={`Report incorrect classification for ${result.title}`}
            onClick={() =>
              (onReport ?? ((action, hit) => void sendFeedback(hit.id, action)))(
                'report_incorrect',
                result,
              )
            }
          >
            Report incorrect
          </Button>{' '}
          <Button
            variant="ghost"
            data-testid="result-report-suspicious"
            aria-label={`Report suspicious website for ${result.title}`}
            onClick={() =>
              (onReport ?? ((action, hit) => void sendFeedback(hit.id, action)))(
                'report_suspicious',
                result,
              )
            }
          >
            Report suspicious
          </Button>
        </p>
      </article>
    </Card>
  );
}
