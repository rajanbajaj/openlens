"""Consistent API error envelope and exception-handler registration.

Every failure — request validation, provider outage, plugin failure,
misconfiguration, or unexpected bug — returns the same shape:

    {"error": {"code": "<machine_code>", "message": "<human_message>"}}

so clients only ever branch on `error.code`.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from openlens.core.interfaces import ConfigError, PluginError, ProviderError


class EngineUnavailableError(RuntimeError):
    """Raised when no Engine is attached to the app (wiring not done)."""


def error_body(code: str, message: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": message}}


def _validation_message(exc: RequestValidationError) -> str:
    parts = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "body"
        parts.append(f"{loc}: {err.get('msg', 'invalid value')}")
    message = "; ".join(parts) or "request validation failed"
    if len(exc.errors()) > 5:
        message += f" (+{len(exc.errors()) - 5} more)"
    return message


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the shared error envelope to every failure mode."""

    @app.exception_handler(EngineUnavailableError)
    async def _engine_unavailable(
        request: Request, exc: EngineUnavailableError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content=error_body("engine_unavailable", str(exc)),
        )

    @app.exception_handler(ProviderError)
    async def _provider_error(request: Request, exc: ProviderError) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content=error_body(f"provider_{exc.code}", str(exc)),
        )

    @app.exception_handler(PluginError)
    async def _plugin_error(request: Request, exc: PluginError) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content=error_body("plugin_error", str(exc)),
        )

    @app.exception_handler(ConfigError)
    async def _config_error(request: Request, exc: ConfigError) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content=error_body("config_error", str(exc)),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_body("validation_error", _validation_message(exc)),
        )

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body("http_error", str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def _unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # Generic message on purpose: never leak internals or secrets.
        return JSONResponse(
            status_code=500,
            content=error_body("internal_error", "unexpected internal error"),
        )
