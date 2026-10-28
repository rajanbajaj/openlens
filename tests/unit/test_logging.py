"""Unit tests for structured request logging + request ids (step 1).

One search emits exactly one JSON INFO record carrying the request id,
mode, limit, provider latencies, and result count — never the raw query
at INFO (DEBUG only, level-gated, off by default). Api-key-shaped values
and the secret-leak sentinels never appear in the record; ``logging.level:
WARNING`` suppresses the INFO record; a supplied ``X-Request-Id`` is
echoed while an absent one is generated.
"""

from __future__ import annotations

import json
import logging as stdlib_logging
import re
import uuid
from collections.abc import Iterator

import pytest
from fastapi import Request

from openlens.api.schemas import DiagnosticsSchema
from openlens.core.context import SearchContext
from openlens.core.interfaces import SearchRequest, SearchResult
from openlens.observability import logging as obs_logging
from openlens.observability.request_id import (
    REQUEST_ID_HEADER,
    get_request_id,
    new_request_id,
    resolve_request_id,
)
from tests.integration.test_secret_leak import SENTINEL_PASSWORD

QUERY = "QUERY-never-at-info-zebra-quixotic-4821"


def _search_request() -> SearchRequest:
    return SearchRequest(query=QUERY, mode="web", limit=5)


def _search_context() -> SearchContext:
    context = SearchContext(request=_search_request(), request_id="req-42")
    context.record_provider_latency("fake-provider", 12.5)
    context.record_provider_results("fake-provider", 2)
    context.record_plugin_execution("safety-filter", "filtering", "ok", latency_ms=1.25)
    context.results = [
        SearchResult(
            id="doc-1",
            title="Title 1",
            snippet="Snippet 1",
            original_score=2.0,
            final_score=2.0,
            provider="fake-provider",
        ),
        SearchResult(
            id="doc-2",
            title="Title 2",
            snippet="Snippet 2",
            original_score=1.0,
            final_score=1.0,
            provider="fake-provider",
        ),
    ]
    return context


