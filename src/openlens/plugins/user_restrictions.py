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
accepted and passed through — never rejected, never type-checked here.
Any ``prefer_*`` key (scoring pack) plus ``max_price``, ``location``,
and ``facilities`` are ordering-only hints consumed downstream by the
commerce reranker. ``exclude_sponsored``, ``min_rating``,
``exclude_over_budget``, ``license``, ``author``, ``cuisine``,
``dietary_tags``, ``ingredients``, ``exclude_allergens``,
``brand``, ``style``, ``size``, ``occasion``, and ``site_category``
are additionally hard-enforced here on crawler-truth metadata:

- ``brand``/``style``/``size``/``occasion`` (string or list of
  strings): a hit is kept only if *every* requested term appears in
  the hit's same-named metadata — compared case-insensitively, either
  as the whole normalized value or as a member of its comma-separated
  term set (the crawler folds color/material/pattern/category into a
  comma-joined ``style`` string). Malformed values (non-string entries,
  empty) leave the key inactive — never fail-closed.
- ``site_category`` (string or list of strings): a hit is kept only if
  its ``site_category`` (falling back to ``category``) metadata term
  set intersects the requested set. Allowed values are exactly
  ``shopping``/``news``/``kids``/``other``. A well-formed but unknown
  value (e.g. ``"gaming"``) raises ``PluginError`` (fail-closed);
  malformed values (non-string/list payloads, empty) leave the key
  inactive. Hits carrying no category marker are excluded while the
  key is active.

Only keys outside the owned schema and that reservation are rejected.
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
from openlens.plugins._signals import coerce_sponsored_marker
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

#: Reserved consumer keys (review D4).
#:
#: ``exclude_sponsored``/``min_rating`` are hard-enforced here on the
#: crawler-truth flags (``metadata["sponsored"]`` / ``metadata["rating"]``)
#: so "exclude sponsored" and "minimum rating" remove hits instead of
#: merely demoting them. Sponsored markers are read with truthy
#: semantics (``True``/``1``/``"true"``/``"yes"`` plus the federated
#: ``is_sponsored`` spelling) and canonicalized to real ``True`` at this
#: boundary — the earliest owned touchpoint — so the scorer and ranker
#: observe the canonical flag. The vertical keys below are likewise hard
#: exclusion filters: ``exclude_over_budget`` gates ``max_price`` (off by
#: default so bare ``max_price`` stays ordering-only for the commerce
#: reranker); ``license``/``author``/``cuisine``/``dietary_tags``/
#: ``ingredients``/``exclude_allergens`` match book/recipe metadata;
#: ``brand``/``style``/``size``/``occasion`` match product metadata
#: (every requested term must appear in the same-named metadata field,
#: case-insensitively); ``site_category`` restricts hits to the
#: requested slice(s) of ``shopping``/``news``/``kids``/``other`` and is
#: fail-closed on unknown values. All vertical keys except
#: ``site_category`` parse leniently (malformed values are ignored, never
#: fail-closed). The remaining keys are consumed downstream by the
#: commerce reranker for ordering.
#:
#: Control keys (validated fail-closed here, consumed downstream):
#: ``personalize`` (bool, default true) opts out of preference boosts;
#: ``weight_overrides`` (mapping) tunes ranking weights in the ranker;
#: ``safety_preset`` (one of ``strict``/``balanced``/``standard``)
#: selects a named safety policy (``strict`` arms default-deny
#: allowlist enforcement in the safety filter for this request).
RESERVED_FILTER_KEYS: frozenset[str] = frozenset(
    {
        "max_price",
        "location",
        "facilities",
        "min_rating",
        "exclude_sponsored",
        "exclude_over_budget",
        "brand",
        "style",
        "size",
        "occasion",
        "license",
        "author",
        "cuisine",
        "dietary_tags",
        "ingredients",
        "exclude_allergens",
        "site_category",
        "personalize",
        "weight_overrides",
        "safety_preset",
    }
)

#: Named safety policies selectable per request via ``safety_preset``.
SAFETY_PRESET_VALUES: frozenset[str] = frozenset({"strict", "balanced", "standard"})

#: Closed value set for the ``site_category`` filter. Unknown *values*
#: (well-formed strings outside this set) are fail-closed; malformed
#: payloads (non-string/list, empty) leave the filter inactive.
SITE_CATEGORY_VALUES: frozenset[str] = frozenset({"shopping", "news", "kids", "other"})

#: Metadata keys consulted by the ``site_category`` filter, in order.
#: ``site_category`` is canonical; ``category`` is the provider-declared
#: fallback spelling. Terms from every present key are unioned.
SITE_CATEGORY_METADATA_KEYS: tuple[str, ...] = ("site_category", "category")

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


