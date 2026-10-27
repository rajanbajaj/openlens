# OpenLens — Custom-Engine Integration Plan

## Goal

Implement the backend integration that `docs/openlens/custom-search-providers.md`
promises: any third-party search engine exposing an HTTP API plugs into
OpenLens via YAML only — no new Python adapter class per engine. Today only
hardcoded adapters exist (`BingSearchProvider` in
`src/openlens/providers/external.py`); a custom engine author with just an
HTTP endpoint cannot integrate without shipping OpenLens code.

## Approach

Add one generic provider type, `http` (`CustomApiSearchProvider` in a new
`src/openlens/providers/http.py`), extending the existing
`ExternalSearchProvider` base so transport, timeouts, and error mapping are
reused untouched. All engine-specific knowledge moves into config: endpoint,
auth placement, query-param names, and dotted-path extraction of results and
fields. Anything the mapping cannot express (OAuth flows, POST bodies beyond a
fixed JSON shape, per-engine pagination tokens) stays out — those still get a
handwritten adapter subclass, which remains the escape hatch.

## Config shape

```yaml
providers:
  myengine:
    enabled: false
    type: http
    timeout_s: 5
    endpoint: ${MYENGINE_URL}
    method: GET                      # GET (query params) or POST (JSON body)
    api_key: ${MYENGINE_API_KEY:-}   # required when enabled, like bing
    auth_in: header                   # header or query
    auth_name: X-Api-Key              # header/query-param name, e.g. bing uses Ocp-Apim-Subscription-Key
    params: { query: q, limit: count, offset: offset }   # param names
    body: { query: q, limit: count, offset: offset }     # POST JSON shape
    results_path: results            # dotted path to the hit list
    fields: { id: id, title: title, snippet: text, url: link }
    score: rank_decay                # or { field: score } for a numeric field
```

Every key validated in `configure` with `ConfigError` on bad shape
(mirroring `external.py` conventions); missing `api_key`-equivalent fails
startup only when the entry is enabled.

## Steps

1. **Mapping core.** Dotted-path extractor (`a.b.0.c` over dicts/lists,
   missing → skip hit, never crash) plus field mapper producing normalized
   `SearchResult`s: `original_score == final_score`, `provider` set to the
   entry name, `metadata` carrying the engine name and request mode.
   Rank-decay (`1/(rank+1)`) when no score field is configured.
2. **Provider.** `CustomApiSearchProvider(ExternalSearchProvider)`:
   `build_params`/`parse_payload` driven by the config mapping; GET sends
   params, POST sends the fixed JSON body. Auth is API-key style like bing:
   top-level `api_key` (env-resolved, required when enabled), injected as the
   `auth_name` header or query param per `auth_in`. Reuse `headers()`,
   `_fetch`, `health_check` semantics from the base.
3. **Registration.** Register type `http` in `register_builtin_providers`
   (lazy import, same pattern as `bing`); add a disabled `http` example entry
   (commented) to `configs/providers.yaml`.
4. **Tests.** Unit tests with stubbed `_fetch` (offline-safe, no network):
   GET/POST wiring, auth placement, dotted-path extraction incl. missing
   fields, rank-decay vs score-field, bad-config rejection, timeout/error
   mapping inherited from the base. Provider-conformance suite must pass for
   the new type; add a config-toggle test (enable/disable via YAML only) and
   confirm the secret-leak suite covers the new auth value.

## Validation Plan

- `uv run pytest tests/unit/test_http_provider.py tests/unit/test_provider_contract.py` green.
- Manual: point `endpoint` at a stub HTTP server returning a foreign payload
  shape, search through the API, confirm normalized results with attribution.
- Contract proof: no changes to `SearchProvider`, the manager, or the
  pipeline — the dev doc's conformance checklist holds for the new type.

## Non-goals / Risks

- No OAuth/token-refresh flows, no pagination-token following, no per-engine
  response transforms beyond dotted paths. Engines needing those still write
  an adapter subclass (documented escape hatch, unchanged).
- Overly clever mapping DSL is the main risk; dotted paths plus rank-decay
  are the ceiling for v1. If two real engines cannot fit, widen deliberately,
  not preemptively.
- Open questions: none.
