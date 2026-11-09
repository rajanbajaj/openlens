"""HTTP schemas for the OpenLens search API.

Pydantic lives only in this package (per the shared build context):
every other layer uses the dataclasses in
`openlens.core.interfaces`. These schemas validate the wire format and
convert to/from those core shapes at the boundary.
"""

from __future__ import annotations

from typing import Annotated, Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from openlens.core.interfaces import SearchResponse as CoreResponse
from openlens.core.interfaces import SearchResult as CoreResult
from openlens.indexing.crawler import DEFAULT_USER_AGENT

MAX_LIMIT = 100
DEFAULT_LIMIT = 10
MAX_QUERY_LENGTH = 1000


class ProviderPrefsSchema(BaseModel):
    """Optional per-query provider preferences on the wire."""

    include: tuple[str, ...] = Field(default_factory=tuple)
    exclude: tuple[str, ...] = Field(default_factory=tuple)
    primary: Optional[str] = None


class SearchRequestSchema(BaseModel):
    """POST /api/v1/search request body."""

    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    mode: str = Field(default="default", min_length=1, max_length=64)
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    offset: int = Field(default=0, ge=0)
    provider_prefs: Optional[ProviderPrefsSchema] = None
    # Opt-in request-scoped history (oldest first) for context-aware
    # expansion. Never persisted server-side beyond the request; the
    # backend honors it only when `query.context.enabled` is set.
    history: list[str] = Field(default_factory=list, max_length=20)
    # Free-form filters passed through to the engine. The `prefer_` prefix
    # is reserved for advisory scoring boosts (never exclusion):
    # `prefer_domains: list[str]` (exact-or-subdomain match) and
    # `prefer_topics: list[str]` (case-insensitive token match) feed the
    # preference scorer; exclusion syntax belongs to filter-pack.
    filters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("query")
    @classmethod
    def query_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value

    @field_validator("mode")
    @classmethod
    def mode_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("mode must not be blank")
        return value

    @field_validator("history")
    @classmethod
    def history_entries_must_fit(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for entry in value:
            if not isinstance(entry, str):
                raise ValueError("history entries must be strings")
            text = entry.strip()
            if not text:
                continue
            if len(text) > MAX_QUERY_LENGTH:
                raise ValueError(
                    f"history entries must be at most {MAX_QUERY_LENGTH} characters"
                )
            cleaned.append(text)
        return cleaned


class SearchResultSchema(BaseModel):
    """One normalized search hit on the wire."""

    id: str
    title: str
    snippet: str
    original_score: float
    final_score: float
    url: Optional[str] = None
    provider: str = ""
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Domain grouping (retrieval/domain_groups.py): members folded into one
    # entry per website. All default to "ungrouped" so old readers that
    # ignore unknown fields keep working; only grouped entries carry them.
    group_count: int = 0
    group_summary: Optional[str] = None
    group_members: list["SearchResultSchema"] = Field(default_factory=list)

    @classmethod
    def from_core(cls, result: CoreResult) -> "SearchResultSchema":
        metadata = dict(result.metadata) if isinstance(result.metadata, dict) else {}
        members: list["SearchResultSchema"] = []
        raw_members = metadata.get("group_members")
        if isinstance(raw_members, list):
            for entry in raw_members[:10]:
                if not isinstance(entry, dict):
                    continue
                url = entry.get("url")
                members.append(
                    cls(
                        id=str(entry.get("id", "")),
                        title=str(entry.get("title", "")),
                        snippet=str(entry.get("snippet", "")),
                        original_score=result.original_score,
                        final_score=result.final_score,
                        url=url if isinstance(url, str) else None,
                        provider=str(entry.get("provider", "")),
                    )
                )
        raw_count = metadata.get("group_count")
        group_count = (
            raw_count
            if isinstance(raw_count, int) and not isinstance(raw_count, bool)
            else (len(members) + 1 if members else 0)
        )
        summary = metadata.get("group_summary")
        return cls(
            id=result.id,
            title=result.title,
            snippet=result.snippet,
            original_score=result.original_score,
            final_score=result.final_score,
            url=result.url,
            provider=result.provider,
            reasons=list(result.reasons),
            warnings=list(result.warnings),
            metadata=metadata,
            group_count=group_count,
            group_summary=summary if isinstance(summary, str) and summary else None,
            group_members=members,
        )


SearchResultSchema.model_rebuild()


class DiagnosticsSchema(BaseModel):
    """Per-request diagnostics embedded in the search response."""

    fallback_used: bool = False
    provider_latencies_ms: dict[str, float] = Field(default_factory=dict)
    provider_result_counts: dict[str, int] = Field(default_factory=dict)
    provider_costs_estimated: dict[str, float] = Field(default_factory=dict)
    plugin_executions: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    query: dict[str, Any] = Field(default_factory=dict)
    request_id: str = ""
    cache: dict[str, str] = Field(default_factory=dict)
    # Agentic search trace (mode="agent" only; {} otherwise):
    # {rewritten_query, filters, steps, fallbacks, model_calls}.
    agent_trace: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_core(cls, diagnostics: dict[str, Any]) -> "DiagnosticsSchema":
        query_entry = diagnostics.get("query", {})
        return cls(
            fallback_used=bool(diagnostics.get("fallback_used", False)),
            provider_latencies_ms=dict(diagnostics.get("provider_latencies_ms", {})),
            provider_result_counts=dict(diagnostics.get("provider_result_counts", {})),
            provider_costs_estimated=dict(
                diagnostics.get("provider_costs_estimated", {})
            ),
            plugin_executions=list(diagnostics.get("plugin_executions", [])),
            errors=list(diagnostics.get("errors", [])),
            query=dict(query_entry) if isinstance(query_entry, dict) else {},
            agent_trace=(
                dict(diagnostics["agent_trace"])
                if isinstance(diagnostics.get("agent_trace"), dict)
                else {}
            ),
            request_id=str(diagnostics.get("request_id", "")),
            cache={
                str(key): str(value)
                for key, value in dict(diagnostics.get("cache", {})).items()
            },
        )


def compute_next_offset(total: int, limit: int, offset: int) -> Optional[int]:
    """Return the next-page offset, or None when this page reaches the end."""
    candidate = offset + limit
    return candidate if candidate < total else None


class SuggestResponseSchema(BaseModel):
    """GET /api/v1/suggest response body: prefix completions for `q`."""

    q: str
    suggestions: list[str] = Field(default_factory=list)


class PerspectiveStanceSchema(BaseModel):
    """One member's stance toward the story claim (keyword heuristic)."""

    source_id: str = ""
    stance: str = "neutral"
    evidence: str = ""


class PerspectiveViewSchema(BaseModel):
    """One story view: claim, member stances, and honesty notes."""

    story_id: str = ""
    claim: str = ""
    representative_id: str = ""
    member_ids: list[str] = Field(default_factory=list)
    stances: list[PerspectiveStanceSchema] = Field(default_factory=list)
    supports: int = Field(default=0, ge=0)
    refutes: int = Field(default=0, ge=0)
    neutral: int = Field(default=0, ge=0)
    multi_source: bool = False
    missing_note: Optional[str] = None
    trust_caveats: list[str] = Field(default_factory=list)

    @classmethod
    def from_mapping(cls, raw: Any) -> "PerspectiveViewSchema":
        """Coerce a builder dict; malformed entries degrade to empty."""
        if not isinstance(raw, dict):
            return cls()
        stances: list[PerspectiveStanceSchema] = []
        for entry in raw.get("stances", []) or []:
            if not isinstance(entry, dict):
                continue
            stance = entry.get("stance", "")
            stances.append(
                PerspectiveStanceSchema(
                    source_id=str(entry.get("source_id", "")),
                    stance=(
                        str(stance)
                        if stance in ("supports", "refutes", "neutral", "unrated")
                        else "neutral"
                    ),
                    evidence=str(entry.get("evidence", "")),
                )
            )
        missing = raw.get("missing_note")
        raw_members = raw.get("member_ids", []) or []
        raw_caveats = raw.get("trust_caveats", []) or []
        return cls(
            story_id=str(raw.get("story_id", "")),
            claim=str(raw.get("claim", "")),
            representative_id=str(raw.get("representative_id", "")),
            member_ids=[str(item) for item in raw_members if isinstance(item, str)],
            stances=stances,
            supports=int(raw.get("supports", 0) or 0),
            refutes=int(raw.get("refutes", 0) or 0),
            neutral=int(raw.get("neutral", 0) or 0),
            multi_source=bool(raw.get("multi_source", False)),
            missing_note=(
                str(missing) if isinstance(missing, str) and missing else None
            ),
            trust_caveats=[str(item) for item in raw_caveats if isinstance(item, str)],
        )


class SearchResponseSchema(BaseModel):
    """POST /api/v1/search response body."""

    query: str
    mode: str
    results: list[SearchResultSchema] = Field(default_factory=list)
    total: int = Field(ge=0)
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    offset: int = Field(default=0, ge=0)
    next_offset: Optional[int] = Field(default=None, ge=0)
    suggestions: list[str] = Field(default_factory=list)
    spelling: Optional[str] = None
    related: list[str] = Field(default_factory=list)
    applied_filters: list[str] = Field(default_factory=list)
    diagnostics: DiagnosticsSchema = Field(default_factory=DiagnosticsSchema)
    perspectives: list[PerspectiveViewSchema] = Field(default_factory=list)

    @classmethod
    def from_core(
        cls,
        response: CoreResponse,
        *,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        suggestions: list[str] | None = None,
        spelling: Optional[str] = None,
        related: list[str] | None = None,
        next_offset: Optional[int] = None,
    ) -> "SearchResponseSchema":
        if next_offset is None:
            next_offset = compute_next_offset(response.total, limit, offset)
        return cls(
            query=response.query,
            mode=response.mode,
            results=[SearchResultSchema.from_core(r) for r in response.results],
            total=response.total,
            limit=limit,
            offset=offset,
            next_offset=next_offset,
            suggestions=list(suggestions) if suggestions is not None else [],
            spelling=spelling,
            related=list(related) if related is not None else [],
            applied_filters=list(response.applied_filters),
            diagnostics=DiagnosticsSchema.from_core(response.diagnostics),
            perspectives=[
                PerspectiveViewSchema.from_mapping(entry)
                for entry in (getattr(response, "perspectives", []) or [])
            ],
        )


class FrontierQueueEntrySchema(BaseModel):
    """One queued URL in the frontier dashboard view."""

    url: str
    depth: int = Field(ge=0)
    status: str = "queued"
    priority: int = 0


class FrontierCheckpointSchema(BaseModel):
    """Latest crawl checkpoint summary."""

    phase: str = "idle"
    pages_attempted: int = Field(default=0, ge=0)
    pages_indexed: int = Field(default=0, ge=0)


class FrontierNewUrlSchema(BaseModel):
    """One newly discovered URL surfaced by describe_refresh."""

    url: str
    status: str = "added"


class FrontierFailedUrlSchema(BaseModel):
    """One failed URL for the admin failed-URL view."""

    url: str
    reason: str = ""
    attempts: int = Field(default=0, ge=0)


class FrontierFailedListSchema(BaseModel):
    """Paginated failed-URL list (HTTP 200 always; empty when unreadable)."""

    urls: list[FrontierFailedUrlSchema] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=0)
    offset: int = Field(default=0, ge=0)


