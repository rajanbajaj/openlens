"""Structured request logging for OpenLens (stdlib only).

One JSON record is emitted per search at INFO level carrying a fixed,
small key set — request id, query mode/limit, provider latencies and
result counts, plugin executions, errors, and the result count. The raw
query string is never logged at INFO; it appears only in a DEBUG-level
record that is suppressed unless the level is lowered explicitly.

Secrets are scrubbed before emission: mapping keys shaped like
``api_key``/``password``/``token`` are redacted wholesale, and
credential-shaped substrings (``sk-...``/``pw-...`` tokens,
``key=value`` pairs, ``Bearer`` credentials) are redacted from free text.

Level comes from the app config ``logging.level`` key (default INFO).
"""

from __future__ import annotations

import json
import logging as stdlib_logging
import re
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from openlens.core.context import SearchContext
from openlens.core.interfaces import SearchRequest

#: Logger carrying the per-request search records.
LOGGER_NAME = "openlens.search"

#: Placeholder replacing redacted secret material.
REDACTED = "[REDACTED]"

#: Default level name when ``logging.level`` is absent or unrecognized.
DEFAULT_LEVEL_NAME = "INFO"

_LEVELS = {
    "CRITICAL": stdlib_logging.CRITICAL,
    "ERROR": stdlib_logging.ERROR,
    "WARNING": stdlib_logging.WARNING,
    "WARN": stdlib_logging.WARNING,
    "INFO": stdlib_logging.INFO,
    "DEBUG": stdlib_logging.DEBUG,
    "NOTSET": stdlib_logging.NOTSET,
}

#: Known secret field names (normalized: lowercase, no -/_). A mapping
#: entry is redacted only when its key is exactly one of these — substring
#: matching used to swallow innocent names (e.g. a provider literally
#: called ``secret_fake``). Credential-shaped *values* are still caught by
#: the free-text patterns below regardless of their key.
_SECRET_KEY_NAMES = frozenset(
    {
        # Compared against the key lowercased with -/_ stripped, so each
        # entry is already in that normalized form.
        "apikey",
        "password",
        "passwd",
        "pwd",
        "secret",
        "apisecret",
        "clientsecret",
        "token",
        "accesstoken",
        "refreshtoken",
        "authtoken",
        "authorization",
        "bearer",
        "credential",
        "credentials",
        "setcookie",
        "privatekey",
    }
)

#: Credential-shaped substrings redacted from free text (compiled once).
_SK_PATTERN = re.compile(r"sk-[A-Za-z0-9\-_]+")
_PW_PATTERN = re.compile(r"pw-[A-Za-z0-9\-_]+")
_KEY_VALUE_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|password|passwd|pwd|secret|token)\s*[:=]\s*\S+"
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/=]+")


def _is_secret_key(key: str) -> bool:
    """Return True when a mapping key is a known secret field name."""
    normalized = key.lower().replace("-", "").replace("_", "")
    return normalized in _SECRET_KEY_NAMES


def _scrub_text(value: str) -> str:
    """Redact credential-shaped substrings from one free-text value."""
    redacted = _SK_PATTERN.sub(REDACTED, value)
    redacted = _PW_PATTERN.sub(REDACTED, redacted)
    redacted = _KEY_VALUE_PATTERN.sub(r"\1=" + REDACTED, redacted)
    return _BEARER_PATTERN.sub("Bearer " + REDACTED, redacted)


