"""FastAPI application entrypoint.

Wiring, CORS, exception handling, and the routers. Business logic lives in
`app/services/` — nothing here decides anything beyond how a failure is rendered.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette import status
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.health import router as health_router
from app.api.v1 import router as v1_router
from app.core.config import get_settings
from app.core.database import dispose_engine
from app.core.exceptions import AppError, ErrorCode, error_body
from app.core.rate_limit import close_client as close_rate_limit_client

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown.

    The database engine and the Redis client both build their pools lazily on first
    use, so there is nothing to open here. Closing them is not optional: pooled
    connections outlive the process otherwise, and linger server-side until the
    database or Redis times them out.
    """
    yield
    await close_rate_limit_client()
    await dispose_engine()


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------
# Every error leaving this API has the shape §42 specifies. Rendering them in one
# place is what makes that true — a route that hand-built a response would be a
# second shape to keep in sync.


async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """Render a deliberate domain error."""
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(exc.code, exc.message),
        headers=exc.headers,
    )


async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Render a Pydantic validation failure as `VALIDATION_ERROR`.

    **Only `loc` and `msg` are forwarded.** Pydantic's error dicts also carry `input`
    — the value the client sent — and on a login or registration payload that is the
    plaintext password. Echoing it back would write a live credential into the
    response body, into any proxy that logs response bodies, and into the browser's
    network panel. The client already knows what it sent; it needs to know which field
    was wrong and why.
    """
    details = [
        {
            "field": ".".join(str(part) for part in error["loc"] if part != "body"),
            "message": error["msg"],
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            **error_body(ErrorCode.VALIDATION_ERROR, "The request could not be validated."),
            "details": details,
        },
    )


async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Render framework-raised errors — unknown path, wrong method — in the same shape.

    Without this, `GET /api/v1/nope` returns `{"detail": "Not Found"}` while every
    other error returns `{"error": {...}}`, and every client needs two parsers.
    """
    # Starlette's status phrases are already the clearest available wording
    # ("Not Found", "Method Not Allowed"), and none of them disclose anything.
    message = exc.detail if isinstance(exc.detail, str) else "Request failed."
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(_code_for_status(exc.status_code), message),
        headers=getattr(exc, "headers", None),
    )


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log a bug with its traceback, return an opaque 500.

    The exception is never included in the response. `str(exc)` on a database error
    carries the failing SQL and its parameters, which is customer data — it belongs in
    the log, where access is controlled, and not in a response body.
    """
    logger.exception(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        error_type=type(exc).__name__,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=error_body(ErrorCode.INTERNAL_SERVER_ERROR, "An unexpected error occurred."),
    )


def _code_for_status(status_code: int) -> ErrorCode:
    """Map an HTTP status to a code, for errors the framework raised.

    Only the statuses Starlette itself produces are listed. Everything else falls back
    to `INTERNAL_SERVER_ERROR`, which is at least fail-closed — it never claims a
    request succeeded.
    """
    return {
        status.HTTP_401_UNAUTHORIZED: ErrorCode.AUTHENTICATION_REQUIRED,
        status.HTTP_403_FORBIDDEN: ErrorCode.FORBIDDEN,
        status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
        status.HTTP_405_METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
    }.get(status_code, ErrorCode.INTERNAL_SERVER_ERROR)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.PROJECT_NAME,
        version="0.1.0",
        # Interactive docs are a development affordance, not a production surface.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
        lifespan=lifespan,
    )

    # Credentials are required for the refresh cookie, so the origin list must be
    # explicit — a wildcard is invalid with allow_credentials=True, and a browser
    # would refuse the response anyway.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Order matters: `AppError` is the most specific, and Starlette resolves handlers
    # by walking the exception's MRO, so a subclass hits the right one regardless.
    app.add_exception_handler(AppError, _app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled_exception_handler)

    app.include_router(health_router, tags=["health"])
    app.include_router(v1_router, prefix=settings.API_V1_PREFIX)

    return app


app = create_app()