def _is_truthy(value: Any) -> bool:
    """True for explicit opt-ins (True / 1 / "true"/"1"/"yes")."""
    if value is True:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


def _as_price_cap(value: Any) -> tuple[float | None, str]:
    """Coerce a ``max_price`` filter to (cap, currency); (None, "") when absent.

    Accepts a bare number/numeric string or a mapping with a
    ``value``/``amount``/``price`` entry plus an optional ``currency``.
    """
    if value is None or isinstance(value, bool):
        return None, ""
    if isinstance(value, dict):
        cap: float | None = None
        for key in ("value", "amount", "price"):
            parsed = _as_threshold(value.get(key))
            if parsed is not None:
                cap = parsed
                break
        if cap is None:
            return None, ""
        currency = value.get("currency")
        return cap, currency.strip().upper() if isinstance(currency, str) else ""
    cap = _as_threshold(value)
    return (cap, "") if cap is not None else (None, "")


def _as_float_or_none(value: Any) -> float | None:
    """Coerce result metadata to float; None when absent/unparseable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", ""))
        except ValueError:
            return None
    return None


def _as_single_term(value: Any) -> str | None:
    """Coerce a one-term filter (``license``/``author``/``cuisine``)."""
    if isinstance(value, list):
        items = [v.strip() for v in value if isinstance(v, str) and v.strip()]
        if not items:
            return None
        value = items[0]
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _as_term_list(value: Any) -> list[str] | None:
    """Coerce a tag-list filter; None when absent/empty (filter inactive)."""
    if isinstance(value, str):
        value = [part for part in value.split(",") if part.strip()]
    if not isinstance(value, (list, tuple)):
        return None
    terms = [v.strip().lower() for v in value if isinstance(v, str) and v.strip()]
    return terms or None


def _meta_terms(value: Any) -> list[str]:
    """Coerce result metadata to a lower-cased term list ([] when absent)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [p.lower() for p in (s.strip() for s in value.split(",")) if p]
    if isinstance(value, (list, tuple)):
        return [v.strip().lower() for v in value if isinstance(v, str) and v.strip()]
    return []


