"""Standalone query processor: normalize -> intent -> expand.

:class:`QueryProcessor` is a standalone stage constructed and configured from
``SearchEngine.configure`` / ``build_engine`` via the ``query:`` config
section (:func:`extract_query_section`). It is NOT a ``PluginRegistry``
plugin and does NOT run inside the post-retrieval ``Pipeline`` — the engine
invokes it before provider selection.

Explicit ``mode`` always wins: when the caller passes a non-empty,
non-``"default"`` mode it becomes the effective mode and the intent
suggestion is recorded separately for observability, never applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openlens.core.interfaces import ConfigError
from openlens.ranking.profiles import get_profile
from openlens.retrieval.query_expand import (
    ExpandedTerm,
    QueryExpander,
    QueryExpanderConfig,
)
from openlens.retrieval.query_intent import (
    INTENT_LABELS,
    IntentResult,
    QueryIntentClassifier,
    QueryIntentConfig,
    classify_with_model,
)
from openlens.retrieval.query_normalize import (
    NormalizedQuery,
    QueryNormalizer,
    QueryNormalizerConfig,
)

#: Config key the engine consumes for this stage (no new config file).
QUERY_CONFIG_KEY = "query"


def extract_query_section(app_config: dict[str, Any]) -> dict[str, Any]:
    """Return the ``query:`` section of the app config ({} when absent)."""
    if not isinstance(app_config, dict):
        raise ConfigError("App config must be a mapping.")
    section = app_config.get(QUERY_CONFIG_KEY, {})
    if not isinstance(section, dict):
        raise ConfigError("Config section 'query' must be a mapping.")
    return dict(section)


#: Default cap on history-derived context terms per request.
DEFAULT_MAX_HISTORY_TERMS = 20


@dataclass
class QueryContextConfig:
    """Stage-owned opt-in config for request-scoped history context.

    Disabled by default: with ``enabled=False`` any caller-supplied
    history is ignored (never persisted, never expanded). When
    enabled, up to ``max_history_terms`` normalized terms from prior
    request-scoped queries may join the expansion set.
    """

    enabled: bool = False
    max_history_terms: int = DEFAULT_MAX_HISTORY_TERMS


@dataclass
class QueryProcessorConfig:
    """Combined stage-owned config for normalize + intent + expand."""

    normalizer: QueryNormalizerConfig = field(default_factory=QueryNormalizerConfig)
    intent: QueryIntentConfig = field(default_factory=QueryIntentConfig)
    expansion: QueryExpanderConfig = field(default_factory=QueryExpanderConfig)
    context: QueryContextConfig = field(default_factory=QueryContextConfig)


@dataclass
class ProcessedQuery:
    """One query after normalize -> intent -> expand."""

    original: str
    normalized: str
    truncated: bool
    intent: str
    suggested_mode: str
    matched_keywords: dict[str, list[str]] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    base_terms: list[str] = field(default_factory=list)
    added: list[ExpandedTerm] = field(default_factory=list)
    explicit_mode: str = "default"
    effective_mode: str = "default"
    history_terms: list[str] = field(default_factory=list)
    rewrite_variant: str = ""

    @property
    def added_terms(self) -> list[str]:
        """Added expansion term strings in deterministic order."""
        return [t.term for t in self.added]

    @property
    def provenance(self) -> dict[str, str]:
        """Per-term expansion provenance: added term -> source set id."""
        return {t.term: t.source for t in self.added}


class QueryProcessor:
    """Standalone pre-retrieval query stage."""

    def __init__(self, config: QueryProcessorConfig | None = None) -> None:
        self.config = config or QueryProcessorConfig()
        self._normalizer = QueryNormalizer(self.config.normalizer)
        self._classifier = QueryIntentClassifier(self.config.intent)
        self._expander = QueryExpander(self.config.expansion)
        self._model_backend: Any = None

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "QueryProcessor":
        """Build a configured processor from the ``query:`` section shape."""
        processor = cls()
        processor.configure(config)
        return processor

    def configure(self, config: dict[str, Any]) -> None:
        """Compile all three sub-stages ONCE from the ``query:`` section."""
        if not isinstance(config, dict):
            raise ConfigError("Query processor config must be a mapping.")
        section = config.get(QUERY_CONFIG_KEY, config)
        if not isinstance(section, dict):
            raise ConfigError("Config section 'query' must be a mapping.")
        normalizer_cfg = section.get("normalizer", {})
        intent_cfg = section.get("intent", {})
        expansion_cfg = section.get("expansion", {})
        context_cfg = section.get("context", {})
        if not isinstance(normalizer_cfg, dict):
            raise ConfigError("'query.normalizer' must be a mapping.")
        if not isinstance(intent_cfg, dict):
            raise ConfigError("'query.intent' must be a mapping.")
        if not isinstance(expansion_cfg, dict):
            raise ConfigError("'query.expansion' must be a mapping.")
        if not isinstance(context_cfg, dict):
            raise ConfigError("'query.context' must be a mapping.")
        context_enabled = context_cfg.get("enabled", False)
        if not isinstance(context_enabled, bool):
            raise ConfigError("'query.context.enabled' must be a bool.")
        max_history_terms = context_cfg.get(
            "max_history_terms", DEFAULT_MAX_HISTORY_TERMS
        )
        if (
            isinstance(max_history_terms, bool)
            or not isinstance(max_history_terms, int)
            or max_history_terms < 0
        ):
            raise ConfigError("'query.context.max_history_terms' must be an int >= 0.")
        max_length = normalizer_cfg.get("max_length", 1000)
        on_overflow = normalizer_cfg.get("on_overflow", "truncate")
        if isinstance(max_length, bool) or not isinstance(max_length, int):
            raise ConfigError("'query.normalizer.max_length' must be an int.")
        if max_length <= 0:
            raise ConfigError("'query.normalizer.max_length' must be positive.")
        if on_overflow not in ("truncate", "reject"):
            raise ConfigError(
                "'query.normalizer.on_overflow' must be 'truncate' or 'reject'."
            )
        self.config = QueryProcessorConfig(
            normalizer=QueryNormalizerConfig(
                max_length=max_length,
                on_overflow=on_overflow,  # type: ignore[arg-type]
            ),
            intent=QueryIntentConfig(),
            expansion=QueryExpanderConfig(),
            context=QueryContextConfig(
                enabled=context_enabled,
                max_history_terms=max_history_terms,
            ),
        )
        self._normalizer = QueryNormalizer(self.config.normalizer)
        self._classifier = QueryIntentClassifier()
        self._classifier.configure(intent_cfg)
        self.config.intent = self._classifier.config
        self._expander = QueryExpander()
        self._expander.configure(expansion_cfg)
        self.config.expansion = self._expander.config

    def attach_model_backend(self, backend: Any) -> None:
        """Attach an optional semantic backend (OFF unless enabled in config)."""
        self._model_backend = backend

    def _semantic_intent(self, normalized: str) -> str | None:
        """Optional model hook: bounded call, None means 'no signal'."""
        if not self._classifier.config.semantic_enabled:
            return None
        return classify_with_model(
            normalized,
            self._model_backend,
            list(INTENT_LABELS),
            timeout_s=self._classifier.config.semantic_timeout_s,
        )

    def _history_terms(
        self, history: tuple[str, ...] | list[str], present: set[str], cap: int
    ) -> list[ExpandedTerm]:
        """Derive context terms from request-scoped history (bounded).

        Each prior query is normalized with the stage normalizer; terms
        that belong to a known synonym set and are absent from the
        current query join the expansion set. Only vocabulary the
        index can already match is admitted, so history can recall but
        never invents terms. Deterministic order, hard-capped.
        """
        terms: list[ExpandedTerm] = []
        seen: set[str] = set(present)
        for prior in reversed(history):
            if len(terms) >= cap:
                break
            if not isinstance(prior, str) or not prior.strip():
                continue
            try:
                prior_normalized = self._normalizer.normalize(prior).normalized
            except Exception:  # noqa: S112 — unparseable history entries are skipped
                continue
            for token in prior_normalized.split():
                if len(terms) >= cap:
                    break
                if token in seen:
                    continue
                sets = self._expander._term_to_sets.get(token, [])
                if not sets:
                    continue
                seen.add(token)
                terms.append(ExpandedTerm(term=token, source=sets[0]))
        return terms

    def process(
        self,
        query: str,
        *,
        mode: str = "default",
        history: tuple[str, ...] | list[str] = (),
    ) -> ProcessedQuery:
        """Run normalize -> intent -> expand; explicit mode always wins.

        ``history`` is request-scoped prior queries (oldest first). It
        is honored only when ``query.context.enabled`` is set; otherwise
        it is ignored entirely (never persisted, never expanded).
        """
        normalized: NormalizedQuery = self._normalizer.normalize(query)
        rule_result: IntentResult = self._classifier.classify(normalized.normalized)
        semantic_label = self._semantic_intent(normalized.normalized)
        intent_result = rule_result
        if semantic_label is not None and semantic_label != rule_result.intent:
            from openlens.retrieval.query_intent import INTENT_TO_MODE

            intent_result = IntentResult(
                intent=semantic_label,
                suggested_mode=INTENT_TO_MODE[semantic_label],
                matched_keywords=dict(rule_result.matched_keywords),
                reasons=[
                    *rule_result.reasons,
                    f"semantic hook refined intent to '{semantic_label}'",
                ],
            )
        expansion = self._expander.expand(normalized.normalized)
        added = list(expansion.added)
        present = set(expansion.base_terms) | {t.term for t in added}
        history_terms: list[ExpandedTerm] = []
        if self.config.context.enabled and history:
            room = max(0, self.config.expansion.max_added_terms - len(added))
            cap = min(self.config.context.max_history_terms, room)
            if cap > 0:
                history_terms = self._history_terms(history, present, cap)
                added.extend(history_terms)
        reasons = list(intent_result.reasons)
        if history_terms:
            remembered = ", ".join(sorted({t.term for t in history_terms}))
            reasons.append(f"context: recalled {remembered} from request history")
        rewrite_variant = self._expander.rewrite(normalized.normalized) or ""
        explicit = mode if isinstance(mode, str) else "default"
        get_profile(explicit or "default")
        effective = (
            explicit
            if explicit not in ("", "default")
            else intent_result.suggested_mode
        )
        return ProcessedQuery(
            original=query,
            normalized=normalized.normalized,
            truncated=normalized.truncated,
            intent=intent_result.intent,
            suggested_mode=intent_result.suggested_mode,
            matched_keywords=dict(intent_result.matched_keywords),
            reasons=reasons,
            base_terms=list(expansion.base_terms),
            added=added,
            explicit_mode=explicit,
            effective_mode=effective,
            history_terms=[t.term for t in history_terms],
            rewrite_variant=rewrite_variant,
        )