class FrontierFailedDomainSchema(BaseModel):
    """One failed host for the admin failed-domains view."""

    domain: str
    failed: int = Field(default=0, ge=0)
    top_reason: str = ""


class FrontierFailedDomainListSchema(BaseModel):
    """Paginated failed-domain rollup (HTTP 200 always; empty when unreadable)."""

    domains: list[FrontierFailedDomainSchema] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=0)
    offset: int = Field(default=0, ge=0)


class FrontierDomainsActionSchema(BaseModel):
    """Domain-scoped failed-queue action: normalized host list in."""

    domains: list[str] = Field(default_factory=list)


class FrontierQueuedDomainSchema(BaseModel):
    """One queued host for the admin queued-domains view."""

    domain: str
    queued: int = Field(default=0, ge=0)
    top_priority: int = Field(default=0)


class FrontierQueuedDomainListSchema(BaseModel):
    """Paginated queued-domain rollup (HTTP 200 always; empty when unreadable)."""

    domains: list[FrontierQueuedDomainSchema] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=0)
    offset: int = Field(default=0, ge=0)


class FrontierDomainPrioritySchema(BaseModel):
    """Domain-scoped priority bump: normalized host list + delta in."""

    domains: list[str] = Field(default_factory=list)
    delta: int = Field(default=1, ge=-100, le=100)


