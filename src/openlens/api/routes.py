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

from collections.abc import Iterable, Mapping
from contextlib import suppress
import asyncio
import logging
import os
import secrets
import time
from typing import Any, Optional
from urllib.parse import urlsplit

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openlens.api.errors import EngineUnavailableError, error_body
from openlens.api import frontier_state
from openlens.api.rate_limit import client_ip
from openlens.api.schemas import AdminConfigDocResponseSchema, AdminConfigPutSchema
from openlens.api.schemas import CrawlRequestListSchema, CrawlRequestSchema
from openlens.api.schemas import CrawlRequestSubmitSchema
from openlens.api.schemas import FrontierResumeSchema, FrontierSettingsResponseSchema
from openlens.api.schemas import FrontierFailedListSchema, FrontierFailedUrlSchema
from openlens.api.schemas import (
    FrontierDomainPrioritySchema,
    FrontierDomainsActionSchema,
    FrontierDomainsResultSchema,
    FrontierFailedDomainListSchema,
    FrontierFailedDomainSchema,
    FrontierQueuedDomainListSchema,
    FrontierQueuedDomainSchema,
)
from openlens.api.schemas import FrontierSettingsSchema, FrontierStatusSchema
from openlens.api.schemas import FrontierWorkerHeartbeatSchema
from openlens.api.schemas import FrontierWorkersResponseSchema
from openlens.api.schemas import (
    CLUSTER_MAP_SETTINGS_KEY,
    KAFKA_NODES_SETTINGS_KEY,
    ClusterMapResponseSchema,
    ClusterMapSchema,
    KafkaNodeJoinedSchema,
    KafkaNodePrepareResponseSchema,
    KafkaNodePrepareSchema,
    KafkaNodesResponseSchema,
    RebalancePlanRequestSchema,
    RebalancePlanResponseSchema,
)
from openlens.api.schemas import (
    ChatCitationSchema,
    ChatRequestSchema,
    ChatResponseSchema,
    ChatTraceSchema,
    ChatTraceStepSchema,
    PerspectiveViewSchema,
)
from openlens.api.schemas import SearchRequestSchema, SearchResponseSchema
from openlens.api.schemas import SearchResultSchema
from openlens.api.schemas import SuggestResponseSchema
from openlens.core.interfaces import (
    ConfigError,
    Engine,
    ProviderError,
    ProviderPrefs,
    SearchRequest,
)
from openlens.storage.app_config import (
    CONFIG_DOC_KEYS,
    AppConfigStore,
    apply_db_config_overlay,
    extract_file_doc,
    missing_secrets,
    open_app_config_store,
    redact_secrets,
    restore_secrets,
    try_get_doc,
    validate_config_doc,
)
from openlens.indexing.kafka_queue import (
    kafka_enabled,
    open_kafka_queue,
    purge_topic,
)
from openlens.retrieval.agent import run_agent_chat
from openlens.retrieval.suggest import (
    complete,
    correct_spelling,
    related_queries,
)
from openlens.storage.frontier import normalize_frontier_url, open_frontier_store

logger = logging.getLogger(__name__)

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

#: In-process anonymous feedback tallies keyed by (doc_id, action, reason).
#: ``reason`` is ``""`` when absent. Only counts are kept: no query
#: text, no user/session identifiers.
_FEEDBACK_COUNTS: dict[tuple[str, str, str], int] = {}

#: In-process named preference sets: name -> filters mapping. The
#: backend-held counterpart to the frontend-local library cache — saved
#: categories, weight overrides, and safety presets survive across
#: clients. Values only: never query text or identifiers.
_SAVED_PREFS: dict[str, dict[str, Any]] = {}

#: Bounds on the saved-prefs store (in-process only, never persisted).
MAX_SAVED_PREFS = 50
MAX_SAVED_PREF_NAME_LENGTH = 64
MAX_SAVED_PREF_FILTERS = 50

#: Canonical feedback actions accepted by POST /api/v1/feedback.
#: Engagement signals plus the kids/privacy/fairness reporting taxonomy:
#: `report_incorrect` (report an incorrect classification) and
#: `report_suspicious` (report a suspicious website). Anything else is
#: rejected with 422 so no free-form text can be smuggled into the store.
FEEDBACK_ACTIONS: frozenset[str] = frozenset(
    {
        "click",
        "impression",
        "save",
        "unsave",
        "helpful",
        "not_helpful",
        "report_incorrect",
        "report_suspicious",
    }
)

#: Spellings normalized to canonical actions before validation.
_FEEDBACK_ACTION_ALIASES: dict[str, str] = {
    "nothelpful": "not_helpful",
    "not-helpful": "not_helpful",
    "not helpful": "not_helpful",
    "reportincorrect": "report_incorrect",
    "report-incorrect": "report_incorrect",
    "report incorrect": "report_incorrect",
    "reportsuspicious": "report_suspicious",
    "report-suspicious": "report_suspicious",
    "report suspicious": "report_suspicious",
}

#: Closed "what was not right" taxonomy for negative/report feedback
#: (privacy, kids-safety, misleading, irrelevant, other). Closed set by
#: design: like actions, free-form text would smuggle PII/queries into
#: the store, so anything outside the set fails with 422.
FEEDBACK_REASONS: frozenset[str] = frozenset(
    {
        "privacy",
        "kids-safety",
        "misleading",
        "irrelevant",
        "other",
    }
)

#: Actions a reason may accompany. Positive/engagement signals carry no
#: "why", so a reason there fails with 422 instead of being silently
#: tallied into a meaningless bucket.
_FEEDBACK_REASON_ACTIONS: frozenset[str] = frozenset(
    {
        "not_helpful",
        "report_incorrect",
        "report_suspicious",
    }
)


def normalize_feedback_reason(raw: str) -> str | None:
    """Return the canonical feedback reason, or None when unknown."""
    key = raw.strip().lower().replace("_", "-").replace(" ", "-")
    if key in FEEDBACK_REASONS:
        return key
    return None


def normalize_feedback_action(raw: str) -> str | None:
    """Return the canonical feedback action for `raw`, or None when unknown."""
    key = raw.strip().lower()
    if key in FEEDBACK_ACTIONS:
        return key
    return _FEEDBACK_ACTION_ALIASES.get(key)


#: Environment variable holding the frontier admin token. When set to a
#: non-empty value, every mutating /frontier/* and /admin/* endpoint requires
#: it via the `X-Admin-Token` header; reads stay open. When unset, mutations
#: are denied unless the explicit local-dev opt-in below is enabled — the
#: gate is fail-closed so an unconfigured server never leaves admin
#: mutations open.
FRONTIER_ADMIN_TOKEN_ENV = "OPENLENS_FRONTIER_ADMIN_TOKEN"  # noqa: S105 (env var name, not a credential)
FRONTIER_ADMIN_HEADER = "X-Admin-Token"

#: Explicit local-dev opt-in that re-opens admin mutations when no token is
#: configured. Accepts 1/true/yes/on. Never enable on a networked server:
#: any unauthenticated caller could then mutate frontier/worker/config state.
ALLOW_OPEN_ADMIN_ENV = "OPENLENS_ALLOW_OPEN_ADMIN"


def _open_admin_allowed() -> bool:
    """Whether the local-dev open-admin opt-in is enabled."""
    return os.environ.get(ALLOW_OPEN_ADMIN_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _admin_denied(provided: str) -> bool:
    """Whether `provided` fails the admin gate (shared by HTTP and WS)."""
    expected = os.environ.get(FRONTIER_ADMIN_TOKEN_ENV, "").strip()
    if expected:
        return not (provided and secrets.compare_digest(provided.strip(), expected))
    return not _open_admin_allowed()


def _frontier_admin_response(request: Request) -> JSONResponse | None:
    """Return a 403 envelope unless the request carries admin rights."""
    provided = request.headers.get(FRONTIER_ADMIN_HEADER) or ""
    if _admin_denied(provided):
        return JSONResponse(
            status_code=403,
            content=error_body("forbidden", "frontier mutation requires admin rights"),
        )
    return None


def _ws_admin_denied(websocket: WebSocket) -> bool:
    """Whether a live-socket handshake fails the admin gate.

    Browsers cannot set custom headers on a WebSocket handshake, so the
    admin UI offers the token as a ``Sec-WebSocket-Protocol`` value. Either
    the header or a matching subprotocol value satisfies the gate.
    """
    candidates = [websocket.headers.get(FRONTIER_ADMIN_HEADER) or ""]
    protocols = websocket.headers.get("sec-websocket-protocol") or ""
    candidates.extend(part.strip() for part in protocols.split(",") if part.strip())
    return all(_admin_denied(candidate) for candidate in candidates)


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


def _coerce_weights(value: Any) -> dict[str, int]:
    """Best-effort coercion of a popularity hook to casefolded weights."""
    if callable(value):
        try:
            value = value()
        except Exception:
            return {}
    if not isinstance(value, dict):
        return {}
    weights: dict[str, int] = {}
    for term, count in value.items():
        if not isinstance(term, str) or not term.strip():
            continue
        if isinstance(count, bool) or not isinstance(count, (int, float)):
            continue
        weights[term.strip().casefold()] = max(0, int(count))
    return weights


def _resolve_weights(request: Request | None) -> dict[str, int]:
    """Return live index term popularity, else ``{}`` (unweighted order).

    Prefers `app.state.suggest_term_weights` (set by server wiring/tests),
    then duck-typed engine hooks (`suggest_term_weights`, unwrapping the
    observability decorator). Never raises.
    """
    if request is not None:
        try:
            state_weights = getattr(request.app.state, "suggest_term_weights", None)
        except Exception:
            state_weights = None
        weights = _coerce_weights(state_weights)
        if weights:
            return weights
        try:
            engine = getattr(request.app.state, "engine", None)
        except Exception:
            engine = None
        candidates = (engine, getattr(engine, "_inner", None))
        for candidate in candidates:
            hook = getattr(candidate, "suggest_term_weights", None)
            weights = _coerce_weights(hook)
            if weights:
                return weights
    return {}


def _assistance(
    query: str,
    terms: list[str],
    *,
    total: int = 0,
    weights: dict[str, int] | None = None,
    history: list[str] | tuple[str, ...] = (),
) -> tuple[list[str], Optional[str], list[str]]:
    """Build (suggestions, spelling, related) for a query; never raises.

    Spelling is offered only when the query returned no results: the
    engine always searches the original query, so hinting at a
    correction alongside real results would mislead. ``weights`` is the
    live index popularity proxy (Google ranks by query popularity;
    offline we rank by index document frequency); ``history`` carries
    the caller's own recent queries, which rank first when they match
    the prefix (request-scoped, never logged).
    """
    try:
        suggestions = complete(
            query, terms, limit=ASSISTANCE_LIMIT, weights=weights, boost=history
        )
    except Exception:
        suggestions = []
    spelling: Optional[str] = None
    if total == 0:
        try:
            spelling = correct_spelling(query, terms, weights=weights)
        except Exception:
            spelling = None
    try:
        related = related_queries(
            query,
            _STATIC_QUERY_COUNTS,
            vocabulary=terms,
            limit=ASSISTANCE_LIMIT,
            weights=weights,
        )
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
        history=tuple(body.history),
    )
    core_response = engine.search(core_request)
    query_text = core_response.query or core_request.query
    _publish_search_served(request, query_text, core_request.mode, core_response.total)
    suggestions, spelling, related = _assistance(
        query_text,
        _resolve_terms(request),
        total=core_response.total,
        weights=_resolve_weights(request),
        history=body.history,
    )
    return SearchResponseSchema.from_core(
        core_response,
        limit=body.limit,
        offset=body.offset,
        suggestions=suggestions,
        spelling=spelling,
        related=related,
    )


