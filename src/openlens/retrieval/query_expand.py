"""Synonym/related-term query expansion with per-term provenance.

Expansion is returned as a structured term set
(:class:`QueryExpansion`), never as a rewritten query string: downstream
``parse_query`` AND-joins its input, so concatenating synonyms into the
query text would turn OR-synonyms into AND-required terms.

Each added term carries its source set id in :attr:`ExpandedTerm.source`.
Added terms are hard-capped (default <= 8 per query, config-tunable) so an
unbounded synonym fan-out can never build a giant FTS MATCH.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openlens.core.interfaces import ConfigError

#: Default cap on added (expanded) terms per query.
DEFAULT_MAX_ADDED_TERMS = 8

#: Default synonym sets, keyed by the set id recorded as provenance.
DEFAULT_SYNONYM_SETS: dict[str, list[str]] = {
    "apparel": ["dress", "dresses", "gown"],
    "footwear": ["shoe", "shoes", "sneaker", "sneakers"],
    "mobile": ["phone", "phones", "smartphone", "mobile"],
    "air-travel": ["flight", "flights", "airfare"],
    "lodging": ["hotel", "hotels", "lodging"],
    "television": ["tv", "television"],
    "computing": ["laptop", "laptops", "notebook"],
    "pricing": ["cheap", "affordable", "budget"],
}


@dataclass
class ExpandedTerm:
    """One added term plus the id of the synonym set that produced it."""

    term: str
    source: str


@dataclass
class QueryExpansion:
    """Structured expansion: base query terms plus capped added terms."""

    base_terms: list[str] = field(default_factory=list)
    added: list[ExpandedTerm] = field(default_factory=list)

    @property
    def added_terms(self) -> list[str]:
        """Added term strings in deterministic order."""
        return [t.term for t in self.added]

    @property
    def provenance(self) -> dict[str, str]:
        """Per-term provenance: added term -> source set id."""
        return {t.term: t.source for t in self.added}


@dataclass
class QueryExpanderConfig:
    """Stage-owned expansion config; synonym sets live in this dict."""

    synonym_sets: dict[str, list[str]] = field(default_factory=dict)
    max_added_terms: int = DEFAULT_MAX_ADDED_TERMS

    def __post_init__(self) -> None:
        if not self.synonym_sets:
            self.synonym_sets = {k: list(v) for k, v in DEFAULT_SYNONYM_SETS.items()}


class QueryExpander:
    """Config-compiled synonym expander (sets compiled once at configure)."""

    def __init__(self, config: QueryExpanderConfig | None = None) -> None:
        self.config = config or QueryExpanderConfig()
        self._sets: dict[str, frozenset[str]] = {}
        self._term_to_sets: dict[str, list[str]] = {}
        self._configure_from_dataclass(self.config)

    def _configure_from_dataclass(self, config: QueryExpanderConfig) -> None:
        if isinstance(config.max_added_terms, bool) or not isinstance(
            config.max_added_terms, int
        ):
            raise ConfigError("'max_added_terms' must be an int.")
        if config.max_added_terms < 0:
            raise ConfigError("'max_added_terms' must be >= 0.")
        compiled: dict[str, frozenset[str]] = {}
        reverse: dict[str, list[str]] = {}
        for set_id, members in config.synonym_sets.items():
            if not isinstance(members, list) or not all(
                isinstance(m, str) for m in members
            ):
                raise ConfigError(
                    f"Synonym set '{set_id}' must be a list of strings. "
                    "Startup validation failed."
                )
            folded = frozenset(m.casefold() for m in members)
            compiled[set_id] = folded
            for term in folded:
                reverse.setdefault(term, []).append(set_id)
        for set_ids in reverse.values():
            set_ids.sort()
        self._sets = compiled
        self._term_to_sets = reverse

    def configure(self, config: dict[str, Any]) -> None:
        """Compile synonym sets ONCE from this stage's config dict."""
        if not isinstance(config, dict):
            raise ConfigError("Expansion config must be a mapping.")
        synonym_sets = config.get("synonym_sets", None)
        if synonym_sets is None:
            synonym_sets = {k: list(v) for k, v in DEFAULT_SYNONYM_SETS.items()}
        if not isinstance(synonym_sets, dict):
            raise ConfigError("Expansion 'synonym_sets' must be a mapping.")
        max_added = config.get("max_added_terms", DEFAULT_MAX_ADDED_TERMS)
        self.config = QueryExpanderConfig(
            synonym_sets={k: list(v) for k, v in synonym_sets.items()},
            max_added_terms=max_added,
        )
        self._configure_from_dataclass(self.config)

    def expand(self, normalized_query: str) -> QueryExpansion:
        """Expand normalized query terms; deterministic order, hard-capped."""
        if not isinstance(normalized_query, str):
            raise TypeError(
                f"normalized_query must be str, got {type(normalized_query).__name__}"
            )
        base_terms = normalized_query.split()
        present = set(base_terms)
        added: list[ExpandedTerm] = []
        seen: set[str] = set()
        cap = self.config.max_added_terms
        for token in base_terms:
            for set_id in self._term_to_sets.get(token, []):
                for candidate in sorted(self._sets[set_id]):
                    if len(added) >= cap:
                        return QueryExpansion(base_terms=list(base_terms), added=added)
                    if candidate in present or candidate in seen:
                        continue
                    seen.add(candidate)
                    added.append(ExpandedTerm(term=candidate, source=set_id))
        return QueryExpansion(base_terms=list(base_terms), added=added)
