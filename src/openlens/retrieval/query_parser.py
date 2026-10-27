"""Query parser: plain text -> safe FTS5 MATCH query."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass
class ParsedQuery:
    original: str
    terms: list[str] = field(default_factory=list)
    fts_query: str = ""


def parse_query(query: str) -> ParsedQuery:
    """Tokenize to lowercase alphanumerics and join as an FTS5 AND query.

    Extracting word tokens (instead of passing raw text through) keeps FTS5
    operators in user input from breaking MATCH syntax.
    """
    terms = _TOKEN_RE.findall(query.lower())
    return ParsedQuery(
        original=query,
        terms=terms,
        fts_query=" AND ".join(terms),
    )