@router.post("/agent/chat", response_model=ChatResponseSchema)
def agent_chat(
    body: ChatRequestSchema,
    request: Request,
    engine: Engine = Depends(get_engine),
) -> ChatResponseSchema:
    """One grounded chat turn over the search index.

    Rewrites the turn with the transcript, searches via the engine as
    the only tool, and synthesizes a cited answer. Fail-closed: a
    missing/disabled model backend (or a model error) returns an empty
    answer with a ``notice`` plus plain-search hits — never a 500.
    """
    history = [(turn.role, turn.content) for turn in body.history]
    outcome = run_agent_chat(
        message=body.message.strip(),
        history=history,
        limit=body.limit,
        engine=engine,
    )
    trace = outcome["trace"] if isinstance(outcome.get("trace"), dict) else {}
    steps = trace.get("steps") if isinstance(trace.get("steps"), list) else []
    citations = [
        ChatCitationSchema(
            ref=int(item.get("ref", index + 1)),
            title=str(item.get("title", "")),
            url=item.get("url") if isinstance(item.get("url"), str) else None,
        )
        for index, item in enumerate(outcome.get("citations", []) or [])
        if isinstance(item, dict)
    ]
    raw_steps = steps if isinstance(steps, list) else []
    notice = outcome.get("notice")
    perspectives = outcome.get("perspectives")
    return ChatResponseSchema(
        answer=str(outcome.get("answer", "")),
        citations=citations,
        results=[
            SearchResultSchema.from_core(hit)
            for hit in (outcome.get("results", []) or [])
        ],
        perspectives=[
            PerspectiveViewSchema.from_mapping(entry)
            for entry in (perspectives if isinstance(perspectives, list) else [])
        ],
        trace=ChatTraceSchema(
            rewritten_query=str(trace.get("rewritten_query", "")),
            filters=(
                dict(trace["filters"]) if isinstance(trace.get("filters"), dict) else {}
            ),
            steps=[
                ChatTraceStepSchema(
                    kind=str(step.get("kind", "")),
                    detail=str(step.get("detail", "")),
                )
                for step in raw_steps
                if isinstance(step, dict)
            ],
            fallbacks=[str(entry) for entry in (trace.get("fallbacks", []) or [])],
            model_calls=int(trace.get("model_calls", 0)),
            model=(
                str(trace.get("model", ""))
                if isinstance(trace.get("model", ""), str)
                else ""
            ),
        ),
        notice=notice if isinstance(notice, str) else None,
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
        suggestions = complete(
            q, _resolve_terms(request), limit=limit, weights=_resolve_weights(request)
        )
    except Exception:
        suggestions = []
    return SuggestResponseSchema(q=q, suggestions=suggestions)


class FeedbackRequestSchema(BaseModel):
    """POST /api/v1/feedback body: anonymous counts with optional reason.

    Extra fields are rejected so no query text or identifiers can be
    smuggled into the feedback store. `action` must name a canonical
    feedback action (see `FEEDBACK_ACTIONS`); common hyphen/space/case
    variants are normalized before validation. `reason` is optional and
    limited to the closed `FEEDBACK_REASONS` taxonomy ("what was not
    right"); it is only accepted with negative/report actions.
    (Defined here because schemas.py is owned by wave0; the integrator
    may relocate it.)
    """

    model_config = ConfigDict(extra="forbid")

    doc_id: str = Field(min_length=1, max_length=512)
    action: str = Field(min_length=1, max_length=64)
    reason: str | None = Field(default=None, min_length=1, max_length=32)

    @field_validator("doc_id")
    @classmethod
    def doc_id_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("action")
    @classmethod
    def action_must_be_known(cls, value: str) -> str:
        canonical = normalize_feedback_action(value)
        if canonical is None:
            known = ", ".join(sorted(FEEDBACK_ACTIONS))
            raise ValueError(f"unknown action; known: {known}")
        return canonical

    @field_validator("reason")
    @classmethod
    def reason_must_be_known(cls, value: str | None) -> str | None:
        if value is None:
            return None
        canonical = normalize_feedback_reason(value)
        if canonical is None:
            known = ", ".join(sorted(FEEDBACK_REASONS))
            raise ValueError(f"unknown reason; known: {known}")
        return canonical

    @model_validator(mode="after")
    def reason_needs_negative_action(self) -> "FeedbackRequestSchema":
        if self.reason is not None and self.action not in _FEEDBACK_REASON_ACTIONS:
            raise ValueError("reason applies only to negative/report actions")
        return self


class FeedbackResponseSchema(BaseModel):
    """Feedback acknowledgement: the triple plus its in-process count."""

    doc_id: str
    action: str
    count: int = Field(ge=1)
    reason: str | None = None


#: Bounds on the admin feedback summary (bounded payloads, no paging).
MAX_FEEDBACK_TOP_DOCS = 50


class FeedbackDocSummarySchema(BaseModel):
    """Per-document feedback rollup for the admin summary."""

    doc_id: str
    total: int = Field(ge=0)
    helpful: int = Field(ge=0)
    not_helpful: int = Field(ge=0)
    reports: int = Field(ge=0)
    top_reason: str | None = None


class FeedbackSummarySchema(BaseModel):
    """Admin feedback overview: totals plus the noisiest documents."""

    total: int = Field(ge=0)
    by_action: dict[str, int]
    by_reason: dict[str, int]
    top_docs: list[FeedbackDocSummarySchema]


class AnalyticsCountsSchema(BaseModel):
    """Admin analytics overview: durable per-name counts over a window."""

    days: int = Field(ge=1)
    total: int = Field(ge=0)
    by_name: dict[str, int]


class AnalyticsTopEntrySchema(BaseModel):
    """One top prop value with its count."""

    value: str
    count: int = Field(ge=0)


class AnalyticsTopSchema(BaseModel):
    """Admin top-values breakdown for one event prop over a window."""

    name: str
    prop: str
    days: int = Field(ge=1)
    total: int = Field(ge=0)
    top: list[AnalyticsTopEntrySchema] = Field(default_factory=list)


class AnalyticsSeriesPointSchema(BaseModel):
    """One throughput bucket: ISO start plus event count."""

    t: str
    count: int = Field(ge=0)


class AnalyticsSeriesSchema(BaseModel):
    """Admin per-bucket event counts over a window (throughput)."""

    name: str
    bucket: str
    days: int = Field(ge=1)
    points: list[AnalyticsSeriesPointSchema] = Field(default_factory=list)


@router.post(
    "/feedback",
    response_model=FeedbackResponseSchema,
    response_model_exclude_none=True,
)
def feedback(body: FeedbackRequestSchema, request: Request) -> FeedbackResponseSchema:
    """Record one anonymous feedback event as an in-process count.

    The tally key is ``(doc_id, action, reason)`` with ``""`` for absent
    reasons, so reason-less posts keep their exact historic shape. The
    module-level tally is the canonical response source; when the app
    engine exposes ``record_feedback`` the ``(doc_id, action)`` event is
    mirrored there too (best-effort, never failing the request) so
    future ranking can consume the counts.
    """
    key = (body.doc_id.strip(), body.action.strip(), body.reason or "")
    count = _FEEDBACK_COUNTS.get(key, 0) + 1
    _FEEDBACK_COUNTS[key] = count
    _publish_feedback_event(request, key[0], key[1], body.reason)
    engine = getattr(request.app.state, "engine", None)
    for candidate in (engine, getattr(engine, "_inner", None)):
        hook = getattr(candidate, "record_feedback", None)
        if callable(hook):
            with suppress(Exception):
                hook(key[0], key[1])
            break
    return FeedbackResponseSchema(
        doc_id=key[0], action=key[1], count=count, reason=body.reason
    )


@router.get("/admin/feedback/summary", response_model=FeedbackSummarySchema)
def admin_feedback_summary(request: Request) -> FeedbackSummarySchema | JSONResponse:
    """Admin-only feedback overview (same gate as config mutations).

    Aggregates the anonymous tallies: totals by action and reason plus
    the noisiest documents. Gated because even anonymous aggregates
    describe user behavior; reads stay open only when no admin token is
    configured (local dev), exactly like the other admin reads.
    """
    denied = _frontier_admin_response(request)
    if denied is not None:
        return denied
    by_action: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    per_doc: dict[str, dict[str, Any]] = {}
    for (doc_id, action, reason), count in _FEEDBACK_COUNTS.items():
        by_action[action] = by_action.get(action, 0) + count
        reason_label = reason or "unspecified"
        by_reason[reason_label] = by_reason.get(reason_label, 0) + count
        entry = per_doc.setdefault(
            doc_id,
            {"total": 0, "helpful": 0, "not_helpful": 0, "reports": 0, "reasons": {}},
        )
        entry["total"] += count
        if action == "helpful":
            entry["helpful"] += count
        elif action == "not_helpful":
            entry["not_helpful"] += count
        elif action in ("report_incorrect", "report_suspicious"):
            entry["reports"] += count
        if reason:
            entry["reasons"][reason] = entry["reasons"].get(reason, 0) + count
    top_docs = []
    for doc_id, entry in sorted(
        per_doc.items(), key=lambda item: item[1]["total"], reverse=True
    )[:MAX_FEEDBACK_TOP_DOCS]:
        reasons = entry["reasons"]
        top_reason = (
            max(reasons.items(), key=lambda item: item[1])[0] if reasons else None
        )
        top_docs.append(
            FeedbackDocSummarySchema(
                doc_id=doc_id,
                total=entry["total"],
                helpful=entry["helpful"],
                not_helpful=entry["not_helpful"],
                reports=entry["reports"],
                top_reason=top_reason,
            )
        )
    return FeedbackSummarySchema(
        total=sum(by_action.values()),
        by_action=by_action,
        by_reason=by_reason,
        top_docs=top_docs,
    )


@router.get("/admin/analytics/counts", response_model=AnalyticsCountsSchema)
def admin_analytics_counts(
    request: Request, days: int = 30
) -> AnalyticsCountsSchema | JSONResponse:
    """Admin-only durable analytics counts (same gate as config mutations).

    Reads the separate analytics database over the trailing ``days``
    window. Fail-closed auth, fail-open store: an unreachable analytics
    database reports 503 instead of breaking the admin screen.
    """
    denied = _frontier_admin_response(request)
    if denied is not None:
        return denied
    window = days if isinstance(days, int) and days >= 1 else 30
    window = min(window, 365)
    try:
        from openlens.analytics.store import open_analytics_store

        app_config = getattr(request.app.state, "config", None)
        section: dict[str, Any] = {}
        if isinstance(app_config, dict):
            raw = app_config.get("analytics", {})
            if isinstance(raw, Mapping):
                section = dict(raw.get("store", {}))
        store = open_analytics_store(section)
    except Exception as exc:
        logger.warning("analytics counts: store unavailable: %s", exc)
        return JSONResponse(
            status_code=503,
            content=error_body("provider_unavailable", "analytics store unavailable"),
        )
    try:
        by_name = store.counts_by_name(window) or {}
    except Exception as exc:
        logger.warning("analytics counts: read failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content=error_body("provider_unavailable", "analytics store unavailable"),
        )
    finally:
        with suppress(Exception):
            store.close()
    return AnalyticsCountsSchema(
        days=window, total=sum(by_name.values()), by_name=by_name
    )


def _admin_analytics_store(
    request: Request,
) -> tuple[Any | None, JSONResponse | None]:
    """Open the configured analytics store for an admin read.

    Returns ``(store, None)`` or ``(None, 503 response)`` when the
    store is unreachable (fail-open).
    """
    try:
        from openlens.analytics.store import open_analytics_store

        app_config = getattr(request.app.state, "config", None)
        section: dict[str, Any] = {}
        if isinstance(app_config, dict):
            raw = app_config.get("analytics", {})
            if isinstance(raw, Mapping):
                section = dict(raw.get("store", {}))
        return open_analytics_store(section), None
    except Exception as exc:
        logger.warning("analytics: store unavailable: %s", exc)
        return None, JSONResponse(
            status_code=503,
            content=error_body("provider_unavailable", "analytics store unavailable"),
        )


@router.get("/admin/analytics/top", response_model=AnalyticsTopSchema)
def admin_analytics_top(
    request: Request,
    name: str = "crawl.page",
    prop: str = Query(default="reason", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"),
    days: int = 30,
    limit: int = 10,
    filter_prop: str | None = Query(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"),
    filter_value: str | None = Query(default=None, max_length=256),
) -> AnalyticsTopSchema | JSONResponse:
    """Admin-only top-values breakdown for one analytics event prop.

    Same gate as the counts endpoint; fail-open store (503 when
    unreachable). ``name`` must be a known event and ``prop`` (plus the
    optional ``filter_prop``) a plain identifier; ``filter_value`` is
    charset-restricted (fail-closed, 422 otherwise) so no user input
    reaches the aggregation query raw. The optional
    ``filter_prop=filter_value`` clause restricts rows first — e.g.
    top domains among failed pages only.
    """
    denied = _frontier_admin_response(request)
    if denied is not None:
        return denied
    from openlens.analytics.events import EVENT_NAMES

    if name not in EVENT_NAMES:
        return JSONResponse(
            status_code=422,
            content=error_body("invalid_request", f"unknown analytics event: {name}"),
        )
    window = days if isinstance(days, int) and days >= 1 else 30
    window = min(window, 365)
    capped = max(1, min(int(limit or 10), 100))
    if (filter_prop is None) != (filter_value is None):
        return JSONResponse(
            status_code=422,
            content=error_body(
                "invalid_request", "filter_prop and filter_value go together"
            ),
        )
    store, unavailable = _admin_analytics_store(request)
    if store is None:
        if unavailable is None:
            return JSONResponse(
                status_code=503,
                content=error_body(
                    "provider_unavailable", "analytics store unavailable"
                ),
            )
        return unavailable
    try:
        pairs = store.top_props(
            name,
            prop,
            since_days=window,
            limit=capped,
            filter_prop=filter_prop,
            filter_value=filter_value,
        )
    except ProviderError as exc:
        if exc.code == ProviderError.INVALID_REQUEST:
            return JSONResponse(
                status_code=422,
                content=error_body("invalid_request", str(exc)),
            )
        logger.warning("analytics top: read failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content=error_body("provider_unavailable", "analytics store unavailable"),
        )
    except Exception as exc:
        logger.warning("analytics top: read failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content=error_body("provider_unavailable", "analytics store unavailable"),
        )
    finally:
        with suppress(Exception):
            store.close()
    total = sum(count for _, count in pairs)
    return AnalyticsTopSchema(
        name=name,
        prop=prop,
        days=window,
        total=total,
        top=[
            AnalyticsTopEntrySchema(value=value, count=count) for value, count in pairs
        ],
    )


@router.get("/admin/analytics/series", response_model=AnalyticsSeriesSchema)
def admin_analytics_series(
    request: Request,
    name: str = "crawl.page",
    bucket: str = Query(default="day", pattern=r"^(hour|day)$"),
    days: int = 30,
) -> AnalyticsSeriesSchema | JSONResponse:
    """Admin-only per-bucket event counts (throughput) over a window.

    Same gate and fail-open store as the counts endpoint. ``bucket`` is
    ``hour`` or ``day``; points come back oldest-first.
    """
    denied = _frontier_admin_response(request)
    if denied is not None:
        return denied
    from openlens.analytics.events import EVENT_NAMES

    if name not in EVENT_NAMES:
        return JSONResponse(
            status_code=422,
            content=error_body("invalid_request", f"unknown analytics event: {name}"),
        )
    window = days if isinstance(days, int) and days >= 1 else 30
    window = min(window, 365)
    store, unavailable = _admin_analytics_store(request)
    if store is None:
        if unavailable is None:
            return JSONResponse(
                status_code=503,
                content=error_body(
                    "provider_unavailable", "analytics store unavailable"
                ),
            )
        return unavailable
    try:
        points = store.series(name, bucket=bucket, since_days=window)
    except ProviderError as exc:
        if exc.code == ProviderError.INVALID_REQUEST:
            return JSONResponse(
                status_code=422,
                content=error_body("invalid_request", str(exc)),
            )
        logger.warning("analytics series: read failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content=error_body("provider_unavailable", "analytics store unavailable"),
        )
    except Exception as exc:
        logger.warning("analytics series: read failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content=error_body("provider_unavailable", "analytics store unavailable"),
        )
    finally:
        with suppress(Exception):
            store.close()
    return AnalyticsSeriesSchema(
        name=name,
        bucket=bucket,
        days=window,
        points=[AnalyticsSeriesPointSchema(t=t, count=count) for t, count in points],
    )


class SavedPrefsSchema(BaseModel):
    """PUT /api/v1/prefs body: one named preference set.

    A saved set is a reusable ``filters`` mapping (preferred domains,
    weight overrides, safety preset, category terms) stored under
    ``name``. Extra fields are rejected so no query text or identifiers
    can be smuggled into the store.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_SAVED_PREF_NAME_LENGTH)
    filters: dict[str, Any] = Field(
        default_factory=dict, max_length=MAX_SAVED_PREF_FILTERS
    )

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value.strip()


class SavedPrefsResponseSchema(BaseModel):
    """Acknowledgement for a saved preference set."""

    name: str
    filters: dict[str, Any]


class SavedPrefsListResponseSchema(BaseModel):
    """GET /api/v1/prefs response body: every saved set."""

    prefs: dict[str, dict[str, Any]]


@router.get("/prefs", response_model=SavedPrefsListResponseSchema)
def list_prefs() -> SavedPrefsListResponseSchema:
    """Return every saved preference set (values only, never queries)."""
    return SavedPrefsListResponseSchema(
        prefs={name: dict(filters) for name, filters in _SAVED_PREFS.items()}
    )


@router.put("/prefs", response_model=SavedPrefsResponseSchema)
def save_prefs(body: SavedPrefsSchema) -> SavedPrefsResponseSchema:
    """Save (or replace) one named preference set.

    Bounded to ``MAX_SAVED_PREFS`` entries; beyond that the request
    fails with 400 instead of growing the in-process store.
    """
    if body.name not in _SAVED_PREFS and len(_SAVED_PREFS) >= MAX_SAVED_PREFS:
        raise HTTPException(
            status_code=400,
            detail=f"too many saved preference sets (max {MAX_SAVED_PREFS})",
        )
    _SAVED_PREFS[body.name] = dict(body.filters)
    return SavedPrefsResponseSchema(name=body.name, filters=dict(body.filters))


def _analytics_queue(request: Request) -> Any | None:
    """Cached analytics producer for this app (fail-open None).

    Fire-and-forget publishing must never slow a search or fail it, so a
    sick broker degrades to a dropped event, never a 500.
    """
    state = getattr(request.app, "state", None)
    if state is None:
        return None
    queue = getattr(state, "_analytics_queue", None)
    if queue is None:
        try:
            from openlens.analytics.events import AnalyticsQueue

            app_config = getattr(state, "config", None)
            queue = AnalyticsQueue.from_app_config(
                app_config if isinstance(app_config, dict) else {}
            )
        except Exception:
            return None
        with suppress(Exception):
            state._analytics_queue = queue
    return queue


def _publish_search_served(request: Request, query: str, mode: str, total: int) -> None:
    """Best-effort ``search.served`` event (raw text never leaves)."""
    queue = _analytics_queue(request)
    if queue is None:
        return
    with suppress(Exception):
        from openlens.analytics.events import served_event

        queue.publish_event(served_event(query, mode, total))


def _publish_feedback_event(
    request: Request, doc_id: str, action: str, reason: str | None
) -> None:
    """Best-effort ``search.feedback`` event mirroring the tally."""
    queue = _analytics_queue(request)
    if queue is None:
        return
    with suppress(Exception):
        from openlens.analytics.events import feedback_event

        queue.publish_event(feedback_event(doc_id, action, reason))


def _frontier_read_store(request: Request | WebSocket) -> Any | None:
    """Cached read-only frontier store for status overlays (fail-open).

    The store is opened once per app lifetime (same tradeoff as the
    cached admin config store): status polls must not pay pool
    construction per request. Unreachable/misconfigured backends yield
    None and the dashboard falls back to the in-memory snapshot.
    """
    store = getattr(request.app.state, "_frontier_read_store", None)
    if store is not None:
        return store
    if getattr(request.app.state, "_frontier_read_store_failed", False):
        return None
    app_config = getattr(request.app.state, "config", None)
    try:
        store = open_frontier_store(app_config if isinstance(app_config, dict) else {})
    except Exception:
        request.app.state._frontier_read_store_failed = True
        return None
    request.app.state._frontier_read_store = store
    return store


def _overlay_store_status(
    payload: FrontierStatusSchema, store: Any | None
) -> FrontierStatusSchema:
    """Replace snapshot queue/checkpoint with store truth when available.

    Snapshot seeds not yet claimed (e.g. sitting in the Kafka topic) are
    appended as ``queued`` so a fresh resume never renders an empty
    dashboard. Everything degrades to the snapshot when the store is
    down, preserving the offline/tests behavior.
    """
    if store is None:
        return payload
    try:
        checkpoint = store.load_checkpoint()
        pending = store.pending(limit=500)
        counts = store.counts()
    except Exception:
        return payload
    merged: dict[str, dict[str, Any]] = {}
    for entry in pending or []:
        url = str(getattr(entry, "url", "") or "")
        if not url.strip():
            continue
        merged[url] = {
            "url": url,
            "depth": int(getattr(entry, "depth", 0) or 0),
            "status": str(getattr(entry, "status", "queued") or "queued"),
            "priority": int(getattr(entry, "priority", 0) or 0),
        }
    # Snapshot entries arrive as submodels (Pydantic coerces the nested
    # dicts on construction), never as plain dicts.
    shaped: list[tuple[str, int, int]] = []
    for item in payload.queue or []:
        if isinstance(item, dict):
            url = str(item.get("url", "") or "")
            try:
                depth = int(item.get("depth", 0) or 0)
            except (TypeError, ValueError):
                depth = 0
        else:
            url = str(getattr(item, "url", "") or "")
            try:
                depth = int(getattr(item, "depth", 0) or 0)
            except (TypeError, ValueError):
                depth = 0
        # Compare normalized URLs: the store keys rows by normalized URL,
        # so a raw snapshot entry (trailing slash, fragment, case) must
        # not shadow a terminal store row as still queued.
        try:
            key = normalize_frontier_url(url) if url else ""
        except Exception:
            key = url
        if not key or key in merged:
            continue
        try:
            priority = int(
                item.get("priority", 0)
                if isinstance(item, dict)
                else getattr(item, "priority", 0) or 0
            )
        except (TypeError, ValueError):
            priority = 0
        shaped.append((key, depth, priority))
    # One bulk terminal-row check for all snapshot seeds: the per-URL
    # status lookup used to cost a round trip per seed on every status
    # poll and 2s WS push (thousands of seeds = multi-second responses).
    terminal: dict[str, str] = {}
    bulk_ok = False
    if store is not None and shaped:
        with suppress(Exception):
            statuses = store.statuses_of([key for key, _, _ in shaped])
            if isinstance(statuses, dict):
                bulk_ok = True
                terminal = {str(url): str(status) for url, status in statuses.items()}
    if not bulk_ok and store is not None and shaped:
        # Older/test doubles without the bulk lookup keep the legacy
        # per-URL check.
        for key, _, _ in shaped:
            if _store_row_terminal(store, key):
                terminal[key] = "done"
    for key, depth, priority in shaped:
        if terminal.get(key) not in (None, "queued", "active"):
            continue
        merged[key] = {
            "url": key,
            "depth": depth,
            "status": "queued",
            "priority": priority,
        }
    queue = list(merged.values())
    try:
        queued = int((counts or {}).get("queued", 0))
        active = int((counts or {}).get("active", 0))
        queue_depth = queued + active if (queued or active) else len(queue)
    except Exception:
        queue_depth = len(queue)
    existing_checkpoint = getattr(payload, "checkpoint", None)
    if isinstance(existing_checkpoint, dict):
        checkpoint_payload = dict(existing_checkpoint)
    elif existing_checkpoint is not None and hasattr(existing_checkpoint, "model_dump"):
        checkpoint_payload = dict(existing_checkpoint.model_dump())
    else:
        checkpoint_payload = {"phase": "idle", "pages_attempted": 0, "pages_indexed": 0}
    if checkpoint is not None:
        checkpoint_payload = {
            "phase": str(getattr(checkpoint, "phase", "running")),
            "pages_attempted": int(getattr(checkpoint, "pages_attempted", 0) or 0),
            "pages_indexed": int(getattr(checkpoint, "pages_indexed", 0) or 0),
        }
    discovery = getattr(payload, "discovery", None)
    if isinstance(discovery, dict):
        discovery_payload: Any = dict(discovery)
    else:
        discovery_payload = discovery
    return FrontierStatusSchema(
        paused=bool(payload.paused),
        queue=queue,  # type: ignore[arg-type]
        queue_depth=queue_depth,
        checkpoint=checkpoint_payload,  # type: ignore[arg-type]
        new_urls=list(payload.new_urls),  # type: ignore[union-attr,arg-type]
        workers=list(payload.workers),  # type: ignore[union-attr,arg-type]
        discovery=discovery_payload,  # type: ignore[arg-type]
        # The rebuild must preserve the serving-config flag: the lag
        # probe runs after this overlay (GET only), so dropping it here
        # reads as "disabled" on every store-backed server.
        kafka_enabled=bool(payload.kafka_enabled),
    )


def _latest_discovery_summary(
    workers: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Aggregate discovery: the latest-reporting worker's summary.

    Workers arrive newest-heartbeat-first; the first entry carrying a
    discovery fragment wins, attributed with its ``worker_id``. Never
    raises: anything unreadable degrades to None.
    """
    try:
        for entry in workers or []:
            if not isinstance(entry, dict):
                continue
            summary = entry.get("discovery")
            if not isinstance(summary, dict):
                continue
            try:
                return {
                    "worker_id": str(entry.get("worker_id", "") or ""),
                    "at": float(summary.get("at", 0.0) or 0.0),
                    "feeds_checked": max(0, int(summary.get("feeds_checked", 0) or 0)),
                    "new_urls": max(0, int(summary.get("new_urls", 0) or 0)),
                    "refreshed": max(0, int(summary.get("refreshed", 0) or 0)),
                }
            except (TypeError, ValueError):
                continue
    except Exception:
        return None
    return None


def _frontier_status_payload(app_config: Any = None) -> FrontierStatusSchema:
    """Shape the in-memory frontier snapshot into the status schema.

    ``app_config`` (when given) seeds the ``kafka_enabled`` flag only —
    the lag probe itself stays in ``_overlay_kafka_status`` (GET only),
    so the 2s live-socket cadence never pays for a throwaway consumer.
    """
    snap = frontier_state.snapshot()
    queue = [
        {
            "url": str(entry.get("url", "")),
            "depth": int(entry.get("depth", 0)),
            "status": str(entry.get("status", "queued")),
            "priority": int(entry.get("priority", 0) or 0),
        }
        for entry in snap["queue"]
        if isinstance(entry, dict) and str(entry.get("url", "")).strip()
    ]
    new_urls = [
        {"url": str(entry.get("url", "")), "status": str(entry.get("status", "added"))}
        for entry in snap["new_urls"]
        if isinstance(entry, dict) and str(entry.get("url", "")).strip()
    ]
    checkpoint = snap["checkpoint"] if isinstance(snap["checkpoint"], dict) else {}
    try:
        workers = frontier_state.list_workers()
    except Exception:
        workers = []
    try:
        discovery = _latest_discovery_summary(workers)
    except Exception:
        discovery = None
    try:
        queue_enabled = bool(
            kafka_enabled(app_config if isinstance(app_config, dict) else None)
        )
    except Exception:
        queue_enabled = False
    return FrontierStatusSchema(
        paused=bool(snap["paused"]),
        queue=queue,  # type: ignore[arg-type]
        queue_depth=len(queue),
        checkpoint={
            "phase": str(checkpoint.get("phase", "idle")),
            "pages_attempted": int(checkpoint.get("pages_attempted", 0)),
            "pages_indexed": int(checkpoint.get("pages_indexed", 0)),
        },  # type: ignore[arg-type]
        new_urls=new_urls,  # type: ignore[arg-type]
        workers=workers,  # type: ignore[arg-type]
        discovery=discovery,  # type: ignore[arg-type]
        kafka_enabled=queue_enabled,
    )


@router.get("/frontier/status", response_model=FrontierStatusSchema)
def frontier_status(request: Request) -> FrontierStatusSchema:
    """Queue view + checkpoint + new-URL list for the dashboard.

    Never 500s on state problems: an unreadable snapshot degrades to
    the empty status, mirroring the suggest endpoint's degrade rule.
    """
    try:
        app_config = getattr(request.app.state, "config", None)
        frontier_state.seed_settings_from_config(app_config)
        return _overlay_kafka_status(
            _overlay_store_status(
                _frontier_status_payload(app_config),
                _frontier_read_store(request),
            ),
            app_config,
        )
    except Exception:
        return FrontierStatusSchema(queue=[], queue_depth=0, new_urls=[])


@router.get("/frontier/failed", response_model=FrontierFailedListSchema)
def frontier_failed(
    request: Request, limit: int = 100, offset: int = 0
) -> FrontierFailedListSchema:
    """Paginated failed-URL list with reasons for the admin dashboard.

    Open read like ``/frontier/status`` (URLs + short error strings only).
    Never 500s: store failures degrade to the empty list.
    """
    capped = max(0, min(int(limit or 0), 500))
    skipped = max(0, int(offset or 0))
    try:
        store = _frontier_read_store(request)
        if store is None:
            return FrontierFailedListSchema(
                urls=[], total=0, limit=capped, offset=skipped
            )
        try:
            total = int(store.counts().get("failed", 0))
        except Exception:
            total = 0
        rows = store.failed(limit=capped, offset=skipped)
        return FrontierFailedListSchema(
            urls=[
                FrontierFailedUrlSchema(
                    url=entry.url,
                    reason=entry.last_error or "",
                    attempts=max(0, int(entry.attempts or 0)),
                )
                for entry in rows
            ],
            total=total,
            limit=capped,
            offset=skipped,
        )
    except Exception:
        return FrontierFailedListSchema(urls=[], total=0, limit=capped, offset=skipped)


@router.get("/frontier/failed/domains", response_model=FrontierFailedDomainListSchema)
def frontier_failed_domains(
    request: Request, limit: int = 100, offset: int = 0
) -> FrontierFailedDomainListSchema:
    """Failed-URL rollup by host with per-host counts (admin failed view).

    Open read like ``/frontier/failed`` (hosts + counts only, no
    secrets). Never 500s: store failures degrade to the empty list.
    """
    capped = max(0, int(limit or 0))
    skipped = max(0, int(offset or 0))
    try:
        store = _frontier_read_store(request)
        if store is None:
            return FrontierFailedDomainListSchema(
                domains=[], total=0, limit=capped, offset=skipped
            )
        try:
            total = int(store.failed_domain_count() or 0)
        except Exception:
            total = 0
        pairs = store.failed_domains(limit=capped, offset=skipped)
        return FrontierFailedDomainListSchema(
            domains=[
                FrontierFailedDomainSchema(
                    domain=domain,
                    failed=max(0, int(count)),
                    top_reason=reason or "",
                )
                for domain, count, reason in pairs
            ],
            total=total,
            limit=capped,
            offset=skipped,
        )
    except Exception:
        return FrontierFailedDomainListSchema(
            domains=[], total=0, limit=capped, offset=skipped
        )


@router.get("/frontier/queued/domains", response_model=FrontierQueuedDomainListSchema)
def frontier_queued_domains(
    request: Request, limit: int = 100, offset: int = 0
) -> FrontierQueuedDomainListSchema:
    """Queued-URL rollup by host with per-host counts (admin queue view).

    Open read like ``/frontier/failed/domains`` (hosts + counts only,
    no secrets). Never 500s: store failures degrade to the empty list.
    """
    capped = max(0, int(limit or 0))
    skipped = max(0, int(offset or 0))
    try:
        store = _frontier_read_store(request)
        if store is None:
            return FrontierQueuedDomainListSchema(
                domains=[], total=0, limit=capped, offset=skipped
            )
        try:
            total = int(store.queued_domain_count() or 0)
        except Exception:
            total = 0
        pairs = store.queued_domains(limit=capped, offset=skipped)
        return FrontierQueuedDomainListSchema(
            domains=[
                FrontierQueuedDomainSchema(
                    domain=domain,
                    queued=max(0, int(count)),
                    top_priority=int(top_priority),
                )
                for domain, count, top_priority in pairs
            ],
            total=total,
            limit=capped,
            offset=skipped,
        )
    except Exception:
        return FrontierQueuedDomainListSchema(
            domains=[], total=0, limit=capped, offset=skipped
        )


def _merge_settings_domains(request: Request, key: str, targets: list[str]) -> None:
    """Union ``targets`` into the frontier settings list ``key``.

    Best effort: the stored row (what workers enforce) and the
    in-process snapshot (what the drawer renders) both update, so a
    skip from the domains view shows up in the settings skip list
    without a separate save. Failures only log, never fail the action.
    """
    try:
        from openlens.storage.frontier import normalize_skip_domains

        clean = normalize_skip_domains(targets) or []
        if not clean:
            return
        app_config = getattr(request.app.state, "config", None)
        stored = _store_get_settings(app_config)
        current = list(stored.get(key, []) or []) if isinstance(stored, dict) else []
        merged = [*current]
        for domain in clean:
            if domain not in merged:
                merged.append(domain)
        _store_save_settings(app_config, {key: merged})
        with suppress(Exception):
            state = frontier_state.get_frontier_state()
            settings = getattr(state, "settings", None)
            if isinstance(settings, dict):
                existing = list(settings.get(key, []) or [])
                for domain in clean:
                    if domain not in existing:
                        existing.append(domain)
                settings[key] = existing
    except Exception as exc:
        logger.warning("frontier settings merge failed: %s", exc)


def _frontier_domain_targets(body: Any) -> list[str] | None:
    """Validated host list for a domain-scoped failed-queue action.

    Fail-closed: non-list bodies, empty lists, and fully-malformed
    lists all return None (the endpoint answers 422).
    """
    from openlens.storage.frontier import normalize_skip_domains

    domains = getattr(body, "domains", None)
    if not isinstance(domains, list) or not domains:
        return None
    targets = normalize_skip_domains(domains)
    return targets or None


def _frontier_domains_action(
    request: Request,
    body: FrontierDomainsActionSchema,
    op: str,
) -> FrontierDomainsResultSchema | JSONResponse:
    """Shared admin gate + dispatch for failed-domain actions."""
    denied = _frontier_admin_response(request)
    if denied is not None:
        return denied
    targets = _frontier_domain_targets(body)
    if targets is None:
        return JSONResponse(
            status_code=422,
            content=error_body(
                "invalid_request", "domains must be a non-empty host list"
            ),
        )
    try:
        store = _frontier_read_store(request)
        if store is None:
            return JSONResponse(
                status_code=503,
                content=error_body(
                    "provider_unavailable", "frontier store unavailable"
                ),
            )
        if op == "requeue":
            affected = int(store.requeue_failed_domains(targets) or 0)
        elif op == "purge":
            affected = int(store.purge_failed_domains(targets) or 0)
        elif op == "skip":
            affected = int(store.skip_queued_domains(targets) or 0)
            _merge_settings_domains(request, "skip_domains", targets)
        else:  # pragma: no cover - dispatch is closed above
            return JSONResponse(
                status_code=422,
                content=error_body("invalid_request", f"unknown domain op: {op}"),
            )
    except ProviderError as exc:
        logger.warning("frontier domains %s failed: %s", op, exc)
        return JSONResponse(
            status_code=503,
            content=error_body("provider_unavailable", "frontier store unavailable"),
        )
    except Exception as exc:
        logger.warning("frontier domains %s failed: %s", op, exc)
        return JSONResponse(
            status_code=503,
            content=error_body("provider_unavailable", "frontier store unavailable"),
        )
    return FrontierDomainsResultSchema(domains=targets, affected=affected)


@router.post("/frontier/domains/requeue", response_model=FrontierDomainsResultSchema)
def frontier_domains_requeue(
    body: FrontierDomainsActionSchema, request: Request
) -> FrontierDomainsResultSchema | JSONResponse:
    """Retry a failed host: failed rows back to queued, attempts reset.

    Admin-only. Attempts reset to 0 with the error cleared (reseed
    semantics), so the rows actually refetch instead of re-terminating
    on the spent retry budget.
    """
    return _frontier_domains_action(request, body, "requeue")


@router.post("/frontier/domains/purge", response_model=FrontierDomainsResultSchema)
def frontier_domains_purge(
    body: FrontierDomainsActionSchema, request: Request
) -> FrontierDomainsResultSchema | JSONResponse:
    """Delete a failed host's history rows (admin-only, irreversible)."""
    return _frontier_domains_action(request, body, "purge")


@router.post("/frontier/domains/skip", response_model=FrontierDomainsResultSchema)
def frontier_domains_skip(
    body: FrontierDomainsActionSchema, request: Request
) -> FrontierDomainsResultSchema | JSONResponse:
    """Skip a host's queued rows (admin-only; same as bulk domain skip)."""
    return _frontier_domains_action(request, body, "skip")


@router.post("/frontier/domains/priority", response_model=FrontierDomainsResultSchema)
def frontier_domains_priority(
    body: FrontierDomainPrioritySchema, request: Request
) -> FrontierDomainsResultSchema | JSONResponse:
    """Shift a host's queued rows' priority by ``delta`` (admin-only).

    Positive deltas fast-lane the host (higher dequeues first,
    ``ORDER BY priority DESC``); negative deltas sink it. Only
    ``queued`` rows move; fail-closed on malformed host lists.
    """
    denied = _frontier_admin_response(request)
    if denied is not None:
        return denied
    targets = _frontier_domain_targets(body)
    if targets is None:
        return JSONResponse(
            status_code=422,
            content=error_body(
                "invalid_request", "domains must be a non-empty host list"
            ),
        )
    try:
        store = _frontier_read_store(request)
        if store is None:
            return JSONResponse(
                status_code=503,
                content=error_body(
                    "provider_unavailable", "frontier store unavailable"
                ),
            )
        affected = int(store.bump_queued_priority(targets, body.delta) or 0)
    except ProviderError as exc:
        logger.warning("frontier domains priority failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content=error_body("provider_unavailable", "frontier store unavailable"),
        )
    except Exception as exc:
        logger.warning("frontier domains priority failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content=error_body("provider_unavailable", "frontier store unavailable"),
        )
    return FrontierDomainsResultSchema(domains=targets, affected=affected)


#: Seconds between pushes on the live frontier websocket. Tests
#: monkeypatch this (module-global lookup per iteration) instead of
#: waiting out the production cadence.
FRONTIER_LIVE_INTERVAL_S = 2.0


@router.websocket("/frontier/live")
async def frontier_live(websocket: WebSocket) -> None:
    """Stream frontier status snapshots until the client disconnects.

    Same queue view as ``GET /frontier/status`` (snapshot overlaid with
    store truth), pushed immediately on connect and every
    ``FRONTIER_LIVE_INTERVAL_S`` after. The Kafka lag/version overlay
    rides the GET only, not this 2s push cadence (a throwaway consumer
    per push would be wasteful). The socket requires the same
    admin rights as the mutating endpoints (``X-Admin-Token`` header):
    the live worker/queue view is an admin surface and an unauthenticated
    socket can be held open to flood the server. Awaiting the
    next client message with a timeout both detects disconnects
    promptly and lets a client nudge an early refresh by sending any
    text frame.
    """
    limiter = getattr(websocket.app.state, "rate_limiter", None)
    if limiter is not None:
        peer = websocket.client.host if websocket.client else None
        if limiter.check("ws", client_ip(dict(websocket.headers), peer)) is not None:
            await websocket.close(code=1008)
            return
    if _ws_admin_denied(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        while True:
            try:
                payload = _overlay_store_status(
                    _frontier_status_payload(
                        getattr(websocket.app.state, "config", None)
                    ),
                    _frontier_read_store(websocket),
                )
            except Exception:
                payload = FrontierStatusSchema(queue=[], queue_depth=0, new_urls=[])
            try:
                await websocket.send_json(payload.model_dump())
            except (WebSocketDisconnect, RuntimeError):
                break
            try:
                await asyncio.wait_for(
                    websocket.receive(), timeout=FRONTIER_LIVE_INTERVAL_S
                )
            except asyncio.TimeoutError:
                continue
            except (WebSocketDisconnect, RuntimeError):
                break
    except (WebSocketDisconnect, RuntimeError):
        pass


def _store_save_settings(app_config: Any, settings: Mapping[str, Any]) -> bool:
    """Mirror dashboard settings into the persistent frontier settings row.

    Workers read ``store.get_settings()`` (not backend memory), so a PUT
    that stayed in-process would never reach the crawlers and would be
    lost on restart. Best effort like :func:`_store_set_paused`.
    """
    try:
        store = open_frontier_store(app_config if isinstance(app_config, dict) else {})
        try:
            current = store.get_settings()
            merged = dict(current) if isinstance(current, dict) else {}
            merged.update(dict(settings))
            store.save_settings(merged)
            return True
        finally:
            with suppress(Exception):
                store.close()
    except Exception:
        return False


def _store_row_terminal(store: Any, url: str) -> bool:
    """Whether the store already finished ``url`` (done/failed/skipped).

    Guards the status overlay against stale in-memory seeds: a raw seed
    that the workers already processed must disappear instead of
    reshowing as queued. Best effort — unknown means "still pending".
    """
    if store is None:
        return False
    try:
        status = store.status_of(url)
    except Exception:
        return False
    return status is not None and status not in ("queued", "active")


def _store_get_settings(app_config: Any) -> dict[str, Any]:
    """Best-effort read of the persistent frontier settings row."""
    try:
        store = open_frontier_store(app_config if isinstance(app_config, dict) else {})
        try:
            settings = store.get_settings()
        finally:
            with suppress(Exception):
                store.close()
        return dict(settings) if isinstance(settings, dict) else {}
    except Exception:
        return {}


def _store_set_paused(app_config: Any, paused: bool) -> bool:
    """Mirror the pause flag into the persistent frontier settings row.

    Workers poll ``store.get_settings()["paused"]`` (not the in-memory
    snapshot), so the dashboard flag alone never stops a worker. Best
    effort: store failures (no DB in tests/offline) never break the
    dashboard write. Returns True when the row was updated.

    When pausing, expired leases are reaped first: rows claimed by dead
    workers return to ``queued`` (rows with a live heartbeat are never
    stolen), so a pause-then-clear actually empties the queue instead
    of leaving orphan ``active`` rows that resume would resurrect.
    """
    try:
        store = open_frontier_store(app_config if isinstance(app_config, dict) else {})
        try:
            settings = store.get_settings()
            merged = dict(settings) if isinstance(settings, dict) else {}
            merged["paused"] = paused
            store.save_settings(merged)
            if paused:
                reap = getattr(store, "reap_expired_leases", None)
                if callable(reap):
                    with suppress(Exception):
                        reap()
        finally:
            with suppress(Exception):
                store.close()
    except Exception as exc:
        logger.warning("frontier pause flag not persisted to store: %s", exc)
        return False
    return True


def _enqueue_seeds_to_store(
    app_config: Any, seeds: list[str], priority: int | None
) -> bool:
    """Best-effort store enqueue for resume seeds (never raises).

    Returns True when the store accepted the write; failures only log,
    so a sick store never turns a successful Kafka publish into a 500.
    """
    try:
        store = open_frontier_store(app_config if isinstance(app_config, dict) else {})
        try:
            for seed in seeds:
                with suppress(Exception):
                    store.enqueue(seed, 0, priority=priority)
        finally:
            with suppress(Exception):
                store.close()
        return True
    except Exception as exc:
        logger.warning("frontier resume: store enqueue failed: %s", exc)
        return False


def _publish_seeds(
    app_config: Any, seeds: list[str], priority: int | None = None
) -> bool:
    """Publish seeds to Kafka, else enqueue them into the persistent store.

    Kafka-first when ``kafka:`` is enabled; the store fallback keeps
    local/test setups crawling when the broker is unreachable. Seeds are
    also kept in the in-memory snapshot by the caller for the dashboard.
    An explicit ``priority`` is stored on insert and overwrites the
    stored value on conflict; ``None`` keeps whatever is there.
    An explicit priority additionally fast-lanes through the store:
    topic order is offset order, so a priority seed would otherwise wait
    behind the whole backlog, while the store copy lets the next
    per-message drain claim it first (``priority DESC``); the later
    Kafka twin then dedups to a no-op.
    Returns True when a durable queue (Kafka or store) accepted them.
    """
    if seeds and kafka_enabled(app_config if isinstance(app_config, dict) else None):
        try:
            queue = open_kafka_queue(app_config)
            try:
                batched = getattr(queue, "publish_many", None)
                if callable(batched):
                    # One broker flush for the whole paste instead of one
                    # round trip per seed.
                    batched([(seed, 0, priority) for seed in seeds])
                else:
                    for seed in seeds:
                        queue.publish(seed, 0, priority=priority)
            finally:
                with suppress(Exception):
                    queue.close()
            if priority is not None:
                _enqueue_seeds_to_store(app_config, seeds, priority)
            return True
        except ProviderError as exc:
            logger.warning(
                "frontier resume: Kafka publish failed, using store fallback: %s", exc
            )
    if not seeds:
        return True
    return _enqueue_seeds_to_store(app_config, seeds, priority)


#: Quiesce budget for POST /frontier/clear: after pausing, in-flight
#: page fetches (bounded by the per-message drain cap) finish and land
#: their discovered links before the store is wiped and the topic purged.
#: Without this, completions landing mid-clear refill the queue seconds
#: later. Polls the live-active count; expired leases are reaped along
#: the way so dead workers cannot stall it past the budget.
_CLEAR_QUIESCE_TIMEOUT_S = 60.0
_CLEAR_QUIESCE_POLL_S = 2.0


def _quiesce_in_flight(store: Any) -> float:
    """Wait for in-flight claims to land, bounded by the quiesce budget.

    Returns seconds waited. Each poll first reaps expired leases (dead
    workers) so only genuinely live fetches hold the gate; a live fetch
    that outlasts the budget does not fail the clear — its links land
    as queued rows the operator can clear again. Never raises.
    """
    waited = 0.0
    deadline = time.monotonic() + max(0.0, _CLEAR_QUIESCE_TIMEOUT_S)
    while True:
        try:
            reap = getattr(store, "reap_expired_leases", None)
            if callable(reap):
                with suppress(Exception):
                    reap()
            counts = store.counts() or {}
            live = int(counts.get("active", 0) or 0)
        except Exception:
            return waited
        if live <= 0:
            return waited
        if time.monotonic() >= deadline:
            logger.warning(
                "frontier clear: quiesce timed out with %d live active URL(s)",
                live,
            )
            return waited
        time.sleep(max(0.0, _CLEAR_QUIESCE_POLL_S))
        waited += _CLEAR_QUIESCE_POLL_S


@router.post("/frontier/clear", response_model=FrontierStatusSchema)
def frontier_clear(request: Request) -> FrontierStatusSchema | JSONResponse:
    """Pause crawling and drop every queued URL (admin-only).

    Clears the persistent store's ``queued`` rows and purges the Kafka
    topic when ``kafka:`` is enabled — the topic must go too, or already
    published messages refill the queue on the next resume. In-flight
    (``active``) rows are kept, as is all history (``done``/``failed``),
    checkpoints, and settings. The in-memory snapshot queue is emptied
    so the dashboard stops showing seeds that no longer exist anywhere.
    Clearing is best-effort per step and always reports honestly: the
    response carries ``cleared_queued`` and ``kafka_purged``.
    """
    denied = _frontier_admin_response(request)
    if denied is not None:
        return denied
    state = frontier_state.get_frontier_state()
    state.paused = True
    state.checkpoint["phase"] = "paused"
    state.queue = []
    app_config = getattr(request.app.state, "config", None)
    _store_set_paused(app_config, True)
    cleared = 0
    try:
        store = _frontier_read_store(request)
        if store is not None:
            _quiesce_in_flight(store)
            cleared = int(store.clear_queued() or 0)
    except Exception as exc:
        logger.warning("frontier clear: store clear failed: %s", exc)
    purged = False
    if kafka_enabled(app_config if isinstance(app_config, dict) else None):
        try:
            result = purge_topic(app_config)
            purged = bool(result.get("purged", False))
        except ProviderError as exc:
            logger.warning("frontier clear: Kafka purge failed: %s", exc)
    payload = _overlay_store_status(
        _frontier_status_payload(app_config), _frontier_read_store(request)
    )
    payload.cleared_queued = cleared
    payload.kafka_purged = purged
    return payload


@router.post("/frontier/pause", response_model=FrontierStatusSchema)
def frontier_pause(request: Request) -> FrontierStatusSchema | JSONResponse:
    """Pause crawling; the queue snapshot is preserved as-is.

    Admin-only (see `_frontier_admin_response`); reads stay open.
    """
    denied = _frontier_admin_response(request)
    if denied is not None:
        return denied
    state = frontier_state.get_frontier_state()
    state.paused = True
    state.checkpoint["phase"] = "paused"
    app_config = getattr(request.app.state, "config", None)
    _store_set_paused(app_config, True)
    return _overlay_store_status(
        _frontier_status_payload(app_config), _frontier_read_store(request)
    )


@router.post("/frontier/resume", response_model=FrontierStatusSchema)
def frontier_resume(
    body: FrontierResumeSchema, request: Request
) -> FrontierStatusSchema | JSONResponse:
    """Resume crawling, optionally replacing the queue with `seeds`.

    Admin-only: seed URLs choose what gets crawled, so untrusted
    callers must not set them.
    """
    denied = _frontier_admin_response(request)
    if denied is not None:
        return denied
    state = frontier_state.get_frontier_state()
    state.paused = False
    state.checkpoint["phase"] = "running"
    seeds = [seed.strip() for seed in body.seeds if seed.strip()]
    priority = body.priority if body.priority is not None else 0
    if seeds:
        state.queue = [
            {"url": seed, "depth": 0, "status": "queued", "priority": priority}
            for seed in seeds
        ]
    app_config = getattr(request.app.state, "config", None)
    _publish_seeds(app_config, seeds, priority=body.priority)
    # Explicit seeds must refetch even when they already finished once:
    # plain enqueue is insert-only and would no-op on the terminal row.
    if seeds:
        try:
            store = _frontier_read_store(request)
            if store is not None:
                bulk = getattr(store, "reseed_many", None)
                if callable(bulk):
                    with suppress(Exception):
                        bulk(seeds)
                else:
                    for seed in seeds:
                        with suppress(Exception):
                            store.reseed(seed)
        except Exception as exc:
            logger.warning("frontier resume: store reseed failed: %s", exc)
    _store_set_paused(app_config, False)
    return _overlay_store_status(
        _frontier_status_payload(app_config), _frontier_read_store(request)
    )


# -- user-requested crawl URLs (public submit, admin review) -------------


def _crawl_request_store(request: Request) -> Any | None:
    """Frontier store for crawl requests (None when unavailable)."""
    return _frontier_read_store(request)


def _valid_crawl_url(raw: str) -> str | None:
    """Normalize a user-submitted URL; None when not http(s)."""
    try:
        from openlens.storage.frontier import normalize_frontier_url
    except Exception:
        return None
    normalized = normalize_frontier_url(raw)
    parsed = urlsplit(normalized)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return normalized


@router.post("/crawl-requests", response_model=CrawlRequestSchema, status_code=201)
def create_crawl_request(body: CrawlRequestSubmitSchema, request: Request) -> Any:
    """Queue a user-requested URL for admin review (rate-limited).

    Dedupes without writing: a URL the frontier already knows returns
    a ``duplicate`` marker (200); an existing ``pending`` request is
    returned as-is (200). Only genuinely new URLs insert a row (201).
    """
    normalized = _valid_crawl_url(body.url)
    if normalized is None:
        return JSONResponse(
            status_code=422,
            content=error_body(
                "invalid_request", "url must be an absolute http(s) URL"
            ),
        )
    store = _crawl_request_store(request)
    if store is None:
        return JSONResponse(
            status_code=503,
            content=error_body(
                "provider_unavailable", "crawl-request store unavailable"
            ),
        )
    if store.is_known(normalized):
        # 200: the frontier already knows this URL; nothing to review.
        return JSONResponse(
            status_code=200,
            content=CrawlRequestSchema(
                id=None,
                url=normalized,
                status="duplicate",
                created_at=None,
                decided_at=None,
            ).model_dump(),
        )
    for existing in store.list_crawl_requests(status="pending", limit=1000):
        if existing.get("url") == normalized:
            # 200: identical request already awaiting review.
            return JSONResponse(status_code=200, content=existing)
    record = store.submit_crawl_request(normalized)
    if record.get("id") is None:
        return JSONResponse(
            status_code=503,
            content=error_body(
                "provider_unavailable", "crawl-request store unavailable"
            ),
        )
    return CrawlRequestSchema(**record)


@router.get("/frontier/crawl-requests", response_model=CrawlRequestListSchema)
def frontier_list_crawl_requests(request: Request, status: str | None = None) -> Any:
    """Admin review queue for user-requested crawl URLs."""
    denied = _frontier_admin_response(request)
    if denied is not None:
        return denied
    if status is not None and status not in (
        "pending",
        "approved",
        "declined",
    ):
        return JSONResponse(
            status_code=422,
            content=error_body("invalid_request", "unknown request status"),
        )
    store = _crawl_request_store(request)
    if store is None:
        return JSONResponse(
            status_code=503,
            content=error_body(
                "provider_unavailable", "crawl-request store unavailable"
            ),
        )
    records = store.list_crawl_requests(status=status, limit=200)
    return CrawlRequestListSchema(
        requests=[CrawlRequestSchema(**record) for record in records],
        total=len(records),
    )


@router.post(
    "/frontier/crawl-requests/{request_id}/approve",
    response_model=CrawlRequestSchema,
)
def frontier_approve_crawl_request(request_id: int, request: Request) -> Any:
    """Admin approves a crawl request: marks approved + enqueues the URL."""
    denied = _frontier_admin_response(request)
    if denied is not None:
        return denied
    store = _crawl_request_store(request)
    if store is None:
        return JSONResponse(
            status_code=503,
            content=error_body(
                "provider_unavailable", "crawl-request store unavailable"
            ),
        )
    pending = {
        record.get("id"): record
        for record in store.list_crawl_requests(status="pending", limit=1000)
    }
    record = pending.get(request_id)
    if record is None:
        return JSONResponse(
            status_code=404,
            content=error_body("not_found", "no pending crawl request"),
        )
    if not store.set_crawl_request_status(request_id, "approved"):
        return JSONResponse(
            status_code=404,
            content=error_body("not_found", "no pending crawl request"),
        )
    app_config = getattr(request.app.state, "config", None)
    _publish_seeds(app_config, [str(record.get("url") or "")])
    with suppress(Exception):
        store.reseed(str(record.get("url") or ""))
    record["status"] = "approved"
    return CrawlRequestSchema(**record)


@router.post(
    "/frontier/crawl-requests/{request_id}/decline",
    response_model=CrawlRequestSchema,
)
def frontier_decline_crawl_request(request_id: int, request: Request) -> Any:
    """Admin declines a crawl request (row stays for audit)."""
    denied = _frontier_admin_response(request)
    if denied is not None:
        return denied
    store = _crawl_request_store(request)
    if store is None:
        return JSONResponse(
            status_code=503,
            content=error_body(
                "provider_unavailable", "crawl-request store unavailable"
            ),
        )
    if not store.set_crawl_request_status(request_id, "declined"):
        return JSONResponse(
            status_code=404,
            content=error_body("not_found", "no pending crawl request"),
        )
    decided = {
        record.get("id"): record
        for record in store.list_crawl_requests(status="declined", limit=1000)
    }
    record = decided.get(request_id, {"id": request_id, "status": "declined"})
    return CrawlRequestSchema(**record)


@router.get("/frontier/settings", response_model=FrontierSettingsResponseSchema)
def frontier_get_settings(request: Request) -> FrontierSettingsResponseSchema:
    """Current frontier settings (config-seeded, then PUT-overridden).

    PUT values persist in the frontier store, so a saved dashboard edit
    survives restarts and reaches the crawler workers (which read the
    store row, not backend memory). Stored values win over the seeds.
    """
    with suppress(Exception):
        app_config = getattr(request.app.state, "config", None)
        frontier_state.seed_settings_from_config(app_config)
    snap = frontier_state.snapshot()
    settings = snap["settings"] if isinstance(snap["settings"], dict) else {}
    stored = _store_get_settings(getattr(request.app.state, "config", None))
    if stored:
        allowed = set(FrontierSettingsSchema.model_fields)
        for key, value in stored.items():
            if key in allowed:
                settings[key] = value
    response = FrontierSettingsResponseSchema(settings=settings)  # type: ignore[arg-type]
    return response


@router.put("/frontier/settings", response_model=FrontierSettingsResponseSchema)
def frontier_put_settings(
    body: FrontierSettingsSchema,
    request: Request,
) -> FrontierSettingsResponseSchema | JSONResponse:
    """Replace the frontier settings (strict body, unknown keys rejected).

    Admin-only.
    """
    denied = _frontier_admin_response(request)
    if denied is not None:
        return denied
    state = frontier_state.get_frontier_state()
    state.settings = body.model_dump()
    _store_save_settings(getattr(request.app.state, "config", None), state.settings)
    return FrontierSettingsResponseSchema(settings=body)


@router.get("/frontier/workers", response_model=FrontierWorkersResponseSchema)
def frontier_list_workers() -> FrontierWorkersResponseSchema:
    """List crawler workers recently heard from by this API replica.

    Read-only: stays open even when `OPENLENS_FRONTIER_ADMIN_TOKEN` is
    set. Only tracks workers heartbeating this replica (see
    `frontier_state.WORKER_STALE_AFTER_S`); entries older than the stale
    window are pruned on read.
    """
    try:
        workers = frontier_state.list_workers()
    except Exception:
        workers = []
    return FrontierWorkersResponseSchema(
        workers=workers,  # type: ignore[arg-type]
        worker_count=len(workers),
    )


@router.post(
    "/frontier/workers/heartbeat",
    response_model=FrontierWorkersResponseSchema,
)
def frontier_worker_heartbeat(
    body: FrontierWorkerHeartbeatSchema, request: Request
) -> FrontierWorkersResponseSchema | JSONResponse:
    """Record one worker heartbeat and return the live worker list.

    Worker-control plane: admin-only (same gate as pause/resume/settings).
    Blank worker ids are
    rejected with 422 so anonymous rows can never enter the registry.
    """
    denied = _frontier_admin_response(request)
    if denied is not None:
        return denied
    worker_id = body.worker_id.strip()
    if not worker_id:
        return JSONResponse(
            status_code=422,
            content=error_body("invalid_request", "worker_id must not be blank"),
        )
    try:
        frontier_state.heartbeat_worker(
            worker_id,
            crawl_id=body.crawl_id,
            claimed=body.claimed,
            status=body.status,
            role=body.role,
            discovery=body.discovery,
            stats=body.stats,
        )
        workers = frontier_state.list_workers()
    except Exception:
        workers = []
    return FrontierWorkersResponseSchema(
        workers=workers,  # type: ignore[arg-type]
        worker_count=len(workers),
    )


# -- Cluster map + Kafka node registry (scale-out control plane) --------


def _kafka_file_section(app_config: Any) -> dict[str, Any]:
    """Validated file-config ``kafka:`` section ({} when absent)."""
    from openlens.indexing.kafka_queue import kafka_section

    try:
        section = kafka_section(app_config if isinstance(app_config, dict) else {})
    except Exception:
        return {}
    return dict(section) if isinstance(section, dict) else {}


def _stored_cluster_map(app_config: Any) -> dict[str, Any]:
    """Stored cluster-map overrides from the settings row ({} when none)."""
    stored = _store_get_settings(app_config).get(CLUSTER_MAP_SETTINGS_KEY, {})
    return dict(stored) if isinstance(stored, dict) else {}


def _cluster_map_payload(app_config: Any) -> ClusterMapResponseSchema:
    """Effective cluster map: file defaults, stored overrides win."""
    file_section = _kafka_file_section(app_config)
    servers = list(file_section.get("bootstrap_servers", ["localhost:9092"]))
    topic = str(file_section.get("topic", "crawl.urls") or "crawl.urls")
    group = str(file_section.get("group_id", "crawlers") or "crawlers")
    version = 0
    source = "file"
    stored = _stored_cluster_map(app_config)
    if stored:
        override = stored.get("bootstrap_servers")
        if (
            isinstance(override, list)
            and override
            and all(isinstance(s, str) and s.strip() for s in override)
        ):
            servers = [s.strip() for s in override]
        for key, current in (("topic", topic), ("group_id", group)):
            value = stored.get(key)
            if isinstance(value, str) and value.strip():
                if key == "topic":
                    topic = value.strip()
                else:
                    group = value.strip()
        raw_version = stored.get("config_version", 0)
        if isinstance(raw_version, bool):
            pass
        elif isinstance(raw_version, int) and raw_version >= 0:
            version = raw_version
        source = "stored"
    return ClusterMapResponseSchema(
        bootstrap_servers=servers,
        topic=topic,
        group_id=group,
        config_version=version,
        source=source,
    )


def _kafka_nodes(app_config: Any) -> list[dict[str, Any]]:
    """Registered Kafka nodes from the settings row ([] when none)."""
    stored = _store_get_settings(app_config).get(KAFKA_NODES_SETTINGS_KEY, [])
    if not isinstance(stored, list):
        return []
    nodes: list[dict[str, Any]] = []
    for entry in stored:
        if not isinstance(entry, dict):
            continue
        try:
            node_id = int(entry.get("node_id", 0))
        except (TypeError, ValueError):
            continue
        if node_id < 1:
            continue
        nodes.append(
            {
                "node_id": node_id,
                "role": str(entry.get("role", "broker") or "broker"),
                "status": str(entry.get("status", "pending") or "pending"),
                "advertise_listener": str(entry.get("advertise_listener", "") or ""),
                "created_at": entry.get("created_at", 0.0),
            }
        )
    nodes.sort(key=lambda node: node["node_id"])
    return nodes


def _save_kafka_nodes(app_config: Any, nodes: list[dict[str, Any]]) -> bool:
    """Persist the node registry alongside the other settings keys."""
    try:
        store = open_frontier_store(app_config if isinstance(app_config, dict) else {})
        try:
            current = store.get_settings()
            merged = dict(current) if isinstance(current, dict) else {}
            merged[KAFKA_NODES_SETTINGS_KEY] = list(nodes)
            store.save_settings(merged)
            return True
        finally:
            with suppress(Exception):
                store.close()
    except Exception:
        return False


def _overlay_kafka_status(
    payload: FrontierStatusSchema, app_config: Any
) -> FrontierStatusSchema:
    """Best-effort lag + map version onto the status payload.

    Never raises and never slows the dashboard when Kafka is
    disabled: the lag probe runs only when the file config enables
    the queue, with a short timeout; failures leave nulls.
    """
    try:
        payload.cluster_config_version = _cluster_map_payload(app_config).config_version
    except Exception:  # noqa: S110 — map failure keeps version 0
        pass
    try:
        from openlens.indexing.kafka_queue import kafka_enabled, topic_group_lag

        if not kafka_enabled(app_config if isinstance(app_config, dict) else None):
            return payload
        resolved = _cluster_map_payload(app_config)
        lag = topic_group_lag(
            list(resolved.bootstrap_servers),
            resolved.topic,
            resolved.group_id,
            timeout_s=2.0,
        )
        payload.kafka_lag_total = int(lag.get("total_lag", 0))
        payload.kafka_lag_partitions = int(lag.get("partitions", 0))
    except Exception:  # noqa: S110 — lag probe is best-effort; nulls stay
        pass
    return payload


@router.get("/frontier/cluster-map", response_model=ClusterMapResponseSchema)
def frontier_cluster_map(request: Request) -> ClusterMapResponseSchema:
    """Effective Kafka reachability for workers (admin-only).

    File config seeds the map; a stored PUT overrides it and bumps
    ``config_version``, which live workers pick up without restarting.
    """
    denied = _frontier_admin_response(request)
    if denied is not None:
        return denied  # type: ignore[return-value]
    return _cluster_map_payload(getattr(request.app.state, "config", None))


@router.put("/frontier/cluster-map", response_model=ClusterMapResponseSchema)
def frontier_put_cluster_map(
    body: ClusterMapSchema, request: Request
) -> ClusterMapResponseSchema | JSONResponse:
    """Replace the stored cluster map and bump its version (admin-only).

    Workers poll the version and recreate their Kafka clients on change,
    so broker rotations need no restarts. Other settings keys are
    preserved; only the map (plus its new version) is written.
    """
    denied = _frontier_admin_response(request)
    if denied is not None:
        return denied
    app_config = getattr(request.app.state, "config", None)
    stored = _stored_cluster_map(app_config)
    try:
        version = int(stored.get("config_version", 0) or 0)
    except (TypeError, ValueError):
        version = 0
    merged = _store_get_settings(app_config)
    merged[CLUSTER_MAP_SETTINGS_KEY] = {
        "bootstrap_servers": list(body.bootstrap_servers),
        "topic": body.topic,
        "group_id": body.group_id,
        "config_version": max(0, version) + 1,
    }
    if not _store_save_settings(app_config, merged):
        return JSONResponse(
            status_code=503,
            content=error_body("unavailable", "could not persist the cluster map"),
        )
    return _cluster_map_payload(app_config)


@router.get("/frontier/kafka/nodes", response_model=KafkaNodesResponseSchema)
def frontier_kafka_nodes(request: Request) -> KafkaNodesResponseSchema | JSONResponse:
    """Registered Kafka broker nodes (admin-only inventory)."""
    denied = _frontier_admin_response(request)
    if denied is not None:
        return denied
    nodes = _kafka_nodes(getattr(request.app.state, "config", None))
    return KafkaNodesResponseSchema(nodes=nodes, node_count=len(nodes))  # type: ignore[arg-type]


@router.post(
    "/frontier/kafka/nodes/prepare",
    response_model=KafkaNodePrepareResponseSchema,
)
def frontier_kafka_node_prepare(
    body: KafkaNodePrepareSchema, request: Request
) -> KafkaNodePrepareResponseSchema | JSONResponse:
    """Reserve a broker node id and return its join parameters (admin-only).

    The operator (or provisioning script) formats the new broker with the
    returned cluster id, starts it against the controller quorum, then
    marks it joined. Ids are explicit so compose-owned 1-3 are never
    reallocated; duplicates are rejected with 409.
    """
    denied = _frontier_admin_response(request)
    if denied is not None:
        return denied
    app_config = getattr(request.app.state, "config", None)
    nodes = _kafka_nodes(app_config)
    if any(node["node_id"] == body.node_id for node in nodes):
        return JSONResponse(
            status_code=409,
            content=error_body(
                "conflict", f"kafka node {body.node_id} is already registered"
            ),
        )
    nodes.append(
        {
            "node_id": body.node_id,
            "role": "broker",
            "status": "pending",
            "advertise_listener": body.advertise_listener,
            "created_at": time.time(),
        }
    )
    if not _save_kafka_nodes(app_config, nodes):
        return JSONResponse(
            status_code=503,
            content=error_body("unavailable", "could not persist the node registry"),
        )
    file_section = _kafka_file_section(app_config)
    return KafkaNodePrepareResponseSchema(
        node_id=body.node_id,
        cluster_id=str(file_section.get("cluster_id", "") or ""),
        controller_quorum=list(file_section.get("controller_quorum", []) or []),
        status="pending",
    )


@router.post(
    "/frontier/kafka/nodes/{node_id}/joined",
    response_model=KafkaNodesResponseSchema,
)
def frontier_kafka_node_joined(
    node_id: int, body: KafkaNodeJoinedSchema, request: Request
) -> KafkaNodesResponseSchema | JSONResponse:
    """Mark a prepared broker joined after verifying it (admin-only)."""
    denied = _frontier_admin_response(request)
    if denied is not None:
        return denied
    app_config = getattr(request.app.state, "config", None)
    nodes = _kafka_nodes(app_config)
    known = [node for node in nodes if node["node_id"] == node_id]
    if not known:
        return JSONResponse(
            status_code=404,
            content=error_body("not_found", f"kafka node {node_id} is not registered"),
        )
    for node in nodes:
        if node["node_id"] == node_id:
            node["status"] = "joined"
            if body.advertise_listener.strip():
                node["advertise_listener"] = body.advertise_listener.strip()
    if not _save_kafka_nodes(app_config, nodes):
        return JSONResponse(
            status_code=503,
            content=error_body("unavailable", "could not persist the node registry"),
        )
    return KafkaNodesResponseSchema(nodes=nodes, node_count=len(nodes))  # type: ignore[arg-type]


@router.post(
    "/frontier/kafka/rebalance-plan",
    response_model=RebalancePlanResponseSchema,
)
def frontier_kafka_rebalance_plan(
    body: RebalancePlanRequestSchema, request: Request
) -> RebalancePlanResponseSchema | JSONResponse:
    """Build an even-spread replica plan for new brokers (admin-only).

    Reads live topic layout (partition count, replication factor unless
    overridden) from the broker, so the driver and a reachable broker
    are required — otherwise 503. The returned JSON feeds
    ``kafka-reassign-partitions.sh``; nothing moves until the operator
    runs it, so existing brokers are never disturbed by planning.
    """
    denied = _frontier_admin_response(request)
    if denied is not None:
        return denied
    app_config = getattr(request.app.state, "config", None)
    resolved = _cluster_map_payload(app_config)
    try:
        from openlens.indexing.kafka_queue import (
            describe_topic_layout,
            plan_reassignment,
        )
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content=error_body("unavailable", f"kafka driver unavailable: {exc}"),
        )
    try:
        layout = describe_topic_layout(
            list(resolved.bootstrap_servers), body.topic, timeout_s=10.0
        )
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content=error_body("unavailable", f"cannot read topic layout: {exc}"),
        )
    replication = body.replication_factor or int(layout["replication_factor"])
    brokers = sorted(set(int(bid) for bid in body.broker_ids))
    try:
        assignment = plan_reassignment(
            body.topic, int(layout["partitions"]), replication, brokers
        )
    except Exception as exc:
        return JSONResponse(
            status_code=422,
            content=error_body("invalid_request", f"cannot plan reassignment: {exc}"),
        )
    slug = "".join(
        ch if ch.isalnum() or ch in ("-", "_", ".") else "-" for ch in body.topic
    )
    seed = (
        resolved.bootstrap_servers[0] if resolved.bootstrap_servers else "<bootstrap>"
    )
    hint = (
        "kafka-reassign-partitions.sh"
        f" --bootstrap-server {seed}"
        f" --reassignment-json-file /tmp/openlens-rebalance-{slug}.json"
        " --execute"
    )
    return RebalancePlanResponseSchema(
        topic=body.topic,
        partitions=int(layout["partitions"]),
        replication_factor=replication,
        broker_ids=brokers,
        assignment=assignment,  # type: ignore[arg-type]
        tool_hint=hint,
    )

    # -- Admin config docs (DB-persisted, file fallback) --------------------


def _admin_config_store(request: Request) -> AppConfigStore:
    """Resolve the config-doc store for this app (lazy, cached, offline-safe).

    ``create_app`` attaches the configured store; apps built without one
    (older tests, embeds) get an in-memory SQLite store so the endpoints
    stay exercisable without a database.
    """
    store = getattr(request.app.state, "app_config_store", None)
    if isinstance(store, AppConfigStore):
        return store
    app_config = getattr(request.app.state, "config", None)
    store = open_app_config_store(app_config if isinstance(app_config, dict) else {})
    request.app.state.app_config_store = store
    return store


@router.get("/admin/config/{doc}", response_model=AdminConfigDocResponseSchema)
def admin_config_get(
    doc: str, request: Request
) -> AdminConfigDocResponseSchema | JSONResponse:
    """Return one effective config doc plus its winning layer.

    Reads stay open (same rule as the frontier reads): a persisted row
    wins with ``source: db``; otherwise the YAML-backed file section is
    returned with ``source: file``. Unknown doc keys fail with 404.
    Live secret values (``api_key`` and friends) are always replaced by
    the ``REDACTED_SECRET`` sentinel before leaving the backend.
    """
    if doc not in CONFIG_DOC_KEYS:
        return JSONResponse(
            status_code=404,
            content=error_body(
                "not_found",
                f"unknown config doc '{doc}'. Known: {', '.join(CONFIG_DOC_KEYS)}.",
            ),
        )
    stored = try_get_doc(_admin_config_store(request), doc)
    if stored is not None:
        raw = dict(stored.body) if isinstance(stored.body, dict) else list(stored.body)
        return AdminConfigDocResponseSchema(
            doc=doc,
            body=redact_secrets(raw),
            version=stored.version,
            updated_at=stored.updated_at or None,
            source="db",
        )
    app_config = getattr(request.app.state, "config", None)
    return AdminConfigDocResponseSchema(
        doc=doc,
        body=redact_secrets(
            extract_file_doc(app_config if isinstance(app_config, dict) else {}, doc)
        ),
        version=0,
        updated_at=None,
        source="file",
    )


@router.put("/admin/config/{doc}", response_model=AdminConfigDocResponseSchema)
def admin_config_put(
    doc: str, put: AdminConfigPutSchema, request: Request
) -> AdminConfigDocResponseSchema | JSONResponse:
    """Persist one config doc (strict body, validated like boot).

    Admin-only — reuses the exact frontier gate
    (`_frontier_admin_response`), no second auth
    scheme. The body is validated with the existing startup validators;
    anything the boot path would reject fails with 422. The
    ``REDACTED_SECRET`` sentinel is accepted for secret fields and means
    "keep the existing value"; a sentinel with no prior value fails with
    422 instead of storing the sentinel as a credential. The write
    replaces the row wholesale (version++); the running app's in-memory
    config is overlaid so reads stay consistent, and the next boot picks
    the row up over the YAML seed. Responses carry the redacted body.
    """
    if doc not in CONFIG_DOC_KEYS:
        return JSONResponse(
            status_code=404,
            content=error_body(
                "not_found",
                f"unknown config doc '{doc}'. Known: {', '.join(CONFIG_DOC_KEYS)}.",
            ),
        )
    denied = _frontier_admin_response(request)
    if denied is not None:
        return denied
    store = _admin_config_store(request)
    app_config = getattr(request.app.state, "config", None)
    prior = try_get_doc(store, doc)
    if prior is not None:
        existing = (
            dict(prior.body) if isinstance(prior.body, dict) else list(prior.body)
        )
    else:
        existing = extract_file_doc(
            app_config if isinstance(app_config, dict) else {}, doc
        )
    effective = restore_secrets(
        dict(put.body) if isinstance(put.body, dict) else list(put.body),
        existing,
    )
    unfilled = missing_secrets(effective)
    if unfilled:
        return JSONResponse(
            status_code=422,
            content=error_body(
                "invalid_request",
                f"secret value required for {', '.join(unfilled)}: "
                "no prior value to keep.",
            ),
        )
    try:
        validate_config_doc(doc, effective)
    except ConfigError as exc:
        return JSONResponse(
            status_code=422,
            content=error_body("invalid_request", str(exc)),
        )
    try:
        stored = store.put(doc, effective)
    except ProviderError as exc:
        return JSONResponse(
            status_code=503,
            content=error_body("config_store_unavailable", str(exc)),
        )
    if isinstance(app_config, dict):
        request.app.state.config = apply_db_config_overlay(
            {
                **app_config,
                doc: (
                    dict(effective) if isinstance(effective, dict) else list(effective)
                ),
            },
            store,
        )
    return AdminConfigDocResponseSchema(
        doc=doc,
        body=redact_secrets(
            dict(stored.body) if isinstance(stored.body, dict) else list(stored.body)
        ),
        version=stored.version,
        updated_at=stored.updated_at or None,
        source="db",
    )
