"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError, ProgrammingError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app import __version__
from app.config import get_settings
from app.db import SessionLocal, get_engine
from app.routers import (
    admin,
    api,
    auth,
    dashboard,
    followups,
    inquiries,
    notifications,
    reports,
)
from app.security import LoginRequired
from app.services.bootstrap import run_bootstrap
from app.services.digests import scheduler_loop
from app.templating import render

log = logging.getLogger("app")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'self'"
    ),
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    get_engine()
    db = SessionLocal()
    try:
        run_bootstrap(db, settings)
    except (OperationalError, ProgrammingError) as exc:
        db.rollback()
        log.error("Database not ready (%s). Did you run `alembic upgrade head`?", exc.orig)
    finally:
        db.close()
    scheduler = None
    if settings.run_scheduler and settings.notifications_enabled:
        scheduler = asyncio.create_task(scheduler_loop())
    yield
    if scheduler is not None:
        scheduler.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler


def create_app() -> FastAPI:
    settings = get_settings()
    settings.validate_for_runtime()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if settings.session_https_only:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="leadtracker_session",
        max_age=settings.session_max_age_hours * 3600,
        same_site="lax",
        https_only=settings.session_https_only,
    )

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    for module in (auth, dashboard, inquiries, followups, reports, admin, api, notifications):
        app.include_router(module.router)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        return RedirectResponse(f"/login?next={quote(exc.next_url)}", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if _wants_json(request):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        message = exc.detail if isinstance(exc.detail, str) else "Something went wrong."
        if exc.status_code == 404 and message == "Not Found":
            message = "Page not found."
        return render(
            request, "error.html", {"status": exc.status_code, "message": message}, exc.status_code
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        if _wants_json(request):
            return JSONResponse({"detail": exc.errors()}, status_code=422)
        return render(
            request,
            "error.html",
            {
                "status": 422,
                "message": "Some fields were missing or invalid. Go back and try again.",
            },
            422,
        )

    return app


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/") or "application/json" in request.headers.get(
        "accept", ""
    )


app = create_app()
