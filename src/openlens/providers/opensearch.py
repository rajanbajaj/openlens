"""OpenSearch search-provider adapter.

Ships **disabled by default** (see ``configs/providers.yaml``): no cluster,
network, or credentials are needed unless the operator enables it. Every
code path is offline-safe — without a reachable cluster, :meth:`search`
raises :class:`ProviderError` (never a raw client exception) and
:meth:`health_check` returns ``False`` instead of raising.
"""

from __future__ import annotations

from typing import Any

from openlens.core.interfaces import (
    ConfigError,
    ProviderError,
    SearchRequest,
    SearchResult,
)
from openlens.providers.base import BaseSearchProvider

_DEFAULT_INDEX = "openlens"


class OpenSearchProvider(BaseSearchProvider):
    """Search backend powered by an OpenSearch cluster."""

    @property
    def name(self) -> str:
        return "opensearch"

    def __init__(self) -> None:
        super().__init__()
        self.hosts: list[str] = []
        self.index: str = _DEFAULT_INDEX
        self.username: str | None = None
        self.password: str | None = None
        self.verify_certs: bool = True

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)
        raw_hosts = config.get("hosts", [])
        if isinstance(raw_hosts, str):
            raw_hosts = [raw_hosts]
        if (
            not isinstance(raw_hosts, list)
            or not raw_hosts
            or not all(isinstance(h, str) and h for h in raw_hosts)
        ):
            raise ConfigError(
                "Provider 'opensearch': 'hosts' must be a non-empty list of "
                f"host URLs, got {raw_hosts!r}. Startup validation failed."
            )
        index = config.get("index", _DEFAULT_INDEX)
        if not isinstance(index, str) or not index:
            raise ConfigError(
                "Provider 'opensearch': 'index' must be a non-empty string, "
                f"got {index!r}. Startup validation failed."
            )
        self.hosts = list(raw_hosts)
        self.index = index
        self.username = config.get("username")
        self.password = config.get("password")
        self.verify_certs = bool(config.get("verify_certs", True))

    def _build_client(self) -> Any:
        """Build an OpenSearch client. Raises ProviderError when unavailable."""
        try:
            from opensearchpy import OpenSearch
        except ImportError as exc:
            raise ProviderError(
                "opensearch-py is not installed",
                provider_name=self.name,
                retryable=False,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        auth = None
        if self.username or self.password:
            auth = (self.username, self.password)
        try:
            return OpenSearch(
                hosts=self.hosts,
                http_auth=auth,
                verify_certs=self.verify_certs,
                request_timeout=self.timeout_s,
            )
        except Exception as exc:  # never leak raw client errors
            raise ProviderError(
                f"cannot build OpenSearch client: {exc}",
                provider_name=self.name,
                retryable=False,
                code=ProviderError.UNAVAILABLE,
            ) from exc

    def _map_exception(self, exc: Exception) -> ProviderError:
        text = f"{type(exc).__name__}: {exc}".lower()
        if "timeout" in text or "timed out" in text:
            return ProviderError.timeout(self.name, f"opensearch timed out: {exc}")
        if "unauthorized" in text or "forbidden" in text or " 401" in text or " 403" in text:
            return ProviderError.auth(self.name, f"opensearch auth failed: {exc}")
        if "429" in text or "rate" in text and "limit" in text:
            return ProviderError.rate_limit(self.name, f"opensearch rate limited: {exc}")
        if isinstance(exc, ProviderError):
            return exc
        return ProviderError(
            f"opensearch search failed: {exc}",
            provider_name=self.name,
            retryable=True,
            code=ProviderError.UNAVAILABLE,
        )

    def search(self, request: SearchRequest) -> list[SearchResult]:
        try:
            client = self._build_client()
        except ProviderError:
            raise
        except Exception as exc:  # defensive: _build_client already wraps
            raise self._map_exception(exc) from exc

        body = {
            "query": {"multi_match": {"query": request.query}},
            "size": max(0, request.limit),
            "from": max(0, request.offset),
        }
        try:
            response = client.search(index=self.index, body=body)
        except Exception as exc:  # opensearch-py raises its own hierarchy
            raise self._map_exception(exc) from exc
        return self._normalize(response, request)

    def _normalize(self, response: Any, request: SearchRequest) -> list[SearchResult]:
        hits = response.get("hits", {}).get("hits", []) if isinstance(response, dict) else []
        results: list[SearchResult] = []
        for hit in hits:
            source = hit.get("_source", {}) if isinstance(hit, dict) else {}
            score = hit.get("_score", 0.0) if isinstance(hit, dict) else 0.0
            try:
                score_f = float(score if score is not None else 0.0)
            except (TypeError, ValueError):
                score_f = 0.0
            doc_id = str(hit.get("_id", source.get("id", ""))) if isinstance(hit, dict) else ""
            title = str(source.get("title", doc_id or "untitled"))
            content = str(source.get("content", source.get("text", "")))
            results.append(
                SearchResult(
                    id=doc_id,
                    title=title,
                    snippet=content[:500],
                    original_score=score_f,
                    final_score=score_f,
                    url=source.get("url"),
                    provider=self.name,
                    metadata={"index": self.index, "mode": request.mode},
                )
            )
        return results

    def health_check(self) -> bool:
        """Return True only when the cluster answers a ping. Never raises."""
        try:
            client = self._build_client()
            return bool(client.ping(request_timeout=min(self.timeout_s, 2.0)))
        except Exception:
            return False
