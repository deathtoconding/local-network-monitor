"""FastAPI application factory.

``create_app(state)`` builds the ASGI app around a :class:`MonitorState`. The
dashboard is served from ``web/`` as static files, so opening
``http://127.0.0.1:8000/`` shows the live UI and ``/api/...`` returns JSON from
the same process - no second server, no CORS configuration.
"""

from __future__ import annotations

import logging
import os
from importlib import resources
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import Config
from .routes import router
from .state import MonitorState, build_state

logger = logging.getLogger(__name__)


#: Where the dashboard assets live. They ship *inside* the package
#: (``network_monitor/web``) so an installed wheel is a complete product - which
#: is verified by the CI package job, not assumed.
def resolve_web_dir() -> Path:
    """Locate the dashboard assets.

    Order: explicit ``LNM_WEB_DIR`` environment override, the packaged
    ``network_monitor/web`` directory, then a source checkout layout. The env
    override exists so the dashboard can be developed or replaced without
    touching the installed package.
    """
    override = os.environ.get("LNM_WEB_DIR")
    if override:
        return Path(override).expanduser()
    try:
        packaged = Path(str(resources.files("network_monitor"))) / "web"
        if packaged.is_dir():
            return packaged
    except (ImportError, ModuleNotFoundError, TypeError):  # pragma: no cover
        pass
    return Path(__file__).resolve().parents[1] / "web"


WEB_DIR = resolve_web_dir()


def create_app(
    state: Optional[MonitorState] = None,
    config: Optional[Config] = None,
    web_dir: Optional[Path] = None,
) -> FastAPI:
    """Create the ASGI application."""
    monitor_state = state or build_state(config or Config())

    app = FastAPI(
        title="Local Network Monitor",
        description=(
            "Local, read-only view of what this machine is doing on the network: "
            "interface throughput, TCP connections with owning processes, and "
            "deterministic detection events."
        ),
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    # The dashboard is same-origin; CORS is only useful for local experiments
    # such as a second dev server. Keep it restricted to loopback origins.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
        allow_methods=["GET", "PATCH", "OPTIONS"],
        allow_headers=["*"],
    )

    app.include_router(router)
    app.state.monitor = monitor_state

    static_dir = Path(web_dir) if web_dir else resolve_web_dir()
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        @app.get("/", include_in_schema=False)
        def dashboard() -> FileResponse:
            return FileResponse(static_dir / "index.html")

        @app.get("/favicon.ico", include_in_schema=False)
        def favicon() -> JSONResponse:  # browsers cope fine with an empty 204
            return JSONResponse(status_code=204, content=None)
    else:  # pragma: no cover - defensive
        logger.warning("web directory not found at %s; dashboard disabled", static_dir)

        @app.get("/", include_in_schema=False)
        def dashboard_missing() -> JSONResponse:
            return JSONResponse(
                status_code=503,
                content={"detail": "dashboard assets are missing", "api": "/api/docs"},
            )

    @app.get("/api/info", include_in_schema=False)
    def api_info(request: Request) -> dict:
        # Enumerate the router's routes rather than app.routes: FastAPI mounts
        # included routers lazily, so app.routes does not list them directly.
        endpoints = sorted({route.path for route in router.routes if route.path.startswith("/api")})
        return {
            "name": "Local Network Monitor",
            "version": __version__,
            "docs": "/api/docs",
            "dashboard": "/",
            "endpoints": endpoints,
        }

    return app
