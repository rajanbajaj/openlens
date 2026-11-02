/**
 * Typed fetch wrapper for `POST {API_URL}/api/v1/search`.
 *
 * The API base URL comes only from `VITE_OPENLENS_API_URL`
 * (dev default `http://localhost:8000`). No hardcoded hosts,
 * no secrets or API keys anywhere in this module.
 */

import {
  isAdminConfigDoc,
  isAnalyticsCounts,
  isAnalyticsSeries,
  isAnalyticsTop,
  isCrawlRequest,
  isCrawlRequestList,
  isErrorEnvelope,
  isFeedbackSummary,
  isFrontierDomainsResult,
  isFrontierFailedDomainList,
  isFrontierFailedList,
  isFrontierQueuedDomainList,
  isFrontierSettingsResponse,
  isFrontierStatus,
  isFrontierWorkersResponse,
  isSearchResponse,
  isSuggestResponse,
} from './guard.ts';
import {
  adminAnalyticsCountsApiV1AdminAnalyticsCountsGet,
  adminAnalyticsSeriesApiV1AdminAnalyticsSeriesGet,
  adminAnalyticsTopApiV1AdminAnalyticsTopGet,
  adminFeedbackSummaryApiV1AdminFeedbackSummaryGet,
  frontierApproveCrawlRequestApiV1FrontierCrawlRequestsRequestIdApprovePost,
  frontierDeclineCrawlRequestApiV1FrontierCrawlRequestsRequestIdDeclinePost,
  frontierDomainsPriorityApiV1FrontierDomainsPriorityPost,
  frontierDomainsPurgeApiV1FrontierDomainsPurgePost,
  frontierDomainsRequeueApiV1FrontierDomainsRequeuePost,
  frontierDomainsSkipApiV1FrontierDomainsSkipPost,
  frontierFailedDomainUrlsApiV1FrontierFailedDomainsDomainUrlsGet,
  frontierQueuedDomainsApiV1FrontierQueuedDomainsGet,
  frontierFailedApiV1FrontierFailedGet,
  frontierFailedDomainsApiV1FrontierFailedDomainsGet,
  frontierListCrawlRequestsApiV1FrontierCrawlRequestsGet,
} from './generated/openlens.ts';
import type {
  AdminConfigDoc,
  AdminConfigName,
  AnalyticsCounts,
  AnalyticsSeries,
  AnalyticsTop,
  CrawlRequest,
  CrawlRequestList,
  FeedbackRequest,
  FeedbackSummary,
  FrontierDomainsResult,
  FrontierFailedDomainList,
  FrontierFailedList,
  FrontierQueuedDomainList,
  FrontierSettings,
  FrontierSettingsResponse,
  FrontierStatus,
  FrontierWorker,
  ProviderPrefs,
  SearchRequest,
  SearchResponse,
} from './types.ts';

/**
 * Dev fallback when `VITE_OPENLENS_API_URL` is unset and the app runs
 * under `vite dev` (cross-origin by design). In a built bundle an unset
 * URL means same-origin (`''`), so requests go through the nginx `/api/`
 * proxy and never trigger a CORS preflight.
 */
export const DEFAULT_API_BASE_URL = 'http://localhost:8000';

/** Resolved API base URL (trailing slashes stripped). */
export const API_BASE_URL: string =
  (import.meta.env.VITE_OPENLENS_API_URL as string | undefined)?.trim().replace(/\/+$/, '') ||
  (import.meta.env.DEV ? DEFAULT_API_BASE_URL : '');

/** Backend default page size (`DEFAULT_LIMIT` in `schemas.py`). */
export const DEFAULT_LIMIT = 10;

/**
 * WebSocket URL for the live frontier stream
 * (`/api/v1/frontier/live` pushes the same payload as the status
 * endpoint). Same host/base as the API:
 * `http(s)` becomes `ws(s)`, and an empty base resolves against the
 * page origin (built bundle behind the nginx `/api/` proxy).
 */
