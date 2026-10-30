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
  it('maps the sponsored toggle onto the metadata flag plus a legacy term filter', () => {
    expect(buildFilters({ ...EMPTY_FILTER_STATE, excludeSponsored: true })).toEqual({
      exclude_sponsored: true,
      must_not_terms: ['sponsored'],
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
      preferSuffix: 'trust',
      blockedDomain: 'example.com',
      allowedDomain: 'trusted.example',
      mustNotTerm: 'advertisement',
    };
    const filters = buildFilters(state);
    expect(filters).toEqual({
      max_price: 250,
      location: 'Berlin',
      facilities: 'wifi, pool',
      prefer_trust: true,
      blocked_domains: ['example.com'],
      allowed_domains: ['trusted.example'],
      must_not_terms: ['advertisement'],
    });
    for (const key of Object.keys(filters)) {
      expect(isKnownFilterKey(key)).toBe(true);
    }
  });

  it('ignores blank inputs and non-numeric prices', () => {
    const filters = buildFilters({
      ...EMPTY_FILTER_STATE,
      maxPrice: 'not-a-number',
      location: '   ',
    });
    expect(filters).toEqual({});
  });

  it('merges the sponsored toggle with a custom must-not term', () => {
    const filters = buildFilters({
      ...EMPTY_FILTER_STATE,
      excludeSponsored: true,
      mustNotTerm: 'advertisement',
    });
    expect(filters).toEqual({
      exclude_sponsored: true,
      must_not_terms: ['sponsored', 'advertisement'],
    });
  });

  it('emits only known backend keys on a fully populated state', () => {
    const filters = buildFilters({
      ...EMPTY_FILTER_STATE,
      excludeSponsored: true,
      maxPrice: '250',
      location: 'Berlin',
      facilities: 'wifi, pool',
      preferSuffix: 'trust',
      blockedDomain: 'example.com',
      allowedDomain: 'trusted.example',
      mustNotTerm: 'advertisement',
    });
    expect(filters).toEqual({
      exclude_sponsored: true,
      max_price: 250,
      location: 'Berlin',
      facilities: 'wifi, pool',
      prefer_trust: true,
      blocked_domains: ['example.com'],
      allowed_domains: ['trusted.example'],
      must_not_terms: ['sponsored', 'advertisement'],
    });
    for (const key of Object.keys(filters)) {
      expect(isKnownFilterKey(key)).toBe(true);
    }
  });

  it('recognizes every owned and reserved backend key', () => {
    for (const key of [...OWNED_FILTER_KEYS, ...RESERVED_FILTER_KEYS, 'prefer_trust']) {
      expect(isKnownFilterKey(key)).toBe(true);
    }
    expect(isKnownFilterKey('exclude_sponsored')).toBe(true);
  });
});
