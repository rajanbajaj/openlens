/**
 * Runtime guards validating the response/error envelope shape of
 * `POST /api/v1/search`. Structural checks only (no `any` leaks:
 * every unknown is narrowed before use).
 */

import type {
  AdminConfigDoc,
  AnalyticsCounts,
  AnalyticsSeries,
  AnalyticsTop,
  CrawlRequest,
  CrawlRequestList,
  ErrorEnvelope,
  FeedbackSummary,
  FrontierDiscovery,
  FrontierDomainsResult,
  FrontierEnqueueSeedsResponse,
  FrontierFailedDomainList,
  FrontierFailedList,
  FrontierQueuedDomainList,
  FrontierTriageResult,
  FrontierFailedUrl,
  FrontierSettings,
  FrontierUrlInfo,
  FrontierSettingsResponse,
  FrontierStatus,
  FrontierWorkersResponse,
  FrontierWorker,
} from './types.ts';

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string');
}

function isNumberRecord(value: unknown): value is Record<string, number> {
  return isRecord(value) && Object.values(value).every((item) => typeof item === 'number');
}

function isFrontierQueueEntry(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return (
    typeof value.url === 'string' &&
    typeof value.depth === 'number' &&
    typeof value.status === 'string'
  );
}

/** One worker entry in `GET /api/v1/frontier/workers` / status. */
export function isFrontierWorker(value: unknown): value is FrontierWorker {
  if (!isRecord(value)) return false;
  return (
    typeof value.worker_id === 'string' &&
    typeof value.crawl_id === 'string' &&
    typeof value.last_seen === 'number' &&
    typeof value.claimed === 'number' &&
    typeof value.status === 'string'
  );
}

/** `GET /api/v1/frontier/status` response shape. */
export function isFrontierStatus(value: unknown): value is FrontierStatus {
  if (!isRecord(value)) return false;
  if (typeof value.paused !== 'boolean') return false;
  if (!Array.isArray(value.queue) || !value.queue.every(isFrontierQueueEntry)) return false;
  if (
    value.workers !== undefined &&
    (!Array.isArray(value.workers) || !value.workers.every(isFrontierWorker))
  ) {
    return false;
  }
  if (typeof value.queue_depth !== 'number') return false;
  if (!isRecord(value.checkpoint)) return false;
  if (
    typeof value.checkpoint.phase !== 'string' ||
    typeof value.checkpoint.pages_attempted !== 'number' ||
    typeof value.checkpoint.pages_indexed !== 'number'
  ) {
    return false;
  }
  if (!Array.isArray(value.new_urls)) return false;
  return value.new_urls.every(
    (entry: unknown) =>
      isRecord(entry) && typeof entry.url === 'string' && typeof entry.status === 'string',
  );
}

/** `GET /api/v1/frontier/workers` response shape. */
export function isFrontierWorkersResponse(value: unknown): value is FrontierWorkersResponse {
  if (!isRecord(value)) return false;
  if (typeof value.worker_count !== 'number') return false;
  return Array.isArray(value.workers) && value.workers.every(isFrontierWorker);
}

/** `GET/PUT /api/v1/frontier/settings` response shape. */
export function isFrontierSettingsResponse(value: unknown): value is FrontierSettingsResponse {
  if (!isRecord(value)) return false;
  const settings: unknown = value.settings;
  if (!isRecord(settings)) return false;
  return (
    typeof settings.max_pages === 'number' &&
    typeof settings.max_queued_urls === 'number' &&
    typeof settings.crawl_delay_s === 'number' &&
    typeof settings.default_max_depth === 'number' &&
    typeof settings.respect_robots === 'boolean' &&
    typeof settings.user_agent === 'string' &&
    Array.isArray(settings.skip_domains) &&
    settings.skip_domains.every((domain) => typeof domain === 'string') &&
    Array.isArray(settings.allow_domains) &&
    settings.allow_domains.every((domain) => typeof domain === 'string') &&
    Array.isArray(settings.deny_domains) &&
    settings.deny_domains.every((domain) => typeof domain === 'string') &&
    Array.isArray(settings.discovery_feeds) &&
    settings.discovery_feeds.every((feed) => typeof feed === 'string') &&
    typeof settings.refresh_after_days === 'number' &&
    typeof settings.discovery_interval_s === 'number'
  );
}