export function frontierLiveUrl(): string {
  const base = API_BASE_URL || `${window.location.protocol}//${window.location.host}`;
  return `${base.replace(/^http/, 'ws')}/api/v1/frontier/live`;
}

/** Backend query ceiling (`MAX_QUERY_LENGTH` in `schemas.py`). */
export const MAX_QUERY_LENGTH = 1000;

/** Options accepted by {@link buildSearchBody}. */
export interface SearchOptions {
  query: string;
  mode?: string;
  limit?: number;
  offset?: number;
  filters?: Record<string, unknown>;
  providerPrefs?: ProviderPrefs | null;
  history?: string[];
}

/**
 * Validate a raw query string against the backend contract:
 * non-blank, 1–1000 chars. Returns an error message, or `null`
 * when the query is valid.
 */
export function validateQuery(query: string): string | null {
  if (!query.trim()) return 'Query must not be blank.';
  if (query.length > MAX_QUERY_LENGTH) {
    return `Query must be at most ${MAX_QUERY_LENGTH} characters.`;
  }
  return null;
}

/**
 * Shape a validated request body for `POST /api/v1/search`.
 * Applies backend defaults (`mode: "default"`, `limit: 10`,
 * `offset: 0`, `filters: {}`) for omitted fields.
 */
export function buildSearchBody(options: SearchOptions): SearchRequest {
  const body: SearchRequest = {
    query: options.query,
    mode: options.mode ?? 'default',
    limit: options.limit ?? DEFAULT_LIMIT,
    offset: options.offset ?? 0,
    filters: options.filters ?? {},
  };
  if (options.providerPrefs !== undefined && options.providerPrefs !== null) {
    body.provider_prefs = options.providerPrefs;
  }
  if (options.history !== undefined && options.history.length > 0) {
    body.history = options.history;
  }
  return body;
}

/** Machine-readable API failure raised by {@link searchLens}. */
export class SearchApiError extends Error {
  code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = 'SearchApiError';
    this.code = code;
  }
}

type FetchImpl = typeof fetch;

/**
 * Issue `POST {API_URL}/api/v1/search` and return the parsed
 * `SearchResponse`. Throws {@link SearchApiError} for error
 * envelopes, malformed payloads, and transport failures.
 */
export async function searchLens(
  body: SearchRequest,
  fetchImpl: FetchImpl = fetch,
): Promise<SearchResponse> {
  let response: Response;
  try {
    response = await fetchImpl(`${API_BASE_URL}/api/v1/search`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (error) {
    throw new SearchApiError(
      'network_error',
      error instanceof Error ? error.message : 'Network request failed.',
    );
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new SearchApiError('invalid_response', 'Response was not valid JSON.');
  }

  if (isSearchResponse(payload)) return payload;
  if (isErrorEnvelope(payload)) {
    throw new SearchApiError(payload.error.code, payload.error.message);
  }
  throw new SearchApiError('invalid_response', 'Unexpected response shape.');
}

/**
 * Page count for a pre-pagination `total` at the given page size.
 * `total` is the full match count reported by the backend, not the
 * length of the current page. Returns 0 when there are no results.
 */
export function totalPages(total: number, limit: number): number {
  if (total <= 0 || limit <= 0) return 0;
  return Math.ceil(total / limit);
}

/** Offset of the first result on 1-based `page` at the given page size. */
export function offsetForPage(page: number, limit: number): number {
  return Math.max(0, (Math.max(1, Math.floor(page)) - 1) * limit);
}

/** 1-based page containing `offset` at the given page size. */
export function pageForOffset(offset: number, limit: number): number {
  if (limit <= 0) return 1;
  return Math.floor(Math.max(0, offset) / limit) + 1;
}

/** Debounce delay for as-you-type suggestions (plan contract). */
export const SUGGEST_DEBOUNCE_MS = 150;

type AnyArgs = unknown[];

/**
 * Debounce `fn`: restart the `delayMs` timer on every call and invoke
 * `fn` with the latest arguments once the timer elapses. The caller
 * owns cancellation via the returned `cancel`.
 */
export function debounce<Args extends AnyArgs>(
  fn: (...args: Args) => void,
  delayMs: number,
): ((...args: Args) => void) & { cancel: () => void } {
  let timer: ReturnType<typeof setTimeout> | null = null;
  const debounced = (...args: Args): void => {
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      fn(...args);
    }, delayMs);
  };
  debounced.cancel = (): void => {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  };
  return debounced;
}

