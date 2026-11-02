/**
 * TypeScript mirror of the OpenLens HTTP contract in
 * `src/openlens/api/schemas.py` (`POST /api/v1/search`).
 *
 * Mirrored by hand, never imported from the backend: if `schemas.py`
 * changes, these types must be re-mirrored to match.
 */

/** Supported search modes (plan contract; backend validates non-blank). */
export type SearchMode = 'default' | 'child-safe' | 'news' | 'shopping' | 'travel';

/** Per-query provider preferences for federated selection (T13). */
export interface ProviderPrefs {
  include: string[];
  exclude: string[];
  primary: string | null;
}

/** POST /api/v1/search request body. */
export interface SearchRequest {
  /** 1-1000 chars, must not be blank. */
  query: string;
  /** Search mode; defaults to "default". */
  mode: string;
  /** 1-100; defaults to 10. */
  limit: number;
  /** >= 0; defaults to 0. */
  offset: number;
  /** Opaque filter mapping forwarded to the backend. */
  filters: Record<string, unknown>;
  /** Federated provider hints; omitted when the user sets none. */
  provider_prefs?: ProviderPrefs | null;
  /** Opt-in request-scoped prior queries (oldest first); omitted when empty. */
  history?: string[];
}

/** One normalized search hit on the wire. */
export interface SearchResult {
  id: string;
  title: string;
  snippet: string;
  original_score: number;
  final_score: number;
  url: string | null;
  provider: string;
  reasons: string[];
  warnings: string[];
  metadata: Record<string, unknown>;
}

/** One plugin-execution record inside diagnostics (opaque entries). */
export type PluginExecution = Record<string, unknown>;

/**
 * Bounded query provenance entry (T9): normalization, intent, expansion,
 * and the mode suggestion. Explicit mode always wins over `suggested_mode`.
 */
export interface QueryProvenance {
  normalized: string;
  truncated: boolean;
  intent: string;
  suggested_mode: string;
  explicit_mode: string;
  effective_mode: string;
  base_terms: string[];
  added_terms: string[];
  provenance: Record<string, string>;
  reasons: string[];
}

/** Per-request diagnostics embedded in the search response. */
export interface Diagnostics {
  fallback_used: boolean;
  provider_latencies_ms: Record<string, number>;
  /** Result counts per provider (T13); absent on older envelopes. */
  provider_result_counts?: Record<string, number>;
  /** Estimated cost per provider (T13); absent on older envelopes. */
  provider_costs_estimated?: Record<string, number>;
  plugin_executions: PluginExecution[];
  errors: string[];
  /** Query provenance (T9); absent when the backend predates it. */
  query?: QueryProvenance;
  /** Opaque request id for log correlation (T15); empty when unset. */
  request_id?: string;
  /** Cache lookup outcome (T21: "hit" | "miss" | "error"); absent when uncached. */
  cache?: CacheStatus;
}

/** Cache lookup outcome reported in diagnostics (T21). */
export interface CacheStatus {
  /** "hit" | "miss" | "error"; absent on uncached responses (`cache: {}`). */
  status?: string;
}

/**
 * Source attribution record for an AI digest (T22: `metadata.ai_citations`).
 * Every field optional so malformed entries degrade instead of crashing;
 * the enrichment reader keeps only entries with a usable id and title.
 */
export interface AiCitation {
  /** Owning result id; absent on malformed records. */
  id?: string;
  /** Human-readable source title; absent on malformed records. */
  title?: string;
  /** Source link; absent or null when the backend reported none. */
  url?: string | null;
}

/** POST /api/v1/search response body. */
export interface SearchResponse {
  query: string;
  mode: string;
  results: SearchResult[];
  total: number;
  /** Page size the backend applied (`DEFAULT_LIMIT` when omitted). */
  limit?: number;
  /** Offset of the first result on this page. */
  offset?: number;
  /** Next-page offset, or null when this page reaches the end. */
  next_offset?: number | null;
  /** Query assistance: prefix completions (absent on older envelopes). */
  suggestions?: string[];
  /** Query assistance: corrected spelling hint (null/absent when none). */
  spelling?: string | null;
  /** Query assistance: related queries (absent on older envelopes). */
  related?: string[];
  applied_filters: string[];
  diagnostics: Diagnostics;
}