/** POST /api/v1/frontier/seeds response shape. */
export function isFrontierEnqueueSeedsResponse(
  value: unknown,
): value is FrontierEnqueueSeedsResponse {
  if (!isRecord(value)) return false;
  return typeof value.enqueued === 'number';
}

export function isFrontierUrlInfo(value: unknown): value is FrontierUrlInfo {
  if (!isRecord(value)) return false;
  if (typeof value.url !== 'string' || typeof value.normalized_url !== 'string') {
    return false;
  }
  if (value.frontier !== null) {
    if (!isRecord(value.frontier) || typeof value.frontier.status !== 'string') return false;
    if (
      (value.frontier.priority !== undefined && typeof value.frontier.priority !== 'number') ||
      (value.frontier.attempts !== undefined && typeof value.frontier.attempts !== 'number') ||
      (value.frontier.last_error !== undefined &&
        value.frontier.last_error !== null &&
        typeof value.frontier.last_error !== 'string')
    ) {
      return false;
    }
  }
  if (value.document !== null) {
    if (!isRecord(value.document) || typeof value.document.title !== 'string') return false;
    if (value.document.chars !== undefined && typeof value.document.chars !== 'number') {
      return false;
    }
  }
  if (value.extracted_links !== undefined && typeof value.extracted_links !== 'number') {
    return false;
  }
  if (value.link_sample !== undefined && !isStringArray(value.link_sample)) return false;
  return true;
}

/** Live workers, or `[]` when absent or malformed. */
export function readFrontierWorkers(value: unknown): FrontierWorker[] {
  if (!isRecord(value) || !Array.isArray(value.workers)) return [];
  return value.workers.filter(isFrontierWorker);
}

/** Queue entries, or `[]` when absent or malformed. */
export function readFrontierQueue(value: unknown): FrontierStatus['queue'] {
  if (!isRecord(value) || !Array.isArray(value.queue)) return [];
  return value.queue.filter(isFrontierQueueEntry) as FrontierStatus['queue'];
}

/** New-URL entries, or `[]` when absent or malformed. */
export function readFrontierNewUrls(value: unknown): FrontierStatus['new_urls'] {
  if (!isRecord(value) || !Array.isArray(value.new_urls)) return [];
  return value.new_urls.filter(
    (entry: unknown): entry is FrontierStatus['new_urls'][number] =>
      isRecord(entry) && typeof entry.url === 'string' && typeof entry.status === 'string',
  );
}

/** Settings payload, or `null` when absent or malformed. */
export function readFrontierSettings(value: unknown): FrontierSettings | null {
  if (!isFrontierSettingsResponse(value)) return null;
  return value.settings;
}

/** One worker discovery-pass summary, or false when absent or malformed. */
export function isFrontierDiscovery(value: unknown): value is FrontierDiscovery {
  if (!isRecord(value)) return false;
  return (
    typeof value.at === 'number' &&
    typeof value.feeds_checked === 'number' &&
    typeof value.new_urls === 'number' &&
    typeof value.refreshed === 'number' &&
    (value.worker_id === undefined || typeof value.worker_id === 'string')
  );
}

/** Discovery summary, or `null` when absent or malformed. */
export function readFrontierDiscovery(value: unknown): FrontierDiscovery | null {
  if (!isRecord(value)) return null;
  const discovery: unknown = value.discovery;
  return isFrontierDiscovery(discovery) ? discovery : null;
}

/** `GET /api/v1/admin/analytics/counts` response shape. */
export function isAnalyticsCounts(value: unknown): value is AnalyticsCounts {
  if (!isRecord(value)) return false;
  return (
    typeof value.days === 'number' &&
    typeof value.total === 'number' &&
    isNumberRecord(value.by_name)
  );
}