/** Options accepted by {@link fetchSuggestions}. */
export interface SuggestOptions {
  /** Prefix-completion cap (backend default 10, max 50). */
  limit?: number;
  /**
   * Caller-owned abort signal: the caller creates the
   * `AbortController`, aborts stale in-flight requests, and passes
   * `controller.signal` here. An abort resolves to `[]`, never throws.
   */
  signal?: AbortSignal;
}

/**
 * Issue `GET {API_URL}/api/v1/suggest` and return the completion
 * list. Never throws into rendering: blank queries skip the fetch,
 * and backend-absent (404/5xx), malformed, or aborted requests
 * resolve to `[]` so the UI degrades to direct search.
 */
export async function fetchSuggestions(
  query: string,
  options: SuggestOptions = {},
  fetchImpl: FetchImpl = fetch,
): Promise<string[]> {
  if (!query.trim()) return [];
  const params = new URLSearchParams({ q: query });
  if (options.limit !== undefined) params.set('limit', String(options.limit));
  let response: Response;
  try {
    response = await fetchImpl(`${API_BASE_URL}/api/v1/suggest?${params.toString()}`, {
      signal: options.signal,
    });
  } catch {
    return [];
  }
  if (!response.ok) return [];
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    return [];
  }
  if (!isSuggestResponse(payload)) return [];
  return payload.suggestions;
}

/**
 * Fire-and-forget `POST {API_URL}/api/v1/feedback` click signal.
 * Sends only the anonymous (doc id, action) pair — no query text, no
 * user/session identifiers. Never rejects into rendering: transport
 * or backend failures resolve silently to `void`.
 */
