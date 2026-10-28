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
export const RESERVED_FILTER_KEYS: readonly string[] = ['max_price', 'location', 'facilities'];

/** Prefix reserved for scoring-preference keys (`prefer_<suffix>`). */
export const PREFER_PREFIX = 'prefer_';

/** Term excluded by the "exclude sponsored" toggle (backend-owned term filter). */
export const SPONSORED_TERM = 'sponsored';

/** Raw panel state: strings straight from the inputs, parsed by {@link buildFilters}. */
export interface UserFilterState {
  excludeSponsored: boolean;
  maxPrice: string;
  location: string;
  facilities: string;
  /** Suffix for `prefer_<suffix>: true`; blank means unset. */
  preferSuffix: string;
  blockedDomain: string;
  allowedDomain: string;
  mustNotTerm: string;
}

export const EMPTY_FILTER_STATE: UserFilterState = {
  excludeSponsored: false,
  maxPrice: '',
  location: '',
  facilities: '',
  preferSuffix: '',
  blockedDomain: '',
  allowedDomain: '',
  mustNotTerm: '',
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
  if (state.excludeSponsored) mustNotTerms.push(SPONSORED_TERM);
  const extraTerm = state.mustNotTerm.trim();
  if (extraTerm) mustNotTerms.push(extraTerm);
  if (mustNotTerms.length > 0) filters['must_not_terms'] = mustNotTerms;

  const maxPrice = Number(state.maxPrice.trim());
  if (state.maxPrice.trim() !== '' && Number.isFinite(maxPrice)) {
    filters['max_price'] = maxPrice;
  }
  const location = state.location.trim();
  if (location) filters['location'] = location;
  const facilities = state.facilities.trim();
  if (facilities) filters['facilities'] = facilities;
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
  return filters;
}
