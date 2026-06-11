from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI
from fastapi.openapi.utils import get_openapi
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles
from starlette.status import HTTP_404_NOT_FOUND

# psycopg3 async cannot use Windows's default ProactorEventLoop. Force the
# SelectorEventLoop policy at import time so uvicorn picks it up when it
# creates the app's loop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(
        asyncio.WindowsSelectorEventLoopPolicy()  # type: ignore[attr-defined]
    )

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.responses import Response
    from starlette.types import Scope

from rehketo.agent.sweep import sweep_abandoned_runs
from rehketo.api.errors import install_error_handlers
from rehketo.auth.cookies import CSRF_HEADER
from rehketo.auth.csrf_middleware import CSRF_EXEMPT_PREFIXES, CSRFMiddleware
from rehketo.auth.dependencies import AuthContext, resolve_session
from rehketo.config import get_settings, get_ui_static_dir
from rehketo.core.logging import get_logger
from rehketo.runs.cancellation import RunControlListener
from rehketo.runs.event_bus import PostgresEventBus
from rehketo.runs.registry import get_registry

logger = get_logger(__name__)

_UNSAFE_METHODS_LC: frozenset[str] = frozenset({"post", "put", "patch", "delete"})


def _install_openapi_csrf_scheme(app: FastAPI) -> None:
    """Override app.openapi to declare X-CSRF-Token as a required header on
    every unsafe-method operation that isn't CSRF-exempt. Keeps routes clean
    — the middleware is the enforcer; this is purely schema declarative."""

    def _openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            routes=app.routes,
        )
        components = schema.setdefault("components", {})
        schemes = components.setdefault("securitySchemes", {})
        schemes["CSRFToken"] = {
            "type": "apiKey",
            "in": "header",
            "name": CSRF_HEADER,
            "description": (
                "Double-submit CSRF token. Required on all unsafe methods "
                "except /auth/* and /healthz."
            ),
        }
        for path, path_item in schema.get("paths", {}).items():
            if any(path.startswith(p) for p in CSRF_EXEMPT_PREFIXES):
                continue
            if not isinstance(path_item, dict):
                continue
            for method, op in path_item.items():
                if method not in _UNSAFE_METHODS_LC or not isinstance(op, dict):
                    continue
                op.setdefault("security", []).append({"CSRFToken": []})
        app.openapi_schema = schema
        return schema

    app.openapi = _openapi  # type: ignore[method-assign]


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    logger.info("rehketo-api starting app_env=%s", settings.app_env)
    try:
        await app.state.event_bus.start()
        await app.state.control_listener.start()
        await sweep_abandoned_runs(app.state.event_bus)
        yield
    finally:
        await app.state.control_listener.stop()
        await app.state.event_bus.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Rehketo API",
        version="0.1.0",
        lifespan=_lifespan,
        # Stock /docs assumes Bearer auth; we serve a Pattern B-aware replacement
        # from rehketo.api.docs that threads cookies + the CSRF header for us.
        # openapi_url=None disables the default anonymous schema route; we
        # remount it below behind resolve_session.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_error_handlers(app)
    app.add_middleware(CSRFMiddleware)

    # Constructed here (no I/O); its LISTEN task starts in _lifespan.
    app.state.event_bus = PostgresEventBus()
    app.state.task_registry = get_registry()
    app.state.control_listener = RunControlListener(app.state.task_registry)

    from rehketo.api import auth_routes
    from rehketo.api import conversations as conversations_api
    from rehketo.api import docs as docs_api
    from rehketo.api import mcp_servers as mcp_servers_api
    from rehketo.api import me as me_api
    from rehketo.api import messages as messages_api
    from rehketo.api import runs as runs_api

    app.include_router(auth_routes.router)
    app.include_router(conversations_api.router)
    app.include_router(docs_api.router)
    app.include_router(mcp_servers_api.router)
    app.include_router(me_api.router)
    app.include_router(messages_api.router)
    app.include_router(runs_api.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/openapi.json", include_in_schema=False)
    async def openapi_schema(
        _auth: Annotated[AuthContext, Depends(resolve_session)],
    ) -> dict[str, Any]:
        return app.openapi()

    _install_openapi_csrf_scheme(app)
    _mount_ui_static_bundle_if_configured(app)

    return app


class _SPAStaticFiles(StaticFiles):
    """Serve the built SvelteKit bundle, falling back to index.html for any
    path Starlette can't resolve to a real file — so client-side routes (like
    /c/<uuid>) survive a full page load. StaticFiles does its own realpath-based
    containment check, so user input never touches a path join we own."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == HTTP_404_NOT_FOUND:
                return await super().get_response("index.html", scope)
            raise


def _mount_ui_static_bundle_if_configured(app: FastAPI) -> None:
    """When UI_STATIC_DIR points at a built SvelteKit bundle, serve it at /
    with SPA fallback: real files under the dir resolve directly, anything
    else returns index.html so client-side routes (like /c/<uuid>) survive a
    full page load. API routers (auth, conversations, runs, me, docs,
    openapi.json, healthz) are registered first so their paths win over this
    catch-all. A no-op when UI_STATIC_DIR is unset (dev runs the UI under
    Vite separately).

    UI_STATIC_DIR is read via get_ui_static_dir() rather than get_settings()
    because create_app() runs at module import; instantiating full Settings
    here would require every production secret and break test collection."""
    ui_static_dir = get_ui_static_dir()
    if not ui_static_dir:
        return
    ui_dir = Path(ui_static_dir)
    if not ui_dir.is_dir():
        logger.warning("UI_STATIC_DIR is set but does not exist: %s", ui_static_dir)
        return
    index_html = ui_dir / "index.html"
    if not index_html.is_file():
        logger.warning(
            "UI_STATIC_DIR has no index.html, skipping mount: %s", ui_static_dir
        )
        return

    app.mount("/", _SPAStaticFiles(directory=ui_dir, html=True), name="ui")


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("rehketo.main:app", host="0.0.0.0", port=8000, reload=True)  # noqa: S104  # nosec B104
