"""FastAPI application factory."""
from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.ai.summarizer import SummaryProvider
from app.api.deps import build_container
from app.api.routes import router
from app.core.clock import Clock
from app.core.config import Settings, get_settings
from app.core.errors import DomainError
from app.core.logging import configure_logging

log = logging.getLogger("opsib.api")


def _error(status: int, code: str, message: str, details: object, request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details, "request_id": request_id}},
        headers={"X-Request-Id": request_id},
    )


def create_app(settings: Settings | None = None, *, clock: Clock | None = None, ai_provider: SummaryProvider | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(
        title="OPSiB receipt-shortfall service",
        version="1.0.0",
        description="Imports retail source data, reconciles PO lines with goods receipts, raises receipt-shortfall "
        "exceptions with provenance, routes investigation tasks to eligible employees and tracks them to verification.",
    )
    app.state.container = build_container(settings, clock=clock, ai_provider=ai_provider)

    @app.middleware("http")
    async def request_id_mw(request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        log.info(
            "http.request",
            extra={"request_id": request.state.request_id, "method": request.method, "path": request.url.path, "status": response.status_code},
        )
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return _error(exc.http_status, exc.code, exc.message, exc.details, getattr(request.state, "request_id", "-"))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"loc": list(e.get("loc", ())), "msg": e.get("msg")} for e in exc.errors()]
        return _error(422, "REQUEST_INVALID", "request body or parameters are invalid", details, getattr(request.state, "request_id", "-"))

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        rid = getattr(request.state, "request_id", "-")
        log.exception("http.unhandled", extra={"request_id": rid, "path": request.url.path})
        return _error(500, "INTERNAL_ERROR", "unexpected error; see logs with the request id", {}, rid)

    app.include_router(router)
    return app
