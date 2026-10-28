"""Baseline relevance scorer: query-term overlap over title + snippet.

Optional plugin: rescores ``final_score`` while preserving ``original_score``
and records a human-readable reason per hit. Deterministic and dependency-free.
"""

from __future__ import annotations

import re
from typing import Any

from openlens.core.context import SearchContext
from openlens.core.interfaces import (
    ConfigError,
    PipelineStage,
    PluginType,
)
from openlens.plugins.base import BasePlugin

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens."""
    return _TOKEN.findall(text.lower())


class BaselineRelevanceScorer(BasePlugin):
    """Adds a term-overlap bonus to ``final_score``; never touches originals."""

    @property
    def name(self) -> str:
        return "baseline-scorer"

    @property
    def plugin_type(self) -> PluginType:
        return PluginType.SCORER

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.SCORING

    @property
    def mandatory(self) -> bool:
        return False

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)
        try:
            self._title_weight = float(self._config.get("title_weight", 2.0))
            self._snippet_weight = float(self._config.get("snippet_weight", 1.0))
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"BaselineRelevanceScorer weights must be numbers: {exc}"
            ) from exc

    def score_hit(
        self, query_terms: list[str], title: str, snippet: str
    ) -> tuple[float, int]:
        """Return (bonus, matched_term_count) for one hit."""
        title_tokens = tokenize(title)
        snippet_tokens = tokenize(snippet)
        matched = 0
        bonus = 0.0
        for term in query_terms:
            title_hits = title_tokens.count(term)
            snippet_hits = snippet_tokens.count(term)
            if title_hits or snippet_hits:
                matched += 1
                bonus += (
                    self._title_weight * title_hits
                    + self._snippet_weight * snippet_hits
                )
        return bonus, matched

    def run(self, context: SearchContext) -> SearchContext:
        query_terms = tokenize(context.request.query)
        for result in context.results:
            bonus, matched = self.score_hit(query_terms, result.title, result.snippet)
            result.final_score = result.original_score + bonus
            result.reasons.append(
                f"matched {matched}/{len(query_terms)} query terms (+{bonus:.2f})"
            )
        return context
