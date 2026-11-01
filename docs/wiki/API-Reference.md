# API Reference (developers)

Source of truth: the checked-in snapshots
`frontend/openapi/openapi.json` and
`admin-frontend/openapi/openapi.json`, regenerated offline via
`create_app().openapi()` (see `frontend/openapi/README.md`).
Typed clients live in each app's `src/api/generated/`.

## Search

`POST /api/v1/search` — `{query, mode, limit, offset, filters}`.
Response: `{query, mode, results[], total, limit, offset,
next_offset, suggestions, spelling, related, applied_filters,
diagnostics, perspectives[]}`. Each result carries id, title,
snippet, scores, url, provider, reasons, warnings, and metadata.
`diagnostics` holds `fallback_used`, per-provider latencies/counts,
plugin executions, errors, query provenance, `request_id`,
and `agent_trace` (agent mode).

## Agent chat

`POST /api/v1/agent/chat` — `{message, history[{role, content}],
limit}`. Response: `{answer, citations[{ref, title, url}], results[],
trace{rewritten_query, filters, steps, fallbacks, model_calls,
model}, notice, perspectives[]}`. Fail-closed: empty answer plus
`notice` and standard hits on model failure — never a 500.

## Feedback

`POST /api/v1/feedback` — `{doc_id, action, reason?}`. Anonymous
`(doc id, action, reason)` counts; unknown actions rejected (422),
reasons limited to the closed taxonomy.

## Frontier & admin (token-gated mutations)

Status/queue/workers/live socket, pause/resume/clear/settings,
cluster map, kafka nodes; `GET/PUT /api/v1/admin/config/*` for
models/providers/ranking/query/plugins docs with redacted secrets.
Reads stay open in local dev; mutations require `X-Admin-Token`
(the live socket also accepts `Sec-WebSocket-Protocol`).