export async function sendFeedback(
  docId: string,
  action: string,
  fetchImpl: FetchImpl = fetch,
): Promise<void> {
  if (!docId.trim() || !action.trim()) return;
  const body: FeedbackRequest = { doc_id: docId, action };
  try {
    await fetchImpl(`${API_BASE_URL}/api/v1/feedback`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch {
    // Fire-and-forget: feedback must never break rendering.
  }
}

/**
 * Speculatively fetch 1-based `page` of a prior search `body` via the
 * existing offset math. Resolves to `null` (instead of throwing) when
 * the prefetch fails, so callers treat a miss as "not prefetched".
 */
export function prefetchPage(
  body: SearchRequest,
  page: number,
  fetchImpl: FetchImpl = fetch,
): Promise<SearchResponse | null> {
  const next: SearchRequest = { ...body, offset: offsetForPage(page, body.limit) };
  return searchLens(next, fetchImpl).then(
    (response) => response,
    () => null,
  );
}

/**
 * Admin surface: URL-frontier dashboard plus DB-backed config editing.
 * Every mutation reuses the backend `X-Admin-Token` gate
 * (`OPENLENS_FRONTIER_ADMIN_TOKEN`); no second auth scheme exists.
 * Reads stay open and degrade to empty views on transport or shape
 * failures so the dashboard never crashes the page.
 */

/**
 * Parse a frontier JSON payload: return the typed body when the guard
 * accepts it, throw {@link SearchApiError} for error envelopes and
 * malformed shapes.
 */
async function parseFrontier<T>(
  response: Response,
  guard: (value: unknown) => value is T,
): Promise<T> {
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new SearchApiError('invalid_response', 'Response was not valid JSON.');
  }
  if (guard(payload)) return payload;
  if (isErrorEnvelope(payload)) {
    throw new SearchApiError(payload.error.code, payload.error.message);
  }
  throw new SearchApiError('invalid_response', 'Unexpected response shape.');
}

/**
 * Issue `GET {API_URL}/api/v1/frontier/status`. Resolves to `null`
 * (instead of throwing) on transport or shape failures so the
 * dashboard degrades to an empty view.
 */
export async function fetchFrontierStatus(
  fetchImpl: FetchImpl = fetch,
): Promise<FrontierStatus | null> {
  let response: Response;
  try {
    response = await fetchImpl(`${API_BASE_URL}/api/v1/frontier/status`);
  } catch {
    return null;
  }
  if (!response.ok) return null;
  try {
    return await parseFrontier(response, isFrontierStatus);
  } catch {
    return null;
  }
}

/**
 * Headers carrying the admin token. Empty when no token is
 * configured, so local-dev calls stay header-free. The same helper
 * serves frontier mutations and admin config writes: both reuse the
 * single `X-Admin-Token` gate, never a second scheme.
 */
export function frontierAdminHeaders(adminToken: string): Record<string, string> {
  return adminToken.trim() ? { 'X-Admin-Token': adminToken.trim() } : {};
}

/**
 * Issue `POST {API_URL}/api/v1/frontier/pause` and return the
 * resulting status. Throws {@link SearchApiError} on failure.
 * Pass `adminToken` when the backend sets
 * `OPENLENS_FRONTIER_ADMIN_TOKEN`.
 */
export async function pauseFrontier(
  fetchImpl: FetchImpl = fetch,
  adminToken: string = '',
): Promise<FrontierStatus> {
  let response: Response;
  try {
    response = await fetchImpl(`${API_BASE_URL}/api/v1/frontier/pause`, {
      method: 'POST',
      headers: { ...frontierAdminHeaders(adminToken) },
    });
  } catch (error) {
    throw new SearchApiError(
      'network_error',
      error instanceof Error ? error.message : 'Network request failed.',
    );
  }
  return parseFrontier(response, isFrontierStatus);
}

/**
 * Issue `POST {API_URL}/api/v1/frontier/clear`: pause and drop every
 * queued URL (store rows plus the Kafka topic when enabled). The
 * response status carries `cleared_queued` and `kafka_purged`.
 * Throws {@link SearchApiError} on failure.
 * Pass `adminToken` when the backend sets
 * `OPENLENS_FRONTIER_ADMIN_TOKEN`.
 */
export async function clearFrontier(
  fetchImpl: FetchImpl = fetch,
  adminToken: string = '',
): Promise<FrontierStatus> {
  let response: Response;
  try {
    response = await fetchImpl(`${API_BASE_URL}/api/v1/frontier/clear`, {
      method: 'POST',
      headers: { ...frontierAdminHeaders(adminToken) },
    });
  } catch (error) {
    throw new SearchApiError(
      'network_error',
      error instanceof Error ? error.message : 'Network request failed.',
    );
  }
  return parseFrontier(response, isFrontierStatus);
}

/**
 * Issue `POST {API_URL}/api/v1/frontier/resume` with optional
 * replacement `seeds`. `priority`, when given, applies to every seed
 * (higher dequeues first; omitted keeps the stored value).
 * Throws {@link SearchApiError} on failure.
 * Pass `adminToken` when the backend sets
 * `OPENLENS_FRONTIER_ADMIN_TOKEN`.
 */
export async function resumeFrontier(
  seeds: string[] = [],
  fetchImpl: FetchImpl = fetch,
  adminToken: string = '',
  priority?: number,
): Promise<FrontierStatus> {
  let response: Response;
  try {
    response = await fetchImpl(`${API_BASE_URL}/api/v1/frontier/resume`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...frontierAdminHeaders(adminToken),
      },
      body: JSON.stringify(priority === undefined ? { seeds } : { seeds, priority }),
    });
  } catch (error) {
    throw new SearchApiError(
      'network_error',
      error instanceof Error ? error.message : 'Network request failed.',
    );
  }
  return parseFrontier(response, isFrontierStatus);
}

/**
 * Issue `GET {API_URL}/api/v1/frontier/settings`. Resolves to `null`
 * on transport or shape failures so the settings form degrades to
 * its local defaults.
 */