class FrontierDomainsResultSchema(BaseModel):
    """Result of a domain-scoped failed-queue action."""

    domains: list[str] = Field(default_factory=list)
    affected: int = Field(default=0, ge=0)


class FrontierAutoTriageSchema(BaseModel):
    """Body for the failed-domains auto-triage pass (admin-only)."""

    dry_run: bool = Field(
        default=False,
        description="Classify only; report planned actions without mutating.",
    )


class FrontierTriageEntrySchema(BaseModel):
    """One triaged host: its top failure reason and the action taken."""

    domain: str = ""
    top_reason: str = ""
    action: str = Field(
        default="unhandled",
        description="skip+purge, requeue+wait, skip+block+purge, or unhandled.",
    )
    affected: int = Field(default=0, ge=0)
    wait_s: float = Field(default=0.0, ge=0.0)


class FrontierTriageResultSchema(BaseModel):
    """Outcome of a failed-domains auto-triage pass."""

    triaged: list[FrontierTriageEntrySchema] = Field(default_factory=list)
    dry_run: bool = False


class FrontierDiscoverySchema(BaseModel):
    """A worker's last discovery-pass summary (feeds + refresh).

    ``at`` is a unix-epoch timestamp for when the worker reported the
    summary; ``worker_id`` names the reporting worker (empty on
    per-worker entries where the parent already names it).
    """

    worker_id: str = ""
    at: float = 0.0
    feeds_checked: int = Field(default=0, ge=0)
    new_urls: int = Field(default=0, ge=0)
    refreshed: int = Field(default=0, ge=0)


