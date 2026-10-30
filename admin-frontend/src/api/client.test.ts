import { describe, expect, it } from 'vitest';
import {
  fetchAdminConfig,
  fetchFrontierSettings,
  fetchFrontierStatus,
  fetchFrontierWorkers,
  frontierAdminHeaders,
  pauseFrontier,
  resumeFrontier,
  saveAdminConfig,
  saveFrontierSettings,
  SearchApiError,
} from './client.ts';
import { isAdminConfigDoc } from './guard.ts';
import type { FrontierSettings } from './types.ts';

type FetchImpl = typeof fetch;

const STATUS = {
  paused: false,
  queue: [],
  queue_depth: 0,
  checkpoint: { phase: 'running', pages_attempted: 1, pages_indexed: 1 },
  new_urls: [],
};

const SETTINGS: FrontierSettings = {
  max_pages: 100,
  max_queued_urls: 1000,
  crawl_delay_s: 0.5,
  default_max_depth: 3,
  respect_robots: true,
};

function jsonResponse(payload: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(payload),
  } as unknown as Response;
}

function captureFetch(
  payload: unknown,
  status = 200,
): {
  fetchImpl: FetchImpl;
  calls: Array<[unknown, RequestInit | undefined]>;
} {
  const calls: Array<[unknown, RequestInit | undefined]> = [];
  const fetchImpl = (async (url: unknown, init?: RequestInit) => {
    calls.push([url, init]);
    return jsonResponse(payload, status);
  }) as unknown as FetchImpl;
  return { fetchImpl, calls };
}

function headerOf(call: [unknown, RequestInit | undefined]): Record<string, string> {
  return ((call[1]?.headers ?? {}) as Record<string, string>) ?? {};
}

describe('frontierAdminHeaders', () => {
  it('sends no header when no token is configured (local-dev header-free)', () => {
    expect(frontierAdminHeaders('')).toEqual({});
    expect(frontierAdminHeaders('   ')).toEqual({});
  });

  it('sends X-Admin-Token once when a token is configured', () => {
    expect(frontierAdminHeaders('secret')).toEqual({ 'X-Admin-Token': 'secret' });
    expect(frontierAdminHeaders('  secret  ')).toEqual({ 'X-Admin-Token': 'secret' });
  });
});

describe('frontier mutations reuse the single X-Admin-Token gate', () => {
  it('pause sends no admin header by default', async () => {
    const { fetchImpl, calls } = captureFetch(STATUS);
    await pauseFrontier(fetchImpl);
    expect(headerOf(calls[0])).toEqual({});
  });

  it('pause sends X-Admin-Token when a token is configured', async () => {
    const { fetchImpl, calls } = captureFetch(STATUS);
    await pauseFrontier(fetchImpl, 'secret');
    expect(headerOf(calls[0])['X-Admin-Token']).toBe('secret');
  });

  it('resume posts seeds and the admin token', async () => {
    const { fetchImpl, calls } = captureFetch(STATUS);
    await resumeFrontier(['https://example.com'], fetchImpl, 'secret');
    expect(calls[0][0]).toContain('/api/v1/frontier/resume');
    expect(JSON.parse(calls[0][1]?.body as string)).toEqual({ seeds: ['https://example.com'] });
    expect(headerOf(calls[0])['X-Admin-Token']).toBe('secret');
  });

  it('save settings PUTs the document with the admin token', async () => {
    const { fetchImpl, calls } = captureFetch({ settings: SETTINGS });
    await saveFrontierSettings(SETTINGS, fetchImpl, 'secret');
    expect(calls[0][0]).toContain('/api/v1/frontier/settings');
    expect(headerOf(calls[0])['X-Admin-Token']).toBe('secret');
  });

  it('a 403 forbidden envelope surfaces as SearchApiError', async () => {
    const { fetchImpl } = captureFetch(
      { error: { code: 'forbidden', message: 'frontier mutation requires admin rights' } },
      403,
    );
    await expect(pauseFrontier(fetchImpl, 'wrong')).rejects.toMatchObject({ code: 'forbidden' });
  });
});

describe('frontier reads degrade instead of throwing', () => {
  it('status resolves to null on transport failure', async () => {
    const failing = (() => Promise.reject(new Error('down'))) as unknown as FetchImpl;
    await expect(fetchFrontierStatus(failing)).resolves.toBeNull();
  });

  it('settings resolve to null and workers to [] on shape failure', async () => {
    const { fetchImpl } = captureFetch({ bogus: true });
    await expect(fetchFrontierSettings(fetchImpl)).resolves.toBeNull();
    await expect(fetchFrontierWorkers(fetchImpl)).resolves.toEqual([]);
  });
});

describe('admin config documents (DB-backed, file fallback)', () => {
  const DOC = {
    doc: 'models',
    body: { primary: 'fake' },
    version: 0,
    updated_at: null,
    source: 'file' as const,
  };

  it('accepts a well-formed versioned document', () => {
    expect(isAdminConfigDoc(DOC)).toBe(true);
    expect(
      isAdminConfigDoc({ ...DOC, source: 'db', version: 2, updated_at: '2026-01-01T00:00:00Z' }),
    ).toBe(true);
    expect(isAdminConfigDoc({ ...DOC, source: 'yaml' })).toBe(false);
    expect(isAdminConfigDoc({ ...DOC, body: 42 })).toBe(false);
    expect(isAdminConfigDoc(null)).toBe(false);
  });

  it('GET resolves to null on 404 for unknown doc keys', async () => {
    const { fetchImpl } = captureFetch({ error: { code: 'not_found', message: 'nope' } }, 404);
    await expect(fetchAdminConfig('models', fetchImpl)).resolves.toBeNull();
  });

  it('GET returns the document once the route exists', async () => {
    const { fetchImpl } = captureFetch(DOC);
    await expect(fetchAdminConfig('ranking', fetchImpl)).resolves.toEqual(DOC);
  });

  it('PUT sends the admin token and returns the saved version', async () => {
    const saved = { ...DOC, source: 'db' as const, version: 2 };
    const { fetchImpl, calls } = captureFetch(saved);
    await expect(
      saveAdminConfig('models', { primary: 'fake' }, fetchImpl, 'secret'),
    ).resolves.toEqual(saved);
    expect(calls[0][0]).toContain('/api/v1/admin/config/models');
    expect(headerOf(calls[0])['X-Admin-Token']).toBe('secret');
    expect(JSON.parse(calls[0][1]?.body as string)).toEqual({ body: { primary: 'fake' } });
  });

  it('PUT surfaces backend validation failures as SearchApiError', async () => {
    const { fetchImpl } = captureFetch(
      { error: { code: 'invalid_request', message: 'unknown profile' } },
      422,
    );
    const error = await saveAdminConfig('ranking', {}, fetchImpl, 'secret').catch(
      (cause: unknown) => cause,
    );
    expect(error).toBeInstanceOf(SearchApiError);
    expect((error as SearchApiError).code).toBe('invalid_request');
  });
});