/** One failed URL entry. */
function isFrontierFailedUrl(value: unknown): value is FrontierFailedUrl {
  if (!isRecord(value)) return false;
  return (
    typeof value.url === 'string' &&
    (value.reason === undefined || value.reason === null || typeof value.reason === 'string') &&
    (value.attempts === undefined || typeof value.attempts === 'number')
  );
}

/** `GET /api/v1/frontier/failed` response shape. */
export function isFrontierFailedList(value: unknown): value is FrontierFailedList {
  if (!isRecord(value)) return false;
  return (
    Array.isArray(value.urls) &&
    value.urls.every(isFrontierFailedUrl) &&
    typeof value.total === 'number' &&
    typeof value.limit === 'number' &&
    typeof value.offset === 'number'
  );
}

function isFrontierFailedDomain(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return (
    typeof value.domain === 'string' &&
    typeof value.failed === 'number' &&
    typeof value.top_reason === 'string'
  );
}

/** `GET /api/v1/frontier/failed/domains` response shape. */
export function isFrontierFailedDomainList(value: unknown): value is FrontierFailedDomainList {
  if (!isRecord(value)) return false;
  return (
    Array.isArray(value.domains) &&
    value.domains.every(isFrontierFailedDomain) &&
    typeof value.total === 'number' &&
    typeof value.limit === 'number' &&
    typeof value.offset === 'number'
  );
}

/** Domain action (`requeue`/`purge`/`skip`) result shape. */
export function isFrontierDomainsResult(value: unknown): value is FrontierDomainsResult {
  if (!isRecord(value)) return false;
  return (
    Array.isArray(value.domains) &&
    value.domains.every((entry) => typeof entry === 'string') &&
    typeof value.affected === 'number'
  );
}

/** Auto-triage pass result shape. */
export function isFrontierTriageResult(value: unknown): value is FrontierTriageResult {
  if (!isRecord(value)) return false;
  return (
    Array.isArray(value.triaged) &&
    value.triaged.every(
      (entry) =>
        isRecord(entry) &&
        typeof entry.domain === 'string' &&
        typeof entry.action === 'string' &&
        typeof entry.affected === 'number',
    ) &&
    typeof value.dry_run === 'boolean'
  );
}

function isFrontierQueuedDomain(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return typeof value.domain === 'string' && typeof value.queued === 'number';
}

/** `GET /api/v1/frontier/queued/domains` response shape. */
export function isFrontierQueuedDomainList(value: unknown): value is FrontierQueuedDomainList {
  if (!isRecord(value)) return false;
  return (
    Array.isArray(value.domains) &&
    value.domains.every(isFrontierQueuedDomain) &&
    typeof value.total === 'number' &&
    typeof value.limit === 'number' &&
    typeof value.offset === 'number'
  );
}

/** One top-values entry. */
function isAnalyticsTopEntry(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return typeof value.value === 'string' && typeof value.count === 'number';
}

/** `GET /api/v1/admin/analytics/top` response shape. */
export function isAnalyticsTop(value: unknown): value is AnalyticsTop {
  if (!isRecord(value)) return false;
  return (
    typeof value.name === 'string' &&
    typeof value.prop === 'string' &&
    typeof value.days === 'number' &&
    typeof value.total === 'number' &&
    Array.isArray(value.top) &&
    value.top.every(isAnalyticsTopEntry)
  );
}

/** One throughput point. */
function isAnalyticsSeriesPoint(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return typeof value.t === 'string' && typeof value.count === 'number';
}

/** `GET /api/v1/admin/analytics/series` response shape. */
export function isAnalyticsSeries(value: unknown): value is AnalyticsSeries {
  if (!isRecord(value)) return false;
  return (
    typeof value.name === 'string' &&
    typeof value.bucket === 'string' &&
    typeof value.days === 'number' &&
    Array.isArray(value.points) &&
    value.points.every(isAnalyticsSeriesPoint)
  );
}

