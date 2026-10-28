"""Generic external-search base class plus one concrete adapter (Bing).

:class:`ExternalSearchProvider` factors out everything REST search APIs
share — endpoint/auth/timeout configuration, ``httpx`` transport, and
:class:`ProviderError` mapping — so a new backend (Google, Brave, a custom
API) only overrides :meth:`build_params`, :meth:`parse_payload`, and
optionally :meth:`headers`. Both the base and :class:`BingSearchProvider`
ship **disabled by default** and are fully offline-safe.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from openlens.core.interfaces import (
    ConfigError,
    ProviderError,
    SearchRequest,
    SearchResult,
)
from openlens.providers.base import BaseSearchProvider

_BING_ENDPOINT = "https://api.bing.microsoft.com/v7.0/search"


class ExternalSearchProvider(BaseSearchProvider, ABC):
    """Base class for HTTP-based external search APIs."""

    @property
    def name(self) -> str:
        return "external"

    def __init__(self) -> None:
        super().__init__()
        self.endpoint: str = ""
        self.api_key: str = ""

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)
        endpoint = config.get("endpoint", "")
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ConfigError(
                f"Provider '{self.name}': 'endpoint' must be a non-empty URL, "
                f"got {endpoint!r}. Startup validation failed."
            )
        api_key = config.get("api_key", "")
        if not isinstance(api_key, str) or not api_key:
            raise ConfigError(
                f"Provider '{self.name}': 'api_key' is required when the "
                "provider is enabled. Set it via '${VAR}' env resolution. "
                "Startup validation failed."
            )
        self.endpoint = endpoint.strip()
        self.api_key = api_key

    def headers(self) -> dict[str, str]:
        """HTTP headers for the upstream request (override per API)."""
        return {"Accept": "application/json"}

    @abstractmethod
    def build_params(self, request: SearchRequest) -> dict[str, Any]:
        """Map a SearchRequest onto upstream query params."""
        raise NotImplementedError

    @abstractmethod
    def parse_payload(self, payload: Any, request: SearchRequest) -> list[SearchResult]:
        """Map a decoded upstream response onto normalized SearchResults."""
        raise NotImplementedError

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

    def _map_transport_error(self, exc: Exception) -> ProviderError:
        name = type(exc).__name__.lower()
        text = f"{name}: {exc}".lower()
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if "timeout" in name or "timeout" in text:
            return ProviderError.timeout(
                self.name, f"provider '{self.name}' timed out after {self.timeout_s}s"
            )
        if status in (401, 403) or "unauthorized" in text or "forbidden" in text:
            return ProviderError.auth(
                self.name, f"provider '{self.name}' auth failed: {exc}"
            )
        if status == 429 or "rate" in text and "limit" in text:
            return ProviderError.rate_limit(
                self.name, f"provider '{self.name}' rate limited: {exc}"
            )
        return ProviderError(
            f"provider '{self.name}' request failed: {exc}",
            provider_name=self.name,
            retryable=True,
            code=ProviderError.UNAVAILABLE,
        )

    def search(self, request: SearchRequest) -> list[SearchResult]:
        payload = self._fetch(request)
        try:
            return self.parse_payload(payload, request)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"provider '{self.name}' returned an unparseable response: {exc}",
                provider_name=self.name,
                retryable=True,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def health_check(self) -> bool:
        """Never raises; False when unreachable, unauthorized, or unconfigured."""
        if not self.endpoint or not self.api_key:
            return False
        try:
            import httpx

            response = httpx.get(
                self.endpoint,
                params={"q": "health"},
                headers=self.headers(),
                timeout=min(self.timeout_s, 2.0),
            )
            return response.status_code < 400
        except Exception:
            return False


class BingSearchProvider(ExternalSearchProvider):
    """Concrete adapter for the Bing Web Search API (disabled by default)."""

    @property
    def name(self) -> str:
        return "bing"

    def __init__(self) -> None:
        super().__init__()
        self.endpoint = _BING_ENDPOINT

    def configure(self, config: dict[str, Any]) -> None:
        config = {"endpoint": _BING_ENDPOINT, **config}
        super().configure(config)

    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Ocp-Apim-Subscription-Key": self.api_key,
        }

    def build_params(self, request: SearchRequest) -> dict[str, Any]:
        return {
            "q": request.query,
            "count": max(1, request.limit),
            "offset": max(0, request.offset),
        }

    def parse_payload(self, payload: Any, request: SearchRequest) -> list[SearchResult]:
        if not isinstance(payload, dict):
            raise ProviderError(
                f"provider '{self.name}' returned a non-object response",
                provider_name=self.name,
                retryable=True,
                code=ProviderError.UNAVAILABLE,
            )
        items = (
            payload.get("webPages", {}).get("value", [])
            if isinstance(payload.get("webPages", {}), dict)
            else []
        )
        results: list[SearchResult] = []
        for rank, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url", item.get("id", f"bing-{rank}")))
            title = str(item.get("name", url))
            snippet = str(item.get("snippet", ""))
            results.append(
                SearchResult(
                    id=url,
                    title=title,
                    snippet=snippet[:500],
                    original_score=1.0 / (rank + 1),
                    final_score=1.0 / (rank + 1),
                    url=url,
                    provider=self.name,
                    metadata={"mode": request.mode},
                )
            )
        return results
