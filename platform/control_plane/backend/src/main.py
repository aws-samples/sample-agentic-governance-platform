"""
Control Plane FastAPI Application
Main entry point for the backend API
"""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import logging

from core.config import settings
from core.database import init_db
# Safe at module scope: `core.registry_resolver` imports only `logging` — boto3 lives inside
# `_client` and it reads no settings. Importing it here costs nothing and touches no AWS.
from core.registry_resolver import (
    AmbiguousRegistryNameError,
    RegistryNotConfiguredError,
    RegistryNotFoundError,
)

# Third-party libraries that log raw HTTP wire traffic at DEBUG. Clamped to a floor of INFO.
#
# SECURITY — DO NOT REMOVE THIS CLAMP. `LOG_LEVEL` is operator-settable (`core/config.py`) and
# `basicConfig` configures the ROOT logger, so `LOG_LEVEL=DEBUG` would otherwise switch DEBUG on
# for every library in the process. `botocore.parsers` then logs each response verbatim
# (`LOG.debug("Response body:\n%r", response["body"])`) and `botocore.awsrequest` logs the request
# body it rewinds — and every credential this backend handles travels through boto3 Secrets
# Manager: GitHub PATs and App private-key PEMs (`services/connection_service.py`), per-user
# GitHub OAuth tokens (`services/github_user_link.py`), and Langfuse public/secret keys
# (`services/langfuse_provisioning.py`). A `GetSecretValue` response body IS the secret, so one
# `LOG_LEVEL=DEBUG` deployment would write live credentials to CloudWatch. Clamping the library
# loggers keeps DEBUG usable for OUR code without enabling anyone else's payload logging.
# Pinned by `tests/test_wire_logger_clamp.py`.
_WIRE_LOGGERS = ("boto3", "botocore", "urllib3", "httpx", "httpcore", "s3transfer")


def configure_logging(log_level: str) -> None:
    """Configure root logging at ``log_level``, then floor the wire loggers at INFO.

    A FLOOR, not a fixed level: a quieter `LOG_LEVEL` (WARNING/ERROR) is left alone, so this only
    ever removes output. At the default `INFO` it is a no-op.
    """
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    for name in _WIRE_LOGGERS:
        wire_logger = logging.getLogger(name)
        if wire_logger.getEffectiveLevel() < logging.INFO:
            wire_logger.setLevel(logging.INFO)


# Configure logging
configure_logging(settings.LOG_LEVEL)

logger = logging.getLogger(__name__)

# A `StripPathPrefixMiddleware` ASGI class was deleted from here (E36/T14). It rewrote
# `scope["path"]` to remove `ROOT_PATH`, but it was never instantiated and never
# `add_middleware`-d — the only `add_middleware` call in this file is `CORSMiddleware` below. It
# was a decoy: it read as the mechanism that makes the stage prefix work, while the real mechanism
# is registering every router twice (bare and under `ROOT_PATH`) at the bottom of this file. Do not
# reintroduce it; stripping the prefix in middleware would make the doubled registrations dead and
# let the two spellings diverge silently.
# Pinned by tests/test_dead_auth_decoys_stay_deleted.py.

# Create FastAPI application
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json"
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_event():
    """
    Initialize application on startup
    """
    logger.info("Starting Control Plane API...")

    # Initialize database
    try:
        init_db()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise

    logger.info(f"{settings.APP_NAME} v{settings.APP_VERSION} started")


# === Prototype — Entra startup hook ===
# When AUTH_PROVIDER=entra, fetch the backend client secret from Secrets Manager
# (or from the literal env var in local dev) at startup so misconfiguration
# (missing ARN, missing IAM perm, blank env) surfaces in CloudWatch *immediately*
# rather than only on first Microsoft Graph call (E5+).
#
# E2-BE stance: log loud, don't crash. Inbound JWT validation doesn't need this
# secret — only outbound Graph calls in E5+ do. When we wire E5+, the hook's
# except block becomes a hard fail.
@app.on_event("startup")
async def load_entra_backend_secret_on_startup():
    from core.config import settings

    if settings.AUTH_PROVIDER != "entra":
        return

    import logging

    log = logging.getLogger(__name__)
    try:
        from core.secrets_loader import load_entra_backend_client_secret

        load_entra_backend_client_secret()
        log.info("[startup] Entra backend client secret loaded successfully")
    except Exception as exc:
        log.error("[startup] Failed to load Entra backend client secret: %s", exc)
        # Do not exit — for the E2-BE prototype, inbound JWT validation does not
        # need this secret, only outbound Graph calls (E5+). We log loud and let
        # the container start so /users/me works even if Graph is misconfigured.
        # When we wire E5+, this becomes a hard fail.


@app.on_event("shutdown")
async def shutdown_event():
    """
    Cleanup on application shutdown
    """
    logger.info("Shutting down Control Plane API...")


@app.get("/")
async def root():
    """
    Root endpoint

    Returns:
        API information
    """
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "status": "running"
    }


