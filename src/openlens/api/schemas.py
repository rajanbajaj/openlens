"""HTTP schemas for the OpenLens search API.

Pydantic lives only in this package (per the shared build context):
every other layer uses the dataclasses in
`openlens.core.interfaces`. These schemas validate the wire format and
convert to/from those core shapes at the boundary.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

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


class ErrorDetailSchema(BaseModel):
    """Machine-readable error code plus a human-readable message."""

    code: str
    message: str


class ErrorSchema(BaseModel):
    """Consistent error envelope used for every API failure."""

    error: ErrorDetailSchema
