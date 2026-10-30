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
];

/** Named safety policies selectable via the `safety_preset` filter. */
export const SAFETY_PRESETS: readonly string[] = ['standard', 'balanced', 'strict'];

/** Prefix reserved for scoring-preference keys (`prefer_<suffix>`). */
export const PREFER_PREFIX = 'prefer_';

/** Term excluded by the "exclude sponsored" toggle (backend-owned term filter). */
export const SPONSORED_TERM = 'sponsored';

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
  /** Suffix for `prefer_<suffix>: true`; blank means unset. */
  preferSuffix: string;
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
  preferSuffix: '',
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
 * Shape raw panel state into the request `filters` dict. Blank inputs
 * and non-numeric prices are dropped; every emitted key satisfies
 * {@link isKnownFilterKey}.
 */
export function buildFilters(state: UserFilterState): Record<string, unknown> {
  const filters: Record<string, unknown> = {};
  const mustNotTerms: string[] = [];
  // Backend hard-excludes crawler-truth sponsored hits via the flag;
  // the legacy text term stays for older backends.
  if (state.excludeSponsored) {
    filters['exclude_sponsored'] = true;
    mustNotTerms.push(SPONSORED_TERM);
  }
  const extraTerm = state.mustNotTerm.trim();
  if (extraTerm) mustNotTerms.push(extraTerm);
  if (mustNotTerms.length > 0) filters['must_not_terms'] = mustNotTerms;

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
  const preferSuffix = state.preferSuffix
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, '_')
    .replace(/^_+|_+$/g, '');
  if (preferSuffix) filters[`${PREFER_PREFIX}${preferSuffix}`] = true;

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
