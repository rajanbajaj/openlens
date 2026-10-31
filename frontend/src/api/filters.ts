/**
 * User filter schema mirroring the backend T10 filter contract
 * (`user-restrictions` owned keys plus the reserved consumer
 * passthrough). The UI never invents wire keys: `buildFilters` emits
 * only keys the backend accepts, so a search cannot 500 with
 * `Unknown filter key`.
 */

/** Exact keys owned by `user-restrictions` (plus `allowed_domains` alias). */
export const OWNED_FILTER_KEYS: readonly string[] = [
  'allow_domains',
  'allowed_domains',
  'blocked_domains',
  'disallowed_domains',
  'exclude_domains',
  'exclude_terms',
  'include_domains',
  'must',
  'must_not',
  'must_terms',
  'must_not_terms',
];

/** Reserved consumer keys passed through untouched by the backend. */
export const RESERVED_FILTER_KEYS: readonly string[] = [
  'max_price',
  'location',
  'facilities',
  'min_rating',
  'exclude_sponsored',
  'exclude_over_budget',
  'brand',
  'style',
  'size',
  'occasion',
  'license',
  'author',
  'cuisine',
  'dietary_tags',
  'ingredients',
  'exclude_allergens',
  'personalize',
  'weight_overrides',
  'safety_preset',
  'site_category',
];

/** Named safety policies selectable via the `safety_preset` filter. */
export const SAFETY_PRESETS: readonly string[] = ['standard', 'balanced', 'strict'];

/**
 * Named site categories selectable via the `site_category` filter.
 * Must stay a subset of the backend-accepted category vocabulary.
 */
export const SITE_CATEGORIES: readonly string[] = ['shopping', 'news', 'kids', 'other'];

/**
 * Sort presets offered by the sort select. Each non-default preset maps
 * to a `weight_overrides` dict keyed by an existing ranker weight field
 * (see `RankingProfile` in `src/openlens/ranking/profiles.py`), so the
 * backend ranker accepts it instead of failing closed.
 */
export const SORT_PRESETS: Record<string, Record<string, number>> = {
  newest: { freshness_weight: 2.0 },
  'most-trusted': { trust_weight: 2.0 },
};

/** Prefix reserved for scoring-preference keys (`prefer_domains`, `prefer_topics`). */
export const PREFER_PREFIX = 'prefer_';

/** Raw panel state: strings straight from the inputs, parsed by {@link buildFilters}. */
export interface UserFilterState {
  excludeSponsored: boolean;
  excludeOverBudget: boolean;
  maxPrice: string;
  minRating: string;
  location: string;
  facilities: string;
  brand: string;
  style: string;
  size: string;
  occasion: string;
  license: string;
  author: string;
  cuisine: string;
  dietaryTags: string;
  ingredients: string;
  excludeAllergens: string;
  /** Comma-separated preferred domains; blank means unset. */
  preferDomains: string;
  /** Comma-separated preferred topics; blank means unset. */
  preferTopics: string;
  /** Sort preset (`''` is Best match); non-default emits `weight_overrides`. */
  sortPreset: string;
  /** Named site category; blank means the backend default. */
  siteCategory: string;
  blockedDomain: string;
  allowedDomain: string;
  mustNotTerm: string;
  /** When false, preference boosts are switched off (`personalize: false`). */
  personalize: boolean;
  /** Named safety policy; blank means the backend default. */
  safetyPreset: string;
}

export const EMPTY_FILTER_STATE: UserFilterState = {
  excludeSponsored: false,
  excludeOverBudget: false,
  maxPrice: '',
  minRating: '',
  location: '',
  facilities: '',
  brand: '',
  style: '',
  size: '',
  occasion: '',
  license: '',
  author: '',
  cuisine: '',
  dietaryTags: '',
  ingredients: '',
  excludeAllergens: '',
  preferDomains: '',
  preferTopics: '',
  sortPreset: '',
  siteCategory: '',
  blockedDomain: '',
  allowedDomain: '',
  mustNotTerm: '',
  personalize: true,
  safetyPreset: '',
};

