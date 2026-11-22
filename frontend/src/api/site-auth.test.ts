import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  SITE_AUTH_HEADER,
  SITE_UNAUTHORIZED_CODE,
  SITE_UNAUTHORIZED_EVENT,
  SearchApiError,
  buildSearchBody,
  clearSiteToken,
  fetchSiteStatus,
  fetchSuggestions,
  getSiteToken,
  searchLens,
  setSiteToken,
  siteAuthHeaders,
} from './client.ts';

type FetchImpl = typeof fetch;

function jsonResponse(payload: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(payload),
  } as unknown as Response;
}

/** Minimal guard-valid search envelope. */
function baseSearchEnvelope(): Record<string, unknown> {
  return {
    query: 'wireless headphones',
    mode: 'default',
    results: [],
    total: 0,
    applied_filters: [],
    diagnostics: {
      fallback_used: false,
      provider_latencies_ms: {},
      plugin_executions: [],
      errors: [],
    },
  };
}

beforeEach(() => {
  window.sessionStorage.clear();
});

describe('site token storage', () => {
  it('round-trips through sessionStorage, never localStorage', () => {
    setSiteToken('s3cret');
    expect(getSiteToken()).toBe('s3cret');
    expect(window.localStorage.getItem('openlens.site_token')).toBeNull();
    clearSiteToken();
    expect(getSiteToken()).toBe('');
  });

  it('emits the auth header only when unlocked', () => {
    expect(siteAuthHeaders()).toEqual({});
    setSiteToken('s3cret');
    expect(siteAuthHeaders()).toEqual({ [SITE_AUTH_HEADER]: 's3cret' });
  });
});

describe('fetchSiteStatus', () => {
  it('parses the gate state and sends the stored token', async () => {
    setSiteToken('s3cret');
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ auth_required: true, authorized: true }),
    ) as unknown as FetchImpl;
    await expect(fetchSiteStatus(fetchImpl)).resolves.toEqual({
      auth_required: true,
      authorized: true,
    });
    const [, init] = vi.mocked(fetchImpl).mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)[SITE_AUTH_HEADER]).toBe('s3cret');
  });

  it('fails open on network errors, legacy servers, and malformed payloads', async () => {
    const failing = vi.fn(async () => {
      throw new Error('down');
    }) as unknown as FetchImpl;
    await expect(fetchSiteStatus(failing)).resolves.toEqual({
      auth_required: false,
      authorized: true,
    });
    const legacy = vi.fn(async () => jsonResponse({ nope: 1 }, 404)) as unknown as FetchImpl;
    await expect(fetchSiteStatus(legacy)).resolves.toEqual({
      auth_required: false,
      authorized: true,
    });
    const malformed = vi.fn(async () =>
      jsonResponse({ auth_required: 'yes' }),
    ) as unknown as FetchImpl;
    await expect(fetchSiteStatus(malformed)).resolves.toEqual({
      auth_required: false,
      authorized: true,
    });
  });
});

describe('gated query calls', () => {
  it('searchLens attaches the site header', async () => {
    setSiteToken('s3cret');
    const fetchImpl = vi.fn(async () => jsonResponse(baseSearchEnvelope())) as unknown as FetchImpl;
    await searchLens(buildSearchBody({ query: 'wire' }), fetchImpl);
    const [, init] = vi.mocked(fetchImpl).mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)[SITE_AUTH_HEADER]).toBe('s3cret');
  });

  it('searchLens throws unauthorized and notifies the shell on 401', async () => {
    const events: string[] = [];
    const listener = () => events.push(SITE_UNAUTHORIZED_EVENT);
    window.addEventListener(SITE_UNAUTHORIZED_EVENT, listener);
    try {
      const fetchImpl = vi.fn(async () =>
        jsonResponse({ error: { code: 'unauthorized', message: 'site login required' } }, 401),
      ) as unknown as FetchImpl;
      const caught: unknown = await searchLens(buildSearchBody({ query: 'wire' }), fetchImpl).catch(
        (error: unknown) => error,
      );
      expect(caught).toBeInstanceOf(SearchApiError);
      expect((caught as SearchApiError).code).toBe(SITE_UNAUTHORIZED_CODE);
      expect(events).toEqual([SITE_UNAUTHORIZED_EVENT]);
    } finally {
      window.removeEventListener(SITE_UNAUTHORIZED_EVENT, listener);
    }
  });

  it('fetchSuggestions sends the site header', async () => {
    setSiteToken('s3cret');
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ q: 'wire', suggestions: [] }),
    ) as unknown as FetchImpl;
    await fetchSuggestions('wire', {}, fetchImpl);
    const [, init] = vi.mocked(fetchImpl).mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)[SITE_AUTH_HEADER]).toBe('s3cret');
  });
});
