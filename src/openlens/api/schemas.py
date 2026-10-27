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


class SearchRequestSchema(BaseModel):
    """POST /api/v1/search request body."""

    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    mode: str = Field(default="default", min_length=1, max_length=64)
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    offset: int = Field(default=0, ge=0)
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
    plugin_executions: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @classmethod
    def from_core(cls, diagnostics: dict[str, Any]) -> "DiagnosticsSchema":
        return cls(
            fallback_used=bool(diagnostics.get("fallback_used", False)),
            provider_latencies_ms=dict(diagnostics.get("provider_latencies_ms", {})),
            plugin_executions=list(diagnostics.get("plugin_executions", [])),
            errors=list(diagnostics.get("errors", [])),
        )


class SearchResponseSchema(BaseModel):
    """POST /api/v1/search response body."""

    query: str
    mode: str
    results: list[SearchResultSchema] = Field(default_factory=list)
    total: int = Field(ge=0)
    applied_filters: list[str] = Field(default_factory=list)
    diagnostics: DiagnosticsSchema = Field(default_factory=DiagnosticsSchema)

    @classmethod
    def from_core(cls, response: CoreResponse) -> "SearchResponseSchema":
        return cls(
            query=response.query,
            mode=response.mode,
            results=[SearchResultSchema.from_core(r) for r in response.results],
            total=response.total,
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
