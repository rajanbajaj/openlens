"""User-defined restrictions: per-request ``filters`` schema, parser, applier.

This plugin owns the :class:`~openlens.core.interfaces.SearchRequest`
``filters`` schema. It is a mandatory ``FILTER`` at the ``FILTERING``
stage and fails closed: any malformed expression raises instead of
letting unfiltered hits escape.

Owned keys (each value is a list of strings):

- ``must``: a hit is kept only if *every* term appears in its
  title/snippet/url.
- ``must_not``: a hit is removed if *any* term appears in its
  title/snippet/url.
- ``include_domains``: when non-empty, a hit is kept only if its URL
  host equals a listed domain or is a subdomain of one. Hits without a
  parseable URL host are removed while this rule is active.
- ``exclude_domains``: a hit is removed if its URL host equals a listed
  domain or is a subdomain of one.

The spellings ``must_terms``, ``must_not_terms`` / ``exclude_terms``,
``allowed_domains`` / ``allow_domains``, and ``blocked_domains`` /
``disallowed_domains`` are accepted as synonyms for the keys above.

User terms are literals: they are escaped with :func:`re.escape` before
any regex use, so per-request input never becomes a raw regex.
Expressions are compiled once per request into a single predicate that
is then applied per hit. Domain rules match on the parsed URL host
(``urlparse(...).hostname``), never on substring search over the whole
URL, so ``evil-example.com`` and ``example.com.evil.test`` do not match
a rule for ``example.com``.

Raise sites (pinned by the filter-pack plan):

- Malformed *config* lists (the same owned keys, used as operator
  defaults applied to every request) raise :class:`ConfigError` at
  ``configure()`` time (HTTP 500 ``config_error`` via the API).
- Malformed *per-request* ``filters`` raise ``PluginError`` with
  ``mandatory=True`` from this applier (fail-closed HTTP 500
  ``plugin_error``). Unknown keys are malformed per-request input.

Forward compatibility (review D4): keys owned by later packs are
accepted and passed through untouched — never consumed, never
rejected, never type-checked here. That reservation is any ``prefer_*``
key (scoring pack) plus ``max_price``, ``location``, and ``facilities``
(rerank pack). Only keys outside the owned schema and that reservation
are rejected.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    PipelineStage,
    PluginError,
    PluginType,
    SearchResult,
)
from openlens.plugins.base import BasePlugin

#: Bounds on per-list size and entry length (schema-level ReDoS/cost caps).
MAX_ENTRIES_PER_LIST = 50
MAX_TERM_LENGTH = 200
MAX_DOMAIN_LENGTH = 253

#: Maps each accepted per-request ``filters`` key to its operator.
_FILTER_KEY_TO_OPERATOR: dict[str, str] = {
    "must": "must",
    "must_terms": "must",
    "must_not": "must_not",
    "must_not_terms": "must_not",
    "exclude_terms": "must_not",
    "include_domains": "include_domains",
    "allowed_domains": "include_domains",
    "allow_domains": "include_domains",
    "exclude_domains": "exclude_domains",
    "blocked_domains": "exclude_domains",
    "disallowed_domains": "exclude_domains",
}

#: Filter keys owned (validated + consumed) by this plugin.
OWNED_FILTER_KEYS: frozenset[str] = frozenset(_FILTER_KEY_TO_OPERATOR)

#: Reserved consumer keys passed through untouched (review D4).
RESERVED_FILTER_KEYS: frozenset[str] = frozenset(
    {"max_price", "location", "facilities"}
)

#: Prefix reserving a whole family of consumer keys (scoring pack).
RESERVED_FILTER_PREFIX = "prefer_"

_OPERATORS: tuple[str, ...] = (
    "must",
    "must_not",
    "include_domains",
    "exclude_domains",
)


def _is_reserved_key(key: str) -> bool:
    return key in RESERVED_FILTER_KEYS or key.startswith(RESERVED_FILTER_PREFIX)


def _hostname_of(url: str) -> str | None:
    """Return the lowercase bare host of ``url`` (no port/userinfo)."""
    if not url:
        return None
    try:
        host = urlparse(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    return host.rstrip(".") or None


def _normalize_domain(raw: str) -> str | None:
    """Normalize one domain rule to a bare lowercase host, or None."""
    value = raw.strip().lower()
    if "://" in value:
        value = _hostname_of(value) or ""
    value = value.split("/", 1)[0].split(":", 1)[0]
    if value.startswith("*."):
        value = value[2:]
    value = value.lstrip(".").rstrip(".")
    if not value or " " in value:
        return None
    return value


def _host_matches(host: str, rules: frozenset[str]) -> bool:
    """True when ``host`` equals a rule or is a subdomain of one."""
    candidate = host
    while candidate:
        if candidate in rules:
            return True
        _, sep, rest = candidate.partition(".")
        if not sep:
            return False
        candidate = rest
    return False


class UserRestrictionsFilter(BasePlugin):
    """Applies per-request user restrictions. Mandatory (fail closed)."""

    @property
    def name(self) -> str:
        return "user-restrictions"

    @property
    def plugin_type(self) -> PluginType:
        return PluginType.FILTER

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.FILTERING

    @property
    def mandatory(self) -> bool:
        return True

    @property
    def config_schema(self) -> dict[str, Any]:
        entry = {"type": "array", "items": {"type": "string"}}
        return {
            "type": "object",
            "properties": {
                "must": entry,
                "must_not": entry,
                "include_domains": entry,
                "exclude_domains": entry,
                "simulate_outage": {"type": "boolean"},
            },
        }

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)
        defaults: dict[str, list[str]] = {op: [] for op in _OPERATORS}
        for key, operator in _FILTER_KEY_TO_OPERATOR.items():
            if key not in self._config:
                continue
            values = self._validate_entries(
                self._config[key],
                key=key,
                domain=operator.endswith("_domains"),
                error=lambda msg: ConfigError(msg),
            )
            for value in values:
                if value not in defaults[operator]:
                    defaults[operator].append(value)
        self._defaults = defaults
        self._simulate_outage = bool(self._config.get("simulate_outage", False))

    @staticmethod
    def _validate_entries(
        value: Any,
        *,
        key: str,
        domain: bool,
        error: Callable[[str], Exception],
    ) -> list[str]:
        """Validate one term/domain list; raise ``error`` when malformed."""
        if not isinstance(value, list):
            raise error(f"UserRestrictionsFilter '{key}' must be a list of strings.")
        if len(value) > MAX_ENTRIES_PER_LIST:
            raise error(
                f"UserRestrictionsFilter '{key}' exceeds the "
                f"{MAX_ENTRIES_PER_LIST}-entry limit."
            )
        limit = MAX_DOMAIN_LENGTH if domain else MAX_TERM_LENGTH
        cleaned: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise error(f"UserRestrictionsFilter '{key}' entries must be strings.")
            if len(item) > limit:
                raise error(f"UserRestrictionsFilter '{key}' entry is too long.")
            normalized = _normalize_domain(item) if domain else item.strip()
            if not normalized:
                raise error(
                    f"UserRestrictionsFilter '{key}' entries must not be empty."
                )
            if normalized not in cleaned:
                cleaned.append(normalized)
        return cleaned

    def _request_error(self, message: str) -> PluginError:
        return PluginError(
            message, plugin_name=self.name, stage=self.stage, mandatory=True
        )

    def _compile_predicate(
        self, filters: dict[str, Any]
    ) -> Callable[[SearchResult], bool]:
        """Validate ``filters`` and compile the per-request predicate once."""
        merged: dict[str, list[str]] = {
            op: list(values) for op, values in self._defaults.items()
        }
        for key, value in filters.items():
            if not isinstance(key, str):
                raise self._request_error(f"Unknown filter key {key!r}.")
            operator = _FILTER_KEY_TO_OPERATOR.get(key)
            if operator is None:
                if _is_reserved_key(key):
                    continue  # Later pack's key: pass through untouched.
                raise self._request_error(
                    f"Unknown filter key '{key}'. Owned keys: "
                    f"{sorted(OWNED_FILTER_KEYS)}; reserved consumer keys "
                    f"('prefer_*', 'max_price', 'location', 'facilities') "
                    "are passed through."
                )
            entries = self._validate_entries(
                value,
                key=key,
                domain=operator.endswith("_domains"),
                error=lambda msg: self._request_error(msg),
            )
            for entry in entries:
                if entry not in merged[operator]:
                    merged[operator].append(entry)

        must_patterns = [
            re.compile(re.escape(term), re.IGNORECASE) for term in merged["must"]
        ]
        must_not_patterns = [
            re.compile(re.escape(term), re.IGNORECASE) for term in merged["must_not"]
        ]
        include_domains = frozenset(merged["include_domains"])
        exclude_domains = frozenset(merged["exclude_domains"])

        def predicate(result: SearchResult) -> bool:
            haystack = f"{result.title}\n{result.snippet}\n{result.url or ''}"
            if any(not pattern.search(haystack) for pattern in must_patterns):
                return False
            if any(pattern.search(haystack) for pattern in must_not_patterns):
                return False
            if include_domains or exclude_domains:
                host = _hostname_of(result.url or "")
                if include_domains and (
                    host is None or not _host_matches(host, include_domains)
                ):
                    return False
                if (
                    exclude_domains
                    and host is not None
                    and _host_matches(host, exclude_domains)
                ):
                    return False
            return True

        return predicate

    def run(self, context: SearchContext) -> SearchContext:
        if self._simulate_outage:
            raise PluginError(
                "user-restrictions backend unavailable",
                plugin_name=self.name,
                stage=self.stage,
                mandatory=True,
            )
        filters = context.request.filters
        if not isinstance(filters, dict):
            raise self._request_error("SearchRequest 'filters' must be a mapping.")
        predicate = self._compile_predicate(filters)
        kept = [result for result in context.results if predicate(result)]
        removed = len(context.results) - len(kept)
        context.results = kept
        context.applied_filters.append(self.name)
        context.metadata["user-restrictions-removed"] = removed
        return context
