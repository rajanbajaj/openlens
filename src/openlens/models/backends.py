"""Model backends: offline stubs for Claude/Muse, live client for local.

Claude and Muse validate credentials at startup but never touch the
network: inference raises ``ProviderError(UNAVAILABLE)`` and
``health_check`` returns False. ``LocalModelProvider`` is a real HTTP
client over ``httpx`` (lazy-imported inside methods, mirroring
``providers/http.py:_fetch``) with ``timeout_s`` enforced via the native
httpx timeout and errors wrapped via ``BaseModelProvider.wrap_error``.
Error messages never contain the endpoint, credentials, or raw client
exception text.
"""

from __future__ import annotations

from typing import Any

from openlens.core.interfaces import ConfigError, ProviderError
from openlens.models.base import BaseModelProvider

_UNAVAILABLE_MESSAGE = (
    "live model calls are not implemented in the skeleton; "
    "use the 'fake' backend for offline runs"
)


class _UnavailableModelBackend(BaseModelProvider):
    """Shared stub behavior: configured, but never performs inference."""

    def complete(self, prompt: str, **kwargs: Any) -> str:
        raise ProviderError(
            f"model backend '{self.name}': {_UNAVAILABLE_MESSAGE}",
            provider_name=self.name,
            retryable=False,
            code=ProviderError.UNAVAILABLE,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise ProviderError(
            f"model backend '{self.name}': {_UNAVAILABLE_MESSAGE}",
            provider_name=self.name,
            retryable=False,
            code=ProviderError.UNAVAILABLE,
        )

    def classify(self, text: str, labels: list[str]) -> str:
        raise ProviderError(
            f"model backend '{self.name}': {_UNAVAILABLE_MESSAGE}",
            provider_name=self.name,
            retryable=False,
            code=ProviderError.UNAVAILABLE,
        )

    def health_check(self) -> bool:
        return False


class ClaudeModelProvider(_UnavailableModelBackend):
    """Anthropic Claude backend stub (disabled by default)."""

    @property
    def name(self) -> str:
        return "claude"

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)
        api_key = self._config.get("api_key", "")
        if not isinstance(api_key, str) or not api_key:
            raise ConfigError(
                f"Model backend '{self.name}': 'api_key' is required when the "
                "backend is enabled. Set it via '${VAR}' env resolution. "
                "Startup validation failed."
            )
        self.api_key = api_key
        self.model = str(self._config.get("model", "claude-3-5-sonnet-latest"))


class MuseModelProvider(_UnavailableModelBackend):
    """Muse backend stub (disabled by default)."""

    @property
    def name(self) -> str:
        return "muse"

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)
        api_key = self._config.get("api_key", "")
        if not isinstance(api_key, str) or not api_key:
            raise ConfigError(
                f"Model backend '{self.name}': 'api_key' is required when the "
                "backend is enabled. Set it via '${VAR}' env resolution. "
                "Startup validation failed."
            )
        self.api_key = api_key
        self.model = str(self._config.get("model", "muse-3.5"))


