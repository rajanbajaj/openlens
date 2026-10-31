"""Query parser: plain text -> safe FTS5 MATCH query.

Tokenizes to Unicode alphanumerics (word characters minus the underscore)
and joins them into an FTS5 query. Extracting word tokens (instead of
passing raw text through) keeps FTS5 operators in user input from breaking
MATCH syntax: only parser-emitted ``AND``/``OR``/parentheses and token
characters ever reach the backend.

Code-significant tokens get recall help without weakening safety:

* hyphen runs (``e-mail``) emit the split parts as AND terms plus the
  joined compound (``email``) as an optional OR alternative;
* pure-numeric and single-character terms are demoted from mandatory AND
  terms to an optional ``OR`` boost group (``(m) OR (m AND w)``);
* terms containing non-ASCII alphanumerics (CJK runs, accented words)
  carry a trailing ``*`` prefix wildcard, because the FTS5 ``unicode61``
  tokenizer indexes such runs whole and would otherwise miss
  prefix/substring queries entirely (silent zero results).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

# Unicode alphanumerics: word characters minus the underscore, so accented
# Latin, CJK, Hangul, digits, etc. all survive as terms while "_" still
# splits (matching the historic ASCII-only behavior for ASCII input).
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

# Maximal hyphen-joined alphanumeric runs ("e-mail", "node-js").
_HYPHEN_RUN_RE = re.compile(r"[^\W_]+(?:-[^\W_]+)+", re.UNICODE)


@dataclass
class ParsedQuery:
    original: str
    terms: list[str] = field(default_factory=list)
    fts_query: str = ""


def _is_weak(term: str) -> bool:
    """Pure-numeric and single-character terms are weak (SHOULD, not MUST).

    A mandatory numeric/single-char AND term either over-restricts
    (version queries like ``python 3.11``) or matches noise, so weak terms
    are demoted to an optional boost group.
    """
    return len(term) <= 1 or term.isdigit()


def _match_term(term: str) -> str:
    """Render one token for MATCH, adding prefix help for non-Latin runs."""
    if any(ord(char) > 127 for char in term):
        return f"{term}*"
    return term


def hyphen_compounds(text: str) -> list[str]:
    """Joined compounds for hyphen runs in ``text`` (``e-mail`` -> ``email``).

    Pure helper over the raw (pre-normalization) query text: normalization
    maps hyphens to spaces, so compounds must be recovered before that.
    Returns casefolded compounds in first-seen order.
    """
    if not isinstance(text, str) or not text:
        return []
    compounds: list[str] = []
    for run in _HYPHEN_RUN_RE.findall(text.casefold()):
        parts = run.split("-")
        if not all(parts):
            continue
        compound = "".join(parts)
        if compound and compound != run and compound not in compounds:
            compounds.append(compound)
    return compounds


def parse_query(query: str, *, extra_optional: Iterable[str] = ()) -> ParsedQuery:
    """Tokenize to casefolded Unicode alphanumerics; build a safe FTS5 query.

    ``extra_optional`` carries hyphen compounds recovered from the raw
    query text (see :func:`hyphen_compounds`) when the input was already
    normalized. Compounds found in ``query`` itself are picked up
    automatically. Every optional term is validated to pure alphanumeric
    tokens, so raw operators can never leak into MATCH output.
    """
    folded = query.casefold() if isinstance(query, str) else ""
    terms = _TOKEN_RE.findall(folded)
    seen = set(terms)
    optional: list[str] = []
    for candidate in [*hyphen_compounds(query), *extra_optional]:
        if not isinstance(candidate, str):
            continue
        cleaned = candidate.casefold()
        if not cleaned or cleaned in seen:
            continue
        if _TOKEN_RE.fullmatch(cleaned) is None:
            continue
        seen.add(cleaned)
        optional.append(cleaned)
    strong = [term for term in terms if not _is_weak(term)]
    weak = [term for term in terms if _is_weak(term)]
    if not terms:
        fts_query = ""
    elif not strong:
        # All weak (e.g. "3", "11", "中"): stay conjunctive so the query
        # cannot degrade into matching everything.
        fts_query = " AND ".join(_match_term(term) for term in terms)
    else:
        mandatory = " AND ".join(_match_term(term) for term in strong)
        extras: list[str] = []
        if weak:
            extras.append(
                "("
                + " AND ".join([mandatory, *(_match_term(term) for term in weak)])
                + ")"
            )
        extras.extend(optional)
        if extras:
            fts_query = f"({mandatory}) OR " + " OR ".join(extras)
        else:
            fts_query = mandatory
    return ParsedQuery(
        original=query,
        terms=terms,
        fts_query=fts_query,
    )