class _CaptureHandler(stdlib_logging.Handler):
    """Collect formatted JSON lines emitted by the search logger."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.setFormatter(obs_logging.JsonFormatter())

    def emit(self, record: stdlib_logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture()
def captured() -> Iterator[tuple[stdlib_logging.Logger, _CaptureHandler]]:
    """Attach a capture handler to the shared search logger, then detach."""
    logger = stdlib_logging.getLogger(obs_logging.LOGGER_NAME)
    handler = _CaptureHandler()
    logger.addHandler(handler)
    try:
        yield logger, handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(stdlib_logging.INFO)


def _payloads(handler: _CaptureHandler) -> list[dict]:
    return [json.loads(line) for line in handler.lines]


def test_one_search_emits_exactly_one_json_info_record(
    captured: tuple[stdlib_logging.Logger, _CaptureHandler],
) -> None:
    logger, handler = captured
    obs_logging.configure_logging("INFO")
    request = _search_request()
    context = _search_context()

    obs_logging.log_search_completed(
        logger, request=request, context=context, request_id="req-42"
    )
    # The DEBUG-only query record stays suppressed at the default level.
    obs_logging.log_search_query_debug(logger, request=request, request_id="req-42")

    assert len(handler.lines) == 1
    (record,) = _payloads(handler)
    assert record["level"] == "INFO"
    assert record["request_id"] == "req-42"
    assert record["mode"] == "web"
    assert record["limit"] == 5
    assert record["provider_latencies_ms"] == {"fake-provider": 12.5}
    assert record["provider_result_counts"] == {"fake-provider": 2}
    assert record["result_count"] == 2
    assert record["plugin_executions"] == [
        {
            "plugin": "safety-filter",
            "stage": "filtering",
            "status": "ok",
            "latency_ms": 1.25,
            "error": None,
        }
    ]
    assert record["errors"] == []
    assert QUERY not in handler.lines[0]


def test_secret_shaped_names_are_preserved_but_secrets_redacted(
    captured: tuple[stdlib_logging.Logger, _CaptureHandler],
) -> None:
    """Key redaction is scoped to known secret fields, not substrings.

    A provider literally named ``secret_fake`` keeps its name in the log
    line, while a real secret value under a known key (``api_key``) is
    still redacted.
    """
    logger, handler = captured
    obs_logging.configure_logging("INFO")
    context = _search_context()
    context.record_provider_latency("secret_fake", 3.0)
    context.record_provider_results("secret_fake", 1)

    obs_logging.log_search_completed(
        logger, request=_search_request(), context=context, request_id="req-42"
    )

    (record,) = _payloads(handler)
    assert record["provider_latencies_ms"]["secret_fake"] == 3.0
    assert record["provider_result_counts"]["secret_fake"] == 1
    scrubbed = obs_logging.scrub_value(
        {"api_key": "live-value", "provider": "secret_fake"}
    )
    assert scrubbed == {"api_key": obs_logging.REDACTED, "provider": "secret_fake"}


def test_raw_query_only_at_debug(
    captured: tuple[stdlib_logging.Logger, _CaptureHandler],
) -> None:
    logger, handler = captured
    obs_logging.configure_logging("DEBUG")
    request = _search_request()

    obs_logging.log_search_completed(
        logger, request=request, context=_search_context(), request_id="req-42"
    )
    obs_logging.log_search_query_debug(logger, request=request, request_id="req-42")

    assert len(handler.lines) == 2
    info_record, debug_record = _payloads(handler)
    assert info_record["level"] == "INFO"
    assert QUERY not in json.dumps(info_record)
    assert debug_record["level"] == "DEBUG"
    assert debug_record["query"] == QUERY
    assert debug_record["request_id"] == "req-42"


def test_secrets_never_in_record(
    captured: tuple[stdlib_logging.Logger, _CaptureHandler],
) -> None:
    logger, handler = captured
    obs_logging.configure_logging("INFO")
    # SENTINEL_KEY / MODEL_SENTINEL_KEY are already placeholder-shaped, so a
    # decoy key plus the genuinely secret-shaped SENTINEL_PASSWORD exercise
    # every scrub path (sk- token, key=value, Bearer, bare free text).
    decoy = "sk-decoy-c0ffee-1234"
    context = _search_context()
    context.add_error(f"provider auth failed: api_key={decoy}")
    context.add_error(f"login rejected, password={SENTINEL_PASSWORD}")
    context.add_error(f"Bearer {decoy} rejected by upstream")
    context.add_error(f"bare credential {SENTINEL_PASSWORD} in free text")
    context.record_plugin_execution(
        "leaky-plugin",
        "enrichment",
        "failed",
        error=f"key={decoy} password={SENTINEL_PASSWORD}",
    )

    obs_logging.log_search_completed(
        logger, request=_search_request(), context=context, request_id="req-42"
    )

    assert len(handler.lines) == 1
    assert decoy not in handler.lines[0]
    assert SENTINEL_PASSWORD not in handler.lines[0]
    # Every planted secret was redacted in place (not dropped): four error
    # strings plus two credential pairs inside the plugin error.
    assert handler.lines[0].count(obs_logging.REDACTED) >= 6
    assert "api_key=" + obs_logging.REDACTED in handler.lines[0]
    assert "Bearer " + obs_logging.REDACTED in handler.lines[0]


def test_warning_level_suppresses_info(
    captured: tuple[stdlib_logging.Logger, _CaptureHandler],
) -> None:
    logger, handler = captured
    obs_logging.configure_logging({"logging": {"level": "WARNING"}})

    obs_logging.log_search_completed(
        logger,
        request=_search_request(),
        context=_search_context(),
        request_id="req-42",
    )

    assert handler.lines == []


def test_resolve_log_level_defaults() -> None:
    assert obs_logging.resolve_log_level(None) == stdlib_logging.INFO
    assert obs_logging.resolve_log_level({}) == stdlib_logging.INFO
    assert (
        obs_logging.resolve_log_level({"logging": {"level": "warning"}})
        == stdlib_logging.WARNING
    )
    assert (
        obs_logging.resolve_log_level({"logging": {"level": "nope"}})
        == stdlib_logging.INFO
    )


def test_configure_logging_keeps_one_handler(
    captured: tuple[stdlib_logging.Logger, _CaptureHandler],
) -> None:
    logger, _ = captured
    obs_logging.configure_logging("INFO")
    obs_logging.configure_logging({"logging": {"level": "DEBUG"}})
    json_handlers = [
        h for h in logger.handlers if isinstance(h, obs_logging._JsonStreamHandler)
    ]
    assert len(json_handlers) == 1


def test_supplied_request_id_echoed() -> None:
    assert resolve_request_id("req-abc-123") == "req-abc-123"


def test_absent_request_id_generated() -> None:
    generated = [resolve_request_id(value) for value in (None, "", "   ")]
    for value in generated:
        assert re.fullmatch(r"[0-9a-f]{32}", value), value
    assert generated[0] != generated[1]
    assert re.fullmatch(r"[0-9a-f]{32}", new_request_id())


def _http_request(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "GET", "headers": raw})


def test_request_id_dependency_echoes_supplied_header() -> None:
    request = _http_request({REQUEST_ID_HEADER: "client-supplied-id"})
    assert get_request_id(request) == "client-supplied-id"


def test_request_id_dependency_generates_when_absent() -> None:
    generated = get_request_id(_http_request({}))
    assert re.fullmatch(r"[0-9a-f]{32}", generated)
    assert uuid.UUID(hex=generated).hex == generated


def test_diagnostics_and_schema_carry_request_id() -> None:
    context = _search_context()
    assert context.diagnostics()["request_id"] == "req-42"

    schema = DiagnosticsSchema.from_core(context.diagnostics())
    assert schema.request_id == "req-42"

    bare = SearchContext(request=_search_request())
    assert bare.diagnostics()["request_id"] == ""
    assert DiagnosticsSchema.from_core(bare.diagnostics()).request_id == ""
