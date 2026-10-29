"""Create and configure the shared FastAPI application."""

import os
import logging
from pathlib import Path
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes.documents import router as documents_router
from app.api.routes.account import router as account_router
from app.api.routes.health import router as health_router
from database import init_db


logger = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parents[2] / "web"
PILOT_ALLOWED_PATHS = frozenset({
    "/health", "/docs", "/redoc", "/openapi.json",
    "/app", "/login",
    "/v1/answers", "/v1/organization", "/v1/device-models",
    "/v1/documents",
})
PILOT_ALLOWED_DYNAMIC_PREFIXES = (
    "/assets/",
    "/v1/auth/",
    "/v1/documents/",
    "/v1/ingestion-jobs/",
)


async def close_legacy_routes_in_pilot_mode(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Keep the old unauthenticated endpoints out of an enabled pilot."""
    if os.getenv("PILOT_API_KEYS") is not None and request.url.path == "/":
        return RedirectResponse("/login", status_code=307)
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
    application.include_router(account_router)
    application.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")

    @application.get("/app", include_in_schema=False)
    def pilot_app() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @application.get("/login", include_in_schema=False)
    def login_page() -> FileResponse:
        return FileResponse(WEB_DIR / "login.html", headers={"Cache-Control": "no-store"})

    return application
