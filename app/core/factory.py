"""Create and configure the shared FastAPI application."""

import os
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.api.routes.documents import router as documents_router
from app.api.routes.health import router as health_router
from database import init_db


logger = logging.getLogger(__name__)
PILOT_ALLOWED_PATHS = frozenset({
    "/health", "/docs", "/redoc", "/openapi.json",
    "/v1/answers", "/v1/organization", "/v1/device-models",
    "/v1/documents",
})
PILOT_ALLOWED_DYNAMIC_PREFIXES = (
    "/v1/documents/",
    "/v1/ingestion-jobs/",
)


async def close_legacy_routes_in_pilot_mode(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Keep the old unauthenticated endpoints out of an enabled pilot."""
    allowed_dynamic_path = request.url.path.startswith(PILOT_ALLOWED_DYNAMIC_PREFIXES)
    if (
        os.getenv("PILOT_API_KEYS") is not None
        and request.url.path not in PILOT_ALLOWED_PATHS
        and not allowed_dynamic_path
    ):
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    return await call_next(request)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Initialize storage before the application serves requests."""
    init_db()
    if os.getenv("PDF_INGESTION_MODE", "background").strip().lower() == "background":
        logger.warning(
            "PDF_INGESTION_MODE=background is for development only; "
            "run a supervised polling Worker for crash recovery in production"
        )
    yield


def create_app() -> FastAPI:
    """Create the application and register migrated route modules."""
    application = FastAPI(
        title="Enterprise AI Assistant",
        description="企业 AI 售后助手",
        version="0.4.0",
        lifespan=lifespan,
    )
    application.middleware("http")(close_legacy_routes_in_pilot_mode)
    application.include_router(health_router)
    application.include_router(documents_router)
    return application
