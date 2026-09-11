"""FastAPI application entrypoint.

Phase C scaffold: wiring, CORS, and health endpoints only. Domain routers arrive in
later phases.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.core.config import get_settings
from app.core.database import dispose_engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown.

    The engine builds its pool lazily on first use, so there is nothing to open
    here. Closing it is not optional: without dispose, pooled connections outlive
    the process and linger server-side until Postgres times them out.
    """
    yield
    await dispose_engine()


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
    # explicit — a wildcard is invalid with allow_credentials=True.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router, tags=["health"])

    return app


app = create_app()