# Add stage-prefixed routes for API Gateway (e.g., /dev, /prod, /staging)
#
# A stage-prefixed `test` route was deliberately REMOVED here (E34/T9). It was unauthenticated
# and undocumented, and it echoed `ROOT_PATH` to anonymous callers — `ecs/main.tf` always sets
# `ROOT_PATH=/${environment}`, so it disclosed the deployment environment while falsifying the
# documented public surface (`/`, `/ping`, `/health`, `/docs`, `/openapi.json`, `/redoc`).
# `{ROOT_PATH}/ping`, served by the prefixed health router below, already covers reachability
# probing. Do not reintroduce it; pinned by test_health_no_internal_disclosure.py.
if settings.ROOT_PATH:
    @app.get(f"{settings.ROOT_PATH}/")
    async def root_stage():
        # Mirrors `/` exactly. It used to carry an extra `"ROOT_PATH"` key — the same anonymous
        # environment echo the `test` route above was deleted for, so it went too (E34/T9).
        return {
            "name": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "status": "running"
        }

    # There is deliberately NO stage-prefixed `health` route defined here (E34/T9).
    # `app.include_router(health_router, prefix=settings.ROOT_PATH)` below already serves
    # `{ROOT_PATH}/health` with the REAL probe logic. A hard-coded `{"status": "healthy"}`
    # route used to sit here and, being registered first, SHADOWED it — Starlette matches in
    # registration order. Consequences, both live: this path reported `healthy` straight
    # through a total database outage, and `health.py`'s probe logic never ran on it at all.
    # It is the path that matters most: API Gateway forwards the stage segment (`$default`
    # route, HTTP_PROXY, no `overwrite:path` mapping), so `{ROOT_PATH}/health` is the only
    # internet-reachable health URL — the ALB carrying bare `/health` is internal.
    #
    # `/ping` now follows the same rule (E36/T14): its stage twin used to sit in this block
    # returning `{"message": "pong"}` — a body the real route (`api/routes/health.py`, which
    # returns `{"ping": "pong", "timestamp": …}`) never produced — and, registered first, it
    # shadowed the prefixed router the same way. Both are gone; the prefixed router owns both
    # paths. Only `{ROOT_PATH}/` above stays hand-written, because bare `/` has no router to
    # fall back to.
    # Pinned by test_health_no_internal_disclosure.py.


def _with_cors(request: Request, response: JSONResponse) -> JSONResponse:
    """Re-attach CORS headers to an error response.

    Load-bearing for every handler below, not cosmetic: `CORSMiddleware` does not decorate a
    response produced by an exception handler, so without this the browser reports a bare CORS
    failure and the SPA never sees the status code or the body at all — an actionable message
    that cannot be read is the same as no message. Factored out (rather than duplicated) so a
    second handler cannot forget it.
    """
    origin = request.headers.get("origin")
    if origin and origin in settings.CORS_ORIGINS:
        response.headers["access-control-allow-origin"] = origin
        response.headers["access-control-allow-credentials"] = "true"
    return response


# === Registry resolution failures — 503 WITH the message (E32 fix) ===
#
# WHY THESE NEED THEIR OWN HANDLER. The registry id is no longer configured anywhere: AWS mints
# it, so the backend resolves AGENT_REGISTRY_NAME / MCP_REGISTRY_NAME to an id at first use. When
# that resolution fails, `core/registry_resolver` raises with a message that is the entire remedy
# — it names the registry, the region, `terraform apply`, the `ensure_registry.py --name` fallback
# and the settings involved. Falling through to `global_exception_handler` below DESTROYS exactly
# that: `DEBUG` defaults to False and is not set in the ECS task definition, so production
# returned `500 / "An error occurred"` and the message survived only in CloudWatch. The operator
# who trips this is the one person who can fix it, and they were the one person not being told.
#
# WHY 503 AND NOT 404. The request was fine; the PLATFORM is misconfigured. A 404 would say "that
# agent does not exist", sending the operator to look for missing data, when nothing about the
# catalog is wrong — the control plane simply cannot reach the registry yet. 503 is the honest
# reading of "a dependency this service needs is absent or ambiguous", and it is accurate about
# retryability too: the same request succeeds unchanged once the registry exists.
#
# WHY THIS DOES NOT WEAKEN THE GENERIC HANDLER. The secret-safety of `global_exception_handler` is
# untouched — every other exception still gets "An error occurred" unless DEBUG. Only these three
# resolver exceptions are exempted, and only because their messages are known in full at the point
# they are raised: a registry NAME, a region, and commands to run. No credentials, no ARNs, no
# account ids, no user data. That is why this is a per-type exemption rather than a DEBUG flip.
@app.exception_handler(RegistryNotFoundError)
@app.exception_handler(RegistryNotConfiguredError)
@app.exception_handler(AmbiguousRegistryNameError)
async def registry_resolution_exception_handler(request: Request, exc):
    """Return 503 carrying the resolver's own actionable message verbatim."""
    # Not exc_info=True: these are configuration states with a known cause, and the message
    # already says everything a traceback would obscure.
    logger.error(f"Registry resolution failed: {exc}")

    return _with_cors(
        request,
        JSONResponse(status_code=503, content={"detail": str(exc)}),
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc):
    """
    Global exception handler

    Args:
        request: FastAPI request
        exc: Exception

    Returns:
        JSON error response
    """
    logger.error(f"Unhandled exception: {exc}", exc_info=True)

    return _with_cors(
        request,
        JSONResponse(
            status_code=500,
            content={
                "detail": "Internal server error",
                "error": str(exc) if settings.DEBUG else "An error occurred"
            }
        ),
    )


