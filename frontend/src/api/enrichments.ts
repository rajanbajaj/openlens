/**
 * Defensive readers for backend enrichment metadata (T11–T14).
 *
 * `SearchResult.metadata` stays free-form on the wire; these helpers
 * extract the documented keys (`signals`, `story_id`, `also_from`,
 * commerce attributes, AI labels/digests) with graceful fallbacks so
 * the UI never crashes on absent or malformed optional keys.
 */

import type { AiCitation } from './types.ts';

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === 'string');
}

function asFiniteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** Display order for known scoring signals; unknown signals sort last. */
export const SIGNAL_ORDER: readonly string[] = [
  'relevance',
  'trust',
  'freshness',
  'preference',
  'diversity',
  'perspective',
  'commerce',
];

/** Finite-number entries of `metadata.signals`, display-ordered. */
export function resultSignals(metadata: Record<string, unknown>): Array<[string, number]> {
  const raw = metadata['signals'];
  if (!isRecord(raw)) return [];
  const entries: Array<[string, number]> = [];
  for (const [name, value] of Object.entries(raw)) {
    const score = asFiniteNumber(value);
    if (score !== null) entries.push([name, score]);
  }
  const rank = (name: string) => {
    const index = SIGNAL_ORDER.indexOf(name);
    return index === -1 ? SIGNAL_ORDER.length : index;
  };
  return entries.sort((a, b) => rank(a[0]) - rank(b[0]));
}

/** `metadata.story_id` (T12 news grouping) or null. */
export function resultStoryId(metadata: Record<string, unknown>): string | null {
  const storyId = metadata['story_id'];
  return typeof storyId === 'string' && storyId !== '' ? storyId : null;
}

/** `metadata.also_from`: co-providers that returned this hit (T13 fusion). */
export function resultAlsoFrom(metadata: Record<string, unknown>): string[] {
  return asStringArray(metadata['also_from']);
}

export interface CommerceAttributes {
  price: number | null;
  currency: string | null;
  location: string | null;
  facilities: string[];
  published: string | null;
}

/** Normalized commerce/article attributes (T12 attribute enricher). */
export function resultCommerce(metadata: Record<string, unknown>): CommerceAttributes {
  const rawFacilities = metadata['facilities'];
  const facilities =
    typeof rawFacilities === 'string' ? [rawFacilities] : asStringArray(rawFacilities);
  const stringOrNull = (value: unknown): string | null =>
    typeof value === 'string' && value !== '' ? value : null;
  return {
    price: asFiniteNumber(metadata['price']),
    currency: stringOrNull(metadata['currency']),
    location: stringOrNull(metadata['location']),
    facilities,
    published: stringOrNull(metadata['published']),
  };
}

/** `metadata.sponsored` crawler-truth flag (CAT6 label-sponsored). */
export function resultSponsored(metadata: Record<string, unknown>): boolean {
  return metadata['sponsored'] === true;
}

/**
 * Deterministic ranking confidence stamped by the backend explanation
 * stage (`metadata.confidence`, 0..1). Returns null when absent or
 * out of range so the UI degrades to the score-only layout.
 */
export function resultConfidence(metadata: Record<string, unknown>): number | null {
  const value = asFiniteNumber(metadata['confidence']);
  if (value === null || value < 0 || value > 1) return null;
  return value;
}

export interface ShoppingRating {
  rating: number | null;
  ratingCount: number | null;
}

/** Normalized rating attributes (CAT6 rating display/filter). */
export function resultRating(metadata: Record<string, unknown>): ShoppingRating {
  const rawCount = metadata['rating_count'];
  return {
    rating: asFiniteNumber(metadata['rating']),
    ratingCount:
      typeof rawCount === 'number' && Number.isFinite(rawCount) ? Math.trunc(rawCount) : null,
  };
}

export interface ShoppingDetails {
  brand: string | null;
  availability: string | null;
  bookingUrl: string | null;
  style: string | null;
  size: string | null;
  occasion: string | null;
}

/** Normalized shopping detail attributes (CAT6 attributes/booking/style). */
export function resultShoppingDetails(metadata: Record<string, unknown>): ShoppingDetails {
  const stringOrNull = (value: unknown): string | null =>
    typeof value === 'string' && value !== '' ? value : null;
  return {
    brand: stringOrNull(metadata['brand']),
    availability: stringOrNull(metadata['availability']),
    bookingUrl: stringOrNull(metadata['booking_url']),
    style: stringOrNull(metadata['style']),
    size: stringOrNull(metadata['size']),
    occasion: stringOrNull(metadata['occasion']),
  };
}