class LocalModelProvider(BaseModelProvider):
    """Self-hosted / local model backend (real HTTP client)."""

    def __init__(self) -> None:
        super().__init__()
        self.endpoint: str = ""
        self.model: str = "default"
        self.api_key: str = ""

    @property
    def name(self) -> str:
        return "local"

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)
        endpoint = self._config.get("endpoint", "")
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ConfigError(
                f"Model backend '{self.name}': 'endpoint' must be a non-empty "
                f"URL, got {endpoint!r}. Startup validation failed."
            )
        self.endpoint = endpoint.strip()
        self.model = str(self._config.get("model", "default"))
        api_key = self._config.get("api_key", "")
        if api_key is None:
            api_key = ""
        if not isinstance(api_key, str):
            raise ConfigError(
                f"Model backend '{self.name}': 'api_key' must be a string. "
                "Startup validation failed."
            )
        self.api_key = api_key

    # -- transport -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _mapped_error(self, exc: BaseException) -> ProviderError:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if not isinstance(status, int):
            # Some clients carry the status on the exception itself.
            candidate = getattr(exc, "status_code", None)
            status = candidate if isinstance(candidate, int) else None
        name = type(exc).__name__.lower()
        text = f"{name}: {exc}".lower()
        if "connecttimeout" in name or "connecterror" in name or "connection" in name:
            return self.wrap_error(
                exc,
                message=f"model backend '{self.name}' is unavailable",
                retryable=True,
                code=ProviderError.UNAVAILABLE,
            )
        if "timeout" in name or "timeout" in text or "timed out" in text:
            return self.wrap_error(
                exc,
                message=(
                    f"model backend '{self.name}' timed out after {self.timeout_s}s"
                ),
                retryable=True,
                code=ProviderError.TIMEOUT,
            )
        if status in (401, 403):
            return self.wrap_error(
                exc,
                message=f"model backend '{self.name}' authentication failed",
                retryable=False,
                code=ProviderError.AUTH,
            )
        if status == 429:
            return self.wrap_error(
                exc,
                message=f"model backend '{self.name}' rate limit exceeded",
                retryable=True,
                code=ProviderError.RATE_LIMIT,
            )
        if status is not None:
            # 5xx and any other HTTP failure: backend unavailable.
            return self.wrap_error(
                exc,
                message=(f"model backend '{self.name}' request failed (HTTP {status})"),
                retryable=True,
                code=ProviderError.UNAVAILABLE,
            )
        if (
            "401" in text
            or "unauthorized" in text
            or "403" in text
            or "forbidden" in text
        ):
            return self.wrap_error(
                exc,
                message=f"model backend '{self.name}' authentication failed",
                retryable=False,
                code=ProviderError.AUTH,
            )
        if "429" in text or ("rate" in text and "limit" in text):
            return self.wrap_error(
                exc,
                message=f"model backend '{self.name}' rate limit exceeded",
                retryable=True,
                code=ProviderError.RATE_LIMIT,
            )
        return self.wrap_error(
            exc,
            message=f"model backend '{self.name}' is unavailable",
            retryable=True,
            code=ProviderError.UNAVAILABLE,
        )

    def _status_error(self, status: int) -> ProviderError:
        if status in (401, 403):
            code, retryable, label = ProviderError.AUTH, False, "authentication failed"
        elif status == 429:
            code, retryable, label = (
                ProviderError.RATE_LIMIT,
                True,
                "rate limit exceeded",
            )
        else:
            code, retryable, label = (
                ProviderError.UNAVAILABLE,
                True,
                f"request failed (HTTP {status})",
            )
        return self.wrap_error(
            RuntimeError(label),
            message=f"model backend '{self.name}' {label}",
            retryable=retryable,
            code=code,
        )

    def _post(self, payload: dict[str, Any]) -> Any:
        if not self.endpoint:
            raise ProviderError(
                f"model backend '{self.name}' is not configured",
                provider_name=self.name,
                retryable=True,
                code=ProviderError.UNAVAILABLE,
            )
        try:
            import httpx
        except ImportError as exc:
            raise self.wrap_error(
                exc,
                message=f"model backend '{self.name}' is unavailable",
                retryable=False,
                code=ProviderError.UNAVAILABLE,
            ) from exc
        try:
            response = httpx.post(
                self.endpoint,
                json=payload,
                headers=self._headers(),
                timeout=self.timeout_s,
            )
        except Exception as exc:
            raise self._mapped_error(exc) from exc
        status = getattr(response, "status_code", 200)
        if isinstance(status, int) and status >= 400:
            raise self._status_error(status)
        try:
            raise_for_status = getattr(response, "raise_for_status", None)
            if callable(raise_for_status):
                raise_for_status()
        except ProviderError:
            raise
        except Exception as exc:
            raise self._mapped_error(exc) from exc
        try:
            return response.json()
        except Exception:
            text = getattr(response, "text", None)
            if isinstance(text, str) and text:
                return text
            raise self.wrap_error(
                RuntimeError("unparseable response"),
                message=f"model backend '{self.name}' returned an unparseable response",
                retryable=True,
                code=ProviderError.UNAVAILABLE,
            ) from None

    # -- inference -------------------------------------------------------

    def complete(self, prompt: str, **kwargs: Any) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ProviderError(
                f"model backend '{self.name}' requires a non-empty prompt",
                provider_name=self.name,
                retryable=False,
                code=ProviderError.INVALID_REQUEST,
            )
        # Never forward credentials accidentally supplied as kwargs.
        safe_kwargs = {
            k: v
            for k, v in kwargs.items()
            if k.lower() not in ("api_key", "authorization", "endpoint")
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            **safe_kwargs,
        }
        data = self._post(payload)
        text = self._extract_text(data)
        if text is None:
            raise self.wrap_error(
                RuntimeError("missing completion text"),
                message=f"model backend '{self.name}' returned an unparseable response",
                retryable=True,
                code=ProviderError.UNAVAILABLE,
            )
        return text

    @staticmethod
    def _extract_text(data: Any) -> str | None:
        if isinstance(data, str):
            return data
        if not isinstance(data, dict):
            return None
        for key in ("text", "completion", "response", "content", "result", "output"):
            value = data.get(key)
            if isinstance(value, str):
                return value
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, str):
                return first
            if isinstance(first, dict):
                for key in ("text", "content", "response"):
                    value = first.get(key)
                    if isinstance(value, str):
                        return value
                message = first.get("message")
                if isinstance(message, dict):
                    value = message.get("content")
                    if isinstance(value, str):
                        return value
        return None

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
            raise ProviderError(
                f"model backend '{self.name}' requires a list of strings to embed",
                provider_name=self.name,
                retryable=False,
                code=ProviderError.INVALID_REQUEST,
            )
        data = self._post({"model": self.model, "texts": texts, "input": texts})
        vectors = self._extract_embeddings(data, expected=len(texts))
        if vectors is None:
            raise self.wrap_error(
                RuntimeError("missing embeddings"),
                message=f"model backend '{self.name}' returned an unparseable response",
                retryable=True,
                code=ProviderError.UNAVAILABLE,
            )
        return vectors

    @staticmethod
    def _coerce_vector(value: Any) -> list[float] | None:
        if isinstance(value, list) and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in value
        ):
            return [float(v) for v in value]
        return None

    @classmethod
    def _extract_embeddings(cls, data: Any, expected: int) -> list[list[float]] | None:
        if isinstance(data, list):
            listed: list[list[float]] = []
            for entry in data:
                coerced = cls._coerce_vector(entry)
                if coerced is None:
                    return None
                listed.append(coerced)
            return listed
        if not isinstance(data, dict):
            return None
        for key in ("embeddings", "vectors", "results"):
            value = data.get(key)
            if isinstance(value, list):
                listed2: list[list[float]] = []
                ok = True
                for entry in value:
                    coerced2 = cls._coerce_vector(entry)
                    if coerced2 is None:
                        ok = False
                        break
                    listed2.append(coerced2)
                if ok:
                    return listed2
        single = cls._coerce_vector(data.get("embedding"))
        if single is not None:
            return [single]
        items = data.get("data")
        if isinstance(items, list):
            from_data: list[list[float]] = []
            for item in items:
                if isinstance(item, dict):
                    vec = cls._coerce_vector(item.get("embedding"))
                    if vec is None:
                        return None
                    from_data.append(vec)
                else:
                    vec2 = cls._coerce_vector(item)
                    if vec2 is None:
                        return None
                    from_data.append(vec2)
            if from_data:
                return from_data
        _ = expected
        return None

    def classify(self, text: str, labels: list[str]) -> str:
        if not labels or not all(isinstance(label, str) for label in labels):
            raise ProviderError(
                f"model backend '{self.name}' requires at least one label",
                provider_name=self.name,
                retryable=False,
                code=ProviderError.INVALID_REQUEST,
            )
        data = self._post(
            {"model": self.model, "text": text, "labels": labels, "input": text}
        )
        label = self._extract_label(data, labels)
        if label is None:
            raise self.wrap_error(
                RuntimeError("missing label"),
                message=f"model backend '{self.name}' returned an unparseable response",
                retryable=True,
                code=ProviderError.UNAVAILABLE,
            )
        return label

    @staticmethod
    def _extract_label(data: Any, labels: list[str]) -> str | None:
        if isinstance(data, str):
            return data if data in labels else _match_label(data, labels)
        if not isinstance(data, dict):
            return None
        for key in (
            "label",
            "classification",
            "result",
            "category",
            "predicted_label",
            "prediction",
        ):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        text = LocalModelProvider._extract_text(data)
        if text:
            matched = _match_label(text, labels)
            if matched is not None:
                return matched
            # A bare completion may itself be the label.
            return text.strip() or None
        return None

    def health_check(self) -> bool:
        """Never raises; False when unreachable, unauthorized, or unconfigured."""
        if not self.endpoint:
            return False
        try:
            import httpx

            response = httpx.get(
                self.endpoint,
                headers=self._headers(),
                timeout=min(self.timeout_s, 2.0),
            )
            status = getattr(response, "status_code", 500)
            return isinstance(status, int) and status < 400
        except Exception:
            return False


def _match_label(text: str, labels: list[str]) -> str | None:
    lowered = text.lower()
    for label in labels:
        if label.lower() in lowered:
            return label
    return None
