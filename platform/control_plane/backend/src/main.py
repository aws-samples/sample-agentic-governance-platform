"""Control-plane FastAPI application.

Every router is registered bare and, when `ROOT_PATH` is set, again under it through
`_register_routers`, because API Gateway forwards the stage segment to the backend;
`tests/test_stage_prefix_parity.py` pins that both spellings serve the same paths and that no router
is registered anywhere else. Errors leave through two handlers: `app_error_handler` renders a
service's `core.errors.AppError` from its `kinds` table, and `global_exception_handler` masks
everything else.
Decision: docs/adr/backend-service-errors-one-handler.md
"""

import asyncio
import logging
import os

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.routes import (
    agent_mcp_router,
    agents_router,
    builds_router,
    connections_router,
    containment_router,
    entra_router,
    github_link_router,
    github_templates_router,
    governance_graph_router,
    grants_router,
    guardrails_router,
    health_router,
    infra_templates_router,
    marketplace_router,
    mcp_cedar_router,
    mcp_grants_router,
    mcp_servers_router,
    observability_router,
    project_repos_router,
    projects_router,
    repositories_router,
    tenants_router,
    users_admin_router,
    users_router,
)
from api.routes.evals import router as evals_router
from api.routes.studio import router as studio_router
from core.config import settings
from core.errors import install_error_handlers
from models.errors import ERROR_RESPONSES
from models.health import RootResponse

_WIRE_LOGGERS = ("boto3", "botocore", "urllib3", "httpx", "httpcore", "s3transfer")
_MONITOR_STOP_SECONDS = 15


def configure_logging(log_level: str) -> None:
    """Configure root logging at `log_level`, then floor the wire loggers at INFO.

    `LOG_LEVEL` is operator-settable and `basicConfig` sets the root logger, so DEBUG would otherwise
    switch on botocore's verbatim request and response body logging, and a `GetSecretValue` body is
    the secret. A floor, not a fixed level: a quieter `LOG_LEVEL` is left alone. Pinned by
    `tests/test_wire_logger_clamp.py`.
    """
    logging.basicConfig(
        level=getattr(logging, log_level), format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    for name in _WIRE_LOGGERS:
        wire_logger = logging.getLogger(name)
        if wire_logger.getEffectiveLevel() < logging.INFO:
            wire_logger.setLevel(logging.INFO)


configure_logging(settings.LOG_LEVEL)

logger = logging.getLogger(__name__)

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    responses=ERROR_RESPONSES,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


_monitor_stop: asyncio.Event | None = None
_monitor_task: asyncio.Task | None = None


@app.on_event("startup")
async def start_containment_monitor():
    """Run the containment monitor loop only when a containment table is configured, so tests and
    local development on the in-memory store never start a background loop."""
    global _monitor_stop, _monitor_task
    if not settings.CONTAINMENT_TABLE_NAME:
        return
    from services.containment_monitor import run_monitor_forever

    _monitor_stop = asyncio.Event()
    _monitor_task = asyncio.create_task(run_monitor_forever(_monitor_stop))
    logger.info("[startup] containment monitor started")


@app.on_event("startup")
async def load_entra_backend_secret_on_startup():
    """Load the Entra backend client secret at startup so a misconfiguration surfaces in the logs at
    once rather than on the first Graph call. Logged, not fatal: inbound token validation does not
    need the secret, only outbound Graph calls do."""
    if settings.AUTH_PROVIDER != "entra":
        return
    try:
        from core.secrets_loader import load_entra_backend_client_secret

        load_entra_backend_client_secret()
        logger.info("[startup] Entra backend client secret loaded successfully")
    except Exception:
        logger.exception("[startup] Failed to load Entra backend client secret")


@app.on_event("startup")
async def refuse_dev_auth_outside_the_local_shape():
    """Say once why the dev-auth headers will not be honoured: ``USE_DEV_AUTH`` is on but a setting
    selects AWS or the directory, so every request needs an Entra token."""
    if settings.USE_DEV_AUTH and not settings.dev_auth_honoured:
        logger.warning(
            "[startup] USE_DEV_AUTH is on but refused: every request needs an Entra token because AWS or the"
            " directory is selected by %s",
            ", ".join(settings.aws_selectors()),
        )


@app.on_event("shutdown")
async def shutdown_event():
    """Stop the containment monitor within half of ECS's default 30 s `stopTimeout`."""
    logger.info("Shutting down Control Plane API...")
    if _monitor_task is not None and _monitor_stop is not None:
        _monitor_stop.set()
        done, _ = await asyncio.wait({_monitor_task}, timeout=_MONITOR_STOP_SECONDS)
        if not done:
            _monitor_task.cancel()


def _root_payload() -> RootResponse:
    return RootResponse(
        name=settings.APP_NAME,
        version=settings.APP_VERSION,
        status="running",
        build_sha=os.environ.get("BUILD_SHA", "unknown"),
    )


@app.get("/", response_model=RootResponse)
async def root():
    return _root_payload()


if settings.ROOT_PATH:

    @app.get(f"{settings.ROOT_PATH}/", response_model=RootResponse)
    async def root_stage():
        return _root_payload()


def _with_cors(request: Request, response: JSONResponse) -> JSONResponse:
    """Re-attach CORS headers to an error response: `CORSMiddleware` does not decorate a response an
    exception handler produced, and a body the browser refuses to hand over is no message at all."""
    origin = request.headers.get("origin")
    if origin and origin in settings.CORS_ORIGINS:
        response.headers["access-control-allow-origin"] = origin
        response.headers["access-control-allow-credentials"] = "true"
    return response


install_error_handlers(app)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc):
    """Mask every unhandled exception as one fixed 500 body for every caller; the text goes to the log."""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return _with_cors(
        request,
        JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "error": "An error occurred"},
        ),
    )


API_ROUTERS: tuple[APIRouter, ...] = (
    users_router,
    guardrails_router,
    agents_router,
    mcp_servers_router,
    grants_router,
    entra_router,
    mcp_grants_router,
    agent_mcp_router,
    mcp_cedar_router,
    marketplace_router,
    governance_graph_router,
    users_admin_router,
    connections_router,
    projects_router,
    project_repos_router,
    evals_router,
    studio_router,
    repositories_router,
    tenants_router,
    github_templates_router,
    infra_templates_router,
    builds_router,
    github_link_router,
    observability_router,
    containment_router,
)


def _register_routers(stage: str) -> None:
    app.include_router(health_router, prefix=stage)
    for router in API_ROUTERS:
        app.include_router(router, prefix=f"{stage}{settings.API_PREFIX}")


_register_routers("")
if settings.ROOT_PATH:
    _register_routers(settings.ROOT_PATH)