def _as_threshold(value: Any) -> float | None:
    """Coerce a ``min_rating`` filter to float; None when absent/invalid."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


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

    def _validate_control_keys(self, filters: dict[str, Any]) -> None:
        """Fail closed on malformed personalization/safety control keys.

        ``personalize`` must be a bool when present; ``safety_preset``
        must name a known policy. ``weight_overrides`` shape is the
        ranker's job (validated there); presence here only reserves the
        key so it passes through.
        """
        if "personalize" in filters and not isinstance(filters["personalize"], bool):
            raise self._request_error(
                "UserRestrictionsFilter 'personalize' must be a bool."
            )
        if "safety_preset" in filters:
            preset = filters["safety_preset"]
            if (
                not isinstance(preset, str)
                or preset.strip().lower() not in SAFETY_PRESET_VALUES
            ):
                raise self._request_error(
                    "UserRestrictionsFilter 'safety_preset' must be one of "
                    f"{sorted(SAFETY_PRESET_VALUES)}."
                )

    def _compile_site_category(self, filters: dict[str, Any]) -> frozenset[str] | None:
        """Compile the ``site_category`` filter (fail-closed on unknowns).

        Returns the requested category set, or None when the key is
        absent/malformed (filter inactive). A well-formed string or list
        entry outside ``SITE_CATEGORY_VALUES`` raises a mandatory
        ``PluginError``. Non-string entries inside a list are malformed
        (ignored, never fail-closed); a non-string/non-list payload or an
        empty result leaves the filter inactive.
        """
        if "site_category" not in filters:
            return None
        raw = filters["site_category"]
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return None  # Malformed payload: lenient, filter inactive.
        wanted: set[str] = set()
        for entry in raw:
            if not isinstance(entry, str):
                continue  # Malformed entry: ignored, never fail-closed.
            term = entry.strip().lower()
            if not term:
                continue
            if term not in SITE_CATEGORY_VALUES:
                raise self._request_error(
                    f"UserRestrictionsFilter 'site_category' has unknown value "
                    f"{entry!r}. Allowed: {sorted(SITE_CATEGORY_VALUES)}."
                )
            wanted.add(term)
        return frozenset(wanted) or None

    def _compile_predicate(
        self, filters: dict[str, Any]
    ) -> Callable[[SearchResult], bool]:
        """Validate ``filters`` and compile the per-request predicate once."""
        self._validate_control_keys(filters)
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
                    f"{sorted(RESERVED_FILTER_KEYS)} and 'prefer_*' "
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

        exclude_sponsored = _is_truthy(filters.get("exclude_sponsored"))
        min_rating = _as_threshold(filters.get("min_rating"))
        exclude_over_budget = _is_truthy(filters.get("exclude_over_budget"))
        budget_cap, budget_currency = _as_price_cap(filters.get("max_price"))
        license_term = _as_single_term(filters.get("license"))
        author_term = _as_single_term(filters.get("author"))
        cuisine_term = _as_single_term(filters.get("cuisine"))
        dietary_terms = _as_term_list(filters.get("dietary_tags"))
        ingredient_terms = _as_term_list(filters.get("ingredients"))
        allergen_terms = _as_term_list(filters.get("exclude_allergens"))
        brand_terms = _as_term_list(filters.get("brand"))
        style_terms = _as_term_list(filters.get("style"))
        size_terms = _as_term_list(filters.get("size"))
        occasion_terms = _as_term_list(filters.get("occasion"))
        site_categories = self._compile_site_category(filters)

        must_patterns = [
            re.compile(re.escape(term), re.IGNORECASE) for term in merged["must"]
        ]
        must_not_patterns = [
            re.compile(re.escape(term), re.IGNORECASE) for term in merged["must_not"]
        ]
        include_domains = frozenset(merged["include_domains"])
        exclude_domains = frozenset(merged["exclude_domains"])

        def _attribute_terms(metadata: dict[str, Any], key: str) -> set[str]:
            """Term set for one product attribute: whole value + split terms."""
            raw = metadata.get(key)
            terms = set(_meta_terms(raw))
            if isinstance(raw, str) and raw.strip():
                terms.add(raw.strip().lower())
            return terms

        def predicate(result: SearchResult) -> bool:
            sponsored = coerce_sponsored_marker(result.metadata)
            if exclude_sponsored and sponsored:
                return False
            if min_rating is not None:
                rating = result.metadata.get("rating")
                if isinstance(rating, bool):
                    return False
                if isinstance(rating, (int, float)):
                    if float(rating) < min_rating:
                        return False
                elif isinstance(rating, str):
                    try:
                        if float(rating.strip()) < min_rating:
                            return False
                    except ValueError:
                        return False
                else:
                    return False
            if exclude_over_budget and budget_cap is not None:
                price = _as_float_or_none(result.metadata.get("price"))
                if price is not None:
                    if budget_currency:
                        actual = result.metadata.get("currency")
                        actual_code = (
                            actual.strip().upper() if isinstance(actual, str) else ""
                        )
                        if actual_code and actual_code != budget_currency:
                            pass  # Incomparable currency: keep (no evidence).
                        elif price > budget_cap:
                            return False
                    elif price > budget_cap:
                        return False
            if license_term is not None:
                actual = result.metadata.get("license")
                if (
                    not isinstance(actual, str)
                    or actual.strip().lower() != license_term.lower()
                ):
                    return False
            if author_term is not None:
                actual = result.metadata.get("author")
                if (
                    not isinstance(actual, str)
                    or author_term.lower() not in actual.lower()
                ):
                    return False
            if cuisine_term is not None:
                actual = result.metadata.get("cuisine")
                if (
                    not isinstance(actual, str)
                    or actual.strip().lower() != cuisine_term.lower()
                ):
                    return False
            if dietary_terms is not None:
                have: set[str] | list[str] = set(
                    _meta_terms(result.metadata.get("dietary_tags"))
                )
                if any(term not in have for term in dietary_terms):
                    return False
            if ingredient_terms is not None:
                have = _meta_terms(result.metadata.get("ingredients"))
                if not have or any(
                    not any(term in item for item in have) for term in ingredient_terms
                ):
                    return False
            if allergen_terms is not None:
                have = _meta_terms(result.metadata.get("allergens"))
                if any(
                    term in item or item in term
                    for term in allergen_terms
                    for item in have
                ):
                    return False
            for key, wanted in (
                ("brand", brand_terms),
                ("style", style_terms),
                ("size", size_terms),
                ("occasion", occasion_terms),
            ):
                if wanted is not None:
                    have = _attribute_terms(result.metadata, key)
                    if any(term not in have for term in wanted):
                        return False
            if site_categories is not None:
                have_categories: set[str] = set()
                for meta_key in SITE_CATEGORY_METADATA_KEYS:
                    have_categories.update(_meta_terms(result.metadata.get(meta_key)))
                if not (have_categories & set(site_categories)):
                    return False
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
        preset = filters.get("safety_preset")
        if isinstance(preset, str) and preset.strip():
            context.applied_filters.append(f"safety_preset:{preset.strip().lower()}")
        if filters.get("personalize") is False:
            context.applied_filters.append("personalize:false")
        context.metadata["user-restrictions-removed"] = removed
        return context
