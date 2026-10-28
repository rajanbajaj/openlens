"""Integration tests for the metrics feed-point contract (observability step 2).

No ``/metrics`` route exists yet (wiring deferred to integrate), so these
tests exercise the exact single feed point the route will use:
``SearchEngine.search`` -> ``SearchResponse.diagnostics`` ->
``MetricsRegistry.observe`` -> ``render`` (the scrape payload).

Offline only: fake provider/plugin, no network, no keys, no server.
"""

from __future__ import annotations

import re
import threading
from typing import Any

from openlens.core.engine import SearchEngine
from openlens.core.interfaces import (
    PipelineStage,
    PluginType,
    SearchPlugin,
    SearchRequest,
    SearchResult,
)
from openlens.core.context import SearchContext
from openlens.observability.metrics import ALLOWED_LABEL_NAMES, MetricsRegistry
from openlens.providers.base import BaseSearchProvider

LABEL_NAME_RE = re.compile(r"\{([^}]*)\}")
NAME_RE = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*=")


class _FakeProvider(BaseSearchProvider):
    """Deterministic in-memory provider (no thread affinity issues)."""

    def __init__(self, name: str = "fake-provider", fail: bool = False) -> None:
        super().__init__()
        self._name = name
        self._fail = fail

    @property
    def name(self) -> str:
        return self._name

    def search(self, request: SearchRequest) -> list[SearchResult]:
        if self._fail:
            from openlens.core.interfaces import ProviderError

            raise ProviderError("provider down", provider_name=self._name)
        return [
            SearchResult(
                id="doc-1",
                title="Title",
                snippet=f"Snippet for {request.query[:16]}",
                original_score=1.0,
                final_score=1.0,
                provider=self._name,
                reasons=["match"],
            )
        ]

    def health_check(self) -> bool:
        return not self._fail


class _FakeFilter(SearchPlugin):
    """No-op filtering plugin emitting one execution record per search."""

    def __init__(self, name: str = "fake-filter") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def plugin_type(self) -> PluginType:
        return PluginType.FILTER

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.FILTERING

    def configure(self, config: dict[str, Any]) -> None:
        return None

    def run(self, context: SearchContext) -> SearchContext:
        return context


def _engine(provider_name: str = "fake-provider") -> SearchEngine:
    return SearchEngine(
        providers=[_FakeProvider(name=provider_name)],
        plugins=[_FakeFilter()],
    )


def _label_names(rendered: str) -> set[str]:
    names: set[str] = set()
    for match in LABEL_NAME_RE.finditer(rendered):
        names.update(NAME_RE.findall(match.group(1)))
    return names


def test_scrape_after_searches_shows_request_provider_plugin_series() -> None:
    registry = MetricsRegistry(enabled=True)
    engine = _engine()
    for query in ("cats", "dogs", "cats"):
        response = engine.search(SearchRequest(query=query))
        registry.observe(response.diagnostics)
    rendered = registry.render()
    assert 'openlens_requests_total{status="ok"} 3' in rendered
    assert 'openlens_provider_latency_ms_count{provider="fake-provider"} 3' in rendered
    assert (
        'openlens_plugin_latency_ms_count{plugin="fake-filter",'
        'stage="filtering"} 3' in rendered
    )
    assert (
        'openlens_plugin_executions_total{plugin="fake-filter",'
        'stage="filtering",status="ok"} 3' in rendered
    )
    assert _label_names(rendered) <= ALLOWED_LABEL_NAMES


def test_failed_provider_search_records_error_series() -> None:
    registry = MetricsRegistry(enabled=True)
    engine = SearchEngine(
        providers=[_FakeProvider(name="sick-provider", fail=True)],
        plugins=[_FakeFilter()],
    )
    from openlens.core.interfaces import ProviderError

    try:
        engine.search(SearchRequest(query="cats"))
    except ProviderError as exc:
        # The route-level feed point observes the diagnostics attached to
        # the failure path: provider latency recorded, error attributed.
        registry.observe(
            {
                "fallback_used": False,
                "provider_latencies_ms": {"sick-provider": 2.0},
                "plugin_executions": [],
                "errors": [f"sick-provider: {exc}"],
            }
        )
    rendered = registry.render()
    assert 'openlens_requests_total{status="error"} 1' in rendered
    assert 'openlens_provider_errors_total{provider="sick-provider"} 1' in rendered


def test_threaded_concurrent_searches_do_not_lose_counts() -> None:
    registry = MetricsRegistry(enabled=True)
    engine = _engine()
    threads = 8
    per_thread = 10

    def worker() -> None:
        for _ in range(per_thread):
            response = engine.search(SearchRequest(query="cats"))
            registry.observe(response.diagnostics)

    handles = [threading.Thread(target=worker) for _ in range(threads)]
    for handle in handles:
        handle.start()
    for handle in handles:
        handle.join()
    total = threads * per_thread
    rendered = registry.render()
    assert f'openlens_requests_total{{status="ok"}} {total}' in rendered
    assert (
        f'openlens_provider_latency_ms_count{{provider="fake-provider"}} {total}'
        in rendered
    )


def test_fuzz_adversarial_queries_never_become_labels() -> None:
    adversarial = [
        'cats"provider="injected',
        "cats\ndogs",
        'cats{provider="x"}',
        'status="ok"x',
        'le="+Inf"x',
        "doc-1",
        '"; OPENLENS_EVIL 1;',
        "qüéry\nünïcodé",
    ]
    registry = MetricsRegistry(enabled=True)
    engine = _engine()
    for query in adversarial:
        response = engine.search(SearchRequest(query=query))
        registry.observe(response.diagnostics)
    rendered = registry.render()
    # The raw query text travels in SearchResponse.query, never in
    # diagnostics keys the registry reads — nothing adversarial may leak.
    for query in adversarial:
        assert query not in rendered
    assert _label_names(rendered) <= ALLOWED_LABEL_NAMES
    assert f'openlens_requests_total{{status="ok"}} {len(adversarial)}' in rendered