class FrontierStatsSchema(BaseModel):
    """Cumulative crawler intake counters from one worker heartbeat."""

    at: float = 0.0
    pages: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)


class FrontierWorkerSchema(BaseModel):
    """One crawler worker recently heard from by this API replica."""

    worker_id: str
    crawl_id: str = "default"
    last_seen: float = 0.0
    claimed: int = Field(default=0, ge=0)
    status: str = "alive"
    role: str = "worker"
    discovery: Optional[FrontierDiscoverySchema] = None
    stats: Optional[FrontierStatsSchema] = None
    pages_per_min: Optional[float] = None
    tokens_per_min: Optional[float] = None


class FrontierWorkerHeartbeatSchema(BaseModel):
    """POST /api/v1/frontier/workers/heartbeat body (strict)."""

    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(min_length=1, max_length=256)
    crawl_id: str = Field(default="default", max_length=256)
    claimed: int = Field(default=0, ge=0)
    status: str = Field(default="alive", max_length=64)
    role: str = Field(default="worker", max_length=64)
    # Optional discovery summary (``{"at", "feeds_checked", "new_urls",
    # "refreshed"}``). Typed as free-form so malformed fragments degrade
    # to null instead of failing the heartbeat with 422; the route
    # sanitizes before storing.
    discovery: Any = None
    # Optional cumulative intake counters (``{"at", "pages", "tokens",
    # ``errors"}``). Same free-form contract as discovery.
    stats: Any = None


class FrontierWorkersResponseSchema(BaseModel):
    """GET /api/v1/frontier/workers response body."""

    workers: list[FrontierWorkerSchema] = Field(default_factory=list)
    worker_count: int = Field(default=0, ge=0)


class FrontierStatusSchema(BaseModel):
    """GET /api/v1/frontier/status response body."""

    paused: bool = False
    queue: list[FrontierQueueEntrySchema] = Field(default_factory=list)
    queue_depth: int = Field(ge=0)
    checkpoint: FrontierCheckpointSchema = Field(
        default_factory=FrontierCheckpointSchema
    )
    new_urls: list[FrontierNewUrlSchema] = Field(default_factory=list)
    workers: list[FrontierWorkerSchema] = Field(default_factory=list)
    # POST /api/v1/frontier/clear echoes what the clean removed; other
    # endpoints leave the defaults (nothing cleared, topic untouched).
    cleared_queued: int = Field(default=0, ge=0)
    kafka_purged: bool = False
    # Best-effort consumer-group lag for the crawl topic (None when the
    # broker is unreachable or Kafka is disabled); the scale signal.
    kafka_lag_total: int | None = None
    kafka_lag_partitions: int | None = None
    # Whether the serving config enables the Kafka queue. False means
    # the queue is store-backed and lag is not applicable; True with
    # null lag means the lag probe failed.
    kafka_enabled: bool = False
    cluster_config_version: int = Field(default=0, ge=0)
    # Most recently active worker's discovery summary (None when no
    # worker ever reported one).
    discovery: Optional[FrontierDiscoverySchema] = None