export async function fetchFrontierSettings(
  fetchImpl: FetchImpl = fetch,
): Promise<FrontierSettings | null> {
  let response: Response;
  try {
    response = await fetchImpl(`${API_BASE_URL}/api/v1/frontier/settings`);
  } catch {
    return null;
  }
  if (!response.ok) return null;
  try {
    const body = await parseFrontier(response, isFrontierSettingsResponse);
    return body.settings;
  } catch {
    return null;
  }
}

/**
 * Issue `PUT {API_URL}/api/v1/frontier/settings` and return the saved
 * settings. Throws {@link SearchApiError} on failure.
 * Pass `adminToken` when the backend sets
 * `OPENLENS_FRONTIER_ADMIN_TOKEN`.
 */
export async function saveFrontierSettings(
  settings: FrontierSettings,
  fetchImpl: FetchImpl = fetch,
  adminToken: string = '',
): Promise<FrontierSettingsResponse> {
  let response: Response;
  try {
    response = await fetchImpl(`${API_BASE_URL}/api/v1/frontier/settings`, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        ...frontierAdminHeaders(adminToken),
      },
      body: JSON.stringify(settings),
    });
  } catch (error) {
    throw new SearchApiError(
      'network_error',
      error instanceof Error ? error.message : 'Network request failed.',
    );
  }
  return parseFrontier(response, isFrontierSettingsResponse);
}

/**
 * Issue `GET {API_URL}/api/v1/frontier/workers`. Resolves to `[]`
 * (instead of throwing) on transport or shape failures so the
 * dashboard degrades to an empty Workers view.
 */
export async function fetchFrontierWorkers(
  fetchImpl: FetchImpl = fetch,
): Promise<FrontierWorker[]> {
  let response: Response;
  try {
    response = await fetchImpl(`${API_BASE_URL}/api/v1/frontier/workers`);
  } catch {
    return [];
  }
  if (!response.ok) return [];
  try {
    const body = await parseFrontier(response, isFrontierWorkersResponse);
    return body.workers;
  } catch {
    return [];
  }
}

/**
 * Issue `GET {API_URL}/api/v1/admin/config/{name}`. Resolves to
 * `null` when the backend route does not exist yet (404), or on any
 * transport or shape failure, so config editors show file defaults
 * with a "pending backend" notice instead of crashing.
 */
export async function fetchAdminConfig(
  name: AdminConfigName,
  fetchImpl: FetchImpl = fetch,
): Promise<AdminConfigDoc | null> {
  let response: Response;
  try {
    response = await fetchImpl(`${API_BASE_URL}/api/v1/admin/config/${name}`);
  } catch {
    return null;
  }
  if (!response.ok) return null;
  try {
    return await parseFrontier(response, isAdminConfigDoc);
  } catch {
    return null;
  }
}

/**
 * Issue `PUT {API_URL}/api/v1/admin/config/{name}` with the edited
 * document and return the saved version. The wrapped `{body}`
 * envelope matches `AdminConfigPutSchema` (strict: unknown top-level
 * keys rejected). Throws {@link SearchApiError} on failure: 403
 * `forbidden` when the admin token is missing or wrong, 404
 * `not_found` for unknown doc keys, 422 `invalid_request` when the
 * edit fails startup validation, 503 `config_store_unavailable`
 * when the store is down. Pass `adminToken` when the backend sets
 * `OPENLENS_FRONTIER_ADMIN_TOKEN`.
 */
/**
 * Issue `GET {API_URL}/api/v1/frontier/crawl-requests` (admin-only
 * read). Resolves to `null` on transport or shape failures so the
 * review section degrades to an empty view. Pass `adminToken` when
 * the backend sets `OPENLENS_FRONTIER_ADMIN_TOKEN`.
 */
export async function listCrawlRequests(
  adminToken: string = '',
  status: 'pending' | 'approved' | 'declined' = 'pending',
): Promise<CrawlRequestList | null> {
  let res;
  try {
    res = await frontierListCrawlRequestsApiV1FrontierCrawlRequestsGet(
      { status },
      { headers: { ...frontierAdminHeaders(adminToken) } },
    );
  } catch {
    return null;
  }
  if (res.status !== 200) return null;
  return isCrawlRequestList(res.data) ? res.data : null;
}

