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

Pure small-talk ("hi", "thanks", "bye") is answered directly in
:func:`run_agent_chat` with no search and no model call. Fail-closed
everywhere else: unparsable JSON searches the raw query, and a
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
import re
from dataclasses import replace
from typing import Any

from openlens.core.interfaces import SearchRequest, SearchResponse
from openlens.ranking.profiles import get_profile

#: Whole-message conversational openers with no search intent ("hi", "thanks",
#: "bye"). Only a full-message match bypasses the pipeline, so a real query
#: that merely starts with a greeting still searches.
_SMALL_TALK_RE = re.compile(
    r"^(?:hi|hello|hey|howdy|good\s?morning|good\s?afternoon|good\s?evening"
    r"|how are you|thanks|thank\s?you|thx|bye(?:-bye)?|goodbye|good\s?night"
    r"|see you)(?:[!.,?\s]*)$",
    re.IGNORECASE,
)

#: Direct replies for the small-talk bypass (no search, no model call).
_SMALL_TALK_GREETING = (
    "Hello! Ask me a question and I'll search the index and answer with sources."
)
_SMALL_TALK_THANKS = (
    "You're welcome! Ask me anything else and I'll search the index for answers."
)
_SMALL_TALK_FAREWELL = (
    "Goodbye! Come back any time with a question and I'll search the index."
)


def _small_talk_reply(raw: str) -> str | None:
    """Return a direct reply for pure small-talk, else ``None`` (search it)."""
    if not _SMALL_TALK_RE.match(raw.strip()):
        return None
    lowered = raw.lower()
    if "thank" in lowered or "thx" in lowered:
        return _SMALL_TALK_THANKS
    if (
        "bye" in lowered
        or "goodbye" in lowered
        or "see you" in lowered
        or "good night" in lowered
        or "goodnight" in lowered
    ):
        return _SMALL_TALK_FAREWELL
    return _SMALL_TALK_GREETING


