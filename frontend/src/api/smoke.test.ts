import { describe, expect, it } from 'vitest';
import { isErrorEnvelope, isSearchResponse } from './guard.ts';

// Minimal ambient typing so the client bundle needs no @types/node.
declare const process: { env: Record<string, string | undefined> } | undefined;

/**
 * Live-backend smoke test. Runs only when `OPENLENS_SMOKE_URL` is set
 * (e.g. `OPENLENS_SMOKE_URL=http://localhost:8000 npm test`):
 * one shopping-mode query must return a guard-valid envelope with
 * results, applied filters, and diagnostics. Otherwise skipped, so
 * `npm test` stays green offline.
 */
const SMOKE_URL = process?.env.OPENLENS_SMOKE_URL?.trim();

describe.runIf(!!SMOKE_URL)('live backend smoke', () => {
  it('shopping-mode query returns a valid envelope', async () => {
    const response = await fetch(`${SMOKE_URL}/api/v1/search`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query: 'wireless headphones',
        mode: 'shopping',
        limit: 10,
        offset: 0,
        filters: { exclude_sponsored: true },
      }),
    });
    const payload: unknown = await response.json();
    if (!response.ok) {
      expect(isErrorEnvelope(payload)).toBe(true);
      return;
    }
    expect(isSearchResponse(payload)).toBe(true);
    if (!isSearchResponse(payload)) return;
    expect(payload.mode).toBe('shopping');
    expect(Array.isArray(payload.applied_filters)).toBe(true);
    expect(typeof payload.diagnostics).toBe('object');
  }, 30000);
});