/**
 * Decide one crawl request (`approve` enqueues the URL, `decline`
 * parks it). Throws {@link SearchApiError} on failure so the drawer
 * can toast the backend message.
 */
export async function decideCrawlRequest(
  requestId: number,
  decision: 'approve' | 'decline',
  adminToken: string = '',
): Promise<CrawlRequest> {
  let res;
  try {
    res =
      decision === 'approve'
        ? await frontierApproveCrawlRequestApiV1FrontierCrawlRequestsRequestIdApprovePost(
            requestId,
            { headers: { ...frontierAdminHeaders(adminToken) } },
          )
        : await frontierDeclineCrawlRequestApiV1FrontierCrawlRequestsRequestIdDeclinePost(
            requestId,
            { headers: { ...frontierAdminHeaders(adminToken) } },
          );
  } catch (error) {
    throw new SearchApiError(
      'network_error',
      error instanceof Error ? error.message : 'Network request failed.',
    );
  }
  if (res.status === 200 && isCrawlRequest(res.data)) return res.data;
  if (isErrorEnvelope(res.data)) {
    throw new SearchApiError(res.data.error.code, res.data.error.message);
  }
  throw new SearchApiError('invalid_response', 'Unexpected response shape.');
}

export async function saveAdminConfig(
  name: AdminConfigName,
  body: Record<string, unknown> | unknown[],
  fetchImpl: FetchImpl = fetch,
  adminToken: string = '',
): Promise<AdminConfigDoc> {
  let response: Response;
  try {
    response = await fetchImpl(`${API_BASE_URL}/api/v1/admin/config/${name}`, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        ...frontierAdminHeaders(adminToken),
      },
      body: JSON.stringify({ body }),
    });
  } catch (error) {
    throw new SearchApiError(
      'network_error',
      error instanceof Error ? error.message : 'Network request failed.',
    );
  }
  return parseFrontier(response, isAdminConfigDoc);
}

/**
 * Issue `GET {API_URL}/api/v1/admin/analytics/counts` (admin-only
 * read). Resolves to `null` on transport, auth, store-unavailable
 * (503), or shape failures so the analytics view degrades to an
 * empty panel with a notice instead of crashing the page. Pass
 * `adminToken` when the backend sets `OPENLENS_FRONTIER_ADMIN_TOKEN`.
 */
export async function fetchAnalyticsCounts(
  days: number = 30,
  adminToken: string = '',
): Promise<AnalyticsCounts | null> {
  let res;
  try {
    res = await adminAnalyticsCountsApiV1AdminAnalyticsCountsGet(
      { days },
      { headers: { ...frontierAdminHeaders(adminToken) } },
    );
  } catch {
    return null;
  }
  if (res.status !== 200) return null;
  return isAnalyticsCounts(res.data) ? res.data : null;
}

/**
 * Issue `GET {API_URL}/api/v1/admin/feedback/summary` (admin-only
 * read). Resolves to `null` on transport, auth, or shape failures so
 * the analytics view degrades to an empty panel with a notice
 * instead of crashing the page. Pass `adminToken` when the backend
 * sets `OPENLENS_FRONTIER_ADMIN_TOKEN`.
 */
export async function fetchFeedbackSummary(
  adminToken: string = '',
): Promise<FeedbackSummary | null> {
  let res;
  try {
    res = await adminFeedbackSummaryApiV1AdminFeedbackSummaryGet({
      headers: { ...frontierAdminHeaders(adminToken) },
    });
  } catch {
    return null;
  }
  if (res.status !== 200) return null;
  return isFeedbackSummary(res.data) ? res.data : null;
}

/**
 * Issue `GET {API_URL}/api/v1/admin/analytics/top` (admin-only
 * read). Resolves to `null` on transport, auth, store-unavailable
 * (503), invalid name/prop (422), or shape failures. Pass
 * `adminToken` when the backend sets
 * `OPENLENS_FRONTIER_ADMIN_TOKEN`.
 */