def scrub_value(value: Any) -> Any:
    """Return a copy of ``value`` with secret material redacted.

    Mapping entries under known secret field names become ``[REDACTED]``;
    every string is additionally passed through the free-text patterns.
    """
    if isinstance(value, Mapping):
        return {
            key: (REDACTED if _is_secret_key(str(key)) else scrub_value(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub_value(item) for item in value]
    if isinstance(value, str):
        return _scrub_text(value)
    return value


def resolve_log_level(config: Mapping[str, Any] | None) -> int:
    """Resolve the ``logging.level`` config key to a stdlib level.

    Unknown, blank, or missing values fall back to INFO; the lookup is
    case-insensitive so ``warning`` and ``WARNING`` behave alike.
    """
    raw = DEFAULT_LEVEL_NAME
    if config is not None:
        section = config.get("logging", {})
        if isinstance(section, Mapping):
            candidate = section.get("level", DEFAULT_LEVEL_NAME)
            if isinstance(candidate, str) and candidate.strip():
                raw = candidate.strip().upper()
    return _LEVELS.get(raw, stdlib_logging.INFO)


class JsonFormatter(stdlib_logging.Formatter):
    """Render a LogRecord as one JSON object per line.

    Records emitted via :func:`log_search_completed` /
    :func:`log_search_query_debug` carry their fixed-shape payload in the
    ``openlens_event`` record attribute, which is merged into the JSON
    object; records without it still render as valid JSON.
    """

    def format(self, record: stdlib_logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        event = record.__dict__.get("openlens_event")
        if isinstance(event, Mapping):
            for key, item in event.items():
                payload[str(key)] = item
        return json.dumps(payload, default=str)


class _JsonStreamHandler(stdlib_logging.StreamHandler):
    """Marker subclass so configure can find (and replace) our handler."""

    def __init__(self) -> None:
        super().__init__(sys.stderr)
        self.setFormatter(JsonFormatter())


def configure_logging(
    level: str | Mapping[str, Any] | None = DEFAULT_LEVEL_NAME,
) -> stdlib_logging.Logger:
    """Configure the search logger from a level name or an app config.

    Accepts a bare level name (``"WARNING"``) or a full app config
    mapping honouring the ``logging.level`` key. The logger keeps exactly
    one JSON handler across repeated calls, and does not propagate to the
    root logger (so one search emits exactly one record).
    """
    if isinstance(level, Mapping):
        resolved = resolve_log_level(level)
    elif isinstance(level, str):
        resolved = resolve_log_level({"logging": {"level": level}})
    else:
        resolved = resolve_log_level(None)
    logger = stdlib_logging.getLogger(LOGGER_NAME)
    logger.setLevel(resolved)
    logger.propagate = False
    for existing in list(logger.handlers):
        if isinstance(existing, _JsonStreamHandler):
            logger.removeHandler(existing)
    logger.addHandler(_JsonStreamHandler())
    return logger


def log_search_completed(
    logger: stdlib_logging.Logger,
    *,
    request: SearchRequest,
    context: SearchContext,
    request_id: str,
    result_count: int | None = None,
) -> None:
    """Emit the single per-request INFO record for one search.

    The payload carries mode/limit/counts only — never ``request.query``.
    """
    total = len(context.results) if result_count is None else result_count
    event = scrub_value(
        {
            "event": "search.completed",
            "request_id": request_id,
            "mode": request.mode,
            "limit": request.limit,
            "provider_latencies_ms": dict(context.provider_latencies_ms),
            "provider_result_counts": dict(context.provider_result_counts),
            "plugin_executions": [
                {
                    "plugin": entry.plugin_name,
                    "stage": entry.stage,
                    "status": entry.status,
                    "latency_ms": entry.latency_ms,
                    "error": entry.error,
                }
                for entry in context.plugin_executions
            ],
            "errors": list(context.errors),
            "result_count": total,
            "fallback_used": context.fallback_used,
        }
    )
    logger.info("search completed", extra={"openlens_event": event})


def log_search_query_debug(
    logger: stdlib_logging.Logger, *, request: SearchRequest, request_id: str
) -> None:
    """Emit the raw query text at DEBUG level (suppressed by default).

    The INFO record never carries the query; operators lower the level to
    DEBUG explicitly when they need per-query detail.
    """
    if not logger.isEnabledFor(stdlib_logging.DEBUG):
        return
    event = scrub_value(
        {"event": "search.query", "request_id": request_id, "query": request.query}
    )
    logger.debug("search query", extra={"openlens_event": event})
