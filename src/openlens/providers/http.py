"""Generic HTTP-API search provider (integration plan step 2).

:class:`CustomApiSearchProvider` extends :class:`ExternalSearchProvider` so
transport, timeouts, and :class:`ProviderError` mapping are reused untouched.
All engine-specific knowledge lives in config: endpoint, HTTP method, auth
placement, query-param/body names, and dotted-path extraction of results and
fields (via :mod:`openlens.providers.http_mapping`).

Anything the mapping cannot express (OAuth flows, per-engine pagination
tokens, non-fixed POST bodies) stays out — those engines still get a
handwritten adapter subclass. Ships **disabled by default** and is fully
offline-safe.
"""

from __future__ import annotations

from typing import Any

from openlens.core.interfaces import (
    ConfigError,
    ProviderError,
    SearchRequest,
    SearchResult,
)
from openlens.providers.external import ExternalSearchProvider
from openlens.providers.http_mapping import ScoreConfig, map_payload_to_results

#: Request attributes that may be renamed onto upstream param/body names.
_REQUEST_FIELDS = ("query", "limit", "offset")

#: Field-map keys accepted in ``fields``; ``url`` is optional.
_REQUIRED_FIELD_KEYS = ("id", "title", "snippet")

_DEFAULT_PARAMS: dict[str, str] = {"query": "q", "limit": "count", "offset": "offset"}


