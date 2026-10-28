import { describe, expect, it, vi } from 'vitest';
import {
  debounce,
  fetchSuggestions,
  prefetchPage,
  sendFeedback,
  SUGGEST_DEBOUNCE_MS,
} from './client.ts';
import {
  isFeedbackResponse,
  isSearchResponse,
  isSuggestResponse,
  readNextOffset,
  readRelated,
  readSpelling,
  readSuggestions,
} from './guard.ts';
import type { SearchRequest } from './types.ts';

type FetchImpl = typeof fetch;

function jsonResponse(payload: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(payload),
  } as unknown as Response;
}

/** Minimal guard-valid search envelope without any assistance fields. */
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

describe('suggest response guard', () => {
  it('accepts the backend SuggestResponseSchema shape', () => {
    const payload: unknown = { q: 'wire', suggestions: ['wireless headphones'] };
    expect(isSuggestResponse(payload)).toBe(true);
    if (!isSuggestResponse(payload)) return;
    expect(payload.q).toBe('wire');
    expect(payload.suggestions).toEqual(['wireless headphones']);
  });

  it('rejects malformed suggest payloads', () => {
    expect(isSuggestResponse(null)).toBe(false);
    expect(isSuggestResponse({ q: 'wire' })).toBe(false);
    expect(isSuggestResponse({ suggestions: ['a'] })).toBe(false);
    expect(isSuggestResponse({ q: 'wire', suggestions: 'nope' })).toBe(false);
    expect(isSuggestResponse({ q: 'wire', suggestions: ['ok', 42] })).toBe(false);
  });
});

