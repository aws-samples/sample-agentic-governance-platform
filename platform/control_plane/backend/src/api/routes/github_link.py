"""Per-user GitHub account link API (E27B/T8).

A human authorizes AGP to act on GitHub **as them**; these five routes are the whole surface.
Structural clone of ``connections.py``: the lazy ``_svc`` / :func:`get_github_link_service`
singleton (tests patch ``_svc`` directly, so this never runs against live AWS), and the
FIXED-``detail`` convention in :func:`_raise_link_error` — **never** ``str(err)``, so a raw
Secrets Manager / DynamoDB / GitHub message can never reach the client.

RBAC: every route is ``require_role(Role.VIEWER)``. Linking your own GitHub account is a
per-user action any authenticated human may take on their OWN link, not a privileged
operation — and the subject is taken from ``principal.oid``, the validated Entra claim,
**never** from a body, a query param, or a path param. A falsy ``oid`` (the dev-auth path has
none) is a ``bad_request``: a link with no Entra subject is unattributable, which is the one
thing this feature exists to provide.

NO ROUTE HERE RETURNS 401. The SPA's api-client interceptor turns a 401 into
``removeItem('auth_token') + reload()``, which would log the human out instead of showing an
error, so every failure status is drawn from ``{400, 404, 409, 502}``. Two of those are
RETRYABLE rather than terminal and are deliberately not collapsed into a terminal kind:
``refresh_in_progress`` (409) is ordinary two-ECS-task contention on the claim-then-rotate
guard, and ``secret_error`` (502) is a transient DynamoDB/Secrets Manager blip. Both mean
"try again in a moment"; reporting either as ``link_revoked`` would send the human through a
whole re-authorization over a 200 ms race.

THE ROUTE OWNS ``org``. ``GitHubUserLink`` deliberately carries no ``org`` — the link service
is connection-unaware by design (it takes an injected credential loader and never loads a
``Connection``) — so the two surfaces are composed HERE, the precedent ``connections.py``'s
delete guard already sets. ``GET ""`` joins ``list_connections()`` with
``list_for_principal(oid)``; a link whose connection no longer exists is skipped rather than
failing the whole view.

A COMMITTED WRITE IS ALWAYS REPORTED AS SUCCESS. The ``org`` lookup is a second store read, so
it can fail on its own — and if it runs after the link row and the token secret are written,
its failure would answer a TERMINAL error for a link that is live at GitHub (the callback page
even says "Nothing was changed"). So ``/verify`` resolves ``org`` BEFORE it mutates — the
repo's resolve-inputs-before-mutating discipline, cf. ``marketplace_service._apply_grant`` and
``project_service.promote_repo`` — while ``/callback``, whose ``connection_id`` is only known
once the stored state is consumed and so CANNOT hoist the lookup, degrades instead: see
:func:`_org_for_committed_link`.

``linked`` IS ``status != UNLINKED``, not ``status == LINKED``. The console's
``deriveLinkCardState`` renders ``linked === false`` as the "your authorization was revoked —
reconnect" card, and a row that is momentarily ``REFRESHING`` has a perfectly good
authorization; telling that human to re-authorize would be wrong and destructive.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Response
from fastapi import Depends as RBACDepends

from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
from models.connection import Provider
from models.github_link import (
    GitHubLinkStatus,
    GitHubLinkView,
    GitHubUserLink,
    LinkCallbackRequest,
    LinkStartRequest,
    LinkStartResponse,
    LinkStatus,
    LinkableConnection,
)
from services.connection_service import ConnectionError
from services.github_user_link import GitHubLinkError, GitHubUserLinkService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/me/github-link", tags=["github-link"])

_svc: Optional[GitHubUserLinkService] = None

# GitHubLinkError.kind → HTTP status. NO 401 anywhere (see the module docstring).
# ``refresh_in_progress`` and ``secret_error`` are RETRYABLE, not terminal.
_ERROR_STATUS = {
    "not_found": 404,
    "bad_request": 400,
    "conflict": 409,
    "oauth_client_missing": 409,
    "refresh_in_progress": 409,
    "link_revoked": 409,
    "provider_error": 502,
    "secret_error": 502,
}
# FIXED detail literals keyed off the same ``.kind`` — never ``str(err)``, which could carry a
# store message. No literal here names a token, a client secret, or a provider body.
_ERROR_DETAIL = {
    "not_found": "GitHub link not found",
    "bad_request": "Invalid request",
    "conflict": "That GitHub account is already linked to another user",
    "oauth_client_missing": "This org connection has no GitHub OAuth client — ask an admin to add one",
    "refresh_in_progress": "GitHub token refresh in progress — retry",
    "link_revoked": "Your GitHub authorization was revoked — reconnect your account",
    "provider_error": "GitHub request failed",
    "secret_error": "Secret store operation failed",
}

# ``ConnectionError.kind`` values the ``org`` lookup can raise, mapped through the SAME status
# table so a vanished or unreadable connection answers in this route's vocabulary.
_CONNECTION_KIND_MAP = {"not_found": "not_found", "secret_error": "secret_error"}


def get_github_link_service() -> GitHubUserLinkService:
    """Lazy :class:`GitHubUserLinkService` singleton built from ``settings``.

    ``table_name`` is the SAME ``CONNECTIONS_TABLE_NAME`` — the link rows and their CSRF/PKCE
    state are new PARTITIONS in the existing table, which needs zero terraform where a new
    table would need an ARN added to the ECS task's explicit DDB allowlist. Empty ⇒ the
    service's in-memory fallback. Tests patch ``_svc`` directly so this never runs live.

    ``secret_prefix`` IS LOAD-BEARING AND VALIDATED HERE. Every per-user token secret is named
    ``<prefix><link_id>``, so an empty prefix would write bare-UUID secrets: outside every
    ``agp-dev/*`` IAM condition and lifecycle rule, and with dev and prod sharing one flat
    namespace. Nothing downstream would complain — the ECS task's Secrets Manager grant is
    ``Resource = "*"`` — so the miss would be silent and only visible as a mess in the account.
    Hence the explicit refusal: a blanked env var fails LOUDLY, as a retryable ``secret_error``
    (502) rather than a 500, and never as a quietly-mislocated secret. It is raised from the
    factory rather than validated on ``Settings`` so one unset feature flag cannot stop the
    whole app from booting.

    ``allowed_origins`` is ``CORS_ORIGINS`` — the same list the API already accepts a browser
    from, which is exactly the set of origins a console callback can legitimately live on. It
    must be NON-EMPTY: the service fails OPEN without it (path and scheme checked, host not),
    leaving GitHub's own registered-callback match as the only thing between a user-supplied
    host and an authorization code.

    ``client_credentials_loader`` is ``ConnectionService.get_oauth_client_credentials`` — the
    seam that keeps the link service connection-unaware. Imported locally to avoid a
    module-load cycle, the idiom ``connections.py``'s delete guard already uses.
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
            raise GitHubLinkError(
                "GitHub user-link storage is not configured", kind="secret_error"
            )
        origins = tuple(settings.CORS_ORIGINS)
        if not origins:
            logger.error(
                "[github-link] CORS_ORIGINS is empty, so a redirect_uri's ORIGIN cannot be "
                "checked server-side"
            )
        from api.routes.connections import get_connection_service

        _svc = GitHubUserLinkService(
            table_name=settings.CONNECTIONS_TABLE_NAME,
            secret_prefix=prefix,
            region=settings.AWS_REGION,
            allowed_origins=origins,
            client_credentials_loader=get_connection_service().get_oauth_client_credentials,
        )
    return _svc


def _raise_link_error(err: GitHubLinkError) -> None:
    """Map a :class:`GitHubLinkError` to an ``HTTPException`` with a FIXED detail literal.

    Never ``str(err)``: the service's messages are safe by construction, but re-using them is
    how provider and store text starts leaking into HTTP bodies. Only ``.kind`` crosses."""
    status = _ERROR_STATUS.get(err.kind, 400)
    detail = _ERROR_DETAIL.get(err.kind, "GitHub link operation failed")
    raise HTTPException(status_code=status, detail=detail)


def _require_oid(principal: Principal) -> str:
    """The subject, from the VALIDATED claims only. A falsy ``oid`` is a ``bad_request``: a
    link with no Entra subject cannot be attributed to a human."""
    if not principal.oid:
        _raise_link_error(GitHubLinkError("a signed-in Entra user is required", kind="bad_request"))
    return principal.oid


def _svc_or_error() -> GitHubUserLinkService:
    """Build the singleton, translating a wiring refusal into this route's status map instead
    of a 500 from the global exception handler."""
    try:
        return get_github_link_service()
    except GitHubLinkError as err:
        _raise_link_error(err)


def _org_for(connection_id: str) -> str:
    """The connection's ``org`` — the one field the link service does not carry.

    Only ever called BEFORE a mutation, so raising here is honest: nothing was written."""
    from api.routes.connections import get_connection_service

    try:
        return get_connection_service().get_connection(connection_id).org
    except ConnectionError as err:
        _raise_link_error(
            GitHubLinkError(
                "could not resolve the org connection",
                kind=_CONNECTION_KIND_MAP.get(err.kind, "provider_error"),
            )
        )


def _org_for_committed_link(connection_id: str) -> str:
    """``org`` for a link that is ALREADY WRITTEN — never raises.

    ``/callback`` cannot resolve the org before mutating: the ``connection_id`` lives in the
    stored PKCE/CSRF state and is only known once ``complete_link`` returns. So the lookup
    happens after the secret and the row are committed, and a failure there must NOT become
    the answer: a 404 ("GitHub link not found") on the callback page is TERMINAL, and its copy
    says "Nothing was changed" — the exact opposite of the truth for a link that is live at
    GitHub. An admin deleting the connection mid-flow, or a DDB blip (``ConnectionService._get``
    collapses a store fault to ``not_found``), would send the human off to burn a second
    authorization over a link that already worked.

    So the degraded answer is an empty ``org``. Nothing consumes it on the callback page — the
    success copy is keyed off ``github_login`` — and the human's next visit to the link page
    re-joins ``list_connections()`` and shows the real org. A committed write is always
    reported as the success it is."""
    from api.routes.connections import get_connection_service

    try:
        return get_connection_service().get_connection(connection_id).org
    except ConnectionError:
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
        # A REFRESHING row still holds a good authorization — see the module docstring.
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
    """This human's links plus every GitHub connection they could link on.

    Composed at the ROUTE level because the link service is deliberately connection-unaware.
    A link pointing at a connection that no longer exists is SKIPPED — the stale row is a
    cleanup concern, not a reason to fail the human's whole view."""
    oid = _require_oid(principal)
    svc = _svc_or_error()
    from api.routes.connections import get_connection_service

    try:
        connections = [
            c for c in get_connection_service().list_connections() if c.provider == Provider.GITHUB
        ]
    except ConnectionError as err:
        _raise_link_error(
            GitHubLinkError(
                "could not list the org connections",
                kind=_CONNECTION_KIND_MAP.get(err.kind, "provider_error"),
            )
        )
    orgs = {c.id: c.org for c in connections}

    try:
        links = svc.list_for_principal(oid)
    except GitHubLinkError as err:
        _raise_link_error(err)

    return GitHubLinkView(
        links=[_status_view(l, orgs[l.connection_id]) for l in links if l.connection_id in orgs],
        connections=[
            LinkableConnection(
                connection_id=c.id, org=c.org, oauth_client_ready=c.has_oauth_client
            )
            for c in connections
        ],
    )


@router.post("/start", response_model=LinkStartResponse)
async def start_github_link(
    body: LinkStartRequest,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """Begin the web flow: mint PKCE + CSRF state and return the GitHub authorize URL.

    The subject is ``principal.oid``; nothing in ``body`` can name a different human."""
    oid = _require_oid(principal)
    svc = _svc_or_error()
    try:
        authorize_url, state = svc.begin_link(oid, body.connection_id, body.redirect_uri)
    except GitHubLinkError as err:
        _raise_link_error(err)
    return LinkStartResponse(authorize_url=authorize_url, state=state)


@router.post("/callback", response_model=GitHubLinkStatus)
async def complete_github_link(
    body: LinkCallbackRequest,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """Finish the web flow: exchange the one-time ``code`` and persist the link.

    The ``redirect_uri`` and PKCE verifier come from the stored state, never from this
    request — that pairing is what binds the callback to the authorize call."""
    oid = _require_oid(principal)
    svc = _svc_or_error()
    try:
        link = svc.complete_link(oid, body.code, body.state)
    except GitHubLinkError as err:
        _raise_link_error(err)
    # AFTER the write, so the org lookup is NON-FATAL — see ``_org_for_committed_link``. The
    # ``connection_id`` only exists once the stored state has been consumed, so this one
    # cannot be resolved before mutating.
    return _status_view(link, _org_for_committed_link(link.connection_id))


@router.post("/{connection_id}/verify", response_model=GitHubLinkStatus)
async def verify_github_link(
    connection_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """Probe GitHub and refresh the stored login. A revoked authorization answers 409 with the
    re-link prompt rather than a 401 (which would log the human out of AGP entirely)."""
    oid = _require_oid(principal)
    svc = _svc_or_error()
    # RESOLVE BEFORE MUTATING (the repo's discipline — cf. ``marketplace_service._apply_grant``,
    # ``project_service.promote_repo``). ``verify_link`` writes: it may rotate the token, and it
    # persists either the refreshed login or an UNLINKED row. Resolving ``org`` afterwards meant
    # a vanished connection or a DDB blip turned a completed write into a terminal error. Here
    # the ``connection_id`` is a path param, so the lookup CAN come first — and a failure at
    # this point is honest, because nothing has been written yet.
    org = _org_for(connection_id)
    try:
        link = svc.verify_link(oid, connection_id)
    except GitHubLinkError as err:
        _raise_link_error(err)
    return _status_view(link, org)


@router.delete("/{connection_id}", status_code=204)
async def unlink_github_link(
    connection_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """Revoke at GitHub, then purge locally. A failed revoke is reported (retryable) rather
    than silently leaving a live grant the human believes is gone."""
    oid = _require_oid(principal)
    svc = _svc_or_error()
    try:
        svc.unlink(oid, connection_id)
    except GitHubLinkError as err:
        _raise_link_error(err)
    return Response(status_code=204)
