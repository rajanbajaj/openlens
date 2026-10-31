"""Agentic search mode (``mode="agent"``): model-guided rewrite + tool search.

Single-tool agent built on existing blocks only:

* step 1 — :meth:`ModelManager.complete` with a constrained prompt that
  demands strict JSON ``{"query": str, "filters": dict, "mode": str}``;
* step 2 — :meth:`SearchEngine.search` with the parsed params as the only
  tool (the same in-process method ``POST /search`` uses: no HTTP hop,
  no duplicated retrieval);
* step 3 — optional :meth:`ModelManager.complete` summary of the top hits,
  recorded as a trace step (max 2 model calls total; per-call timeouts come
  from the model backend's own ``timeout_s``).

Fail-closed everywhere: unparsable JSON searches the raw query, and a
model error (or no enabled model backend at all) serves standard results
with an ``agent_status`` notice instead of raising. The plan output is
validated, never trusted: the model can only influence ``query``,
``filters``, and ``mode`` — never ``limit``/``offset``/provider prefs —
and an unknown or ``agent`` mode coerces to ``default`` so the tool call
shape cannot be altered (including by prompt-injected queries).

Stdlib only (``json``, ``dataclasses``). This module is retrieval-scoped:
it must never import crawling, queue-management, or config-gate modules.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from openlens.core.interfaces import SearchRequest, SearchResponse
from openlens.ranking.profiles import get_profile

#: Request mode that routes through the agent instead of the plain pipeline.
AGENT_MODE = "agent"

#: Fallback mode when the model suggests an unknown mode (or ``agent``
#: itself, which would recurse).
DEFAULT_AGENT_MODE = "default"

#: Bounds keeping a hostile model reply (or injected query) small.
MAX_PLAN_QUERY_CHARS = 1000
MAX_PLAN_FILTERS = 20
MAX_SUMMARY_CHARS = 500

#: Notice prefix recorded in diagnostics errors when the agent degrades.
AGENT_STATUS_PREFIX = "agent_status:"


def build_parse_prompt(query: str) -> str:
    """Constrained rewrite prompt: strict JSON, tool-shape only."""
    return (
        "Rewrite the user search query for a document search engine.\n"
        "Reply with ONLY a JSON object (no markdown, no extra text) with "
        'exactly these keys: "query" (string, the rewritten query), '
        '"filters" (object, search filters), "mode" (string, one of '
        '"default", "news", "shopping", "travel", "child-safe").\n'
        "User query:\n"
        f"{query}"
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    """Parse the first ``{``…last ``}`` span as JSON; must be an object."""
    if not isinstance(text, str):
        raise ValueError("model reply is not a string")
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("model reply contains no JSON object")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("model reply JSON is not an object")
    return data


def _coerce_plan(
    data: dict[str, Any], raw_query: str
) -> tuple[str, dict[str, Any], str]:
    """Validate a parsed plan; untrusted fields fall back to safe values."""
    query = data.get("query")
    if not isinstance(query, str) or not query.strip():
        query = raw_query
    query = query.strip()[:MAX_PLAN_QUERY_CHARS] or raw_query

    filters = data.get("filters")
    if not isinstance(filters, dict):
        filters = {}
    else:
        filters = {
            str(key): value
            for key, value in list(filters.items())[:MAX_PLAN_FILTERS]
            if isinstance(key, str)
        }

    mode = data.get("mode")
    if not isinstance(mode, str):
        mode = DEFAULT_AGENT_MODE
    else:
        mode = mode.strip() or DEFAULT_AGENT_MODE
    if mode == AGENT_MODE:
        mode = DEFAULT_AGENT_MODE
    try:
        get_profile(mode)
    except Exception:
        mode = DEFAULT_AGENT_MODE
    return query, filters, mode


def _extractive_summary(results: list[Any]) -> str:
    """Short offline summary: truncated top-hit snippet (no model call)."""
    for result in results:
        snippet = getattr(result, "snippet", "")
        if isinstance(snippet, str) and snippet.strip():
            return snippet.strip()[:MAX_SUMMARY_CHARS]
    return ""


def _empty_trace(raw_query: str) -> dict[str, Any]:
    return {
        "rewritten_query": raw_query,
        "filters": {},
        "steps": [],
        "fallbacks": [],
        "model_calls": 0,
    }


def run_agent_search(request: SearchRequest, engine: Any) -> SearchResponse:
    """Run one agentic search; never raises for model-side failures.

    ``engine`` is duck-typed (needs ``.search`` and ``.model_manager``) so
    this module stays decoupled from ``core.engine`` (no import cycle).
    Retrieval errors from the inner tool search propagate exactly as they
    would for a plain search; only model-side failures degrade.
    """
    raw_query = request.query
    trace: dict[str, Any] = _empty_trace(raw_query)
    steps = trace["steps"]
    fallbacks = trace["fallbacks"]

    model_manager = getattr(engine, "model_manager", None)

    def _standard_search(
        query: str, mode: str, filters: dict[str, Any], notice: str | None = None
    ) -> SearchResponse:
        response: SearchResponse = engine.search(
            replace(request, query=query, mode=mode, filters=filters)
        )
        response.mode = AGENT_MODE
        if notice is not None:
            response.diagnostics.setdefault("errors", []).append(notice)
        response.diagnostics["agent_trace"] = trace
        return response

    if model_manager is None:
        notice = (
            f"{AGENT_STATUS_PREFIX} no model backend enabled; serving standard results"
        )
        fallbacks.append("model_unavailable: no enabled model backend")
        return _standard_search(
            raw_query, DEFAULT_AGENT_MODE, dict(request.filters), notice
        )

    # -- step 1: constrained rewrite -------------------------------------
    try:
        raw_plan = model_manager.complete(build_parse_prompt(raw_query))
        trace["model_calls"] += 1
    except Exception as exc:  # noqa: BLE001 — fail closed, never 500
        fallbacks.append(f"model_error: {type(exc).__name__}")
        notice = (
            f"{AGENT_STATUS_PREFIX} model unavailable "
            f"({type(exc).__name__}); serving standard results"
        )
        return _standard_search(
            raw_query, DEFAULT_AGENT_MODE, dict(request.filters), notice
        )

    try:
        rewritten, plan_filters, plan_mode = _coerce_plan(
            _extract_json_object(raw_plan), raw_query
        )
        parsed_ok = True
    except (ValueError, json.JSONDecodeError) as exc:
        rewritten, plan_filters, plan_mode = (
            raw_query,
            {},
            DEFAULT_AGENT_MODE,
        )
        fallbacks.append(f"parse_fallback: {type(exc).__name__}")
        parsed_ok = False

    trace["rewritten_query"] = rewritten
    trace["filters"] = dict(plan_filters)
    steps.append({"kind": "rewrite", "detail": rewritten})
    if not parsed_ok:
        steps.append({"kind": "parse_fallback", "detail": "searched raw query"})

    # -- step 2: the single tool — engine.search with parsed params -------
    merged_filters = {**dict(request.filters), **plan_filters}
    response: SearchResponse = engine.search(
        replace(request, query=rewritten, mode=plan_mode, filters=merged_filters)
    )
    steps.append(
        {
            "kind": "search",
            "detail": f"mode={plan_mode} filters={sorted(merged_filters)}",
        }
    )

    # -- step 3: optional summary (second and last model call) ------------
    try:
        top = [
            getattr(hit, "snippet", "")
            for hit in response.results[:3]
            if isinstance(getattr(hit, "snippet", ""), str)
        ]
        summary_prompt = (
            "Summarize these search results in one short sentence:\n"
            + "\n".join(top)[:MAX_PLAN_QUERY_CHARS]
        )
        summary = model_manager.complete(summary_prompt)
        trace["model_calls"] += 1
        summary = summary.strip()[:MAX_SUMMARY_CHARS] or _extractive_summary(
            response.results
        )
    except Exception as exc:  # noqa: BLE001 — summary never fails search
        fallbacks.append(f"summary_fallback: {type(exc).__name__}")
        summary = _extractive_summary(response.results)
    steps.append({"kind": "summary", "detail": summary})

    response.mode = AGENT_MODE
    response.diagnostics["agent_trace"] = trace
    return response