/** GET /api/v1/suggest response body (`SuggestResponseSchema`). */
export interface SuggestResponse {
  q: string;
  suggestions: string[];
}

/**
 * POST /api/v1/feedback request body: anonymous (doc id, action)
 * counts only. Never carries query text or user/session identifiers.
 */
export interface FeedbackRequest {
  doc_id: string;
  action: string;
}

/** POST /api/v1/feedback acknowledgement. */
export interface FeedbackResponse {
  doc_id: string;
  action: string;
  count: number;
}

/** One queued URL in the frontier dashboard view. */
export interface FrontierQueueEntry {
  url: string;
  depth: number;
  status: string;
  /** Dequeue precedence (higher first); absent on pre-priority backends. */
  priority?: number;
}

/** Latest crawl checkpoint summary. */
export interface FrontierCheckpoint {
  phase: string;
  pages_attempted: number;
  pages_indexed: number;
}

/** One newly discovered URL surfaced by describe_refresh. */
export interface FrontierNewUrl {
  url: string;
  status: string;
}

/** Cumulative crawler intake counters from one worker heartbeat. */
export interface FrontierStats {
  at: number;
  pages: number;
  tokens: number;
  errors: number;
}

/** One worker's last discovery-pass summary (feeds + refresh). */
export interface FrontierDiscovery {
  worker_id?: string;
  at: number;
  feeds_checked: number;
  new_urls: number;
  refreshed: number;
}

/** One crawler worker recently heard from by the API replica. */
export interface FrontierWorker {
  worker_id: string;
  crawl_id: string;
  last_seen: number;
  claimed: number;
  status: string;
  role?: string;
  discovery?: FrontierDiscovery | null;
  stats?: FrontierStats | null;
  pages_per_min?: number | null;
  tokens_per_min?: number | null;
}

/** GET /api/v1/frontier/workers response body. */
export interface FrontierWorkersResponse {
  workers: FrontierWorker[];
  worker_count: number;
}

/** One user-requested crawl URL (`crawl_requests` row). */
export interface CrawlRequest {
  id: number | null;
  url: string;
  status: 'pending' | 'approved' | 'declined' | 'duplicate';
  created_at: string | null;
  decided_at: string | null;
}

/** Admin review queue for user-requested crawl URLs. */
export interface CrawlRequestList {
  requests: CrawlRequest[];
  total: number;
}

/** GET /api/v1/frontier/status response body. */
export interface FrontierStatus {
  paused: boolean;
  queue: FrontierQueueEntry[];
  queue_depth: number;
  checkpoint: FrontierCheckpoint;
  new_urls: FrontierNewUrl[];
  /** Live workers; absent on pre-worker backends (read via reader). */
  workers?: FrontierWorker[];
  /** POST /api/v1/frontier/clear echo; absent or zero elsewhere. */
  cleared_queued?: number;
  /** POST /api/v1/frontier/clear echo; absent or false elsewhere. */
  kafka_purged?: boolean;
  /** Whether the serving config enables the Kafka queue. */
  kafka_enabled?: boolean;
  /** Best-effort consumer-group lag for the crawl topic (null when unknown). */
  kafka_lag_total?: number | null;
  /** Lag broken down per partition (null when unknown). */
  kafka_lag_partitions?: number | null;
  /** Cluster-map version the API replica serves. */
  cluster_config_version?: number;
  /** Latest discovery-pass summary (null when no pass ran yet). */
  discovery?: FrontierDiscovery | null;
}

/** Admin analytics overview: durable per-name counts over a window. */
export interface AnalyticsCounts {
  days: number;
  total: number;
  by_name: Record<string, number>;
}

/** Per-document feedback rollup for the admin summary. */
export interface FeedbackDocSummary {
  doc_id: string;
  total: number;
  helpful: number;
  not_helpful: number;
  reports: number;
  top_reason?: string | null;
}

/** Admin feedback overview: totals plus the noisiest documents. */
export interface FeedbackSummary {
  total: number;
  by_action: Record<string, number>;
  by_reason: Record<string, number>;
  top_docs: FeedbackDocSummary[];
}

/** One failed URL with its short terminal reason. */
export interface FrontierFailedUrl {
  url: string;
  reason?: string | null;
  attempts?: number | null;
}

