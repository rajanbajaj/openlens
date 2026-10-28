"""Search routes: POST /api/v1/search, GET /api/v1/suggest.

Thin translation layer over the Wave-0 `Engine` interface: validate the
wire format (schemas), build a core `SearchRequest`, delegate to the
engine, and convert the core `SearchResponse` back. Pagination
(`limit`/`offset`) is passed through to the engine, which owns slicing
and the pre-pagination `total`; `next_offset` is derived at the
boundary via `schemas.compute_next_offset`. Assistance fields
(`suggestions`, `spelling`, `related`) are populated from
`retrieval/suggest.py` over the live index vocabulary when the app
exposes one, else a static fallback — failures degrade to empty
assistance, never to a 500. Error mapping lives in
`openlens.api.errors`; this module never invents error shapes.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import suppress
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from openlens.api.errors import EngineUnavailableError
from openlens.api.schemas import SearchRequestSchema, SearchResponseSchema
from openlens.api.schemas import SuggestResponseSchema
from openlens.core.interfaces import Engine, ProviderPrefs, SearchRequest
from openlens.retrieval.suggest import (
    complete,
    correct_spelling,
    related_queries,
)

router = APIRouter(prefix="/api/v1", tags=["search"])

DEFAULT_SUGGEST_LIMIT = 10
MAX_SUGGEST_LIMIT = 50
ASSISTANCE_LIMIT = 5

#: Static fallback vocabulary used when the app exposes no live index
#: terms (offline, tests, empty index). Never empty on purpose so the
#: endpoints stay exercisable without a booted store.
_STATIC_SUGGEST_TERMS: tuple[str, ...] = (
    "budget travel japan",
    "coffee",
    "coffee maker",
    "cold brew recipe",
    "running shoes",
    "running watch",
    "trail shoes",
    "wireless charger",
    "wireless headphones",
)

#: Static fallback query counts backing `related` when no live counts
#: are available. Anonymous aggregate text only — no identifiers.
_STATIC_QUERY_COUNTS: dict[str, int] = {
    "budget travel japan": 14,
    "coffee maker": 22,
    "cold brew recipe": 9,
    "running shoes": 42,
    "running watch": 25,
    "trail shoes": 19,
    "wireless charger": 12,
    "wireless headphones": 31,
}

#: In-process anonymous feedback tallies keyed by (doc_id, action).
#: Only counts are kept: no query text, no user/session identifiers.
_FEEDBACK_COUNTS: dict[tuple[str, str], int] = {}


def _coerce_terms(value: Any) -> list[str]:
    """Best-effort coercion of a vocabulary hook to usable term strings."""
    if callable(value):
        try:
            value = value()
        except Exception:
            return []
    if isinstance(value, (str, bytes)) or isinstance(value, dict):
        return []
    if not isinstance(value, Iterable):
        return []
    terms: list[str] = []
    for term in value:
        if isinstance(term, str) and term.strip():
            terms.append(term.strip())
    return terms


def _resolve_terms(request: Request | None) -> list[str]:
    """Return the live suggest vocabulary, else the static fallback.

    Prefers `app.state.suggest_terms` (set by server wiring/tests),
    then duck-typed engine hooks (`suggest_terms` / `vocabulary` /
    `terms`, unwrapping the observability decorator). Never raises:
    unusable hooks fall through to the static list.
    """
    if request is not None:
        try:
            state_terms = getattr(request.app.state, "suggest_terms", None)
        except Exception:
            state_terms = None
        terms = _coerce_terms(state_terms)
        if terms:
            return terms
        try:
            engine = getattr(request.app.state, "engine", None)
        except Exception:
            engine = None
        candidates = (engine, getattr(engine, "_inner", None))
        for candidate in candidates:
            for attr in ("suggest_terms", "vocabulary", "terms"):
                hook = getattr(candidate, attr, None)
                terms = _coerce_terms(hook)
                if terms:
                    return terms
    return list(_STATIC_SUGGEST_TERMS)


def _assistance(
    query: str, terms: list[str]
) -> tuple[list[str], Optional[str], list[str]]:
    """Build (suggestions, spelling, related) for a query; never raises."""
    try:
        suggestions = complete(query, terms, limit=ASSISTANCE_LIMIT)
    except Exception:
        suggestions = []
    try:
        spelling = correct_spelling(query, terms)
    except Exception:
        spelling = None
    try:
        related = related_queries(query, _STATIC_QUERY_COUNTS, limit=ASSISTANCE_LIMIT)
    except Exception:
        related = []
    return suggestions, spelling, related


def get_engine(request: Request) -> Engine:
    """Resolve the engine attached to the app (overridable in tests)."""
    engine = getattr(request.app.state, "engine", None)
    if not isinstance(engine, Engine):
        raise EngineUnavailableError("search engine is not configured")
    return engine


@router.post("/search", response_model=SearchResponseSchema)
def search(
    body: SearchRequestSchema,
    request: Request,
    engine: Engine = Depends(get_engine),
) -> SearchResponseSchema:
    prefs = (
        ProviderPrefs(
            include=tuple(body.provider_prefs.include),
            exclude=tuple(body.provider_prefs.exclude),
            primary=body.provider_prefs.primary,
        )
        if body.provider_prefs is not None
        else ProviderPrefs()
    )
    core_request = SearchRequest(
        query=body.query.strip(),
        mode=body.mode,
        limit=body.limit,
        offset=body.offset,
        filters=dict(body.filters),
        provider_prefs=prefs,
    )
    core_response = engine.search(core_request)
    query_text = core_response.query or core_request.query
    suggestions, spelling, related = _assistance(query_text, _resolve_terms(request))
    return SearchResponseSchema.from_core(
        core_response,
        limit=body.limit,
        offset=body.offset,
        suggestions=suggestions,
        spelling=spelling,
        related=related,
    )


@router.get("/suggest", response_model=SuggestResponseSchema)
def suggest(
    request: Request,
    q: str = Query(default=""),
    limit: int = Query(default=DEFAULT_SUGGEST_LIMIT, ge=1, le=MAX_SUGGEST_LIMIT),
) -> SuggestResponseSchema:
    """Prefix completions for `q` from index terms (static fallback).

    Blank `q` returns `[]` with 200. Never 500s: helper failures
    degrade to `[]`.
    """
    if not q.strip():
        return SuggestResponseSchema(q=q, suggestions=[])
    try:
        suggestions = complete(q, _resolve_terms(request), limit=limit)
    except Exception:
        suggestions = []
    return SuggestResponseSchema(q=q, suggestions=suggestions)


class FeedbackRequestSchema(BaseModel):
    """POST /api/v1/feedback body: anonymous (doc_id, action) counts only.

    Extra fields are rejected so no query text or identifiers can be
    smuggled into the feedback store. (Defined here because schemas.py
    is owned by wave0; the integrator may relocate it.)
    """

    model_config = ConfigDict(extra="forbid")

    doc_id: str = Field(min_length=1, max_length=512)
    action: str = Field(min_length=1, max_length=64)

    @field_validator("doc_id", "action")
    @classmethod
    def must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class FeedbackResponseSchema(BaseModel):
    """Feedback acknowledgement: the pair plus its in-process count."""

    doc_id: str
    action: str
    count: int = Field(ge=1)


@router.post("/feedback", response_model=FeedbackResponseSchema)
def feedback(body: FeedbackRequestSchema, request: Request) -> FeedbackResponseSchema:
    """Record one anonymous feedback event as an in-process count.

    The module-level tally is the canonical response source; when the
    app engine exposes ``record_feedback`` the event is mirrored there
    too (best-effort, never failing the request) so future ranking can
    consume the counts.
    """
    key = (body.doc_id.strip(), body.action.strip())
    count = _FEEDBACK_COUNTS.get(key, 0) + 1
    _FEEDBACK_COUNTS[key] = count
    engine = getattr(request.app.state, "engine", None)
    for candidate in (engine, getattr(engine, "_inner", None)):
        hook = getattr(candidate, "record_feedback", None)
        if callable(hook):
            with suppress(Exception):
                hook(key[0], key[1])
            break
    return FeedbackResponseSchema(doc_id=key[0], action=key[1], count=count)
