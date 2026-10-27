# Integrating a Custom Search Engine with OpenLens

This is the reference for search-engine authors whose backend — a custom
index, a managed cluster, or a third-party HTTP API — should be retrievable
through OpenLens. You implement one Python class; OpenLens handles selection,
timeouts, fallback, filtering, ranking, and API serving around it.

Start from the contracts in `src/openlens/core/interfaces.py`
(`SearchProvider`, `SearchRequest`, `SearchResult`, `ProviderError`) and the
working examples in `src/openlens/providers/` (`custom_index.py` for a local
backend, `opensearch.py` for a managed cluster, `external.py` for a
third-party HTTP API).

## The interface: four members

Subclass `BaseSearchProvider` (`src/openlens/providers/base.py`), not the ABC
directly — it validates `timeout_s` and maps raw failures for you.

| Member | Contract |
|---|---|
| `name: str` (property) | Unique provider name. Used in config keys, result `provider` fields, and diagnostics. |
| `configure(config: dict)` | Validate your section; raise `ConfigError` on anything invalid. Call `super().configure(config)` first for `timeout_s` handling. Never contact the network here beyond cheap validation. |
| `search(request: SearchRequest) -> list[SearchResult]` | Run the query; return normalized hits (below). May raise `ProviderError`; must never leak raw client exceptions — use `self.wrap_error(exc, retryable=..., code=...)`. |
| `health_check() -> bool` | Return `True` only when the backend is reachable and usable. Must never raise; the manager treats exceptions as unhealthy. Keep it cheap and offline-safe (return `False` when unconfigured). |

`SearchRequest` fields: `query: str`, `mode: str` (default `"default"`),
`limit: int` (default 10), `offset: int` (default 0),
`filters: dict` (opaque to you; honor what you document, ignore the rest).

## Normalizing results

Every hit must be a `SearchResult` with these fields populated:

| Field | Rule |
|---|---|
| `id` | Stable document key. Deduplication collapses equal canonical URLs/ids across providers, so keep it stable per document. |
| `title`, `snippet` | Human-readable; snippet capped (~500 chars). Never empty-title a hit — fall back to the id or `"untitled"`. |
| `original_score` | Your backend's raw relevance signal as a float. OpenLens never compares these across providers (fusion is rank-based). |
| `final_score` | Initialize equal to `original_score`; ranking plugins overwrite it downstream. |
| `url` | Optional but recommended; missing URLs weaken dedup and display. |
| `provider` | Set to `self.name` so attribution survives merging. |
| `reasons`, `warnings` | Leave empty unless your backend genuinely knows why a hit matched; explanation assembly fills user-facing reasons later. |
| `metadata` | Backend-specific attributes (date, price, location, index name). This is the channel for mode-specific ranking (shopping, travel, news) — put structured values here, not in the snippet. |

Honor `limit`/`offset` in your backend query when it supports paging; otherwise
slice client-side. Return `[]` for genuinely empty result sets — the manager
distinguishes empty (no fallback) from failure (fallback eligible).

## Error taxonomy

Map every backend failure to `ProviderError` with the right code:

| Backend condition | Code | `retryable` |
|---|---|---|
| Timeout / deadline exceeded | `TIMEOUT` (or `ProviderError.timeout(name, msg)`) | `True` |
| Bad or missing credentials | `AUTH` | `False` |
| Quota / 429 | `RATE_LIMIT` | `True` |
| Connection refused, 5xx, unparseable | `UNAVAILABLE` | `True` |
| caller bug (bad query construction) | `INVALID_REQUEST` | `False` |
| Anything else | `UNKNOWN` | `False` |

Timeouts are enforced twice: the manager runs your `search` bounded by
`timeout_s` (default 5s, hard ceiling 300s), and `run_bounded()` is available
for nested client calls. A hung backend surfaces as `TIMEOUT`, never a stall.

## Configuration conventions

A provider section looks like this (see `configs/providers.yaml`):

```yaml
providers:
  myengine:
    enabled: true
    type: myengine
    timeout_s: 5
    endpoint: ${MYENGINE_URL:-http://localhost:9201}
    api_key: ${MYENGINE_API_KEY:-}
```

Rules: `enabled: false` (the default) means never instantiated, never
contacted — keep backends construction-free until `configure`. Secrets resolve
from `${ENV_VAR}` at load time; never log or return them (there is a
secret-leak conformance test). Fail startup validation loudly on bad config;
fail search-time problems as `ProviderError`, not `ConfigError`.

## Registration

```python
from openlens.providers.registry import register
from my_plugin import MyEngineProvider  # your SearchProvider subclass

register("myengine", MyEngineProvider)
```

`register` rejects non-subclasses and still-abstract classes at import time.
The manager instantiates one object per *enabled* config entry, so two entries
may share your type with different configs (e.g. two indexes).

## Minimal skeleton

```python
from openlens.core.interfaces import SearchRequest, SearchResult
from openlens.providers.base import BaseSearchProvider


class MyEngineProvider(BaseSearchProvider):
    @property
    def name(self) -> str:
        return "myengine"

    def configure(self, config: dict) -> None:
        super().configure(config)
        self.endpoint = config.get("endpoint", "http://localhost:9201")

    def search(self, request: SearchRequest) -> list[SearchResult]:
        def run() -> list[SearchResult]:
            try:
                hits = self._client_query(request)  # your backend call
            except Exception as exc:
                raise self.wrap_error(exc, retryable=True,
                                      code="unavailable") from None
            return [self._normalize(h) for h in hits[: request.limit]]

        return self.run_bounded(run)

    def health_check(self) -> bool:
        try:
            return self._client_ping()
        except Exception:
            return False
```

## Conformance checklist

Before shipping, your provider must: pass the provider-conformance suite
(`tests/unit/test_provider_contract.py` — normalization shape, pagination,
timeout, failure mapping); toggle on/off purely via config; never leak raw
exceptions or secrets; report `health_check() == False` (not raise) when
unreachable; and return `[]`, not an error, for empty result sets.
