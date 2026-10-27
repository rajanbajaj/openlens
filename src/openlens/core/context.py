"""Shared per-request pipeline state.

SearchContext flows through every pipeline stage. Providers seed it with raw
hits; plugins mutate it (filter, rescore, enrich); the engine assembles the
final SearchResponse from it, including diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from openlens.core.interfaces import PipelineStage, SearchRequest, SearchResult


@dataclass
class PluginExecution:
    """Record of one plugin's execution for diagnostics."""

    plugin_name: str
    stage: str
    status: str  # "ok" | "skipped" | "failed"
    latency_ms: float = 0.0
    error: Optional[str] = None


@dataclass
class SearchContext:
    """Mutable state shared across pipeline stages for one request."""

    request: SearchRequest
    results: list[SearchResult] = field(default_factory=list)
    applied_filters: list[str] = field(default_factory=list)
    plugin_executions: list[PluginExecution] = field(default_factory=list)
    provider_latencies_ms: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    fallback_used: bool = False

    def record_provider_latency(self, provider_name: str, latency_ms: float) -> None:
        self.provider_latencies_ms[provider_name] = latency_ms

    def record_plugin_execution(
        self,
        plugin_name: str,
        stage: PipelineStage | str,
        status: str,
        latency_ms: float = 0.0,
        error: Optional[str] = None,
    ) -> None:
        stage_name = stage.value if isinstance(stage, PipelineStage) else str(stage)
        self.plugin_executions.append(
            PluginExecution(
                plugin_name=plugin_name,
                stage=stage_name,
                status=status,
                latency_ms=latency_ms,
                error=error,
            )
        )

    def add_error(self, message: str) -> None:
        self.errors.append(message)

    def diagnostics(self) -> dict[str, Any]:
        """Diagnostics payload embedded in SearchResponse."""
        return {
            "fallback_used": self.fallback_used,
            "provider_latencies_ms": dict(self.provider_latencies_ms),
            "plugin_executions": [
                {
                    "plugin": e.plugin_name,
                    "stage": e.stage,
                    "status": e.status,
                    "latency_ms": e.latency_ms,
                    "error": e.error,
                }
                for e in self.plugin_executions
            ],
            "errors": list(self.errors),
        }