/** Paginated failed-URL list response. */
export interface FrontierFailedList {
  urls: FrontierFailedUrl[];
  total: number;
  limit: number;
  offset: number;
}

/** One failed host with its failure count and most common reason. */
export interface FrontierFailedDomain {
  domain: string;
  failed: number;
  top_reason: string;
}

/** Paginated failed-domain rollup response. */
export interface FrontierFailedDomainList {
  domains: FrontierFailedDomain[];
  total: number;
  limit: number;
  offset: number;
}

/** Result of a domain-scoped failed-queue action. */
export interface FrontierDomainsResult {
  domains: string[];
  affected: number;
}

/** One triaged host: its top failure reason and the action taken. */
export interface FrontierTriageEntry {
  domain: string;
  top_reason: string;
  action: string;
  affected: number;
  wait_s: number;
}

/** Outcome of a failed-domains auto-triage pass. */
export interface FrontierTriageResult {
  triaged: FrontierTriageEntry[];
  dry_run: boolean;
}

/** One queued host with its queued-URL count and top queued priority. */
export interface FrontierQueuedDomain {
  domain: string;
  queued: number;
  top_priority: number;
}

/** Paginated queued-domain rollup response. */
export interface FrontierQueuedDomainList {
  domains: FrontierQueuedDomain[];
  total: number;
  limit: number;
  offset: number;
}

/** One top prop value with its count. */
export interface AnalyticsTopEntry {
  value: string;
  count: number;
}

/** Top-values breakdown for one analytics event prop. */
export interface AnalyticsTop {
  name: string;
  prop: string;
  days: number;
  total: number;
  top: AnalyticsTopEntry[];
}

/** One throughput bucket: ISO start plus event count. */
export interface AnalyticsSeriesPoint {
  t: string;
  count: number;
}

/** Per-bucket event counts over a window (throughput). */
export interface AnalyticsSeries {
  name: string;
  bucket: string;
  days: number;
  points: AnalyticsSeriesPoint[];
}

/** Frontier crawl settings (PUT body is strict: unknown keys rejected). */
export interface FrontierSettings {
  max_pages: number;
  max_queued_urls: number;
  crawl_delay_s: number;
  default_max_depth: number;
  respect_robots: boolean;
  user_agent: string;
  /** Domains whose queued URLs workers skip (exact host or subdomain). */
  skip_domains: string[];
  /**
   * Allowlist: when non-empty, workers fetch only these domains.
   * The same-domain-only switch; empty means all domains allowed.
   */
  allow_domains: string[];
  /** Domains workers never fetch (exact host or subdomain). */
  deny_domains: string[];
  /** Feed/sitemap URLs the discovery pass polls for new URLs. */
  discovery_feeds: string[];
  /** Done pages older than this are requeued (days; 0 disables). */
  refresh_after_days: number;
  /** Seconds between worker discovery passes (0 disables). */
  discovery_interval_s: number;
}

/** GET/PUT /api/v1/frontier/settings response body. */
export interface FrontierSettingsResponse {
  settings: FrontierSettings;
}

/**
 * DB-backed admin config document (`GET /api/v1/admin/config/{name}`).
 * Mirrors `AdminConfigDocResponseSchema`: `body` is the effective
 * document (DB row wins once written, YAML seed otherwise, list-form
 * allowed for `plugins:`); `source` names the winning layer;
 * `version`/`updated_at` are row audit fields (0/`null` on the file
 * seed). The client degrades to `null` on 404/transport failure so
 * editors show file defaults instead.
 */
export type AdminConfigName = 'models' | 'providers' | 'ranking' | 'query' | 'plugins' | 'indexing';

/** One admin config document with versioning metadata. */
export interface AdminConfigDoc {
  /** Doc key (`models`/`providers`/`ranking`/`query`/`plugins`/`indexing`). */
  doc: string;
  body: Record<string, unknown> | unknown[];
  version: number;
  updated_at: string | null;
  source: 'db' | 'file';
}

/** Machine-readable error code plus a human-readable message. */
export interface ErrorDetail {
  code: string;
  message: string;
}

/** Consistent error envelope used for every API failure. */
export interface ErrorEnvelope {
  error: ErrorDetail;
}

/** Either a successful search response or an error envelope. */
export type SearchApiPayload = SearchResponse | ErrorEnvelope;
