"""HTTP schemas for the OpenLens search API.

Pydantic lives only in this package (per the shared build context):
every other layer uses the dataclasses in
`openlens.core.interfaces`. These schemas validate the wire format and
convert to/from those core shapes at the boundary.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from openlens.core.interfaces import SearchResponse as CoreResponse
from openlens.core.interfaces import SearchResult as CoreResult

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

    @classmethod
    def from_core(cls, result: CoreResult) -> "SearchResultSchema":
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
            metadata=dict(result.metadata),
        )


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
        )


class FrontierQueueEntrySchema(BaseModel):
    """One queued URL in the frontier dashboard view."""

    url: str
    depth: int = Field(ge=0)
    status: str = "queued"


class FrontierCheckpointSchema(BaseModel):
    """Latest crawl checkpoint summary."""

    phase: str = "idle"
    pages_attempted: int = Field(default=0, ge=0)
    pages_indexed: int = Field(default=0, ge=0)


class FrontierNewUrlSchema(BaseModel):
    """One newly discovered URL surfaced by describe_refresh."""

    url: str
    status: str = "added"


class FrontierWorkerSchema(BaseModel):
    """One crawler worker recently heard from by this API replica."""

    worker_id: str
    crawl_id: str = "default"
    last_seen: float = 0.0
    claimed: int = Field(default=0, ge=0)
    status: str = "alive"


class FrontierWorkerHeartbeatSchema(BaseModel):
    """POST /api/v1/frontier/workers/heartbeat body (strict)."""

    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(min_length=1, max_length=256)
    crawl_id: str = Field(default="default", max_length=256)
    claimed: int = Field(default=0, ge=0)
    status: str = Field(default="alive", max_length=64)


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


class FrontierSettingsSchema(BaseModel):
    """Frontier crawl settings (strict: unknown keys rejected)."""

    model_config = ConfigDict(extra="forbid")

    max_pages: int = Field(default=100, ge=1)
    max_queued_urls: int = Field(default=1000, ge=1)
    crawl_delay_s: float = Field(default=0.5, ge=0)
    default_max_depth: int = Field(default=3, ge=0)
    respect_robots: bool = True


class FrontierSettingsResponseSchema(BaseModel):
    """GET/PUT /api/v1/frontier/settings response body."""

    settings: FrontierSettingsSchema


class FrontierResumeSchema(BaseModel):
    """POST /api/v1/frontier/resume body: optional replacement seeds."""

    model_config = ConfigDict(extra="forbid")

    seeds: list[str] = Field(default_factory=list)


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


class ErrorDetailSchema(BaseModel):
    """Machine-readable error code plus a human-readable message."""

    code: str
    message: str


class ErrorSchema(BaseModel):
    """Consistent error envelope used for every API failure."""

    error: ErrorDetailSchema
