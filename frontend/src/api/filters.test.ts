import { describe, expect, it } from 'vitest';
import {
  EMPTY_FILTER_STATE,
  OWNED_FILTER_KEYS,
  RESERVED_FILTER_KEYS,
  buildFilters,
  isKnownFilterKey,
  type UserFilterState,
} from './filters.ts';

/**
 * Regression: the skeleton UI sent `exclude_sponsored`, a key the real
 * backend rejects (`plugin_error: Unknown filter key`). Every key the
 * filter mapper emits must be backend-owned (user-restrictions owned
 * keys) or a reserved consumer passthrough (`prefer_*`, `max_price`,
 * `min_rating`, `exclude_sponsored`, `location`, `facilities`, `brand`,
 * `style`, `size`, `occasion`).
 */
describe('user filter mapper', () => {
  it('maps the sponsored toggle onto the metadata flag alone', () => {
    expect(buildFilters({ ...EMPTY_FILTER_STATE, excludeSponsored: true })).toEqual({
      exclude_sponsored: true,
    });
  });

  it('emits an empty dict when nothing is set', () => {
    expect(buildFilters(EMPTY_FILTER_STATE)).toEqual({});
  });

  it('emits personalize:false only when the toggle is off', () => {
    expect(buildFilters({ ...EMPTY_FILTER_STATE, personalize: false })).toEqual({
      personalize: false,
    });
    expect(buildFilters({ ...EMPTY_FILTER_STATE, personalize: true })).toEqual({});
  });

  it('emits a known safety preset and drops unknown ones', () => {
    expect(buildFilters({ ...EMPTY_FILTER_STATE, safetyPreset: 'strict' })).toEqual({
      safety_preset: 'strict',
    });
    expect(buildFilters({ ...EMPTY_FILTER_STATE, safetyPreset: 'paranoid' })).toEqual({});
    expect(buildFilters(EMPTY_FILTER_STATE)).toEqual({});
  });

  it('maps the advanced panel onto owned and reserved keys only', () => {
    const state: UserFilterState = {
      ...EMPTY_FILTER_STATE,
      maxPrice: '250',
      location: 'Berlin',
      facilities: 'wifi, pool',
      preferDomains: 'example.com, trusted.example',
      preferTopics: 'science, history',
      blockedDomain: 'example.com',
      allowedDomain: 'trusted.example',
      mustNotTerm: 'advertisement',
    };
    const filters = buildFilters(state);
    expect(filters).toEqual({
      max_price: 250,
      location: 'Berlin',
      facilities: 'wifi, pool',
      prefer_domains: ['example.com', 'trusted.example'],
      prefer_topics: ['science', 'history'],
      blocked_domains: ['example.com'],
      allowed_domains: ['trusted.example'],
      must_not_terms: ['advertisement'],
    });
    for (const key of Object.keys(filters)) {
      expect(isKnownFilterKey(key)).toBe(true);
    }
  });

  it('emits prefer lists as string lists and drops blanks', () => {
    expect(
      buildFilters({
        ...EMPTY_FILTER_STATE,
        preferDomains: ' example.com ,, trusted.example ',
        preferTopics: '  ',
      }),
    ).toEqual({ prefer_domains: ['example.com', 'trusted.example'] });
  });

  it('maps sort presets onto weight_overrides with ranker weight names', () => {
    expect(buildFilters(EMPTY_FILTER_STATE)).toEqual({});
    expect(buildFilters({ ...EMPTY_FILTER_STATE, sortPreset: 'newest' })).toEqual({
      weight_overrides: { freshness_weight: 2.0 },
    });
    expect(buildFilters({ ...EMPTY_FILTER_STATE, sortPreset: 'most-trusted' })).toEqual({
      weight_overrides: { trust_weight: 2.0 },
    });
    expect(buildFilters({ ...EMPTY_FILTER_STATE, sortPreset: 'bogus' })).toEqual({});
  });

  it('emits site_category only for known categories', () => {
    expect(buildFilters({ ...EMPTY_FILTER_STATE, siteCategory: 'news' })).toEqual({
      site_category: 'news',
    });
    expect(buildFilters({ ...EMPTY_FILTER_STATE, siteCategory: 'movies' })).toEqual({});
  });

  it('ignores blank inputs and non-numeric prices', () => {
    const filters = buildFilters({
      ...EMPTY_FILTER_STATE,
      maxPrice: 'not-a-number',
      location: '   ',
    });
    expect(filters).toEqual({});
  });

  it('keeps the sponsored flag separate from a custom must-not term', () => {
    const filters = buildFilters({
      ...EMPTY_FILTER_STATE,
      excludeSponsored: true,
      mustNotTerm: 'advertisement',
    });
    expect(filters).toEqual({
      exclude_sponsored: true,
      must_not_terms: ['advertisement'],
    });
  });

  it('emits only known backend keys on a fully populated state', () => {
    const filters = buildFilters({
      ...EMPTY_FILTER_STATE,
      excludeSponsored: true,
      maxPrice: '250',
      location: 'Berlin',
      facilities: 'wifi, pool',
      preferDomains: 'example.com',
      preferTopics: 'science',
      sortPreset: 'newest',
      siteCategory: 'news',
      blockedDomain: 'example.com',
      allowedDomain: 'trusted.example',
      mustNotTerm: 'advertisement',
    });
    expect(filters).toEqual({
      exclude_sponsored: true,
      max_price: 250,
      location: 'Berlin',
      facilities: 'wifi, pool',
      prefer_domains: ['example.com'],
      prefer_topics: ['science'],
      weight_overrides: { freshness_weight: 2.0 },
      site_category: 'news',
      blocked_domains: ['example.com'],
      allowed_domains: ['trusted.example'],
      must_not_terms: ['advertisement'],
    });
    for (const key of Object.keys(filters)) {
      expect(isKnownFilterKey(key)).toBe(true);
    }
  });

  it('recognizes every owned and reserved backend key', () => {
    for (const key of [...OWNED_FILTER_KEYS, ...RESERVED_FILTER_KEYS, 'prefer_topics']) {
      expect(isKnownFilterKey(key)).toBe(true);
    }
    expect(isKnownFilterKey('exclude_sponsored')).toBe(true);
  });
});
