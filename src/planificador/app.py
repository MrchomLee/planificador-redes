"""Application factory: middleware, routers, static files, lifespan (scheduler + services), error pages."""

from __future__ import annotations

import dataclasses
import logging
import logging.handlers
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from planificador import __version__, config, crypto
from planificador.config import AppSettings
from planificador.db import Database, set_current_database
from planificador.paths import STATIC_DIR, DataPaths
from planificador.security import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    HostCheckMiddleware,
    SecurityMiddleware,
)
from planificador.security import router as auth_router
from planificador.timeutil import Clock, SystemClock

log = logging.getLogger("planificador")

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


# ---------------------------------------------------------------------------------------------- logging


_TRACEBACK_FORMATTER = logging.Formatter()


class RedactingFilter(logging.Filter):
    """Scrub tokens/codes from every log record: the message (e.g. OAuth callback query strings in access
    logs) **and** the traceback text (e.g. an ``httpx.HTTPStatusError`` whose message contains the request URL
    with ``client_secret=...``), plus ``stack_info``."""

    def filter(self, record: logging.LogRecord) -> bool:
        from planificador.providers.base import scrub_text

        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed record; let logging report it
            return True
        cleaned = scrub_text(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = None
        # Formatter.format() reuses ``record.exc_text`` when set, so pre-render and scrub the traceback here.
        if record.exc_info and not record.exc_text:
            try:
                record.exc_text = _TRACEBACK_FORMATTER.formatException(record.exc_info)
            except Exception:  # pragma: no cover - let logging render it as usual
                pass
        if record.exc_text:
            record.exc_text = scrub_text(record.exc_text)
        if record.stack_info:
            record.stack_info = scrub_text(record.stack_info)
        return True


def setup_logging(paths: DataPaths, *, console: bool = False, level: int = logging.INFO) -> None:
    """Rotating ``<data>/logs/app.log`` (+ optional console). Idempotent: replaces our previous handlers."""
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_planificador", False):
            root.removeHandler(handler)
            handler.close()
    formatter = logging.Formatter(LOG_FORMAT)
    redactor = RedactingFilter()

    file_handler = logging.handlers.RotatingFileHandler(
        paths.log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8", delay=True
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)
    file_handler._planificador = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        stream.addFilter(redactor)
        stream._planificador = True  # type: ignore[attr-defined]
        root.addHandler(stream)

    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    for noisy in ("httpx", "httpcore", "multipart", "PIL", "watchfiles"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ------------------------------------------------------------------------------------------ rendering


def _wants_html(request: Request) -> bool:
    if request.headers.get("hx-request") == "true":
        return False
    accept = request.headers.get("accept", "")
    return "text/html" in accept or accept in ("", "*/*")


def render_error_page(request: Request, status_code: int, title: str, message: str) -> Response:
    """Render ``errors/<status>.html`` via templating; plain-text fallback if anything is missing."""
    try:
        from planificador import templating

        response = templating.render(
            request, f"errors/{status_code}.html", title=title, message=message, status_code=status_code
        )
        response.status_code = status_code
        return response
    except Exception:  # templating absent, template missing, or context building failed
        log.debug("Error template errors/%s.html unavailable; using plain text", status_code, exc_info=True)
        return PlainTextResponse(f"{title}\n\n{message}", status_code=status_code)


async def _http_exception(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code == 404 and _wants_html(request):
        return render_error_page(
            request, 404, "Página no encontrada", "La página que buscas no existe o se ha movido."
        )
    return await http_exception_handler(request, exc)


async def _unhandled_exception(request: Request, exc: Exception) -> Response:
    log.exception("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    if _wants_html(request):
        return render_error_page(
            request,
            500,
            "Algo ha fallado",
            "Se ha producido un error inesperado. Los detalles están en el registro (logs/app.log).",
        )
    return PlainTextResponse("Error interno. Revisa logs/app.log.", status_code=500)


# ----------------------------------------------------------------------------------------- lifespan


def _call_service(name: str, fn: Callable[[], Any]) -> bool:
    try:
        fn()
        return True
    except NotImplementedError:
        log.info("%s not implemented yet; skipping", name)
    except Exception:
        log.exception("%s failed", name)
    return False


def _start_public_media(app: FastAPI) -> None:
    """Create the public media service (``app.state.public_media``; its ``stop`` runs at shutdown) and start
    its server unless public media is off. The Cloudflare tunnel itself only starts on first use."""
    from planificador import publicmedia

    publicmedia.get_service(app).start()


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    state = app.state
    settings: AppSettings = state.settings
    started_scheduler = False
    _call_service("Public media service", lambda: _start_public_media(app))
    if settings.scheduler_enabled:
        from planificador import scheduler

        started_scheduler = _call_service("Scheduler start", lambda: scheduler.start(app))
    else:
        log.info("Scheduler disabled")
    log.info("Planificador %s ready on %s (data: %s)", __version__, settings.base_url, settings.data_dir)
    try:
        yield
    finally:
        if started_scheduler:
            from planificador import scheduler

            _call_service("Scheduler stop", lambda: scheduler.stop(app))
        for callback in reversed(list(state.shutdown_callbacks)):
            _call_service(f"Shutdown callback {getattr(callback, '__name__', callback)!s}", callback)
        state.db.checkpoint()
        log.info("Planificador stopped")


# ------------------------------------------------------------------------------------------ factory


def create_app(
    settings: AppSettings | None = None,
    *,
    clock: Clock | None = None,
    log_to_console: bool | None = None,
    **overrides: Any,
) -> FastAPI:
    """Build the FastAPI app.

    ``settings`` defaults to :func:`config.load_app_settings` (env vars) updated with ``overrides`` (e.g.
    ``create_app(data_dir=tmp, scheduler_enabled=False)``). The database is initialised here (not in the
    lifespan) so a ``TestClient`` used without a ``with`` block still works.
    """
    if settings is None:
        settings = config.load_app_settings(**overrides)
    elif overrides:
        if "data_dir" in overrides:
            overrides["data_dir"] = Path(overrides["data_dir"]).expanduser().resolve()
        settings = dataclasses.replace(settings, **overrides)
    config.set_app_settings(settings)

    paths = settings.paths.ensure()
    setup_logging(paths, console=settings.log_to_console if log_to_console is None else log_to_console)
    crypto.init(paths.secret_key_file)

    database = Database(paths.db_path)
    database.init()
    set_current_database(database)

    app = FastAPI(
        title="Planificador de Redes",
        version=__version__,
        lifespan=_lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.paths = paths
    app.state.db = database
    app.state.clock = clock or SystemClock()
    app.state.shutdown_callbacks = []  # zero-arg callables run at shutdown (services register themselves)
    app.state.public_media = None  # publicmedia.PublicMediaService (created in the lifespan / on first use)

    # Middleware: the last added is the outermost → Host check, then session, then CSRF/auth.
    app.add_middleware(SecurityMiddleware, allowed_origins=settings.allowed_origins)
    app.add_middleware(
        SessionMiddleware,
        secret_key=crypto.session_secret(),
        session_cookie=SESSION_COOKIE,
        max_age=SESSION_MAX_AGE,
        path="/",
        same_site="lax",
        https_only=False,
    )
    app.add_middleware(
        HostCheckMiddleware, allowed_hosts=settings.allowed_hosts, canonical_origin=settings.base_url
    )

    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(Exception, _unhandled_exception)

    app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "app": "planificador", "version": __version__})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        for candidate in ("img/favicon.svg", "img/logo.svg"):
            path = STATIC_DIR / candidate
            if path.is_file():
                return FileResponse(path, media_type="image/svg+xml")
        return Response(status_code=204)

    app.include_router(auth_router)
    from planificador.routes import include_routers

    include_routers(app)
    return app
