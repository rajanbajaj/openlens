"""Mandatory exclusion filter: removes blocklisted / unsafe hits.

Fail-closed: any internal failure raises ``PluginError(mandatory=True)`` so
the pipeline aborts instead of leaking unfiltered results.

Matching tiers, cheapest first (per hit):

1. ``blocked_terms`` — substring over title/snippet/url (unchanged).
2. Host sets — the URL's host is parsed with ``urlparse(...).hostname``
   (never substring over the whole URL, never ``.netloc``, which keeps
   port/userinfo) and looked up in the blocked/allowed domain sets.
   A plain entry matches the bare host exactly, so blocking
   ``example.com`` matches neither ``evil-example.com`` nor
   ``example.com.evil.test``; an explicit ``*.example.com`` entry matches
   that domain and its subdomains. Port/userinfo URLs parse to the bare
   host, so ``https://user@example.com:8443/x`` is judged on
   ``example.com``.
3. URL patterns — glob/regex lists from config, compiled once at
   ``configure()`` into a single alternation per list and run only after
   the host-set tier, so most hits never reach a regex. Pattern count
   and length are capped at ``configure()`` (ReDoS hygiene: glob is the
   preferred form; raw regex only ever comes from trusted config, never
   from per-request input).

Allowlist (default-deny) mode is active ONLY when
``context.request.mode == "child-safe"`` — no other mode implies it.
In that mode a hit passes only when its host is allowlisted or its URL
matches an allowed URL pattern, and blocklist hits are still excluded
(block wins over allow). Outside ``child-safe`` the allowlists are
inert. With no allowlist configured, ``child-safe`` denies everything.

Config keys (all lists of strings unless noted):

- ``blocked_terms``, ``blocked_domains`` (existing)
- ``blocked_url_globs``, ``blocked_url_regexes``,
  ``blocked_url_patterns`` (entries are glob strings, ``"glob:..."`` /
  ``"regex:..."`` prefixed strings, or ``{"pattern": ..., "kind":
  "glob"|"regex"}`` mappings)
- ``allowed_domains`` (alias ``allowlist_domains``),
  ``allowed_url_globs``, ``allowed_url_regexes``,
  ``allowed_url_patterns``
- ``simulate_outage`` (existing test hook)
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any
from urllib.parse import urlparse

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    PipelineStage,
    PluginError,
    PluginType,
)
from openlens.plugins.base import BasePlugin

DEFAULT_BLOCKED_TERMS: tuple[str, ...] = (
    "explicit",
    "porn",
    "gore",
    "malware",
    "phishing",
)

#: ReDoS hygiene caps, validated at ``configure()`` time.
MAX_URL_PATTERNS_PER_LIST = 100
MAX_URL_PATTERN_LENGTH = 512

_CHILD_SAFE_MODE = "child-safe"


def _host_of(url: str) -> str:
    """Bare lowercased host of ``url`` (no port/userinfo), or ``""``."""
    if not url:
        return ""
    try:
        host = urlparse(url).hostname
        if host is None and "://" not in url:
            # Bare-host URLs ("example.com/x") parse as a path otherwise.
            host = urlparse(f"//{url}").hostname
    except ValueError:  # malformed URL (e.g. bad IPv6 literal)
        return ""
    return (host or "").lower().rstrip(".")


def _normalize_domain(raw: str) -> tuple[str, bool]:
    """Return ``(bare_host, is_wildcard)`` for a config domain entry."""
    entry = raw.strip().lower()
    wildcard = entry.startswith("*.")
    if wildcard:
        entry = entry[2:]
    if "://" in entry:
        entry = _host_of(entry) or entry
    else:
        entry = entry.split("/")[0].split(":")[0]
    return entry.strip(".").rstrip("."), wildcard


def _domain_sets(
    entries: list[str], field: str
) -> tuple[frozenset[str], tuple[str, ...]]:
    exact: set[str] = set()
    wildcards: list[str] = []
    for raw in entries:
        host, wildcard = _normalize_domain(raw)
        if not host:
            raise ConfigError(f"SafetyFilter '{field}' has an empty domain entry.")
        if wildcard:
            wildcards.append(host)
        else:
            exact.add(host)
    return frozenset(exact), tuple(wildcards)


def _host_matches(host: str, exact: frozenset[str], wildcards: tuple[str, ...]) -> bool:
    if not host:
        return False
    if host in exact:
        return True
    return any(host == base or host.endswith(f".{base}") for base in wildcards)


def _string_list(config: dict[str, Any], *keys: str) -> list[str] | None:
    """First present key's value, type-checked as a list of strings."""
    for key in keys:
        if key in config:
            value = config[key]
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise ConfigError(f"SafetyFilter '{key}' must be a list of strings.")
            return list(value)
    return None


def _pattern_entries(config: dict[str, Any], key: str) -> list[tuple[str, str]]:
    """Typed ``(kind, pattern)`` entries from a ``*_url_patterns`` list."""
    if key not in config:
        return []
    value = config[key]
    if not isinstance(value, list):
        raise ConfigError(f"SafetyFilter '{key}' must be a list.")
    entries: list[tuple[str, str]] = []
    for item in value:
        if isinstance(item, str):
            if item.startswith("regex:"):
                entries.append(("regex", item[len("regex:") :]))
            elif item.startswith("glob:"):
                entries.append(("glob", item[len("glob:") :]))
            else:
                entries.append(("glob", item))
        elif isinstance(item, dict):
            kind = item.get("kind", item.get("type", "glob"))
            pattern = item.get("pattern", item.get("glob", item.get("regex")))
            if kind not in ("glob", "regex") or not isinstance(pattern, str):
                raise ConfigError(
                    f"SafetyFilter '{key}' mappings need a string 'pattern' "
                    "and 'kind' of 'glob' or 'regex'."
                )
            entries.append((str(kind), pattern))
        else:
            raise ConfigError(
                f"SafetyFilter '{key}' entries must be strings or mappings."
            )
    return entries