class CrawlRequestSubmitSchema(BaseModel):
    """POST /api/v1/crawl-requests body (public, rate-limited)."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2048)


class CrawlRequestSchema(BaseModel):
    """One stored crawl request; ``duplicate`` is never persisted."""

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    url: str
    status: str
    created_at: str | None = None
    decided_at: str | None = None


class CrawlRequestListSchema(BaseModel):
    """Admin review queue for user-requested crawl URLs."""

    model_config = ConfigDict(extra="forbid")

    requests: list[CrawlRequestSchema] = Field(default_factory=list)
    total: int = 0


class FrontierSettingsSchema(BaseModel):
    """Frontier crawl settings (strict: unknown keys rejected)."""

    model_config = ConfigDict(extra="forbid")

    max_pages: int = Field(default=100, ge=1)
    max_queued_urls: int = Field(default=1000, ge=1)
    crawl_delay_s: float = Field(default=0.5, ge=0)
    default_max_depth: int = Field(default=2, ge=0)
    respect_robots: bool = True
    user_agent: str = Field(
        default=DEFAULT_USER_AGENT,
        min_length=1,
        max_length=256,
        description=(
            "Crawler User-Agent applied live by workers; empty/oversize "
            "values keep the previous agent."
        ),
    )
    skip_domains: list[Annotated[str, Field(min_length=1, max_length=253)]] = Field(
        default_factory=list,
        description=(
            "Domain skip-list applied live by workers; entries are "
            "bare lowercase hostnames (subdomains match too); "
            "malformed entries are ignored, never fatal."
        ),
    )
    allow_domains: list[Annotated[str, Field(min_length=1, max_length=253)]] = Field(
        default_factory=list,
        description=(
            "Domain allowlist applied live by workers: when non-empty, "
            "only these bare lowercase hostnames (subdomains match too) "
            "are fetched — the same-domain-only switch. Empty means all "
            "domains are allowed. Malformed entries are ignored, "
            "never fatal."
        ),
    )
    deny_domains: list[Annotated[str, Field(min_length=1, max_length=253)]] = Field(
        default_factory=list,
        description=(
            "Domain blocklist applied live by workers; entries are "
            "bare lowercase hostnames (subdomains match too); "
            "malformed entries are ignored, never fatal."
        ),
    )
    discovery_feeds: list[Annotated[str, Field(min_length=1, max_length=2000)]] = Field(
        default_factory=list,
        description=(
            "Sitemap/RSS/Atom URLs polled by workers for new articles; "
            "empty disables discovery. Malformed entries are ignored, "
            "never fatal."
        ),
    )
    refresh_after_days: int = Field(
        default=0,
        ge=0,
        description=(
            "Requeue done URLs older than this many days for a conditional "
            "refetch (0 disables the refresh pass)."
        ),
    )
    discovery_interval_s: int = Field(
        default=21600,
        ge=300,
        le=86400,
        description=(
            "Minimum seconds between worker discovery passes (feeds + "
            "refresh). Applies live without restarting workers."
        ),
    )


class FrontierSettingsResponseSchema(BaseModel):
    """GET/PUT /api/v1/frontier/settings response body."""

    settings: FrontierSettingsSchema


#: Settings-row keys carrying the live cluster map and node registry.
#: They bypass ``FrontierSettingsSchema`` (which stays crawl-tuning
#: only) and are managed by the dedicated cluster endpoints below.
CLUSTER_MAP_SETTINGS_KEY = "cluster_map"
KAFKA_NODES_SETTINGS_KEY = "kafka_nodes"


class ClusterMapSchema(BaseModel):
    """Live Kafka reachability for workers (strict PUT body)."""

    model_config = ConfigDict(extra="forbid")

    bootstrap_servers: list[Annotated[str, Field(min_length=1, max_length=256)]] = (
        Field(min_length=1, max_length=64)
    )
    topic: str = Field(min_length=1, max_length=256)
    group_id: str = Field(min_length=1, max_length=256)


class ClusterMapResponseSchema(BaseModel):
    """GET/PUT /api/v1/frontier/cluster-map response body."""

    bootstrap_servers: list[str] = Field(default_factory=list)
    topic: str = "crawl.urls"
    group_id: str = "crawlers"
    config_version: int = Field(default=0, ge=0)
    source: str = "file"


class KafkaNodeSchema(BaseModel):
    """One registered Kafka broker node (admin inventory)."""

    node_id: int = Field(ge=1)
    role: str = "broker"
    status: str = "pending"
    advertise_listener: str = ""
    created_at: float = 0.0


class KafkaNodePrepareSchema(BaseModel):
    """POST /api/v1/frontier/kafka/nodes/prepare body (strict)."""

    model_config = ConfigDict(extra="forbid")

    node_id: int = Field(ge=1, le=100000)
    advertise_listener: str = Field(min_length=1, max_length=256)


class KafkaNodePrepareResponseSchema(BaseModel):
    """Allocated broker identity + join parameters."""

    node_id: int
    cluster_id: str
    controller_quorum: list[str] = Field(default_factory=list)
    status: str = "pending"


class KafkaNodesResponseSchema(BaseModel):
    """GET /api/v1/frontier/kafka/nodes response body."""

    nodes: list[KafkaNodeSchema] = Field(default_factory=list)
    node_count: int = Field(default=0, ge=0)


class KafkaNodeJoinedSchema(BaseModel):
    """POST /api/v1/frontier/kafka/nodes/{id}/joined body (strict)."""

    model_config = ConfigDict(extra="forbid")

    advertise_listener: str = Field(default="", max_length=256)


class RebalancePlanRequestSchema(BaseModel):
    """POST /api/v1/frontier/kafka/rebalance-plan body (strict)."""

    model_config = ConfigDict(extra="forbid")

    topic: str = Field(default="crawl.urls", min_length=1, max_length=256)
    broker_ids: list[Annotated[int, Field(ge=0, le=100000)]] = Field(min_length=1)
    replication_factor: int | None = Field(default=None, ge=1)


class RebalanceAssignmentSchema(BaseModel):
    """One partition's planned replica set (reassign-partitions shape)."""

    topic: str
    partition: int
    replicas: list[int]


