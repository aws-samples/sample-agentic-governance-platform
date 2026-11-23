"""Per-user GitHub account link API: the five routes through which a human authorizes AGP to act on
GitHub as them.

Every route is ``require_role(Role.VIEWER)``: linking your own account is a per-user action, and the
subject is ``principal.oid``, the validated Entra claim, never a body, query or path value; a falsy
``oid`` (the dev-auth path) is a ``bad_request``. No route answers 401, which the SPA's interceptor
would turn into a sign-out; every failure renders through ``GitHubLinkError``'s or
``ConnectionError``'s table.

The link service is connection-unaware: it resolves a link's ``org`` through the loader this module
injects (``connection_org``), and the view joins ``list_connections()`` with
``list_for_principal(oid)``, skipping a link whose connection no longer exists. ``/verify`` resolves
``org`` before it mutates, so a vanished connection is an honest 404 with nothing written;
``/callback`` learns the ``connection_id`` only once the stored state is consumed, so its lookup runs
after the write and degrades to an empty ``org`` rather than report a committed link as a failure.
``linked`` is ``status != UNLINKED``: a ``REFRESHING`` row still holds a good authorization.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Response
from fastapi import Depends as RBACDepends

from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
from models.connection import Provider
from models.github_link import (
    GitHubLinkStatus,
    GitHubLinkView,
    GitHubUserLink,
    LinkableConnection,
    LinkCallbackRequest,
    LinkStartRequest,
    LinkStartResponse,
    LinkStatus,
)
from services.github_user_link import GitHubLinkError, GitHubUserLinkService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/me/github-link", tags=["github-link"])

_svc: Optional[GitHubUserLinkService] = None


def get_github_link_service() -> GitHubUserLinkService:
    """Lazy :class:`GitHubUserLinkService` singleton built from ``settings``.

    The link rows and their CSRF/PKCE state are partitions of ``CONNECTIONS_TABLE_NAME``. An empty
    ``GITHUB_USER_LINK_SECRET_PREFIX`` is refused as a retryable ``secret_error``: every per-user
    token secret is named ``<prefix><link_id>``, so an empty prefix would write bare-UUID secrets
    outside every IAM condition and lifecycle rule, and nothing downstream would complain.
    ``allowed_origins`` is ``CORS_ORIGINS``, the set of origins a console callback can live on;
    the service fails open without it, so an empty list is logged. The two loaders are
    ``ConnectionService.get_oauth_client_credentials`` and a read of ``get_connection(...).org``,
    imported locally to avoid a module-load cycle. Tests patch ``_svc`` directly.
    """
    global _svc
    if _svc is None:
        prefix = settings.GITHUB_USER_LINK_SECRET_PREFIX
        if not prefix:
            logger.error(
                "[github-link] GITHUB_USER_LINK_SECRET_PREFIX is empty; refusing to store "
                "per-user GitHub tokens at a bare-UUID secret name (outside every agp-dev/* "
                "IAM condition and lifecycle rule, with dev and prod sharing one namespace)"
            )
            raise GitHubLinkError("GitHub user-link storage is not configured", kind="secret_error")
        origins = tuple(settings.CORS_ORIGINS)
        if not origins:
            logger.error(
                "[github-link] CORS_ORIGINS is empty, so a redirect_uri's ORIGIN cannot be "
                "checked server-side"
            )
        from api.routes.connections import get_connection_service

        connections = get_connection_service()
        _svc = GitHubUserLinkService(
            table_name=settings.CONNECTIONS_TABLE_NAME,
            secret_prefix=prefix,
            region=settings.AWS_REGION,
            allowed_origins=origins,
            client_credentials_loader=connections.get_oauth_client_credentials,
            connection_org_loader=lambda connection_id: connections.get_connection(connection_id).org,
        )
    return _svc


def _require_oid(principal: Principal) -> str:
    """The subject, from the validated claims only; a falsy ``oid`` is a ``bad_request``, since a
    link with no Entra subject cannot be attributed to a human."""
    if not principal.oid:
        raise GitHubLinkError("a signed-in Entra user is required", kind="bad_request")
    return principal.oid


def _org_for_committed_link(svc: GitHubUserLinkService, connection_id: str) -> str:
    """``org`` for a link that is already written; never raises.

    The callback page renders a 404 as terminal with copy saying nothing was changed, the opposite
    of the truth for a link that is live at GitHub, so a connection deleted mid-flow or a store
    blip degrades to an empty ``org``. Nothing on the callback page consumes it, and the next visit
    to the link page re-joins ``list_connections()``."""
    try:
        return svc.connection_org(connection_id)
    except GitHubLinkError:
        logger.warning(
            "[github-link] link for connection %s is stored, but its org could not be "
            "resolved for the response; answering success with an empty org",
            connection_id,
        )
        return ""


def _status_view(link: GitHubUserLink, org: str) -> GitHubLinkStatus:
    return GitHubLinkStatus(
        connection_id=link.connection_id,
        org=org,
        linked=link.status != LinkStatus.UNLINKED,
        status=link.status.value,
        github_login=link.github_login,
        last_verified_at=link.last_verified_at,
    )


@router.get("", response_model=GitHubLinkView)
async def get_github_link_view(
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """This human's links plus every GitHub connection they could link on. A link pointing at a
    connection that no longer exists is skipped rather than failing the whole view."""
    oid = _require_oid(principal)
    svc = get_github_link_service()
    from api.routes.connections import get_connection_service

    connections = [c for c in get_connection_service().list_connections() if c.provider == Provider.GITHUB]
    orgs = {c.id: c.org for c in connections}
    links = svc.list_for_principal(oid)
    return GitHubLinkView(
        links=[_status_view(link, orgs[link.connection_id]) for link in links if link.connection_id in orgs],
        connections=[
            LinkableConnection(connection_id=c.id, org=c.org, oauth_client_ready=c.has_oauth_client)
            for c in connections
        ],
    )


@router.post("/start", response_model=LinkStartResponse)
async def start_github_link(
    body: LinkStartRequest,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """Begin the web flow: mint PKCE and CSRF state and return the GitHub authorize URL. The
    subject is ``principal.oid``; nothing in ``body`` can name a different human."""
    oid = _require_oid(principal)
    svc = get_github_link_service()
    authorize_url, state = svc.begin_link(oid, body.connection_id, body.redirect_uri)
    return LinkStartResponse(authorize_url=authorize_url, state=state)


@router.post("/callback", response_model=GitHubLinkStatus)
async def complete_github_link(
    body: LinkCallbackRequest,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """Finish the web flow: exchange the one-time ``code`` and persist the link. The
    ``redirect_uri`` and PKCE verifier come from the stored state, never from this request."""
    oid = _require_oid(principal)
    svc = get_github_link_service()
    link = svc.complete_link(oid, body.code, body.state)
    return _status_view(link, _org_for_committed_link(svc, link.connection_id))


@router.post("/{connection_id}/verify", response_model=GitHubLinkStatus)
async def verify_github_link(
    connection_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """Probe GitHub and refresh the stored login. ``org`` is resolved before ``verify_link``
    writes, so a vanished connection answers with nothing mutated."""
    oid = _require_oid(principal)
    svc = get_github_link_service()
    org = svc.connection_org(connection_id)
    link = svc.verify_link(oid, connection_id)
    return _status_view(link, org)


@router.delete("/{connection_id}", status_code=204)
async def unlink_github_link(
    connection_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """Revoke at GitHub, then purge locally; a failed revoke is reported as retryable rather than
    leaving a live grant the human believes is gone."""
    oid = _require_oid(principal)
    svc = get_github_link_service()
    svc.unlink(oid, connection_id)
    return Response(status_code=204)