/** True when `key` is backend-owned, reserved, or a `prefer_*` key. */
export function isKnownFilterKey(key: string): boolean {
  return (
    (OWNED_FILTER_KEYS as readonly string[]).includes(key) ||
    (RESERVED_FILTER_KEYS as readonly string[]).includes(key) ||
    key.startsWith(PREFER_PREFIX)
  );
}

/**
 * Split a comma-separated input into a clean string list, dropping
 * blanks and duplicates while preserving first-seen order.
 */
export function parseStringList(raw: string): string[] {
  const seen = new Set<string>();
  for (const part of raw.split(',')) {
    const entry = part.trim();
    if (entry && !seen.has(entry)) seen.add(entry);
  }
  return [...seen];
}

/**
 * Shape raw panel state into the request `filters` dict. Blank inputs
 * and non-numeric prices are dropped; every emitted key satisfies
 * {@link isKnownFilterKey}.
 */
export function buildFilters(state: UserFilterState): Record<string, unknown> {
  const filters: Record<string, unknown> = {};
  // The backend hard-excludes crawler-truth sponsored hits via the
  // flag alone — no legacy `must_not_terms` fallback is sent.
  if (state.excludeSponsored) {
    filters['exclude_sponsored'] = true;
  }
  const extraTerm = state.mustNotTerm.trim();
  if (extraTerm) filters['must_not_terms'] = [extraTerm];

  const maxPrice = Number(state.maxPrice.trim());
  if (state.maxPrice.trim() !== '' && Number.isFinite(maxPrice)) {
    filters['max_price'] = maxPrice;
  }
  const minRating = Number(state.minRating.trim());
  if (state.minRating.trim() !== '' && Number.isFinite(minRating)) {
    filters['min_rating'] = minRating;
  }
  const location = state.location.trim();
  if (location) filters['location'] = location;
  const facilities = state.facilities.trim();
  if (facilities) filters['facilities'] = facilities;
  const brand = state.brand.trim();
  if (brand) filters['brand'] = brand;
  const style = state.style.trim();
  if (style) filters['style'] = style;
  const size = state.size.trim();
  if (size) filters['size'] = size;
  if (state.excludeOverBudget) filters['exclude_over_budget'] = true;
  const occasion = state.occasion.trim();
  if (occasion) filters['occasion'] = occasion;
  const license = state.license.trim();
  if (license) filters['license'] = license;
  const author = state.author.trim();
  if (author) filters['author'] = author;
  const cuisine = state.cuisine.trim();
  if (cuisine) filters['cuisine'] = cuisine;
  const dietaryTags = state.dietaryTags.trim();
  if (dietaryTags) filters['dietary_tags'] = dietaryTags;
  const ingredients = state.ingredients.trim();
  if (ingredients) filters['ingredients'] = ingredients;
  const excludeAllergens = state.excludeAllergens.trim();
  if (excludeAllergens) filters['exclude_allergens'] = excludeAllergens;
  const preferDomains = parseStringList(state.preferDomains);
  if (preferDomains.length > 0) filters['prefer_domains'] = preferDomains;
  const preferTopics = parseStringList(state.preferTopics);
  if (preferTopics.length > 0) filters['prefer_topics'] = preferTopics;
  const sortPreset = state.sortPreset.trim().toLowerCase();
  if (sortPreset && sortPreset in SORT_PRESETS) {
    filters['weight_overrides'] = { ...SORT_PRESETS[sortPreset] };
  }
  const siteCategory = state.siteCategory.trim().toLowerCase();
  if (siteCategory && (SITE_CATEGORIES as readonly string[]).includes(siteCategory)) {
    filters['site_category'] = siteCategory;
  }

  const blockedDomain = state.blockedDomain.trim();
  if (blockedDomain) filters['blocked_domains'] = [blockedDomain];
  const allowedDomain = state.allowedDomain.trim();
  if (allowedDomain) filters['allowed_domains'] = [allowedDomain];
  if (state.personalize === false) filters['personalize'] = false;
  const safetyPreset = state.safetyPreset.trim().toLowerCase();
  if (safetyPreset && (SAFETY_PRESETS as readonly string[]).includes(safetyPreset)) {
    filters['safety_preset'] = safetyPreset;
  }
  return filters;
}
