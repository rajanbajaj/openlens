"""Federated-search eval harness: quality / latency / cost table.

Offline only (no network, no keys): two test-local fake providers with
distinct names serve the fixture corpus from ``tests/fixtures/corpus.py``
via in-memory token-overlap search and ``normalize_many`` — deliberately
NOT seeded ``CustomIndexProvider`` instances (every such instance shares
the name ``"custom_index"``, so results would be unattributable) and NOT
the ``http`` stub (which would need a mock transport).

Protocol (pinned, deterministic shape — no flaky millisecond asserts):
each of the 4 fixture requests runs 3 warmups then 20 timed repetitions
per strategy. Assertions compare shapes only:

* recall (expected-doc hit rate) of federated >= single-provider recall,
* latency diagnostics keys present per strategy; federated p50 within a
  documented multiple (<= 3x) of single p50 — never exact milliseconds,
* the printed cost table has one row per strategy, with estimated cost
  derived from the declared per-call rates below.

Estimated-cost grounding: ``COST_PER_CALL`` declares the per-call rate
for each fake. ``ProviderManager.from_config`` cost plumbing
(``cost_per_call`` validation + per-request ``provider_costs_estimated``
writes) is owned by the manager wave; until that lands, the harness
computes estimated cost as ``calls_per_request[strategy] * rate`` and
asserts the accounting contract (context storage -> diagnostics ->
``DiagnosticsSchema.from_core`` pass-through) directly. The per-request
diagnostics keys are asserted present end-to-end through a real
``manager.search`` call.
"""

from __future__ import annotations

import re
import statistics
import time
from typing import Any

from openlens.api.schemas import DiagnosticsSchema
from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    Document,
    ProviderError,
    SearchProvider,
    SearchRequest,
    SearchResult,
)
from openlens.providers.manager import ProviderManager
from openlens.retrieval.normalizer import normalize_many
from tests.fixtures.corpus import fixture_documents, fixture_requests

_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Declared per-call rate for each fake (USD). The harness multiplies by
#: the number of provider calls a strategy makes per request.
COST_PER_CALL: dict[str, float] = {"alpha": 0.002, "beta": 0.003}

WARMUPS = 3
REPS = 20

#: Expected doc per fixture query (in request order).
EXPECTED_DOCS = (
    "doc-shopping-1",  # "running shoes"
    "doc-news-1",  # "park funding"
    "doc-tech-1",  # "SQLite FTS5 search"
    "doc-travel-1",  # "coast trip"
)


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.casefold()))


class FakeTokenProvider(SearchProvider):
    """Test-local in-memory provider with a distinct name.

    Serves a fixed document subset scored by query-token overlap and
    returns normalized hits via ``normalize_many``. ``dead=True`` makes
    every call raise ``ProviderError`` (kill-switch for the
    partial-failure proof).
    """

    def __init__(self, provider_name: str, docs: list[Document]) -> None:
        self._provider_name = provider_name
        self._docs = list(docs)
        self.dead = False

    @property
    def name(self) -> str:
        return self._provider_name

    def configure(self, config: dict[str, Any]) -> None:
        return None

    def health_check(self) -> bool:
        return not self.dead

    def search(self, request: SearchRequest) -> list[SearchResult]:
        # Deterministic 5ms service time: real providers have I/O latency,
        # and without it thread-spawn overhead alone would dominate the
        # single-vs-federated p50 ratio the harness pins.
        time.sleep(0.005)
        if self.dead:
            raise ProviderError(
                f"provider '{self._provider_name}' is down",
                provider_name=self._provider_name,
                retryable=False,
                code=ProviderError.UNAVAILABLE,
            )
        query_tokens = _tokens(request.query)
        scored: list[tuple[Document, float]] = []
        for doc in self._docs:
            haystack = _tokens(f"{doc.title} {doc.content}")
            overlap = len(query_tokens & haystack)
            if overlap:
                scored.append((doc, float(overlap)))
        scored.sort(key=lambda item: (-item[1], item[0].id))
        return normalize_many(scored, self._provider_name, request.query)


def _split_corpus() -> tuple[list[Document], list[Document]]:
    """Disjoint halves: alpha owns the first three answers, beta the last."""
    alpha_ids = {
        "doc-shopping-1",
        "doc-shopping-2",
        "doc-news-1",
        "doc-tech-1",
    }
    docs = fixture_documents()
    alpha = [d for d in docs if d.id in alpha_ids]
    beta = [d for d in docs if d.id not in alpha_ids]
    assert alpha and beta
    return alpha, beta


def _make_manager(strategy: str) -> ProviderManager:
    alpha_docs, beta_docs = _split_corpus()
    return ProviderManager(
        {
            "alpha": FakeTokenProvider("alpha", alpha_docs),
            "beta": FakeTokenProvider("beta", beta_docs),
        },
        primary="alpha",
        strategy=strategy,
    )


def _recall(hits: list[SearchResult], expected_id: str) -> float:
    return 1.0 if any(h.id == expected_id for h in hits) else 0.0


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = min(len(ordered) - 1, int(pct / 100.0 * len(ordered)))
    return ordered[rank]