class CustomApiSearchProvider(ExternalSearchProvider):
    """Config-driven search provider for any API-key HTTP search API."""

    @property
    def name(self) -> str:
        return "http"

    def __init__(self) -> None:
        super().__init__()
        self.method: str = "GET"
        self.auth_in: str = "header"
        self.auth_name: str = "X-Api-Key"
        self.params: dict[str, str] = dict(_DEFAULT_PARAMS)
        self.body: dict[str, str] = dict(_DEFAULT_PARAMS)
        self.results_path: str = "results"
        self.fields: dict[str, str] = {"id": "id", "title": "title", "snippet": "snippet"}
        self.score: ScoreConfig = "rank_decay"

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)
        self.method = self._validate_method(config.get("method", "GET"))
        self.auth_in = self._validate_auth_in(config.get("auth_in", "header"))
        self.auth_name = self._validate_auth_name(config.get("auth_name", "X-Api-Key"))
        self.params = self._validate_name_map(config.get("params", None), "params")
        self.body = self._validate_name_map(config.get("body", None), "body")
        self.results_path = self._validate_results_path(config.get("results_path", "results"))
        self.fields = self._validate_fields(config.get("fields", None))
        self.score = self._validate_score(config.get("score", "rank_decay"))

    def _validate_method(self, value: Any) -> str:
        if not isinstance(value, str) or value.upper() not in ("GET", "POST"):
            raise ConfigError(
                f"Provider '{self.name}': 'method' must be 'GET' or 'POST', "
                f"got {value!r}. Startup validation failed."
            )
        return value.upper()

    def _validate_auth_in(self, value: Any) -> str:
        if not isinstance(value, str) or value.lower() not in ("header", "query"):
            raise ConfigError(
                f"Provider '{self.name}': 'auth_in' must be 'header' or 'query', "
                f"got {value!r}. Startup validation failed."
            )
        return value.lower()

    def _validate_auth_name(self, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"Provider '{self.name}': 'auth_name' must be a non-empty string, "
                f"got {value!r}. Startup validation failed."
            )
        return value.strip()

    def _validate_name_map(self, value: Any, key: str) -> dict[str, str]:
        if value is None:
            return dict(_DEFAULT_PARAMS)
        if not isinstance(value, dict) or not value:
            raise ConfigError(
                f"Provider '{self.name}': '{key}' must be a non-empty mapping "
                f"of request field to upstream name, got {value!r}. "
                "Startup validation failed."
            )
        for map_key, map_value in value.items():
            if map_key not in _REQUEST_FIELDS:
                raise ConfigError(
                    f"Provider '{self.name}': '{key}' has unknown request field "
                    f"{map_key!r} (expected one of {list(_REQUEST_FIELDS)}). "
                    "Startup validation failed."
                )
            if not isinstance(map_value, str) or not map_value.strip():
                raise ConfigError(
                    f"Provider '{self.name}': '{key}[{map_key}]' must be a "
                    f"non-empty string, got {map_value!r}. "
                    "Startup validation failed."
                )
        return {k: v.strip() for k, v in value.items()}

    def _validate_results_path(self, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"Provider '{self.name}': 'results_path' must be a non-empty "
                f"dotted-path string, got {value!r}. Startup validation failed."
            )
        return value.strip()

    def _validate_fields(self, value: Any) -> dict[str, str]:
        if value is None:
            return {"id": "id", "title": "title", "snippet": "snippet"}
        if not isinstance(value, dict):
            raise ConfigError(
                f"Provider '{self.name}': 'fields' must be a mapping of result "
                f"field to dotted path, got {value!r}. Startup validation failed."
            )
        for required in _REQUIRED_FIELD_KEYS:
            path = value.get(required, "")
            if not isinstance(path, str) or not path.strip():
                raise ConfigError(
                    f"Provider '{self.name}': 'fields' must map {required!r} to a "
                    f"non-empty dotted-path string, got {path!r}. "
                    "Startup validation failed."
                )
        url_path = value.get("url", None)
        if url_path is not None and (not isinstance(url_path, str) or not url_path.strip()):
            raise ConfigError(
                f"Provider '{self.name}': 'fields['url']' must be a non-empty "
                f"dotted-path string, got {url_path!r}. Startup validation failed."
            )
        for map_key in value:
            if map_key not in (*_REQUIRED_FIELD_KEYS, "url"):
                raise ConfigError(
                    f"Provider '{self.name}': 'fields' has unknown key {map_key!r} "
                    "(expected 'id', 'title', 'snippet', 'url'). "
                    "Startup validation failed."
                )
        cleaned = {k: v.strip() for k, v in value.items() if isinstance(v, str)}
        return cleaned

    def _validate_score(self, value: Any) -> ScoreConfig:
        if isinstance(value, str):
            if value != "rank_decay":
                raise ConfigError(
                    f"Provider '{self.name}': 'score' string must be 'rank_decay', "
                    f"got {value!r}. Startup validation failed."
                )
            return value
        if isinstance(value, dict):
            field_path = value.get("field", "")
            if not isinstance(field_path, str) or not field_path.strip():
                raise ConfigError(
                    f"Provider '{self.name}': 'score' mapping must carry a "
                    f"non-empty 'field' dotted path, got {field_path!r}. "
                    "Startup validation failed."
                )
            if set(value) != {"field"}:
                raise ConfigError(
                    f"Provider '{self.name}': 'score' mapping supports only the "
                    f"'field' key, got {sorted(value)}. Startup validation failed."
                )
            return {"field": field_path.strip()}
        raise ConfigError(
            f"Provider '{self.name}': 'score' must be 'rank_decay' or "
            f"{{'field': '<dotted-path>'}}, got {value!r}. "
            "Startup validation failed."
        )

    def headers(self) -> dict[str, str]:
        """Upstream headers; the API key rides here when ``auth_in: header``."""
        headers = {"Accept": "application/json"}
        if self.auth_in == "header":
            headers[self.auth_name] = self.api_key
        return headers

    def _auth_query_params(self) -> dict[str, str]:
        """API-key query params; non-empty only when ``auth_in: query``."""
        if self.auth_in == "query":
            return {self.auth_name: self.api_key}
        return {}

    def _mapped_values(self, request: SearchRequest, mapping: dict[str, str]) -> dict[str, Any]:
        raw: dict[str, Any] = {
            "query": request.query,
            "limit": max(1, request.limit),
            "offset": max(0, request.offset),
        }
        return {mapping[field]: raw[field] for field in mapping}

    def build_params(self, request: SearchRequest) -> dict[str, Any]:
        """Map a SearchRequest onto upstream query params (GET)."""
        return {**self._mapped_values(request, self.params), **self._auth_query_params()}

    def build_body(self, request: SearchRequest) -> dict[str, Any]:
        """Map a SearchRequest onto the fixed upstream JSON body (POST)."""
        return self._mapped_values(request, self.body)

    def _fetch(self, request: SearchRequest) -> Any:
        try:
            import httpx
        except ImportError as exc:
            raise ProviderError(
                "httpx is not installed",
                provider_name=self.name,
                retryable=False,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        try:
            if self.method == "POST":
                response = httpx.post(
                    self.endpoint,
                    params=self._auth_query_params(),
                    json=self.build_body(request),
                    headers=self.headers(),
                    timeout=self.timeout_s,
                )
            else:
                response = httpx.get(
                    self.endpoint,
                    params=self.build_params(request),
                    headers=self.headers(),
                    timeout=self.timeout_s,
                )
            response.raise_for_status()
            return response.json()
        except ProviderError:
            raise
        except Exception as exc:
            raise self._map_transport_error(exc) from exc

    def parse_payload(self, payload: Any, request: SearchRequest) -> list[SearchResult]:
        """Map a decoded upstream response onto normalized SearchResults."""
        if not isinstance(payload, dict):
            raise ProviderError(
                f"provider '{self.name}' returned a non-object response",
                provider_name=self.name,
                retryable=True,
                code=ProviderError.UNAVAILABLE,
            )
        return map_payload_to_results(
            payload,
            results_path=self.results_path,
            fields=self.fields,
            score=self.score,
            provider_name=self.name,
            engine_name=self.name,
            request_mode=request.mode,
        )

    def health_check(self) -> bool:
        """Never raises; False when unreachable, unauthorized, or unconfigured."""
        if not self.endpoint or not self.api_key:
            return False
        try:
            import httpx

            response = httpx.get(
                self.endpoint,
                params={"q": "health", **self._auth_query_params()},
                headers=self.headers(),
                timeout=min(self.timeout_s, 2.0),
            )
            return response.status_code < 400
        except Exception:
            return False
