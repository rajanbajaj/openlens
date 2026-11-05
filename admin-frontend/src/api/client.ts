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
  isFrontierEnqueueSeedsResponse,
  isFrontierFailedDomainList,
  isFrontierFailedList,
  isFrontierQueuedDomainList,
  isFrontierTriageResult,
  isFrontierSettingsResponse,
  isFrontierStatus,
  isFrontierUrlInfo,
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
  frontierFailedDomainsAutoTriageApiV1FrontierFailedDomainsAutoTriagePost,
  frontierDomainsRequeueApiV1FrontierDomainsRequeuePost,
  frontierDomainsSkipApiV1FrontierDomainsSkipPost,
  frontierFailedDomainUrlsApiV1FrontierFailedDomainsDomainUrlsGet,
  frontierQueuedDomainUrlsApiV1FrontierQueuedDomainsDomainUrlsGet,
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
  FeedbackSummary,
  FrontierDomainsResult,
  FrontierEnqueueSeedsResponse,
  FrontierFailedDomainList,
  FrontierFailedList,
  FrontierQueuedDomainList,
  FrontierTriageResult,
  FrontierSettings,
  FrontierUrlInfo,
  FrontierSettingsResponse,
  FrontierStatus,
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

/** Query params for the live analytics stream (mirrors the panel GETs). */
export interface AnalyticsLiveParams {
  days: number;
  bucket: string;
  start?: string;
  end?: string;
  prevStart?: string;
  prevEnd?: string;
}

/**
 * WebSocket URL for the live analytics stream
 * (`/api/v1/admin/analytics/live` pushes the same counts/series the
 * panel GETs). Same host/base mapping as `frontierLiveUrl`.
 */
export function analyticsLiveUrl(params: AnalyticsLiveParams): string {
  const query = new URLSearchParams();
  query.set('days', String(params.days));
  query.set('bucket', params.bucket);
  if (params.start !== undefined) query.set('start', params.start);
  if (params.end !== undefined) query.set('end', params.end);
  if (params.prevStart !== undefined) query.set('prev_start', params.prevStart);
  if (params.prevEnd !== undefined) query.set('prev_end', params.prevEnd);
  const base = API_BASE_URL || `${window.location.protocol}//${window.location.host}`;
  return `${base.replace(/^http/, 'ws')}/api/v1/admin/analytics/live?${query.toString()}`;
}

/** Machine-readable API failure raised by the admin mutations below. */
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
 * Issue `GET {API_URL}/api/v1/frontier/url-info?url=...`: one URL's
 * crawl story (queue row, indexed document, extracted-link fan-out).
 * Resolves to `null` on blank input, transport, or shape failures so
 * the inspector degrades to an empty note instead of throwing.
 */
export async function fetchFrontierUrlInfo(
  url: string,
  fetchImpl: FetchImpl = fetch,
): Promise<FrontierUrlInfo | null> {
  if (!url.trim()) return null;
  let response: Response;
  try {
    response = await fetchImpl(
      `${API_BASE_URL}/api/v1/frontier/url-info?url=${encodeURIComponent(url.trim())}`,
    );
  } catch {
    return null;
  }
  if (!response.ok) return null;
  try {
    return await parseFrontier(response, isFrontierUrlInfo);
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
 * Issue `POST {API_URL}/api/v1/frontier/seeds` with `seeds` to enqueue.
 * Unlike resume this never touches crawl phase or the in-memory queue:
 * running crawlers pick the seeds up through the durable queue without
 * interruption. `priority` and `depth`, when given, apply to every seed
 * (depth defaults to 0, the seed convention, when omitted).
 * Throws {@link SearchApiError} on failure.
 * Pass `adminToken` when the backend sets
 * `OPENLENS_FRONTIER_ADMIN_TOKEN`.
 */
export async function enqueueSeeds(
  seeds: string[],
  fetchImpl: FetchImpl = fetch,
  adminToken: string = '',
  priority?: number,
  depth?: number,
): Promise<FrontierEnqueueSeedsResponse> {
  let response: Response;
  try {
    response = await fetchImpl(`${API_BASE_URL}/api/v1/frontier/seeds`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...frontierAdminHeaders(adminToken),
      },
      body: JSON.stringify({
        seeds,
        ...(priority === undefined ? {} : { priority }),
        ...(depth === undefined ? {} : { depth }),
      }),
    });
  } catch (error) {
    throw new SearchApiError(
      'network_error',
      error instanceof Error ? error.message : 'Network request failed.',
    );
  }
  return parseFrontier(response, isFrontierEnqueueSeedsResponse);
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
  start?: string,
  end?: string,
): Promise<AnalyticsCounts | null> {
  let res;
  try {
    res = await adminAnalyticsCountsApiV1AdminAnalyticsCountsGet(
      {
        days,
        ...(start !== undefined ? { start } : {}),
        ...(end !== undefined ? { end } : {}),
      },
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
  offset?: number,
  match?: string,
  start?: string,
  end?: string,
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
        ...(offset !== undefined ? { offset } : {}),
        ...(match !== undefined ? { match } : {}),
        ...(start !== undefined ? { start } : {}),
        ...(end !== undefined ? { end } : {}),
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
  start?: string,
  end?: string,
  filterProp?: string,
  filterValue?: string,
): Promise<AnalyticsSeries | null> {
  let res;
  try {
    res = await adminAnalyticsSeriesApiV1AdminAnalyticsSeriesGet(
      {
        name,
        bucket,
        days,
        ...(start !== undefined ? { start } : {}),
        ...(end !== undefined ? { end } : {}),
        ...(filterProp !== undefined ? { filter_prop: filterProp } : {}),
        ...(filterValue !== undefined ? { filter_value: filterValue } : {}),
      },
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

/**
 * Issue `GET {API_URL}/api/v1/frontier/queued/domains/{domain}/urls`
 * (open read). Resolves to `null` on transport or shape failures so
 * the drill-down degrades to an empty list instead of crashing.
 */
export async function fetchQueuedDomainUrls(
  domain: string,
  limit: number = 20,
  offset: number = 0,
): Promise<FrontierFailedList | null> {
  let res;
  try {
    res = await frontierQueuedDomainUrlsApiV1FrontierQueuedDomainsDomainUrlsGet(domain, {
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
 * Run the failed-domains auto-triage pass (admin-only). Throws
 * {@link SearchApiError} on failure so the drawer can toast the
 * backend message.
 */
export async function autoTriageFailedDomains(
  dryRun: boolean,
  adminToken: string = '',
): Promise<FrontierTriageResult> {
  let res;
  try {
    res = await frontierFailedDomainsAutoTriageApiV1FrontierFailedDomainsAutoTriagePost(
      { dry_run: dryRun },
      { headers: { ...frontierAdminHeaders(adminToken) } },
    );
  } catch (error) {
    throw new SearchApiError(
      'network_error',
      error instanceof Error ? error.message : 'Network request failed.',
    );
  }
  if (res.status === 200 && isFrontierTriageResult(res.data)) return res.data;
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
