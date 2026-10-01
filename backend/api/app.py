"""FastAPI factory, private local origin boundary, and shared service lifetime."""

# ruff: noqa: E402
# Version checks intentionally precede Python-3.11 dependency imports.
from backend.api.runtime import check_python

check_python()  # Give an actionable version error before dependency imports.

import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException
from fastapi.exceptions import RequestValidationError
from starlette.middleware.cors import CORSMiddleware

from backend.api.errors import error_response, internal_error, request_error
from backend.api.pipeline import Pipeline
from backend.api.replay_cache import ReplayCache
from backend.api.settings_service import SettingsService
from backend.api import routes_imports, routes_sessions, routes_settings, routes_replays
from backend.domain.errors import User_Error
from backend.domain.timezones import DEFAULT_SOURCE_TIMEZONES, DISPLAY_TIMEZONE
from backend.persistence.safe_logging import configure_logging, shutdown_logging


def create_app(data_dir=None, *, display_timezone=None, frontend_origin=None):
    @asynccontextmanager
    async def lifespan(app):
        pipeline = Pipeline(data_dir, display_timezone=display_timezone)
        try:
            configure_logging(pipeline.data_dir)
            app.state.pipeline = pipeline
            app.state.operation_lock = threading.RLock()
            app.state.generation_lock = threading.Lock()
            app.state.settings = SettingsService(pipeline.metadata_store)
            app.state.settings.get()  # validate stored settings before requests
            app.state.cache = ReplayCache(pipeline.data_dir, pipeline.metadata_store)
            app.state.max_upload_bytes = routes_imports.MAX_UPLOAD_BYTES
            yield
        finally:
            pipeline.close()
            shutdown_logging()

    app = FastAPI(
        title="Sleep Replay",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    origin = frontend_origin or os.environ.get(
        "SLEEP_REPLAY_FRONTEND_ORIGIN", "http://127.0.0.1:8734"
    )
    origins = {origin}
    if origin == "http://127.0.0.1:8734":
        origins.add("http://localhost:8734")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(origins),
        allow_methods=["GET", "POST", "PUT", "HEAD"],
        allow_headers=["Content-Type", "Range"],
    )

    @app.middleware("http")
    async def boundary(request, call_next):
        if (
            request.headers.get("origin") is not None
            and request.headers["origin"] not in origins
        ):
            return error_response(
                request_error(
                    "This browser origin is not allowed.",
                    "Open Sleep Replay from its local frontend URL.",
                ),
                403,
            )
        try:
            return await call_next(request)
        except User_Error as error:
            return error_response(error)
        except Exception as error:
            return internal_error(error)

    @app.exception_handler(User_Error)
    async def user_error(request, error):
        return error_response(error)

    @app.exception_handler(RequestValidationError)
    async def validation(request, error):
        return error_response(
            request_error(
                "The request fields or JSON body are invalid.",
                "Check the required fields and their allowed values.",
            ),
            422,
        )

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        response = error_response(
            request_error(
                str(error.detail), "Check the local URL or generate the replay again."
            ),
            error.status_code,
        )
        response.headers.update(error.headers or {})
        return response

    @app.get("/api/status")
    def status(request: Request):
        p = request.app.state.pipeline
        return dict(
            has_imports=bool(p.metadata_store.list_import_ids()),
            display_timezone=p.display_tz.key,
            max_upload_bytes=request.app.state.max_upload_bytes,
            source_timezones={
                k: p.display_tz.key if v == DISPLAY_TIMEZONE else v
                for k, v in DEFAULT_SOURCE_TIMEZONES.items()
            },
        )

    for router in (
        routes_imports.router,
        routes_sessions.router,
        routes_settings.router,
        routes_replays.router,
    ):
        app.include_router(router)
    return app


def main():
    from backend.api.runtime import api_server, check_python, bind_socket
    import sys

    check_python()
    host = os.environ.get("SLEEP_REPLAY_API_HOST", "127.0.0.1")
    try:
        with bind_socket(host, 8735) as sock:
            server = api_server()
            server.run(sockets=[sock])
            return 0 if server.started else 1
    except (OSError, User_Error) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