# Import and include routers
from api.routes import health_router, users_router, guardrails_router, agents_router, mcp_servers_router, grants_router, entra_router, mcp_grants_router, agent_mcp_router, mcp_cedar_router, marketplace_router, governance_graph_router, users_admin_router, connections_router, projects_router, repositories_router, tenants_router, github_templates_router, builds_router, github_link_router, observability_router

# Include routers
app.include_router(health_router)
app.include_router(users_router, prefix=settings.API_PREFIX)
app.include_router(guardrails_router, prefix=settings.API_PREFIX)
app.include_router(agents_router, prefix=settings.API_PREFIX)
app.include_router(mcp_servers_router, prefix=settings.API_PREFIX)
app.include_router(grants_router, prefix=settings.API_PREFIX)
app.include_router(entra_router, prefix=settings.API_PREFIX)
app.include_router(mcp_grants_router, prefix=settings.API_PREFIX)
app.include_router(agent_mcp_router, prefix=settings.API_PREFIX)
app.include_router(mcp_cedar_router, prefix=settings.API_PREFIX)
app.include_router(marketplace_router, prefix=settings.API_PREFIX)
app.include_router(governance_graph_router, prefix=settings.API_PREFIX)
app.include_router(users_admin_router, prefix=settings.API_PREFIX)
app.include_router(connections_router, prefix=settings.API_PREFIX)
app.include_router(projects_router, prefix=settings.API_PREFIX)
app.include_router(repositories_router, prefix=settings.API_PREFIX)
app.include_router(tenants_router, prefix=settings.API_PREFIX)
app.include_router(github_templates_router, prefix=settings.API_PREFIX)
app.include_router(builds_router, prefix=settings.API_PREFIX)
app.include_router(github_link_router, prefix=settings.API_PREFIX)
app.include_router(observability_router, prefix=settings.API_PREFIX)

# Include routers with stage prefix for API Gateway (e.g., /dev, /prod)
if settings.ROOT_PATH:
    app.include_router(health_router, prefix=settings.ROOT_PATH)
    app.include_router(users_router, prefix=f"{settings.ROOT_PATH}{settings.API_PREFIX}")
    app.include_router(guardrails_router, prefix=f"{settings.ROOT_PATH}{settings.API_PREFIX}")
    app.include_router(agents_router, prefix=f"{settings.ROOT_PATH}{settings.API_PREFIX}")
    app.include_router(mcp_servers_router, prefix=f"{settings.ROOT_PATH}{settings.API_PREFIX}")
    app.include_router(grants_router, prefix=f"{settings.ROOT_PATH}{settings.API_PREFIX}")
    app.include_router(entra_router, prefix=f"{settings.ROOT_PATH}{settings.API_PREFIX}")
    app.include_router(mcp_grants_router, prefix=f"{settings.ROOT_PATH}{settings.API_PREFIX}")
    app.include_router(agent_mcp_router, prefix=f"{settings.ROOT_PATH}{settings.API_PREFIX}")
    app.include_router(mcp_cedar_router, prefix=f"{settings.ROOT_PATH}{settings.API_PREFIX}")
    app.include_router(marketplace_router, prefix=f"{settings.ROOT_PATH}{settings.API_PREFIX}")
    app.include_router(governance_graph_router, prefix=f"{settings.ROOT_PATH}{settings.API_PREFIX}")
    app.include_router(users_admin_router, prefix=f"{settings.ROOT_PATH}{settings.API_PREFIX}")
    app.include_router(connections_router, prefix=f"{settings.ROOT_PATH}{settings.API_PREFIX}")
    app.include_router(projects_router, prefix=f"{settings.ROOT_PATH}{settings.API_PREFIX}")
    app.include_router(repositories_router, prefix=f"{settings.ROOT_PATH}{settings.API_PREFIX}")
    app.include_router(tenants_router, prefix=f"{settings.ROOT_PATH}{settings.API_PREFIX}")
    app.include_router(github_templates_router, prefix=f"{settings.ROOT_PATH}{settings.API_PREFIX}")
    app.include_router(builds_router, prefix=f"{settings.ROOT_PATH}{settings.API_PREFIX}")
    app.include_router(github_link_router, prefix=f"{settings.ROOT_PATH}{settings.API_PREFIX}")
    app.include_router(observability_router, prefix=f"{settings.ROOT_PATH}{settings.API_PREFIX}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower()
    )