describe('fetchSuggestions with mocked fetch', () => {
  it('returns completions and queries the suggest endpoint', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ q: 'wire', suggestions: ['wireless headphones'] }),
    ) as unknown as FetchImpl;
    await expect(fetchSuggestions('wire', {}, fetchImpl)).resolves.toEqual(['wireless headphones']);
    expect(fetchImpl).toHaveBeenCalledOnce();
    const url = String(vi.mocked(fetchImpl).mock.calls[0]?.[0] ?? '');
    expect(url).toContain('/api/v1/suggest');
    expect(url).toContain('q=wire');
  });

  it('skips the fetch for blank queries', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({})) as unknown as FetchImpl;
    await expect(fetchSuggestions('   ', {}, fetchImpl)).resolves.toEqual([]);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it('degrades backend-absent (404) to an empty list', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({}, 404)) as unknown as FetchImpl;
    await expect(fetchSuggestions('wire', {}, fetchImpl)).resolves.toEqual([]);
  });

  it('degrades malformed assistance payloads to an empty list', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ q: 'wire', suggestions: ['ok', 42] }),
    ) as unknown as FetchImpl;
    await expect(fetchSuggestions('wire', {}, fetchImpl)).resolves.toEqual([]);
  });

  it('degrades transport failures and aborts to an empty list', async () => {
    const failing = vi.fn(async () => {
      throw new Error('boom');
    }) as unknown as FetchImpl;
    await expect(fetchSuggestions('wire', {}, failing)).resolves.toEqual([]);
    const controller = new AbortController();
    controller.abort();
    const aborting = vi.fn(async () => {
      throw new DOMException('aborted', 'AbortError');
    }) as unknown as FetchImpl;
    await expect(
      fetchSuggestions('wire', { signal: controller.signal }, aborting),
    ).resolves.toEqual([]);
  });

  it('exposes the plan debounce delay', () => {
    expect(SUGGEST_DEBOUNCE_MS).toBe(150);
  });

  it('debounces rapid calls and supports caller cancellation', async () => {
    vi.useFakeTimers();
    try {
      const fn = vi.fn();
      const debounced = debounce(fn, SUGGEST_DEBOUNCE_MS);
      debounced('a');
      debounced('b');
      expect(fn).not.toHaveBeenCalled();
      vi.advanceTimersByTime(SUGGEST_DEBOUNCE_MS);
      expect(fn).toHaveBeenCalledOnce();
      expect(fn).toHaveBeenCalledWith('b');
      debounced('c');
      debounced.cancel();
      vi.advanceTimersByTime(SUGGEST_DEBOUNCE_MS);
      expect(fn).toHaveBeenCalledOnce();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('assistance readers through guard validation', () => {
  it('reads suggestions, spelling, related, and next_offset when present', () => {
    const envelope: unknown = {
      ...baseSearchEnvelope(),
      limit: 10,
      offset: 0,
      next_offset: 10,
      suggestions: ['wireless headphones', 'wireless charger'],
      spelling: 'wireless headphones',
      related: ['running shoes'],
    };
    expect(isSearchResponse(envelope)).toBe(true);
    expect(readSuggestions(envelope)).toEqual(['wireless headphones', 'wireless charger']);
    expect(readSpelling(envelope)).toBe('wireless headphones');
    expect(readRelated(envelope)).toEqual(['running shoes']);
    expect(readNextOffset(envelope)).toBe(10);
  });

  it('renders malformed assistance as absent without failing the page guard', () => {
    const envelope: unknown = {
      ...baseSearchEnvelope(),
      suggestions: ['ok', 42],
      spelling: 42,
      related: 'nope',
      next_offset: -1,
    };
    // The page still parses: assistance degrades, it never 500s the UI.
    expect(isSearchResponse(envelope)).toBe(true);
    expect(readSuggestions(envelope)).toEqual([]);
    expect(readSpelling(envelope)).toBeNull();
    expect(readRelated(envelope)).toEqual([]);
    expect(readNextOffset(envelope)).toBeNull();
  });

  it('renders backend-absent assistance as absent', () => {
    const envelope: unknown = baseSearchEnvelope();
    expect(isSearchResponse(envelope)).toBe(true);
    expect(readSuggestions(envelope)).toEqual([]);
    expect(readSpelling(envelope)).toBeNull();
    expect(readRelated(envelope)).toEqual([]);
    expect(readNextOffset(envelope)).toBeNull();
    expect(readSuggestions(null)).toEqual([]);
    expect(readSpelling(undefined)).toBeNull();
    expect(readRelated(' assistance ')).toEqual([]);
    expect(readNextOffset(null)).toBeNull();
  });
});

describe('sendFeedback fire-and-forget', () => {
  it('posts only the anonymous doc id + action pair', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ doc_id: 'h1', action: 'save', count: 1 }),
    ) as unknown as FetchImpl;
    await expect(sendFeedback('h1', 'save', fetchImpl)).resolves.toBeUndefined();
    expect(fetchImpl).toHaveBeenCalledOnce();
    const [url, init] = vi.mocked(fetchImpl).mock.calls[0] ?? [];
    expect(String(url)).toContain('/api/v1/feedback');
    const sent: unknown = JSON.parse(String((init as RequestInit)?.body ?? '{}'));
    expect(sent).toEqual({ doc_id: 'h1', action: 'save' });
    expect(isFeedbackResponse({ doc_id: 'h1', action: 'save', count: 1 })).toBe(true);
    expect(isFeedbackResponse({ doc_id: 'h1' })).toBe(false);
  });

  it('never rejects into rendering, even on transport failure', async () => {
    const failing = vi.fn(async () => {
      throw new Error('offline');
    }) as unknown as FetchImpl;
    await expect(sendFeedback('h1', 'save', failing)).resolves.toBeUndefined();
    const serverError = vi.fn(async () =>
      jsonResponse({ error: { code: 'x', message: 'y' } }, 500),
    ) as unknown as FetchImpl;
    await expect(sendFeedback('h1', 'save', serverError)).resolves.toBeUndefined();
  });

  it('skips blank doc ids or actions without fetching', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({})) as unknown as FetchImpl;
    await sendFeedback('  ', 'save', fetchImpl);
    await sendFeedback('h1', '  ', fetchImpl);
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

describe('prefetchPage via existing offset math', () => {
  const body: SearchRequest = {
    query: 'wireless headphones',
    mode: 'default',
    limit: 10,
    offset: 0,
    filters: {},
  };

  it('requests the offset for the target page', async () => {
    let seenOffset: unknown;
    const fetchImpl = (async (_url: string | URL | Request, init?: RequestInit) => {
      seenOffset = (JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>)['offset'];
      return jsonResponse({ ...baseSearchEnvelope(), offset: seenOffset });
    }) as unknown as FetchImpl;
    const result = await prefetchPage(body, 3, fetchImpl);
    expect(seenOffset).toBe(20);
    expect(result?.offset).toBe(20);
  });

  it('resolves null when the prefetch fails', async () => {
    const failing = vi.fn(async () => {
      throw new Error('offline');
    }) as unknown as FetchImpl;
    await expect(prefetchPage(body, 2, failing)).resolves.toBeNull();
  });
});
