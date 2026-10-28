"""Unit tests for the in-process metrics registry (observability step 2).

Offline only: no network, no API server, no engine. The registry is
fed synthetic ``SearchResponse.diagnostics`` payloads directly.
"""

from __future__ import annotations

import re
import threading
from typing import Any

import pytest

from openlens.observability.metrics import (
    ALLOWED_LABEL_NAMES,
    LATENCY_BUCKETS_MS,
    MetricsRegistry,
    escape_label_value,
    format_labels,
)

LABEL_NAME_RE = re.compile(r"\{([^}]*)\}")
NAME_RE = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*=")


def _diagnostics(
    providers: dict[str, float] | None = None,
    plugins: list[dict[str, Any]] | None = None,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "fallback_used": False,
        "provider_latencies_ms": dict(providers or {"fake-provider": 12.5}),
        "plugin_executions": (
            plugins
            if plugins is not None
            else [
                {
                    "plugin": "fake-filter",
                    "stage": "filtering",
                    "status": "ok",
                    "latency_ms": 0.5,
                    "error": None,
                }
            ]
        ),
        "errors": list(errors or []),
    }


def _label_names(rendered: str) -> set[str]:
    names: set[str] = set()
    for match in LABEL_NAME_RE.finditer(rendered):
        for name in NAME_RE.findall(match.group(1)):
            names.add(name)
    return names


def test_disabled_registry_is_inert_by_default() -> None:
    registry = MetricsRegistry()
    assert registry.enabled is False
    registry.observe(_diagnostics())
    assert registry.render() == ""


def test_disabled_registry_ignores_observations_after_disable() -> None:
    registry = MetricsRegistry(enabled=True)
    registry.observe(_diagnostics())
    assert registry.render() != ""
    registry.disable()
    assert registry.render() == ""
    # Observations while disabled record nothing new.
    registry.observe(_diagnostics())
    registry.enable()
    assert 'openlens_requests_total{status="ok"} 1' in registry.render()


def test_enabled_scrape_shows_request_provider_plugin_series() -> None:
    registry = MetricsRegistry(enabled=True)
    registry.observe(_diagnostics())
    registry.observe(
        _diagnostics(
            providers={"fake-provider": 30.0},
            plugins=[
                {
                    "plugin": "fake-filter",
                    "stage": "filtering",
                    "status": "skipped",
                    "latency_ms": 0.1,
                    "error": None,
                }
            ],
            errors=["fake-provider: boom"],
        )
    )
    rendered = registry.render()
    assert 'openlens_requests_total{status="ok"} 1' in rendered
    assert 'openlens_requests_total{status="error"} 1' in rendered
    assert 'openlens_provider_latency_ms_count{provider="fake-provider"} 2' in rendered
    assert 'openlens_provider_errors_total{provider="fake-provider"} 1' in rendered
    assert (
        'openlens_plugin_executions_total{plugin="fake-filter",'
        'stage="filtering",status="ok"} 1' in rendered
    )
    assert (
        'openlens_plugin_executions_total{plugin="fake-filter",'
        'stage="filtering",status="skipped"} 1' in rendered
    )
    # Histogram bucket lines exist for the fixed bounds, cumulative.
    assert (
        "openlens_provider_latency_ms_bucket{"
        'le="+Inf",provider="fake-provider"} 2' in rendered
    )
    # Only fixed label names appear anywhere in the exposition.
    assert _label_names(rendered) <= ALLOWED_LABEL_NAMES


def test_histogram_buckets_are_cumulative_over_fixed_bounds() -> None:
    registry = MetricsRegistry(enabled=True)
    registry.observe(_diagnostics(providers={"p": 7.0}))
    rendered = registry.render()
    bucket: dict[str, int] = {}
    for line in rendered.splitlines():
        if line.startswith("openlens_provider_latency_ms_bucket{"):
            match = re.search(r'le="([^"]+)".*\} (\d+)$', line)
            assert match is not None
            bucket[match.group(1)] = int(match.group(2))
    assert bucket["5"] == 0
    assert bucket["10"] == 1
    assert bucket["+Inf"] == 1
    assert len(bucket) == len(LATENCY_BUCKETS_MS)


