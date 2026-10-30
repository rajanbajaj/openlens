"""OpenLens core contracts.

This module is the single source of truth for the shared data model, the
provider/plugin interfaces, the pipeline stages, the error taxonomy, and the
Engine signatures. Every provider, plugin, ranking, retrieval, and API module
codes against these shapes; do not invent parallel models.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # avoids a runtime cycle with core/context.py
    from openlens.core.context import SearchContext


# ---------------------------------------------------------------------------
# Pipeline stages (the spec's 10-stage pipeline)
# ---------------------------------------------------------------------------


class PipelineStage(str, Enum):
    """Ordered stages of the OpenLens search pipeline."""

    QUERY_PARSING = "query_parsing"
    QUERY_REWRITE = "query_rewrite"
    RETRIEVAL = "retrieval"
    NORMALIZATION = "normalization"
    DEDUPLICATION = "deduplication"
    FILTERING = "filtering"
    SCORING = "scoring"
    RANKING = "ranking"
    ENRICHMENT = "enrichment"
    EXPLANATION = "explanation"

    @classmethod
    def ordered(cls) -> list["PipelineStage"]:
        """Return all stages in execution order."""
        return list(cls)


class PluginType(str, Enum):
    """Kind of work a SearchPlugin performs."""

    FILTER = "filter"
    SCORER = "scorer"
    RANKER = "ranker"
    ENRICHER = "enricher"


# ---------------------------------------------------------------------------
# Data model (dataclasses only; no Pydantic outside api/)
# ---------------------------------------------------------------------------


@dataclass
class Document:
    """A document stored in / returned from an index."""

    id: str
    title: str
    content: str
    url: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderPrefs:
    """Per-query provider preferences (federated selection hints)."""

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    primary: Optional[str] = None


@dataclass
class SearchRequest:
    """A search request as issued to the Engine or a provider.

    ``history`` carries opt-in request-scoped prior queries (oldest
    first) for context-aware expansion. It is never persisted: the
    engine holds it only for the life of the request and no store,
    log, or diagnostics payload may retain the raw strings beyond
    the bounded derived terms.
    """

    query: str
    mode: str = "default"
    limit: int = 10
    offset: int = 0
    filters: dict[str, Any] = field(default_factory=dict)
    provider_prefs: ProviderPrefs = field(default_factory=ProviderPrefs)
    history: tuple[str, ...] = ()


@dataclass
class SearchResult:
    """One normalized search hit.

    `original_score` is the raw provider signal; `final_score` is the
    OpenLens-ranked score. `reasons` explains the ranking in human-readable
    form; `warnings` carries non-fatal annotations (e.g. trust warnings).
    """

    id: str
    title: str
    snippet: str
    original_score: float
    final_score: float
    url: Optional[str] = None
    provider: str = ""
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResponse:
    """Normalized API-facing response for one search."""

    query: str
    mode: str
    results: list[SearchResult] = field(default_factory=list)
    total: int = 0
    applied_filters: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------


class OpenLensError(Exception):
    """Base class for all OpenLens errors."""


class ProviderError(OpenLensError):
    """Raised by SearchProviders instead of leaking raw client errors.

    Wraps timeouts, auth failures, and rate limits. Carries the originating
    provider name and whether the caller may retry.
    """

    TIMEOUT = "timeout"
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    UNAVAILABLE = "unavailable"
    INVALID_REQUEST = "invalid_request"
    UNKNOWN = "unknown"

    def __init__(
        self,
        message: str,
        provider_name: str = "",
        retryable: bool = False,
        code: str = UNKNOWN,
    ) -> None:
        super().__init__(message)
        self.provider_name = provider_name
        self.retryable = retryable
        self.code = code

    @classmethod
    def timeout(
        cls, provider_name: str, message: str = "provider timed out"
    ) -> "ProviderError":
        return cls(
            message, provider_name=provider_name, retryable=True, code=cls.TIMEOUT
        )

    @classmethod
    def auth(
        cls, provider_name: str, message: str = "provider authentication failed"
    ) -> "ProviderError":
        return cls(message, provider_name=provider_name, retryable=False, code=cls.AUTH)

    @classmethod
    def rate_limit(
        cls, provider_name: str, message: str = "provider rate limit exceeded"
    ) -> "ProviderError":
        return cls(
            message, provider_name=provider_name, retryable=True, code=cls.RATE_LIMIT
        )


class PluginError(OpenLensError):
    """Raised by SearchPlugins on failure.

    Mandatory-filter failures propagate (fail closed); optional-plugin
    failures are caught by the pipeline, logged, and recorded in
    SearchContext diagnostics.
    """

    def __init__(
        self,
        message: str,
        plugin_name: str = "",
        stage: Optional[PipelineStage] = None,
        mandatory: bool = False,
    ) -> None:
        super().__init__(message)
        self.plugin_name = plugin_name
        self.stage = stage
        self.mandatory = mandatory


class ConfigError(OpenLensError, ValueError):
    """Raised when configuration is missing, invalid, or fails validation.

    Unresolved `${ENV_VAR}` secrets fail startup validation with this error.
    """


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------


class SearchProvider(ABC):
    """Plug-and-play search backend (local custom index)."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique provider name used in config and diagnostics."""
        raise NotImplementedError

    @abstractmethod
    def configure(self, config: dict[str, Any]) -> None:
        """Apply this provider's config section. Raise ConfigError if invalid."""
        raise NotImplementedError

    @abstractmethod
    def search(self, request: SearchRequest) -> list[SearchResult]:
        """Run the request against this backend.

        Returns normalized hits with `original_score` set (`final_score`
        initialized to the same value; ranking happens downstream). May raise
        ProviderError; must never leak raw client exceptions.
        """
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> bool:
        """Return True when this backend is reachable and usable."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Plugin interface
# ---------------------------------------------------------------------------


class SearchPlugin(ABC):
    """One unit of filtering, scoring, ranking, or enrichment work."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique plugin name used in config and diagnostics."""
        raise NotImplementedError

    @property
    @abstractmethod
    def plugin_type(self) -> PluginType:
        """What kind of work this plugin performs."""
        raise NotImplementedError

    @property
    @abstractmethod
    def stage(self) -> PipelineStage:
        """The pipeline stage this plugin runs in."""
        raise NotImplementedError

    @property
    def config_schema(self) -> dict[str, Any]:
        """JSON-schema-like description of this plugin's config. Empty = any."""
        return {}

    @property
    def dependencies(self) -> tuple[str, ...]:
        """Names of plugins that must run before this one."""
        return ()

    @property
    def mandatory(self) -> bool:
        """True = failure propagates (fail closed, e.g. safety filters)."""
        return False

    @abstractmethod
    def configure(self, config: dict[str, Any]) -> None:
        """Apply this plugin's config section. Raise ConfigError if invalid."""
        raise NotImplementedError

    @abstractmethod
    def run(self, context: "SearchContext") -> "SearchContext":
        """Execute this plugin against the pipeline context.

        May mutate and return the context. Raise PluginError on failure.
        """
        raise NotImplementedError


# Engine public interface: the signatures the API layer codes against.


class Engine(ABC):
    """Orchestrates providers and the plugin pipeline for a search request."""

    @abstractmethod
    def configure(self, config: dict[str, Any]) -> None:
        """Apply engine config (providers, plugins, ranking).

        Raise ConfigError if invalid.
        """
        raise NotImplementedError

    @abstractmethod
    def search(self, request: SearchRequest) -> SearchResponse:
        """Run the full pipeline for one request and return normalized results."""
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> dict[str, bool]:
        """Return per-provider health keyed by provider name."""
        raise NotImplementedError