def _compile_url_patterns(config: dict[str, Any], base: str) -> re.Pattern[str] | None:
    """Compile one list family into a single alternation, once.

    ``base`` is e.g. ``"blocked"``/``"allowed"``; reads ``<base>_url_globs``,
    ``<base>_url_regexes`` and ``<base>_url_patterns``. Returns ``None``
    when no patterns are configured.
    """
    globs = _string_list(config, f"{base}_url_globs") or []
    regexes = (
        _string_list(config, f"{base}_url_regexes", f"{base}_url_regex_patterns") or []
    )
    typed = _pattern_entries(config, f"{base}_url_patterns")
    globs.extend(p for kind, p in typed if kind == "glob")
    regexes.extend(p for kind, p in typed if kind == "regex")
    total = len(globs) + len(regexes)
    if total > MAX_URL_PATTERNS_PER_LIST:
        raise ConfigError(
            f"SafetyFilter '{base}_url_*' allows at most "
            f"{MAX_URL_PATTERNS_PER_LIST} patterns, got {total}."
        )
    branches: list[str] = []
    for pattern in [*globs, *regexes]:
        if not pattern or len(pattern) > MAX_URL_PATTERN_LENGTH:
            raise ConfigError(
                f"SafetyFilter '{base}_url_*' patterns must be 1.."
                f"{MAX_URL_PATTERN_LENGTH} characters."
            )
    branches.extend(fnmatch.translate(pattern) for pattern in globs)
    for pattern in regexes:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(
                f"SafetyFilter '{base}_url_regexes' has an invalid "
                f"regex {pattern!r}: {exc}."
            ) from exc
        branches.append(f"(?:{pattern})")
    if not branches:
        return None
    return re.compile("|".join(branches), re.IGNORECASE)


class SafetyFilter(BasePlugin):
    """Exclusion filter over title/snippet/url. Mandatory (fail closed)."""

    @property
    def name(self) -> str:
        return "safety-filter"

    @property
    def plugin_type(self) -> PluginType:
        return PluginType.FILTER

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.FILTERING

    @property
    def mandatory(self) -> bool:
        return True

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)
        terms = self._config.get("blocked_terms", list(DEFAULT_BLOCKED_TERMS))
        domains = self._config.get("blocked_domains", [])
        if not isinstance(terms, list) or not all(isinstance(t, str) for t in terms):
            raise ConfigError("SafetyFilter 'blocked_terms' must be a list of strings.")
        if not isinstance(domains, list) or not all(
            isinstance(d, str) for d in domains
        ):
            raise ConfigError(
                "SafetyFilter 'blocked_domains' must be a list of strings."
            )
        allowed = _string_list(self._config, "allowed_domains", "allowlist_domains")
        self._blocked_terms = [t.lower() for t in terms]
        self._blocked_exact, self._blocked_wildcards = _domain_sets(
            [d.lower() for d in domains], "blocked_domains"
        )
        self._allowed_exact, self._allowed_wildcards = _domain_sets(
            [d.lower() for d in (allowed or [])], "allowed_domains"
        )
        self._blocked_url_pattern = _compile_url_patterns(self._config, "blocked")
        self._allowed_url_pattern = _compile_url_patterns(self._config, "allowed")
        self._simulate_outage = bool(self._config.get("simulate_outage", False))

    def _is_blocked(self, title: str, snippet: str, url: str, host: str) -> bool:
        haystack = f"{title}\n{snippet}\n{url}".lower()
        if any(term in haystack for term in self._blocked_terms):
            return True
        # Host-set tier (set lookup on the parsed host) before any regex.
        if _host_matches(host, self._blocked_exact, self._blocked_wildcards):
            return True
        return bool(
            url
            and self._blocked_url_pattern is not None
            and self._blocked_url_pattern.search(url)
        )

    def _is_allowed(self, url: str, host: str) -> bool:
        if _host_matches(host, self._allowed_exact, self._allowed_wildcards):
            return True
        return bool(
            url
            and self._allowed_url_pattern is not None
            and self._allowed_url_pattern.search(url)
        )

    def run(self, context: SearchContext) -> SearchContext:
        if self._simulate_outage:
            raise PluginError(
                "safety-filter backend unavailable",
                plugin_name=self.name,
                stage=self.stage,
                mandatory=True,
            )
        child_safe = context.request.mode == _CHILD_SAFE_MODE
        kept = []
        removed = 0
        for result in context.results:
            url = result.url or ""
            host = _host_of(url)
            if self._is_blocked(result.title, result.snippet, url, host):
                removed += 1
                continue
            if child_safe and not self._is_allowed(url, host):
                removed += 1
                continue
            kept.append(result)
        context.results = kept
        context.applied_filters.append(self.name)
        context.metadata["safety-filter-removed"] = removed
        return context
