"""Rule-based query-intent classification.

Maps a normalized query to one of five intent labels
(``navigational`` / ``informational`` / ``shopping`` / ``travel`` / ``news``)
using config-driven keyword sets. The sets are compiled ONCE in
:meth:`QueryIntentClassifier.configure` into casefolded set lookups; per
request work is a single pass over the query tokens.

Pinned intent -> mode map (``child-safe`` is never suggested, only ever
explicit)::

    shopping -> shopping, travel -> travel, news -> news,
    navigational -> default, informational -> default

Deterministic tie priority (documented here and in
:attr:`QueryIntentConfig.tie_priority`)::

    navigational > shopping > travel > news > informational

An explicit ``mode`` on the request always wins; the suggestion is recorded
separately for observability and never applied silently.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Literal

from openlens.core.interfaces import ConfigError
from openlens.ranking.profiles import get_profile

IntentLabel = Literal["navigational", "informational", "shopping", "travel", "news"]

INTENT_LABELS: tuple[str, ...] = (
    "navigational",
    "informational",
    "shopping",
    "travel",
    "news",
)

#: Pinned intent -> suggested-mode map. ``child-safe`` is absent by design:
#: it is never suggested, only ever explicit.
INTENT_TO_MODE: dict[str, str] = {
    "shopping": "shopping",
    "travel": "travel",
    "news": "news",
    "navigational": "default",
    "informational": "default",
}

#: Default label when no keyword set matches.
DEFAULT_INTENT: str = "informational"

#: Deterministic tie priority, highest first. Documented in the config
#: schema (:meth:`QueryIntentClassifier.config_schema`).
DEFAULT_TIE_PRIORITY: tuple[str, ...] = (
    "navigational",
    "shopping",
    "travel",
    "news",
    "informational",
)

#: Default config-driven keyword sets, keyed by intent label.
DEFAULT_INTENT_KEYWORDS: dict[str, list[str]] = {
    "navigational": [
        "login",
        "signin",
        "sign in",
        "signup",
        "homepage",
        "official",
        "website",
        "portal",
        "download",
        "app",
    ],
    "shopping": [
        "buy",
        "price",
        "cheap",
        "deal",
        "deals",
        "shop",
        "shopping",
        "dress",
        "dresses",
        "shoes",
        "discount",
        "coupon",
        "sale",
        "store",
        "order",
        "under",
        "best",
    ],
    "travel": [
        "flight",
        "flights",
        "hotel",
        "hotels",
        "trip",
        "travel",
        "vacation",
        "booking",
        "itinerary",
        "visa",
        "beach",
    ],
    "news": [
        "news",
        "breaking",
        "headlines",
        "election",
        "elections",
        "results",
        "today",
        "latest",
        "update",
    ],
    "informational": [
        "how",
        "what",
        "why",
        "when",
        "who",
        "explain",
        "tutorial",
        "guide",
        "review",
        "reviews",
        "compare",
    ],
}


@dataclass
class QueryIntentConfig:
    """Stage-owned intent config; keyword sets live in this dict, not code."""

    keywords: dict[str, list[str]] = field(default_factory=dict)
    tie_priority: tuple[str, ...] = DEFAULT_TIE_PRIORITY
    semantic_enabled: bool = False
    semantic_timeout_s: float = 1.0

    def __post_init__(self) -> None:
        if not self.keywords:
            self.keywords = {k: list(v) for k, v in DEFAULT_INTENT_KEYWORDS.items()}


@dataclass
class IntentResult:
    """Outcome of rule-based classification for one query."""

    intent: str
    suggested_mode: str
    matched_keywords: dict[str, list[str]] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


def classify_with_model(
    text: str,
    backend: Any,
    labels: list[str],
    *,
    timeout_s: float,
) -> str | None:
    """Call ``backend.classify`` bounded by ``timeout_s``; None on any failure.

    Optional semantic hook only. Never raises: timeouts, errors, and unknown
    labels all fall back to the rule-based result (None signals "no signal").
    """
    if backend is None:
        return None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(backend.classify, text, labels)
            label = future.result(timeout=timeout_s)
    except Exception:  # noqa: BLE001 — hook failures always fall back to rules
        return None
    return label if label in labels else None


class QueryIntentClassifier:
    """Config-compiled rule-based intent classifier.

    Lifecycle: constructed standalone, compiled via :meth:`configure` (the
    construction step driven by ``SearchEngine.configure`` / ``build_engine``
    through the ``query:`` config section). Never a ``PluginRegistry`` plugin
    and never run inside the post-retrieval ``Pipeline``.
    """

    def __init__(self, config: QueryIntentConfig | None = None) -> None:
        self.config = config or QueryIntentConfig()
        self._compiled: dict[str, frozenset[str]] = {}
        self._priority: tuple[str, ...] = DEFAULT_TIE_PRIORITY
        self._configure_from_dataclass(self.config)

    @property
    def config_schema(self) -> dict[str, Any]:
        """JSON-schema-like description of this stage's config section."""
        return {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "object",
                    "description": (
                        "Intent keyword sets keyed by intent label. "
                        "Ties break by tie_priority order."
                    ),
                },
                "tie_priority": {
                    "type": "array",
                    "description": (
                        "Deterministic tie priority, highest first. "
                        "Default: navigational > shopping > travel > "
                        "news > informational."
                    ),
                },
                "semantic_enabled": {
                    "type": "boolean",
                    "description": "Optional model hook; OFF by default.",
                },
                "semantic_timeout_s": {
                    "type": "number",
                    "description": "Bound for each optional model call.",
                },
            },
        }

    def _configure_from_dataclass(self, config: QueryIntentConfig) -> None:
        priority = tuple(config.tie_priority)
        if set(priority) != set(INTENT_LABELS):
            raise ConfigError(
                "Intent 'tie_priority' must list each intent label exactly "
                f"once, got {list(priority)!r}. Startup validation failed."
            )
        compiled: dict[str, frozenset[str]] = {}
        for label in INTENT_LABELS:
            terms = config.keywords.get(label, [])
            if not isinstance(terms, list) or not all(
                isinstance(t, str) for t in terms
            ):
                raise ConfigError(
                    f"Intent keywords for '{label}' must be a list of "
                    f"strings, got {terms!r}. Startup validation failed."
                )
            compiled[label] = frozenset(t.casefold() for t in terms)
        self._compiled = compiled
        self._priority = priority

    def configure(self, config: dict[str, Any]) -> None:
        """Compile keyword sets ONCE from this stage's config dict."""
        if not isinstance(config, dict):
            raise ConfigError("Intent config must be a mapping.")
        keywords = config.get("keywords", None)
        if keywords is None:
            keywords = {k: list(v) for k, v in DEFAULT_INTENT_KEYWORDS.items()}
        if not isinstance(keywords, dict):
            raise ConfigError("Intent 'keywords' must be a mapping.")
        priority = config.get("tie_priority", list(DEFAULT_TIE_PRIORITY))
        semantic_enabled = config.get("semantic_enabled", False)
        timeout_s = config.get("semantic_timeout_s", 1.0)
        if not isinstance(semantic_enabled, bool):
            raise ConfigError("'semantic_enabled' must be a boolean.")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
            raise ConfigError("'semantic_timeout_s' must be a number.")
        if not float(timeout_s) > 0:
            raise ConfigError("'semantic_timeout_s' must be positive.")
        self.config = QueryIntentConfig(
            keywords={k: list(v) for k, v in keywords.items()},
            tie_priority=tuple(priority),
            semantic_enabled=semantic_enabled,
            semantic_timeout_s=float(timeout_s),
        )
        self._configure_from_dataclass(self.config)
        # Suggested modes must exist as ranking profiles by construction.
        for label, mode in INTENT_TO_MODE.items():
            get_profile(mode)

    def classify(self, normalized_query: str) -> IntentResult:
        """Classify an already-normalized query string (single token pass)."""
        if not isinstance(normalized_query, str):
            raise TypeError(
                f"normalized_query must be str, got {type(normalized_query).__name__}"
            )
        tokens = set(normalized_query.split())
        matched: dict[str, list[str]] = {}
        for label in INTENT_LABELS:
            hits = sorted(tokens & set(self._compiled.get(label, frozenset())))
            if hits:
                matched[label] = hits
        intent = DEFAULT_INTENT
        for label in self._priority:
            if label in matched:
                intent = label
                break
        reasons = [
            f"intent '{intent}' matched keyword '{kw}'"
            for kw in matched.get(intent, [])
        ] or [f"intent '{intent}' by default (no keyword match)"]
        return IntentResult(
            intent=intent,
            suggested_mode=INTENT_TO_MODE[intent],
            matched_keywords=matched,
            reasons=reasons,
        )
