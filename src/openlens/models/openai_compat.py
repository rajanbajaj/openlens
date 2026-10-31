"""OpenAI-compatible chat-completions model backend (neutral transport).

Posts ``POST {base_url}/chat/completions`` with ``{"model", "messages"}``
and reads ``choices[0].message.content`` — the shape served by any
OpenAI-compatible endpoint (OpenAI, Meta's developer API, Ollama, vLLM,
LiteLLM, ...). No vendor SDK is used: ``httpx`` is lazy-imported inside
methods, mirroring ``LocalModelProvider``.

``embed`` uses ``POST {base_url}/embeddings`` (OpenAI ``{"data":
[{"embedding": [...]}]}`` shape); ``classify`` asks the model to reply
with exactly one of the candidate labels and matches it. Error taxonomy
and redaction discipline mirror ``backends.py``: messages never carry
the endpoint, key, or raw client text.
"""

from __future__ import annotations

from typing import Any

from openlens.core.interfaces import ConfigError, ProviderError
from openlens.models.base import BaseModelProvider


class OpenAICompatModelProvider(BaseModelProvider):
    """Any OpenAI-compatible chat-completions endpoint (e.g. muse-spark)."""

    def __init__(self) -> None:
        super().__init__()
        self.base_url: str = ""
        self.model: str = "muse-spark-1.3-contributor"
        self.api_key: str = ""

    @property
    def name(self) -> str:
        return "openai_compat"

    def configure(self, config: dict[str, Any]) -> None:
        super().configure(config)
        base_url = self._config.get("base_url", "")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ConfigError(
                f"Model backend '{self.name}': 'base_url' must be a "
                f"non-empty URL, got {base_url!r}. Startup validation failed."
            )
        self.base_url = base_url.strip().rstrip("/")
        model = self._config.get("model", "muse-spark-1.3-contributor")
        if not isinstance(model, str) or not model.strip():
            raise ConfigError(
                f"Model backend '{self.name}': 'model' must be a non-empty "
                f"string, got {model!r}. Startup validation failed."
            )
        self.model = model.strip()
        api_key = self._config.get("api_key", "")
        if api_key is None:
            api_key = ""
        if not isinstance(api_key, str) or not api_key:
            raise ConfigError(
                f"Model backend '{self.name}': 'api_key' is required when the "
                "backend is enabled. Set it via '${VAR}' env resolution. "
                "Startup validation failed."
            )
        self.api_key = api_key

    # -- transport -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
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
                f"{self.base_url}{path}",
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

    def _mapped_error(self, exc: BaseException) -> ProviderError:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if not isinstance(status, int):
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

    # -- inference -------------------------------------------------------

    @staticmethod
    def _safe_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
        """Drop anything credential-shaped before it reaches the wire."""
        return {
            k: v
            for k, v in kwargs.items()
            if k.lower()
            not in ("api_key", "authorization", "base_url", "endpoint", "model")
        }

    @staticmethod
    def _extract_text(data: Any) -> str | None:
        if isinstance(data, str):
            return data
        if not isinstance(data, dict):
            return None
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, str):
                return first
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str):
                        return content
                for key in ("text", "content"):
                    value = first.get(key)
                    if isinstance(value, str):
                        return value
        for key in ("text", "completion", "response", "content", "result"):
            value = data.get(key)
            if isinstance(value, str):
                return value
        return None

    def complete(self, prompt: str, **kwargs: Any) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ProviderError(
                f"model backend '{self.name}' requires a non-empty prompt",
                provider_name=self.name,
                retryable=False,
                code=ProviderError.INVALID_REQUEST,
            )
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            **self._safe_kwargs(kwargs),
        }
        text = self._extract_text(self._post("/chat/completions", payload))
        if text is None:
            raise self.wrap_error(
                RuntimeError("missing completion text"),
                message=f"model backend '{self.name}' returned an unparseable response",
                retryable=True,
                code=ProviderError.UNAVAILABLE,
            )
        return text

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
            raise ProviderError(
                f"model backend '{self.name}' requires a list of strings to embed",
                provider_name=self.name,
                retryable=False,
                code=ProviderError.INVALID_REQUEST,
            )
        data = self._post("/embeddings", {"model": self.model, "input": texts})
        vectors = self._extract_embeddings(data)
        if vectors is None or len(vectors) != len(texts):
            raise self.wrap_error(
                RuntimeError("missing embeddings"),
                message=f"model backend '{self.name}' returned an unparseable response",
                retryable=True,
                code=ProviderError.UNAVAILABLE,
            )
        return vectors

    @staticmethod
    def _extract_embeddings(data: Any) -> list[list[float]] | None:
        if not isinstance(data, dict):
            return None
        items = data.get("data")
        if not isinstance(items, list):
            return None
        vectors: list[list[float]] = []
        for item in items:
            if not isinstance(item, dict):
                return None
            raw = item.get("embedding")
            if (
                not isinstance(raw, list)
                or not raw
                or not all(
                    isinstance(v, (int, float)) and not isinstance(v, bool) for v in raw
                )
            ):
                return None
            vectors.append([float(v) for v in raw])
        return vectors

    def classify(self, text: str, labels: list[str]) -> str:
        if not labels or not all(isinstance(label, str) for label in labels):
            raise ProviderError(
                f"model backend '{self.name}' requires at least one label",
                provider_name=self.name,
                retryable=False,
                code=ProviderError.INVALID_REQUEST,
            )
        choices = ", ".join(labels)
        answer = self.complete(
            "Reply with exactly one of the following labels and nothing "
            f"else: {choices}. Text: {text}"
        )
        matched = _match_label(answer, labels)
        return matched if matched is not None else answer.strip()

    def health_check(self) -> bool:
        """Never raises; False when unreachable, unauthorized, or unconfigured."""
        if not self.base_url:
            return False
        try:
            import httpx

            response = httpx.get(
                f"{self.base_url}/models",
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
