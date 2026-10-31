/**
 * TypeScript mirror of the OpenLens HTTP contract in
 * `src/openlens/api/schemas.py` (`POST /api/v1/search`).
 *
 * Mirrored by hand, never imported from the backend: if `schemas.py`
 * changes, these types must be re-mirrored to match.
 */

/** Supported search modes (plan contract; backend validates non-blank). */
export type SearchMode = 'default' | 'child-safe' | 'news' | 'shopping' | 'travel' | 'agent';

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

/**
 * One agentic-planning step inside an agent trace. Every field is
 * optional so malformed entries degrade instead of crashing; the
 * reader keeps entries with at least one usable string field.
 */
export interface AgentTraceStep {
  /** Step kind (e.g. "rewrite", "retrieve", "rerank"); absent when unreported. */
  kind?: string;
  /** Human-readable step detail; absent when unreported. */
  detail?: string;
}

/**
 * Agentic-mode planning trace (`metadata.agent_trace` on the search
 * response envelope). Every field is optional: the backend omits the
 * whole key outside agent mode, and partial traces render only the
 * fields present.
 */
export interface AgentTrace {
  /** Query rewritten by the agent planner; absent when unreported. */
  rewritten_query?: string;
  /** Filters the agent applied; absent when unreported. */
  filters?: Record<string, unknown>;
  /** Ordered planning/execution steps; absent when unreported. */
  steps?: AgentTraceStep[];
  /** Fallback notices (e.g. degraded providers); absent when unreported. */
  fallbacks?: string[];
  /** Model call count; absent when unreported. */
  model_calls?: number;
}

/** Opaque top-level response metadata; new keys degrade to absent. */
export interface SearchResponseMetadata {
  /** Agentic-mode trace; absent outside agent mode or on older envelopes. */
  agent_trace?: AgentTrace;
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
  /** Opaque response metadata (carries `agent_trace` in agent mode). */
  metadata?: SearchResponseMetadata;
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

/**
 * URL-frontier shapes live in the admin app now: the dashboard moved
 * out of the user frontend. The backend `/api/v1/frontier/*` routes
 * and their `X-Admin-Token` mutation gate are unchanged.
 */

/** One turn of chat history sent with `POST /api/v1/agent/chat`. */
export interface ChatHistoryTurn {
  role: 'user' | 'assistant';
  content: string;
}

/** `POST /api/v1/agent/chat` request body. */
export interface ChatRequest {
  /** 1-1000 chars, must not be blank. */
  message: string;
  /** Prior turns, oldest first; capped at 10 turns. */
  history: ChatHistoryTurn[];
  /** Retrieved-hit cap (1-10); defaults to 5. */
  limit: number;
}

/** One source-attribution chip on a chat answer (`ref` is 1-based). */
export interface ChatCitation {
  ref: number;
  title: string;
  url: string | null;
}

/**
 * Planning trace echoed on the chat response. Mirrors {@link AgentTrace}:
 * every field is optional at render time so partial traces degrade
 * instead of crashing; readers keep only the fields present.
 */
export type ChatTrace = AgentTrace;

/** `POST /api/v1/agent/chat` response body. */
export interface ChatResponse {
  /** Grounded answer text; empty when `notice` is set (fail-closed). */
  answer: string;
  /** Source-attribution chips (`ref` is 1-based). */
  citations: ChatCitation[];
  /** Retrieved hits backing the answer, capped by `limit`. */
  results: SearchResult[];
  /** Planning trace; every field optional at render. */
  trace: ChatTrace;
  /** Set when the model was disabled or errored; null otherwise. */
  notice: string | null;
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