class RebalancePlanResponseSchema(BaseModel):
    """Even-spread replica plan + the tool command that applies it."""

    topic: str
    partitions: int
    replication_factor: int
    broker_ids: list[int]
    assignment: list[RebalanceAssignmentSchema]
    tool_hint: str


class FrontierResumeSchema(BaseModel):
    """POST /api/v1/frontier/resume body: optional replacement seeds.

    `priority` applies to every seed in the call: higher values dequeue
    first (`ORDER BY priority DESC, depth ASC`). Omitted/`None` keeps
    whatever the store already has (``0`` for new rows). Negative
    values deprioritize below the default.
    """

    model_config = ConfigDict(extra="forbid")

    seeds: list[str] = Field(default_factory=list)
    priority: int | None = Field(default=None)

    @field_validator("priority")
    @classmethod
    def priority_must_be_int(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("priority must be an integer")
        return value


class FrontierSeedsSchema(BaseModel):
    """POST /api/v1/frontier/seeds body: URLs to enqueue, nothing else.

    Same seed shape as resume, but enqueue-only: no phase change, no
    in-memory queue replacement, no interruption to running crawlers —
    seeds go to the durable queue (Kafka, store fallback) and refetch
    even when already finished once.
    """

    model_config = ConfigDict(extra="forbid")

    seeds: list[str] = Field(default_factory=list)
    priority: int | None = Field(default=None)
    depth: int | None = Field(default=None)

    @field_validator("priority")
    @classmethod
    def priority_must_be_int(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("priority must be an integer")
        return value

    @field_validator("depth")
    @classmethod
    def depth_must_be_whole(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("depth must be a whole number (0 or more)")
        return value


class FrontierEnqueueSeedsResponse(BaseModel):
    """POST /api/v1/frontier/seeds response: seeds accepted to the queue."""

    enqueued: int
    skipped_over_cap: int = Field(
        default=0,
        ge=0,
        description=(
            "Genuinely new seeds refused because the queue already sits at "
            "max_queued_urls. Re-pastes of known URLs still count as "
            "enqueued; only growth past the cap is reported here."
        ),
    )


class FrontierUrlInspectorFrontierSchema(BaseModel):
    """Queue row for one inspected URL (None when never seen)."""

    status: str
    priority: int = 0
    attempts: int = Field(default=0, ge=0)
    last_error: str | None = None


class FrontierUrlInspectorDocumentSchema(BaseModel):
    """Indexed document for one inspected URL (None when not indexed)."""

    title: str
    chars: int = Field(ge=0)


class FrontierUrlInfoSchema(BaseModel):
    """GET /api/v1/frontier/url-info response: one URL's crawl story.

    Combines the frontier queue row, the indexed document, and the
    extracted-link fan-out so the admin dashboard can answer "did my
    seed work?" without joining three views by hand.
    """

    url: str
    normalized_url: str
    frontier: FrontierUrlInspectorFrontierSchema | None = None
    document: FrontierUrlInspectorDocumentSchema | None = None
    extracted_links: int = Field(default=0, ge=0)
    link_sample: list[str] = Field(default_factory=list)


class AdminConfigPutSchema(BaseModel):
    """PUT /api/v1/admin/config/{doc} body: one config doc section.

    The wrapped mapping (or list, for the list-form ``plugins:``
    section) is validated by the existing startup validators
    (models/providers/ranking/query/plugins); anything the boot path
    would reject fails with 422. Extra top-level keys are rejected so no
    adjacent sections can be smuggled into a single-doc write.
    """

    model_config = ConfigDict(extra="forbid")

    body: dict[str, Any] | list[Any]


class AdminConfigDocResponseSchema(BaseModel):
    """GET/PUT /api/v1/admin/config/{doc} response body.

    ``source`` names the winning layer: ``db`` when a row was written,
    ``file`` for the YAML-backed fallback. ``version``/``updated_at``
    are the row audit fields (version 0 / null when no row exists).
    """

    doc: str
    body: dict[str, Any] | list[Any] = Field(default_factory=dict)
    version: int = Field(default=0, ge=0)
    updated_at: Optional[str] = None
    source: str = "file"


class ChatHistoryTurnSchema(BaseModel):
    """One prior chat turn (oldest first) sent with a chat request."""

    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=1, max_length=16)
    content: str = Field(min_length=1, max_length=1000)

    @field_validator("role")
    @classmethod
    def role_must_be_known(cls, value: str) -> str:
        if value not in ("user", "assistant"):
            raise ValueError("role must be 'user' or 'assistant'")
        return value

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value


#: Bounds for POST /api/v1/agent/chat (see the cross-track contract).
MAX_CHAT_MESSAGE_LENGTH = 1000
MAX_CHAT_HISTORY_TURNS = 10
MAX_CHAT_LIMIT = 10
DEFAULT_CHAT_LIMIT = 5


class ChatRequestSchema(BaseModel):
    """POST /api/v1/agent/chat request body."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=MAX_CHAT_MESSAGE_LENGTH)
    history: list[ChatHistoryTurnSchema] = Field(
        default_factory=list, max_length=MAX_CHAT_HISTORY_TURNS
    )
    limit: int = Field(default=DEFAULT_CHAT_LIMIT, ge=1, le=MAX_CHAT_LIMIT)

    @field_validator("message")
    @classmethod
    def message_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


class ChatCitationSchema(BaseModel):
    """One server-side citation: 1-based ref into the returned hits."""

    ref: int = Field(ge=1)
    title: str
    url: Optional[str] = None


class ChatTraceStepSchema(BaseModel):
    """One step of the chat trace (fixed {kind, detail} shape)."""

    kind: str
    detail: str


class ChatTraceSchema(BaseModel):
    """Per-turn chat trace: rewrite, tool searches, fallbacks, call count."""

    rewritten_query: str = ""
    filters: dict[str, Any] = Field(default_factory=dict)
    steps: list[ChatTraceStepSchema] = Field(default_factory=list)
    fallbacks: list[str] = Field(default_factory=list)
    model_calls: int = Field(default=0, ge=0)
    model: str = ""


class ChatResponseSchema(BaseModel):
    """POST /api/v1/agent/chat response body."""

    answer: str
    citations: list[ChatCitationSchema] = Field(default_factory=list)
    results: list[SearchResultSchema] = Field(default_factory=list)
    trace: ChatTraceSchema = Field(default_factory=ChatTraceSchema)
    notice: Optional[str] = None
    perspectives: list[PerspectiveViewSchema] = Field(default_factory=list)


class ErrorDetailSchema(BaseModel):
    """Machine-readable error code plus a human-readable message."""

    code: str
    message: str


class ErrorSchema(BaseModel):
    """Consistent error envelope used for every API failure."""

    error: ErrorDetailSchema
