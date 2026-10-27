"""Retrieval helpers: parsing, normalization, deduplication."""

from openlens.retrieval.deduplicator import dedupe_key, deduplicate
from openlens.retrieval.normalizer import make_snippet, normalize, normalize_many
from openlens.retrieval.query_parser import ParsedQuery, parse_query

__all__ = [
    "ParsedQuery",
    "dedupe_key",
    "deduplicate",
    "make_snippet",
    "normalize",
    "normalize_many",
    "parse_query",
]