export async function fetchAnalyticsTop(
  name: string = 'crawl.page',
  prop: string = 'reason',
  days: number = 30,
  limit: number = 10,
  adminToken: string = '',
  filterProp?: string,
  filterValue?: string,
): Promise<AnalyticsTop | null> {
  let res;
  try {
    res = await adminAnalyticsTopApiV1AdminAnalyticsTopGet(
      {
        name,
        prop,
        days,
        limit,
        ...(filterProp !== undefined ? { filter_prop: filterProp } : {}),
        ...(filterValue !== undefined ? { filter_value: filterValue } : {}),
      },
      { headers: { ...frontierAdminHeaders(adminToken) } },
    );
  } catch {
    return null;
  }
  if (res.status !== 200) return null;
  return isAnalyticsTop(res.data) ? res.data : null;
}

/**
 * Issue `GET {API_URL}/api/v1/admin/analytics/series` (admin-only
 * read). Resolves to `null` on transport, auth, store-unavailable
 * (503), invalid args (422), or shape failures. Pass `adminToken`
 * when the backend sets `OPENLENS_FRONTIER_ADMIN_TOKEN`.
 */
export async function fetchAnalyticsSeries(
  name: string = 'crawl.page',
  bucket: string = 'day',
  days: number = 30,
  adminToken: string = '',
): Promise<AnalyticsSeries | null> {
  let res;
  try {
    res = await adminAnalyticsSeriesApiV1AdminAnalyticsSeriesGet(
      { name, bucket, days },
      { headers: { ...frontierAdminHeaders(adminToken) } },
    );
  } catch {
    return null;
  }
  if (res.status !== 200) return null;
  return isAnalyticsSeries(res.data) ? res.data : null;
}

/**
 * Issue `GET {API_URL}/api/v1/frontier/failed` (open read, like the
 * status endpoint). Resolves to `null` on transport or shape
 * failures so the frontier view degrades to an empty section with a
 * notice instead of crashing the page.
 */
export async function fetchFailedUrls(
  limit: number = 20,
  offset: number = 0,
): Promise<FrontierFailedList | null> {
  let res;
  try {
    res = await frontierFailedApiV1FrontierFailedGet({ limit, offset });
  } catch {
    return null;
  }
  if (res.status !== 200) return null;
  return isFrontierFailedList(res.data) ? res.data : null;
}

/**
 * Issue `GET {API_URL}/api/v1/frontier/failed/domains` (open read).
 * Resolves to `null` on transport or shape failures so the domains
 * view degrades to an empty section instead of crashing the page.
 */
export async function fetchFailedDomains(
  limit: number = 20,
  offset: number = 0,
): Promise<FrontierFailedDomainList | null> {
  let res;
  try {
    res = await frontierFailedDomainsApiV1FrontierFailedDomainsGet({ limit, offset });
  } catch {
    return null;
  }
  if (res.status !== 200) return null;
  return isFrontierFailedDomainList(res.data) ? res.data : null;
}

/**
 * Issue `GET {API_URL}/api/v1/frontier/queued/domains` (open read).
 * Resolves to `null` on transport or shape failures so the queued
 * view degrades to an empty section instead of crashing the drawer.
 */
export async function fetchQueuedDomains(
  limit: number = 20,
  offset: number = 0,
): Promise<FrontierQueuedDomainList | null> {
  let res;
  try {
    res = await frontierQueuedDomainsApiV1FrontierQueuedDomainsGet({ limit, offset });
  } catch {
    return null;
  }
  if (res.status !== 200) return null;
  return isFrontierQueuedDomainList(res.data) ? res.data : null;
}

/**
 * Issue `GET {API_URL}/api/v1/frontier/failed/domains/{domain}/urls`
 * (open read). Resolves to `null` on transport or shape failures so
 * the drill-down degrades to an empty list instead of crashing.
 */