export interface BookAttributes {
  license: string | null;
  author: string | null;
  isbn: string | null;
  publisher: string | null;
}

/** Normalized book attributes (T8 license/author display and filtering). */
export function resultBook(metadata: Record<string, unknown>): BookAttributes {
  const stringOrNull = (value: unknown): string | null =>
    typeof value === 'string' && value !== '' ? value : null;
  return {
    license: stringOrNull(metadata['license']),
    author: stringOrNull(metadata['author']),
    isbn: stringOrNull(metadata['isbn']),
    publisher: stringOrNull(metadata['publisher']),
  };
}

export interface FoodAttributes {
  cuisine: string | null;
  ingredients: string[];
  dietaryTags: string[];
  allergens: string[];
}

/** Normalized recipe attributes (T9 cuisine/dietary/allergen display and filtering). */
export function resultFood(metadata: Record<string, unknown>): FoodAttributes {
  const stringOrNull = (value: unknown): string | null =>
    typeof value === 'string' && value !== '' ? value : null;
  const lowerList = (value: unknown): string[] => {
    if (typeof value === 'string') return value ? [value] : [];
    if (!Array.isArray(value)) return [];
    return value.filter((item): item is string => typeof item === 'string');
  };
  return {
    cuisine: stringOrNull(metadata['cuisine']),
    ingredients: lowerList(metadata['ingredients']),
    dietaryTags: lowerList(metadata['dietary_tags']),
    allergens: lowerList(metadata['allergens']),
  };
}

/** Advisory fact/opinion label (CAT4 opinion separation). */
export function resultOpinion(metadata: Record<string, unknown>): string | null {
  const opinion = metadata['opinion'];
  return typeof opinion === 'string' && opinion !== '' ? opinion : null;
}

/** Deterministic key-claim spans attached by the news reranker (CAT4). */
export function resultKeyClaims(metadata: Record<string, unknown>): string[] {
  return asStringArray(metadata['key_claims']);
}

export interface AiDigest {
  summary: string;
  perspectives: string[];
  model: string;
  citations: AiCitation[];
}

/**
 * `metadata.ai_citations` (T22): array of `{id, title, url}` source
 * records. Non-array values, non-record entries, and records missing a
 * usable id or title are dropped — never a crash. An empty or missing
 * url degrades to `url: null` (rendered as plain title text).
 */
export function resultAiCitations(metadata: Record<string, unknown>): AiCitation[] {
  const raw = metadata['ai_citations'];
  if (!Array.isArray(raw)) return [];
  const citations: AiCitation[] = [];
  for (const entry of raw) {
    if (!isRecord(entry)) continue;
    const { id, title, url } = entry;
    if (typeof id !== 'string' || id === '') continue;
    if (typeof title !== 'string' || title === '') continue;
    citations.push({
      id,
      title,
      url: typeof url === 'string' && url !== '' ? url : null,
    });
  }
  return citations;
}

/**
 * AI summary block (T14), labelled with its generating model. Null when
 * the backend produced no summary — AI chrome must not render then.
 */
export function resultAiDigest(metadata: Record<string, unknown>): AiDigest | null {
  const summary = metadata['ai_summary'];
  if (typeof summary !== 'string' || summary === '') return null;
  const model = metadata['ai_model'];
  return {
    summary,
    perspectives: asStringArray(metadata['ai_perspectives']),
    model: typeof model === 'string' && model !== '' ? model : 'unknown',
    citations: resultAiCitations(metadata),
  };
}

export interface AiUnderstanding {
  intent: string;
  entities: string[];
  model: string;
}

/** Rule-or-model understanding labels attached to each hit (T14). */
export function resultUnderstanding(metadata: Record<string, unknown>): AiUnderstanding | null {
  const intent = metadata['ai_intent'];
  if (typeof intent !== 'string' || intent === '') return null;
  const model = metadata['ai_model'];
  return {
    intent,
    entities: asStringArray(metadata['ai_entities']),
    model: typeof model === 'string' && model !== '' ? model : 'unknown',
  };
}

export interface PluginSummary {
  plugin: string;
  stage: string;
  status: string;
  latencyMs: number | null;
  error: string | null;
}

/** Human-readable projection of one opaque plugin-execution record. */
export function summarizePluginExecution(execution: Record<string, unknown>): PluginSummary {
  const text = (value: unknown): string =>
    typeof value === 'string' && value !== '' ? value : 'unknown';
  return {
    plugin: text(execution['plugin']),
    stage: text(execution['stage']),
    status: text(execution['status']),
    latencyMs: asFiniteNumber(execution['latency_ms']),
    error: typeof execution['error'] === 'string' ? execution['error'] : null,
  };
}
