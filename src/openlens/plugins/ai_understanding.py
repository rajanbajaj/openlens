"""AI query-understanding plugin: post-retrieval intent/entity labeling.

V1 is post-retrieval intent/entity LABELING only.
It does not influence retrieval: it never changes which documents are
fetched, never rewrites
``context.request.query`` for retrieval, never reorders results, and never
mutates scores or ``context.results`` membership. It only writes labeling
metadata (intent/entities plus the ``ai_model`` provenance label).

Influencing retrieval would require the shared pre-retrieval hook from
the query-intelligence seam S1. That hook is a declared dependency of a
future version, not claimed here: this plugin runs in-pipeline at the
QUERY_PARSING stage, which the engine executes after retrieval today,
and it does not claim retrieval influence.

The plugin uses the injected model (manager-primary seam, see
:meth:`set_model` / the ``model`` constructor argument) when a backend is
available. When no backend is injected (disabled / no-backend), or when
the model raises :class:`ProviderError` mid-query, it falls back to
deterministic rule-based keyword/pattern heuristics. Rule output is
labeled ``ai_model: rule-based``; model output is labeled with the
backend name (``ai_model: <backend name>``). A model failure is recorded
in diagnostics (``SearchContext.errors`` / context metadata) and the
request still succeeds: this is an optional plugin, so model failure
never fails the request.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    PipelineStage,
    PluginType,
    ProviderError,
)
from openlens.models.interfaces import AIModelProvider
from openlens.plugins.base import BasePlugin
from openlens.retrieval.query_intent import (
    INTENT_LABELS,
    QueryIntentClassifier,
)

#: Provenance label used whenever rule-based heuristics produced output.
RULE_BASED_LABEL = "rule-based"

_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "at",
        "be",
        "best",
        "buy",
        "cheap",
        "do",
        "does",
        "for",
        "from",
        "get",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "near",
        "of",
        "on",
        "online",
        "or",
        "please",
        "show",
        "the",
        "to",
        "under",
        "what",
        "when",
        "where",
        "who",
        "why",
        "with",
    }
)

# Intent cue words are classified, not entities.
_CUE_WORDS: frozenset[str] = frozenset(
    {
        "app",
        "booking",
        "breaking",
        "compare",
        "coupon",
        "deal",
        "deals",
        "discount",
        "download",
        "election",
        "elections",
        "explain",
        "flight",
        "flights",
        "guide",
        "headlines",
        "homepage",
        "hotel",
        "hotels",
        "latest",
        "login",
        "news",
        "official",
        "order",
        "portal",
        "price",
        "results",
        "review",
        "reviews",
        "sale",
        "shop",
        "shopping",
        "signin",
        "signup",
        "store",
        "today",
        "travel",
        "trip",
        "tutorial",
        "update",
        "vacation",
        "website",
    }
)


def _rule_entities(query: str) -> list[str]:
    """Extract entities via quoted/capitalized/pattern heuristics."""
    entities: list[str] = []

    def _add(value: str) -> None:
        cleaned = value.strip(" \t\r\n\"'.,;:!?()[]{}")
        if cleaned and cleaned.casefold() not in {e.casefold() for e in entities}:
            entities.append(cleaned)

    for match in re.findall(r'"([^"]+)"', query):
        _add(match)
    # Capitalized sequences (e.g. "New York", "Paris") in the raw query.
    for match in re.findall(r"\b(?:[A-Z][\w'-]*)(?:\s+[A-Z][\w'-]*)*\b", query):
        lowered = match.casefold()
        if lowered not in _STOPWORDS and lowered not in _CUE_WORDS:
            _add(match)
    for token in re.findall(r"[A-Za-z][A-Za-z'-]*|\d+(?:[.,]\d+)?", query):
        lowered = token.casefold()
        if token.isdigit() or any(ch.isdigit() for ch in token):
            _add(token)
        elif (
            lowered not in _STOPWORDS
            and lowered not in _CUE_WORDS
            and lowered not in INTENT_LABELS
            and len(lowered) > 2
        ):
            _add(lowered)
    return entities[:8]


def _parse_model_output(text: Any) -> tuple[str | None, list[str]]:
    """Parse intent/entities from a model completion, tolerantly."""
    if not isinstance(text, str) or not text.strip():
        return None, []
    stripped = text.strip()
    candidates = [stripped]
    brace = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if brace:
        candidates.append(brace.group(0))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            intent_raw = (
                parsed.get("intent") or parsed.get("label") or parsed.get("category")
            )
            intent = (
                intent_raw.strip().casefold() if isinstance(intent_raw, str) else None
            )
            if intent not in INTENT_LABELS:
                intent = None
            raw_entities = parsed.get("entities") or parsed.get("entity") or []
            if isinstance(raw_entities, str):
                raw_entities = [raw_entities]
            parsed_entities = (
                [
                    str(item).strip()
                    for item in raw_entities
                    if isinstance(item, (str, int, float))
                    and str(item).strip()
                    and str(item).strip() not in {"...", "<intent>"}
                    and not str(item).strip().startswith("<")
                ]
                if isinstance(raw_entities, list)
                else []
            )
            return intent, parsed_entities[:8]
    intent_match = re.search(
        r"intent\s*[:=]\s*['\"]?([a-z][a-z_-]*)", stripped, flags=re.IGNORECASE
    )
    intent = intent_match.group(1).casefold() if intent_match else None
    if intent not in INTENT_LABELS:
        intent = None
    entities: list[str] = []
    entities_match = re.search(r"entities\s*[:=]\s*(.+)", stripped, flags=re.IGNORECASE)
    if entities_match:
        entities = [
            part.strip(" \t\"'[]")
            for part in re.split(r"[,;]", entities_match.group(1))
            if part.strip(" \t\"'[]")
            and part.strip(" \t\"'[]") != "..."
            and not part.strip(" \t\"'[]").startswith("<")
        ][:8]
    if intent is None and stripped.casefold() in INTENT_LABELS:
        intent = stripped.casefold()
    return intent, entities


class AIUnderstandingPlugin(BasePlugin):
    """Labels query intent/entities post-retrieval; never affects retrieval.

    This plugin does not influence retrieval (see module docstring). The
    shared pre-retrieval hook from query-intelligence seam S1 is a
    declared dependency for any future retrieval influence, not claimed
    here.
    """

    @property
    def name(self) -> str:
        return "ai-understanding"

    @property
    def plugin_type(self) -> PluginType:
        return PluginType.ENRICHER

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.QUERY_PARSING

    @property
    def mandatory(self) -> bool:
        return False

    @property
    def dependencies(self) -> tuple[str, ...]:
        # Declared dependency on the shared pre-retrieval hook (query-
        # intelligence seam S1) for future retrieval influence. It is not
        # a pipeline plugin dependency and is not claimed/required here,
        # so it is intentionally not listed as a plugin dependency that
        # the pipeline would try to resolve.
        return ()

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        model: Optional[AIModelProvider] = None,
    ) -> None:
        self._model: AIModelProvider | None = model
        self._classifier = QueryIntentClassifier()
        self._simulate_failure = False
        super().__init__(config)

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)
        self._simulate_failure = bool(self._config.get("simulate_failure", False))
        # Optional per-plugin model mapping is tolerated for symmetry with
        # AISummaryPlugin construction in tests, but the manager-primary
        # injection (set_model) wins whenever the registry/engine supplies
        # a model after configure.
        model_config = self._config.get("model")
        if model_config is None:
            return
        if isinstance(model_config, AIModelProvider):
            self._model = model_config
            return
        if not isinstance(model_config, dict):
            raise ConfigError(
                "AIUnderstandingPlugin 'model' must be a backend mapping."
            )
        model_type = model_config.get("type", "fake")
        if not isinstance(model_type, str) or not model_type:
            raise ConfigError(
                "AIUnderstandingPlugin 'model.type' must be a non-empty string."
            )
        options = {k: v for k, v in model_config.items() if k != "type"}
        from openlens.models.registry import create as create_model
        from openlens.models.registry import register_builtin_models

        register_builtin_models()
        created = create_model(model_type, options)
        # Only adopt config-built model when none was injected directly.
        if self._model is None:
            self._model = created

    @property
    def model(self) -> AIModelProvider | None:
        """The injected model backend, or None when running rule-based."""
        return self._model

    def set_model(self, model: AIModelProvider | None) -> None:
        """Inject the manager-primary backend (manager-primary seam)."""
        self._model = model

    # -- labeling -----------------------------------------------------

    def _rule_labels(self, query: str) -> tuple[str, list[str]]:
        intent = self._classifier.classify(query.casefold()).intent
        return intent, _rule_entities(query)

    def _model_labels(
        self, query: str, rule_intent: str, rule_entities: list[str]
    ) -> tuple[str, list[str]]:
        """Label via the model. Raises ProviderError on model failure."""
        model = self._model
        if model is None:  # pragma: no cover - guarded by caller
            raise ProviderError(
                "no model backend available",
                provider_name="ai-understanding",
                code=ProviderError.UNAVAILABLE,
            )
        if self._simulate_failure:
            raise ProviderError(
                "ai backend unavailable (simulate_failure)",
                provider_name=getattr(model, "name", "model"),
                code=ProviderError.UNAVAILABLE,
            )
        classify_intent: str | None = None
        classify = getattr(model, "classify", None)
        if callable(classify):
            # Rule intent first: FakeModelProvider returns the first label
            # present in the text, else labels[0]; ordering keeps its
            # fallback aligned with the deterministic rule result.
            labels = [rule_intent] + [
                label for label in INTENT_LABELS if label != rule_intent
            ]
            try:
                raw_label = classify(query, labels)
            except ProviderError:
                raise
            except Exception as exc:
                raise ProviderError(
                    f"model classify failed: {exc}",
                    provider_name=getattr(model, "name", "model"),
                    code=ProviderError.UNKNOWN,
                ) from exc
            if isinstance(raw_label, str) and raw_label.casefold() in INTENT_LABELS:
                classify_intent = raw_label.casefold()
        completion_intent: str | None = None
        completion_entities: list[str] = []
        complete = getattr(model, "complete", None)
        if callable(complete):
            prompt = f"Understand this search query: {query}"
            try:
                completion = complete(prompt)
            except ProviderError:
                raise
            except Exception as exc:
                raise ProviderError(
                    f"model complete failed: {exc}",
                    provider_name=getattr(model, "name", "model"),
                    code=ProviderError.UNKNOWN,
                ) from exc
            completion_intent, completion_entities = _parse_model_output(completion)
        intent = completion_intent or classify_intent or rule_intent
        entities = completion_entities or rule_entities
        return intent, entities

    def run(self, context: SearchContext) -> SearchContext:
        query = context.request.query if context.request is not None else ""
        rule_intent, rule_entities = self._rule_labels(query)
        intent, entities = rule_intent, list(rule_entities)
        ai_model = RULE_BASED_LABEL
        fallback = False
        fallback_error: str | None = None

        if self._model is not None:
            try:
                intent, entities = self._model_labels(query, rule_intent, rule_entities)
                backend_name = getattr(self._model, "name", None)
                ai_model = (
                    backend_name
                    if isinstance(backend_name, str) and backend_name
                    else RULE_BASED_LABEL
                )
            except ProviderError as exc:
                # Optional-plugin isolation, handled internally so rule
                # labels still land: request succeeds, diagnostics record
                # the fallback instead of a failure/500.
                fallback = True
                fallback_error = str(exc)
                intent, entities = rule_intent, list(rule_entities)
                ai_model = RULE_BASED_LABEL
                context.fallback_used = True
                context.add_error(
                    f"{self.name}: model failed ({exc}); "
                    "fell back to rule-based labeling"
                )
            except Exception as exc:  # noqa: BLE001 — optional plugin, fall back
                fallback = True
                fallback_error = str(exc)
                intent, entities = rule_intent, list(rule_entities)
                ai_model = RULE_BASED_LABEL
                context.fallback_used = True
                context.add_error(
                    f"{self.name}: model failed ({exc}); "
                    "fell back to rule-based labeling"
                )

        understanding: dict[str, Any] = {
            "intent": intent,
            "entities": list(entities),
            "ai_model": ai_model,
            "model": ai_model,
            "source": "rule-based" if ai_model == RULE_BASED_LABEL else "model",
            "fallback": fallback,
            "fallback_used": fallback,
            "error": fallback_error,
        }
        context.metadata["ai_understanding"] = understanding
        context.metadata["ai_intent"] = intent
        context.metadata["ai_entities"] = list(entities)
        context.metadata["ai_model"] = ai_model
        for result in context.results:
            result.metadata["ai_intent"] = intent
            result.metadata["ai_entities"] = list(entities)
            result.metadata["ai_model"] = ai_model
            result.metadata["intent"] = intent
            result.metadata["entities"] = list(entities)
            result.metadata["ai_understanding"] = dict(understanding)
            if fallback:
                result.metadata["ai_fallback"] = True
                result.metadata["ai_error"] = fallback_error
        return context
