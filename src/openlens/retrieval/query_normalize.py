"""Query-text normalizer: raw user input -> stable normalized query text.

Unicode NFKC, casefold, whitespace collapse, and a punctuation policy that
maps punctuation to spaces (never deletes it) so token boundaries survive
and downstream ``parse_query`` output stays stable.

This module normalizes QUERY text only. It must not be confused with
``retrieval/normalizer.py``, which normalizes retrieval RESULTS.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Literal

DEFAULT_MAX_QUERY_LENGTH = 1000

OverflowPolicy = Literal["truncate", "reject"]


class QueryTooLongError(ValueError):
    """Raised when a query exceeds the normalizer bound under "reject"."""


@dataclass
class QueryNormalizerConfig:
    """Stage-owned bound; never assumes the API 1000-char wire cap."""

    max_length: int = DEFAULT_MAX_QUERY_LENGTH
    on_overflow: OverflowPolicy = "truncate"


@dataclass
class NormalizedQuery:
    original: str
    normalized: str
    truncated: bool = False


def _punct_to_space(text: str) -> str:
    # Keep letters/digits (all scripts) and whitespace; every other char
    # becomes a space so "hello,world" stays two tokens, never "helloworld".
    return "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text)


def normalize_query(
    query: str,
    *,
    max_length: int = DEFAULT_MAX_QUERY_LENGTH,
    on_overflow: OverflowPolicy = "truncate",
) -> NormalizedQuery:
    """Normalize raw query text and enforce this stage's own input bound."""
    if not isinstance(query, str):
        raise TypeError(f"query must be str, got {type(query).__name__}")
    if on_overflow not in ("truncate", "reject"):
        raise ValueError("on_overflow must be 'truncate' or 'reject'")
    if max_length <= 0:
        raise ValueError("max_length must be positive")

    truncated = False
    working = query
    if len(working) > max_length:
        if on_overflow == "reject":
            raise QueryTooLongError(
                f"query length {len(working)} exceeds bound {max_length}"
            )
        working = working[:max_length]
        truncated = True

    text = unicodedata.normalize("NFKC", working)
    text = text.casefold()
    normalized = " ".join(_punct_to_space(text).split())

    # NFKC can expand text (e.g. ligatures), so re-check the bound.
    if len(normalized) > max_length:
        normalized = normalized[:max_length].rstrip()
        truncated = True

    return NormalizedQuery(original=query, normalized=normalized, truncated=truncated)


@dataclass
class QueryNormalizer:
    """Config-owned entry point; keyword sets compile here in step 2."""

    config: QueryNormalizerConfig = field(default_factory=QueryNormalizerConfig)

    def normalize(self, query: str) -> NormalizedQuery:
        return normalize_query(
            query,
            max_length=self.config.max_length,
            on_overflow=self.config.on_overflow,
        )
