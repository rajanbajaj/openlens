import { describe, expect, it, vi } from 'vitest';
import {
  clearFrontier,
  fetchAdminConfig,
  fetchAnalyticsCounts,
  fetchAnalyticsSeries,
  fetchAnalyticsTop,
  fetchFrontierSettings,
  fetchFrontierStatus,
  fetchFrontierUrlInfo,
  fetchFrontierWorkers,
  frontierAdminHeaders,
  frontierLiveUrl,
  pauseFrontier,
  enqueueSeeds,
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
  user_agent: 'TestBot/1.0 (+https://example.com)',
  skip_domains: [],
  allow_domains: [],
  deny_domains: [],
  discovery_feeds: [],
  refresh_after_days: 30.0,
  discovery_interval_s: 21600.0,
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

describe('frontierLiveUrl', () => {
  it('maps the API base to the live socket path', () => {
    expect(frontierLiveUrl()).toBe('ws://localhost:8000/api/v1/frontier/live');
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

  it('clear pauses, drops the queue, and reports counts', async () => {
    const cleared = { ...STATUS, cleared_queued: 3, kafka_purged: true };
    const { fetchImpl, calls } = captureFetch(cleared);
    const status = await clearFrontier(fetchImpl, 'secret');
    expect(calls[0][0]).toContain('/api/v1/frontier/clear');
    expect(headerOf(calls[0])['X-Admin-Token']).toBe('secret');
    expect(status.cleared_queued).toBe(3);
    expect(status.kafka_purged).toBe(true);
  });

  it('resume includes priority only when given', async () => {
    const { fetchImpl, calls } = captureFetch(STATUS);
    await resumeFrontier(['https://example.com'], fetchImpl, 'secret', 6);
    expect(JSON.parse(calls[0][1]?.body as string)).toEqual({
      seeds: ['https://example.com'],
      priority: 6,
    });
  });

  it('enqueueSeeds posts to the seeds endpoint with token and priority', async () => {
    const { fetchImpl, calls } = captureFetch({ enqueued: 3 });
    const accepted = await enqueueSeeds(['https://example.com'], fetchImpl, 'secret', 6);
    expect(calls[0][0]).toContain('/api/v1/frontier/seeds');
    expect(JSON.parse(calls[0][1]?.body as string)).toEqual({
      seeds: ['https://example.com'],
      priority: 6,
    });
    expect(headerOf(calls[0])['X-Admin-Token']).toBe('secret');
    expect(accepted.enqueued).toBe(3);
  });

  it('enqueueSeeds omits priority when blank', async () => {
    const { fetchImpl, calls } = captureFetch({ enqueued: 1 });
    await enqueueSeeds(['https://example.com/x'], fetchImpl, 'secret');
    expect(JSON.parse(calls[0][1]?.body as string)).toEqual({
      seeds: ['https://example.com/x'],
    });
  });

  it('fetchFrontierUrlInfo returns the crawl story for one URL', async () => {
    const payload = {
      url: 'https://example.com/a',
      normalized_url: 'https://example.com/a',
      frontier: { status: 'done', priority: 9, attempts: 0, last_error: null },
      document: { title: 'Example A', chars: 11 },
      extracted_links: 2,
      link_sample: ['https://example.com/b', 'https://example.com/c'],
    };
    const { fetchImpl, calls } = captureFetch(payload);
    const info = await fetchFrontierUrlInfo('https://example.com/a', fetchImpl);
    expect(String(calls[0][0])).toContain('/api/v1/frontier/url-info?url=');
    expect(info).toEqual(payload);
  });

  it('fetchAnalyticsTop passes paging, match, and range params', async () => {
    const seen: string[] = [];
    const impl = vi.fn().mockImplementation((url: unknown) => {
      seen.push(String(url));
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers(),
        text: () =>
          Promise.resolve(
            JSON.stringify({
              name: 'crawl.page',
              prop: 'domain',
              days: 30,
              total: 1,
              top: [{ value: 'example.com', count: 3 }],
            }),
          ),
        json: () =>
          Promise.resolve({
            name: 'crawl.page',
            prop: 'domain',
            days: 30,
            total: 1,
            top: [{ value: 'example.com', count: 3 }],
          }),
      });
    });
    vi.stubGlobal('fetch', impl);
    try {
      const top = await fetchAnalyticsTop(
        'crawl.page',
        'domain',
        30,
        10,
        '',
        undefined,
        undefined,
        20,
        'example',
        '2026-10-01',
        '2026-10-10',
      );
      expect(top?.top).toEqual([{ value: 'example.com', count: 3 }]);
    } finally {
      vi.unstubAllGlobals();
    }
    const url = seen.find((candidate) => candidate.includes('/api/v1/admin/analytics/top')) ?? '';
    expect(url).toContain('offset=20');
    expect(url).toContain('match=example');
    expect(url).toContain('start=2026-10-01');
    expect(url).toContain('end=2026-10-10');
  });

  it('fetchAnalyticsCounts and fetchAnalyticsSeries pass range params', async () => {
    const seen: string[] = [];
    const impl = vi.fn().mockImplementation((url: unknown) => {
      seen.push(String(url));
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers(),
        text: () => Promise.resolve('{}'),
        json: () => Promise.resolve({}),
      });
    });
    vi.stubGlobal('fetch', impl);
    try {
      await fetchAnalyticsCounts(30, '', '2026-10-01', '2026-10-10');
      await fetchAnalyticsSeries('crawl.page', 'day', 30, '', '2026-10-01', '2026-10-10');
    } finally {
      vi.unstubAllGlobals();
    }
    expect(seen.some((url) => url.includes('start=2026-10-01'))).toBe(true);
    expect(seen.some((url) => url.includes('end=2026-10-10'))).toBe(true);
  });

  it('fetchFrontierUrlInfo degrades blank, failed, and malformed reads to null', async () => {
    const { fetchImpl } = captureFetch(null);
    await expect(fetchFrontierUrlInfo('   ', fetchImpl)).resolves.toBeNull();
    const failing: FetchImpl = () => Promise.reject(new Error('down'));
    await expect(fetchFrontierUrlInfo('https://example.com/a', failing)).resolves.toBeNull();
    const { fetchImpl: malformed } = captureFetch({ nope: true });
    await expect(fetchFrontierUrlInfo('https://example.com/a', malformed)).resolves.toBeNull();
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