def _run_strategy(strategy: str) -> dict[str, Any]:
    """Run the pinned protocol for one strategy; return recall/latency rows."""
    manager = _make_manager(strategy)
    requests = fixture_requests()
    assert len(requests) == len(EXPECTED_DOCS) == 4
    latencies: list[float] = []
    recalls: list[float] = []
    last_diagnostics: dict[str, Any] = {}
    for request, expected_id in zip(requests, EXPECTED_DOCS):
        fetch = SearchRequest(
            query=request.query, mode=request.mode, limit=request.limit
        )
        for _ in range(WARMUPS):
            manager.search(fetch, SearchContext(request=fetch))
        hits: list[SearchResult] = []
        for _ in range(REPS):
            context = SearchContext(request=fetch)
            started = time.perf_counter()
            hits = manager.search(fetch, context)
            latencies.append((time.perf_counter() - started) * 1000.0)
            last_diagnostics = context.diagnostics()
        recalls.append(_recall(hits, expected_id))
    if strategy == "single":
        per_request_cost = COST_PER_CALL["alpha"]
    else:
        per_request_cost = sum(COST_PER_CALL[name] for name in ("alpha", "beta"))
    return {
        "strategy": strategy,
        "recall": sum(recalls) / len(recalls),
        "p50_ms": statistics.median(latencies),
        "p99_ms": _percentile(latencies, 99),
        "est_cost_per_request": per_request_cost,
        "diagnostics": last_diagnostics,
    }


def _print_table(rows: list[dict[str, Any]]) -> None:
    header = (
        f"{'strategy':<10}{'recall':>8}{'p50_ms':>10}{'p99_ms':>10}{'est_cost':>12}"
    )
    print("\n[federated-eval] quality/latency/cost")
    print(header)
    for row in rows:
        print(
            f"{row['strategy']:<10}"
            f"{row['recall']:>8.2f}"
            f"{row['p50_ms']:>10.3f}"
            f"{row['p99_ms']:>10.3f}"
            f"{row['est_cost_per_request']:>12.4f}"
        )


def test_federated_eval_table() -> None:
    """Eval harness: recall/latency-shape/cost table for single vs federated."""
    rows = [_run_strategy("single"), _run_strategy("federated")]
    _print_table(rows)
    by_strategy = {row["strategy"]: row for row in rows}

    # One row per strategy in the cost table.
    assert sorted(by_strategy) == ["federated", "single"]

    # Recall: federated covers both halves, so it must match-or-beat single.
    assert by_strategy["federated"]["recall"] >= by_strategy["single"]["recall"]
    assert by_strategy["federated"]["recall"] == 1.0

    # Latency shape only: keys present, p50 within a documented multiple.
    single, federated = by_strategy["single"], by_strategy["federated"]
    for row in rows:
        assert "p50_ms" in row and "p99_ms" in row
        assert row["p50_ms"] >= 0.0 and row["p99_ms"] >= row["p50_ms"]
        assert set(row["diagnostics"]) >= {
            "provider_latencies_ms",
            "provider_result_counts",
            "provider_costs_estimated",
            "errors",
        }
    assert federated["p50_ms"] <= 3.0 * max(single["p50_ms"], 1e-9)

    # Cost: federated fans out to both providers, so it costs more.
    assert federated["est_cost_per_request"] > single["est_cost_per_request"]

    # Diagnostics survive the API boundary with the new accounting keys.
    schema = DiagnosticsSchema.from_core(federated["diagnostics"])
    assert set(schema.provider_latencies_ms) >= {"alpha", "beta"}


def test_federated_merges_with_attribution() -> None:
    """Manual proof 1: federated hits carry per-provider attribution."""
    manager = _make_manager("federated")
    request = SearchRequest(query="running shoes", limit=10)
    hits = manager.search(request, SearchContext(request=request))
    assert hits, "federated query over two fakes must return hits"
    providers = {h.provider for h in hits}
    assert providers <= {"alpha", "beta"}
    assert "alpha" in providers


def test_federated_partial_failure_degrades() -> None:
    """Manual proof 2: killing one provider keeps survivor hits + error."""
    manager = _make_manager("federated")
    manager.get("beta").dead = True  # type: ignore[attr-defined]
    request = SearchRequest(query="running shoes", limit=10)
    context = SearchContext(request=request)
    hits = manager.search(request, context)
    assert hits, "survivor provider must still return hits"
    assert all(h.provider == "alpha" for h in hits)
    assert any("beta" in message for message in context.errors)


def test_accounting_storage_and_schema_passthrough() -> None:
    """Accounting contract: counts/costs survive context -> schema."""
    context = SearchContext(request=SearchRequest(query="shoes"))
    context.record_provider_results("alpha", 4)
    context.record_provider_results("beta", 2)
    context.record_provider_cost("alpha", 0.002)
    context.record_provider_cost("beta", 0.003)
    payload = context.diagnostics()
    assert payload["provider_result_counts"] == {"alpha": 4, "beta": 2}
    assert payload["provider_costs_estimated"] == {"alpha": 0.002, "beta": 0.003}
    schema = DiagnosticsSchema.from_core(payload)
    assert schema.provider_result_counts == {"alpha": 4, "beta": 2}
    assert schema.provider_costs_estimated == {"alpha": 0.002, "beta": 0.003}