def test_format_labels_rejects_unknown_names() -> None:
    with pytest.raises(ValueError):
        format_labels({"query": "cats"})
    with pytest.raises(ValueError):
        format_labels({"provider": "p", "doc_id": "doc-1"})
    # Fixed names are accepted, values are escaped.
    assert format_labels({"provider": 'a"b\\c'}) == '{provider="a\\"b\\\\c"}'


def test_escape_label_value_covers_prometheus_specials() -> None:
    assert escape_label_value('a"b') == 'a\\"b'
    assert escape_label_value("a\nb") == "a\\nb"
    assert escape_label_value("a\\b") == "a\\\\b"


def test_observe_ignores_unknown_diagnostics_keys() -> None:
    registry = MetricsRegistry(enabled=True)
    payload = _diagnostics()
    payload["query"] = 'evil{x="y"}, provider="injected"'
    payload["adversarial_label"] = "nope"
    registry.observe(payload)
    rendered = registry.render()
    assert "evil" not in rendered
    assert "injected" not in rendered
    assert "adversarial_label" not in rendered
    assert _label_names(rendered) <= ALLOWED_LABEL_NAMES


def test_observe_never_raises_on_malformed_payloads() -> None:
    registry = MetricsRegistry(enabled=True)
    for bad in [
        {"provider_latencies_ms": "not-a-mapping"},
        {"provider_latencies_ms": {"p": float("nan")}},
        {"provider_latencies_ms": {"p": -3.0}},
        {"plugin_executions": "not-a-list"},
        {"plugin_executions": [{"plugin": 42}]},
        {"errors": "not-a-list"},
        {"errors": [None, 42]},
    ]:
        registry.observe(bad)  # must not raise
    registry.observe("not-a-dict")  # type: ignore[arg-type]  # must not raise


def test_threaded_concurrent_observes_do_not_lose_counts() -> None:
    registry = MetricsRegistry(enabled=True)
    threads = 8
    per_thread = 50

    def worker() -> None:
        for _ in range(per_thread):
            registry.observe(_diagnostics())

    handles = [threading.Thread(target=worker) for _ in range(threads)]
    for handle in handles:
        handle.start()
    for handle in handles:
        handle.join()
    rendered = registry.render()
    total = threads * per_thread
    assert f'openlens_requests_total{{status="ok"}} {total}' in rendered
    assert (
        f'openlens_provider_latency_ms_count{{provider="fake-provider"}} {total}'
        in rendered
    )
    assert (
        'openlens_provider_latency_ms_bucket{le="+Inf",provider="fake-provider"} '
        f"{total}" in rendered
    )


def test_fuzz_adversarial_query_text_never_becomes_label() -> None:
    adversarial = [
        'cats"dogs',
        "cats\ndogs",
        "cats\\dogs",
        'provider="injected"',
        '{provider="x"}',
        'a,b="c"',
        'le="5"x',
        "}\nopenlens_evil_total 1\n# HELP x",
        'status="ok",extra="1"',
        '";\nDROP TABLE metrics;--',
        "fält-er_ünïcodé",
        "x" * 2000,
    ]
    registry = MetricsRegistry(enabled=True)
    for text in adversarial:
        # Adversarial text only ever travels in ignored keys/values.
        registry.observe(
            {
                "fallback_used": False,
                "provider_latencies_ms": {"fake-provider": 1.0},
                "plugin_executions": [
                    {
                        "plugin": "fake-filter",
                        "stage": "filtering",
                        "status": "ok",
                        "latency_ms": 0.1,
                        "error": None,
                    }
                ],
                "errors": [],
                "query": text,
                "raw_query": text,
            }
        )
    rendered = registry.render()
    for text in adversarial:
        assert text not in rendered
        assert escape_label_value(text) not in rendered
    assert _label_names(rendered) <= ALLOWED_LABEL_NAMES
    assert f'openlens_requests_total{{status="ok"}} {len(adversarial)}' in rendered