def _error_detail(exc: BaseException) -> str:
    """One-line error identity: class name plus truncated message, if any."""
    detail = str(exc).strip().replace("\n", " ")[:200]
    name = type(exc).__name__
    return f"{name}: {detail}" if detail else name


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
        fallbacks.append(f"model_error: {_error_detail(exc)}")
        notice = (
            f"{AGENT_STATUS_PREFIX} model unavailable "
            f"({_error_detail(exc)}); serving standard results"
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
        fallbacks.append(f"summary_fallback: {_error_detail(exc)}")
        summary = _extractive_summary(response.results)
    steps.append({"kind": "summary", "detail": summary})

    response.mode = AGENT_MODE
    response.diagnostics["agent_trace"] = trace
    return response


#: Bounds for the chat turn loop (``POST /api/v1/agent/chat``).
MAX_CHAT_HISTORY_TURNS = 10
MAX_CHAT_SNIPPET_CHARS = 500
MAX_CHAT_ANSWER_CHARS = 2000
MAX_CHAT_LIMIT = 10

#: Honest fallback when the retrieved hits cannot answer the turn.
UNKNOWN_ANSWER = "Unknown: I don't know the answer based on the provided sources."


def _normalize_chat_turns(history: Any) -> list[tuple[str, str]]:
    """Coerce caller turns to ``(role, content)`` pairs, bounded and clean."""
    turns: list[tuple[str, str]] = []
    if history is None:
        return turns
    try:
        items = list(history)
    except TypeError:
        return turns
    for turn in items:
        if isinstance(turn, (tuple, list)) and len(turn) == 2:
            role, content = turn
        elif isinstance(turn, dict):
            role, content = turn.get("role", ""), turn.get("content", "")
        else:
            role, content = getattr(turn, "role", ""), getattr(turn, "content", "")
        if role not in ("user", "assistant"):
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        turns.append((role, content.strip()[:MAX_PLAN_QUERY_CHARS]))
    return turns[-MAX_CHAT_HISTORY_TURNS:]


def build_chat_rewrite_prompt(message: str, history: Any) -> str:
    """Constrained rewrite prompt: transcript plus strict-JSON instruction."""
    turns = _normalize_chat_turns(history)
    lines = ["Rewrite the last user message as a standalone document search query."]
    lines.append(
        "Reply with ONLY a JSON object (no markdown, no extra text) with "
        'exactly these keys: "query" (string, the standalone rewritten '
        'query), "filters" (object, search filters), "mode" (string, one of '
        '"default", "news", "shopping", "travel", "child-safe").'
    )
    if turns:
        lines.append("Conversation so far (oldest first):")
        for role, content in turns:
            lines.append(f"{role}: {content}")
    lines.append("Last user message:")
    lines.append(message)
    return "\n".join(lines)


def build_chat_answer_prompt(message: str, history: Any, hits: list[Any]) -> str:
    """Grounded answer prompt: cite provided sources, else admit unknown."""
    turns = _normalize_chat_turns(history)
    lines = [
        "Answer the last user message using ONLY the sources below.",
        "Cite every factual claim as [1], [2], and so on, matching the "
        "source numbers. When the sources are topically relevant but do not "
        "fully answer the question, summarize what they do say (with "
        "citations) and briefly note what is missing instead of refusing. "
        "Only when none of the sources is relevant at all, say plainly that "
        "they do not contain the answer instead of guessing.",
    ]
    if turns:
        lines.append("Conversation so far (oldest first):")
        for role, content in turns:
            lines.append(f"{role}: {content}")
    lines.append("Last user message:")
    lines.append(message)
    lines.append("Sources:")
    for index, hit in enumerate(hits, start=1):
        title = getattr(hit, "title", "")
        snippet = getattr(hit, "snippet", "")
        if not isinstance(title, str):
            title = ""
        if not isinstance(snippet, str):
            snippet = ""
        lines.append(f"[{index}] {title}: {snippet[:MAX_CHAT_SNIPPET_CHARS]}")
    return "\n".join(lines)


def _chat_citations(hits: list[Any]) -> list[dict[str, Any]]:
    """Map hits to 1-based citations server-side (never model-written)."""
    citations: list[dict[str, Any]] = []
    for index, hit in enumerate(hits, start=1):
        title = getattr(hit, "title", "")
        url = getattr(hit, "url", None)
        citations.append(
            {
                "ref": index,
                "title": title if isinstance(title, str) else "",
                "url": url if isinstance(url, str) else None,
            }
        )
    return citations


def _empty_chat_trace(raw_query: str) -> dict[str, Any]:
    return {
        "rewritten_query": raw_query,
        "filters": {},
        "steps": [],
        "fallbacks": [],
        "model_calls": 0,
    }


def run_agent_chat(
    message: str,
    history: Any,
    limit: int,
    engine: Any,
) -> dict[str, Any]:
    """Run one chat turn: rewrite -> tool search -> grounded answer.

    The model only ever influences ``query``/``filters``/``mode`` (via
    :func:`_coerce_plan`) and the answer wording: ``limit`` comes from
    the caller and the only tool is ``engine.search`` in-process.
    Retrieval errors propagate exactly as for a plain search; only
    model-side failures degrade (``notice`` set, ``answer`` empty,
    plain-search hits, never raising).
    """
    raw = message.strip() if isinstance(message, str) else ""
    try:
        tight_limit = int(limit)
    except (TypeError, ValueError):
        tight_limit = MAX_CHAT_LIMIT
    tight_limit = max(1, min(tight_limit, MAX_CHAT_LIMIT))

    trace: dict[str, Any] = _empty_chat_trace(raw)
    steps = trace["steps"]
    fallbacks = trace["fallbacks"]

    def _plain_hits() -> list[Any]:
        response = engine.search(
            SearchRequest(
                query=raw, mode=DEFAULT_AGENT_MODE, limit=tight_limit, filters={}
            )
        )
        return list(response.results[:tight_limit])

    def _done(answer: str, hits: list[Any], notice: str | None) -> dict[str, Any]:
        return {
            "answer": answer,
            "citations": _chat_citations(hits) if hits else [],
            "results": hits,
            "trace": trace,
            "notice": notice,
        }

    # -- step 0: pure small-talk needs no search and no model call ----------
    small_talk = _small_talk_reply(raw)
    if small_talk is not None:
        steps.append({"kind": "smalltalk", "detail": raw})
        return _done(small_talk, [], None)

    model_manager = getattr(engine, "model_manager", None)
    if model_manager is None:
        fallbacks.append("model_unavailable: no enabled model backend")
        notice = (
            f"{AGENT_STATUS_PREFIX} no model backend enabled; serving standard results"
        )
        return _done("", _plain_hits(), notice)

    # -- step 1: one constrained rewrite using the transcript -------------
    try:
        raw_plan = model_manager.complete(build_chat_rewrite_prompt(raw, history))
        trace["model_calls"] += 1
    except Exception as exc:  # noqa: BLE001 — fail closed, never 500
        fallbacks.append(f"model_error: {_error_detail(exc)}")
        notice = (
            f"{AGENT_STATUS_PREFIX} model unavailable "
            f"({_error_detail(exc)}); serving standard results"
        )
        return _done("", _plain_hits(), notice)

    try:
        rewritten, plan_filters, plan_mode = _coerce_plan(
            _extract_json_object(raw_plan), raw
        )
        parsed_ok = True
    except (ValueError, json.JSONDecodeError) as exc:
        rewritten, plan_filters, plan_mode = raw, {}, DEFAULT_AGENT_MODE
        fallbacks.append(f"parse_fallback: {type(exc).__name__}")
        parsed_ok = False

    trace["rewritten_query"] = rewritten
    trace["filters"] = dict(plan_filters)
    steps.append({"kind": "rewrite", "detail": rewritten})
    if not parsed_ok:
        steps.append({"kind": "parse_fallback", "detail": "searched raw message"})

    # -- step 2: the single tool — engine.search with validated params ----
    merged_filters = dict(plan_filters)
    response = engine.search(
        SearchRequest(
            query=rewritten,
            mode=plan_mode,
            limit=tight_limit,
            filters=merged_filters,
        )
    )
    steps.append(
        {
            "kind": "search",
            "detail": f"mode={plan_mode} filters={sorted(merged_filters)}",
        }
    )

    # -- step 2b: one broadened retry when the rewrite found nothing -----
    if not response.results:
        fallbacks.append("broadened_retry: no hits for rewritten query")
        steps.append({"kind": "retry", "detail": raw})
        response = engine.search(
            SearchRequest(
                query=raw, mode=DEFAULT_AGENT_MODE, limit=tight_limit, filters={}
            )
        )

    hits = list(response.results[:tight_limit])
    if not hits:
        return _done(UNKNOWN_ANSWER, [], None)

    # -- step 3: grounded synthesis (second and last model call) ---------
    try:
        answer_prompt = build_chat_answer_prompt(raw, history, hits)
        answer = model_manager.complete(answer_prompt)
        trace["model_calls"] += 1
        answer = answer.strip()[:MAX_CHAT_ANSWER_CHARS]
        if not answer:
            answer = _extractive_summary(hits) or UNKNOWN_ANSWER
    except Exception as exc:  # noqa: BLE001 — synthesis never fails search
        fallbacks.append(f"summary_fallback: {_error_detail(exc)}")
        answer = _extractive_summary(hits) or UNKNOWN_ANSWER
    steps.append({"kind": "synthesize", "detail": answer[:MAX_SUMMARY_CHARS]})
    return _done(answer, hits, None)