/** Live frame pushed by the `/api/v1/admin/analytics/live` websocket. */
export interface AnalyticsLiveSnapshot {
  type: 'analytics.snapshot';
  counts: AnalyticsCounts;
  prev_counts: AnalyticsCounts;
  series: Record<'fetched' | 'failed' | 'skipped', AnalyticsSeries>;
  interval_s: number;
}

const LIVE_OUTCOMES = ['fetched', 'failed', 'skipped'] as const;

/** Valid live analytics frame (same counts/series shapes as the GETs). */
export function isAnalyticsSnapshot(value: unknown): value is AnalyticsLiveSnapshot {
  if (!isRecord(value)) return false;
  if (value.type !== 'analytics.snapshot') return false;
  if (typeof value.interval_s !== 'number') return false;
  if (!isAnalyticsCounts(value.counts)) return false;
  if (!isAnalyticsCounts(value.prev_counts)) return false;
  const series: unknown = value.series;
  if (!isRecord(series)) return false;
  return LIVE_OUTCOMES.every((outcome) => isAnalyticsSeries(series[outcome]));
}

/** Fail-open marker the live socket sends when the store is unreachable. */
export function isAnalyticsLiveUnavailable(value: unknown): value is {
  type: 'analytics.snapshot';
  unavailable: true;
} {
  if (!isRecord(value)) return false;
  return value.type === 'analytics.snapshot' && value.unavailable === true;
}

/** One per-document feedback rollup. */
function isFeedbackDocSummary(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return (
    typeof value.doc_id === 'string' &&
    typeof value.total === 'number' &&
    typeof value.helpful === 'number' &&
    typeof value.not_helpful === 'number' &&
    typeof value.reports === 'number'
  );
}

/** `GET /api/v1/admin/feedback/summary` response shape. */
export function isFeedbackSummary(value: unknown): value is FeedbackSummary {
  if (!isRecord(value)) return false;
  return (
    typeof value.total === 'number' &&
    isNumberRecord(value.by_action) &&
    isNumberRecord(value.by_reason) &&
    Array.isArray(value.top_docs) &&
    value.top_docs.every(isFeedbackDocSummary)
  );
}

/** `GET /api/v1/admin/config/{name}` response shape (versioned doc). */
export function isAdminConfigDoc(value: unknown): value is AdminConfigDoc {
  if (!isRecord(value)) return false;
  if (typeof value.doc !== 'string') return false;
  const body: unknown = value.body;
  if (!isRecord(body) && !Array.isArray(body)) return false;
  if (typeof value.version !== 'number' || !Number.isInteger(value.version)) return false;
  if (typeof value.updated_at !== 'string' && value.updated_at !== null) return false;
  return value.source === 'db' || value.source === 'file';
}

export function isErrorEnvelope(value: unknown): value is ErrorEnvelope {
  if (!isRecord(value)) return false;
  if (!isRecord(value.error)) return false;
  return typeof value.error.code === 'string' && typeof value.error.message === 'string';
}

/** One `crawl_requests` row. */
export function isCrawlRequest(value: unknown): value is CrawlRequest {
  if (!isRecord(value)) return false;
  if (typeof value.url !== 'string') return false;
  if (
    value.status !== 'pending' &&
    value.status !== 'approved' &&
    value.status !== 'declined' &&
    value.status !== 'duplicate'
  ) {
    return false;
  }
  if (value.id !== null && typeof value.id !== 'number') return false;
  if (value.created_at !== null && typeof value.created_at !== 'string') return false;
  if (value.decided_at !== null && typeof value.decided_at !== 'string') return false;
  return true;
}

/** `GET /api/v1/frontier/crawl-requests` response shape. */
export function isCrawlRequestList(value: unknown): value is CrawlRequestList {
  if (!isRecord(value)) return false;
  if (!Array.isArray(value.requests) || !value.requests.every(isCrawlRequest)) return false;
  return typeof value.total === 'number';
}