export async function fetchFailedDomainUrls(
  domain: string,
  limit: number = 20,
  offset: number = 0,
): Promise<FrontierFailedList | null> {
  let res;
  try {
    res = await frontierFailedDomainUrlsApiV1FrontierFailedDomainsDomainUrlsGet(domain, {
      limit,
      offset,
    });
  } catch {
    return null;
  }
  if (res.status !== 200) return null;
  return isFrontierFailedList(res.data) ? res.data : null;
}

/** Domain actions shared by the failed-domains buttons. */
export type FrontierDomainOp = 'requeue' | 'purge' | 'skip';

/**
 * Issue `POST {API_URL}/api/v1/frontier/domains/{requeue|purge|skip}`.
 * Throws {@link SearchApiError} on failure so the drawer can toast
 * the backend message. Pass `adminToken` when the backend sets
 * `OPENLENS_FRONTIER_ADMIN_TOKEN`.
 */
export async function domainAction(
  op: FrontierDomainOp,
  domains: string[],
  adminToken: string = '',
): Promise<FrontierDomainsResult> {
  const call = {
    requeue: frontierDomainsRequeueApiV1FrontierDomainsRequeuePost,
    purge: frontierDomainsPurgeApiV1FrontierDomainsPurgePost,
    skip: frontierDomainsSkipApiV1FrontierDomainsSkipPost,
  }[op];
  let res;
  try {
    res = await call({ domains }, { headers: { ...frontierAdminHeaders(adminToken) } });
  } catch (error) {
    throw new SearchApiError(
      'network_error',
      error instanceof Error ? error.message : 'Network request failed.',
    );
  }
  if (res.status === 200 && isFrontierDomainsResult(res.data)) return res.data;
  if (isErrorEnvelope(res.data)) {
    throw new SearchApiError(res.data.error.code, res.data.error.message);
  }
  throw new SearchApiError('invalid_response', 'Unexpected response shape.');
}

/**
 * Shift a host's queued rows' priority by `delta` (positive fast-lanes,
 * negative sinks). Throws {@link SearchApiError} on failure so the
 * drawer can toast the backend message.
 */
export async function bumpDomainPriority(
  domains: string[],
  delta: number,
  adminToken: string = '',
): Promise<FrontierDomainsResult> {
  let res;
  try {
    res = await frontierDomainsPriorityApiV1FrontierDomainsPriorityPost(
      { domains, delta },
      { headers: { ...frontierAdminHeaders(adminToken) } },
    );
  } catch (error) {
    throw new SearchApiError(
      'network_error',
      error instanceof Error ? error.message : 'Network request failed.',
    );
  }
  if (res.status === 200 && isFrontierDomainsResult(res.data)) return res.data;
  if (isErrorEnvelope(res.data)) {
    throw new SearchApiError(res.data.error.code, res.data.error.message);
  }
  throw new SearchApiError('invalid_response', 'Unexpected response shape.');
}

/**
 * Block hosts: merge them into the `indexing` doc's `deny_domains`
 * (deduped, lowercased) and save. Throws {@link SearchApiError} on
 * failure, including when the doc shape is unexpected. Callers mirror
 * the host into the frontier settings deny list (which is what the
 * drawer renders) with their own settings state.
 */
export async function blockDomains(domains: string[], adminToken: string = ''): Promise<void> {
  const doc = await fetchAdminConfig('indexing');
  if (doc == null || Array.isArray(doc.body) || typeof doc.body !== 'object') {
    throw new SearchApiError('invalid_response', 'Indexing config unavailable.');
  }
  const current = Array.isArray((doc.body as Record<string, unknown>).deny_domains)
    ? ((doc.body as Record<string, unknown>).deny_domains as unknown[]).filter(
        (entry): entry is string => typeof entry === 'string',
      )
    : [];
  const merged = [...current];
  for (const domain of domains) {
    const host = domain.trim().toLowerCase();
    if (host && !merged.includes(host)) merged.push(host);
  }
  await saveAdminConfig('indexing', { ...doc.body, deny_domains: merged }, fetch, adminToken);
}
