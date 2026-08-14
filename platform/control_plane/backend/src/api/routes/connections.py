"""Git org Connections admin API (Epic 19 + E20/T9 GitHub App auth).

Admin-gated CRUD over Git org connections. Structural clone of ``marketplace.py``: the
lazy ``_svc`` / ``get_connection_service()`` singleton (tests patch ``_svc`` directly so
this never runs against live AWS/Secrets Manager), and the FIXED-``detail`` convention in
``_raise_connection_error`` — NEVER ``str(err)``, so a raw DDB/SM ``ClientError`` can never
leak its message to the client (App-auth errors are mapped the same way).

``POST /admin/connections`` (``ConnectionCreate``) supports two auth types; the required
credential fields depend on ``auth_type`` (validated by the model):

  - ``pat`` (default): ``token`` required. Secret stored: ``{"token": <PAT>}``.
  - ``github_app``: ``app_id`` + ``installation_id`` + ``private_key`` (PEM) required.
    ``app_id``/``installation_id`` are NON-secret (returned on the read model); the private
    key is stored in Secrets Manager only. AGP mints a fresh installation token per
    operation. Operator setup runbook: ``platform/control_plane/docs/github-app-connection.md``.

No credential (PAT or private key) ever crosses the read boundary: the service returns
``Connection`` (no credential field), credentials flow in only through the write-only
``ConnectionCreate``/``ConnectionTokenReplace`` bodies. ``created_by`` is taken from the
validated ``principal.email``, NEVER from the body.

RBAC: connections are a trust-boundary surface. Operators CONSUME it (the ``GET`` list is
``require_role(Role.OPERATOR)`` so they can pick a connection + resolve org names when
provisioning projects — the read model carries NO secret); only admins MANAGE it, so every
mutation (POST create, POST /{id}/test, PUT /{id}/token, PUT /{id}/key, PUT /{id}/oauth-client,
manifest/start, manifest/callback, /{id}/finalize, DELETE) stays ``require_role(Role.ADMIN)``.

THE ONE-TIME PER-ORG CLIENT-SECRET PASTE (E27B). ``PUT /{id}/oauth-client`` exists because GitHub
exposes an App's OAuth ``client_secret`` through NO API once the App has been created — it is shown
exactly once, in the org-admin UI, and is thereafter unrecoverable programmatically. AGP now captures
the pair automatically at manifest conversion, but an org whose App predates that (every existing
installation) can only enable per-user linking if an admin pastes the pair in once. The non-secret
half IS recoverable (``GET /app``), which is what the paste is verified against before anything is
stored. Hence the shape: a write-only body, verify-then-write in the service, and the ordinary
``Connection`` read model as the response — so the pasted secret is checked, stored, and never echoed.
GitHub Enterprise is refused here rather than stored: the OAuth authorize/token legs are
github.com-only (a web base cannot be derived from an API base), so a stored GHE pair would be a
one-shot paste into a surface that can never read it back.
"""

import logging
from typing import List, Optional
from urllib.parse import urlsplit, urlunsplit

import boto3
from fastapi import APIRouter, HTTPException, Response
from fastapi import Depends as RBACDepends
from pydantic import BaseModel

from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
from models.connection import (
    Connection,
    ConnectionCreate,
    ConnectionFinalize,
    ConnectionKeyReplace,
    ConnectionOAuthClient,
    ConnectionTokenReplace,
    ManifestCallback,
    ManifestCallbackResponse,
    ManifestStart,
    ManifestStartResponse,
    Provider,
)
from services.connection_service import ConnectionError, ConnectionService
from services.ecr_push_role_service import EcrPushRoleService
from services.github_app_manifest import build_manifest, register_url
from services.github_oidc_provider_service import (
    GitHubOidcProviderService,
    resolve_github_oidc_provider_arn,
)
from services.github_repo_service import GitHubRepoError, GitHubRepoService
from services.github_template_service import TemplateView
from services.template_registry import TemplateRegistry
from services.template_rollout_service import RolloutError, RolloutService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/connections", tags=["connections"])

_svc: Optional[ConnectionService] = None
_rollout_svc: Optional[RolloutService] = None

# ConnectionError.kind → HTTP status. FIXED detail literals (the secret-leak guard) keyed
# off the same hint — never str(err), which could carry a DDB/Secrets Manager message.
# ``verify_failed`` is the lone kind whose detail is the err.message (a curated §4 reason,
# safe to surface).
_ERROR_STATUS = {
    "verify_failed": 400,
    "not_found": 404,
    "conflict": 409,
    "secret_error": 502,
    "bad_request": 400,
}
_ERROR_DETAIL = {
    "not_found": "Connection not found",
    "conflict": "Connection already exists",
    "secret_error": "Secret store operation failed",
    "bad_request": "Invalid request",
}


def get_connection_service() -> ConnectionService:
    """Lazy ``ConnectionService`` singleton built from ``settings``.

    Empty ``CONNECTIONS_TABLE_NAME`` ⇒ the service's in-memory fallback. Tests patch
    ``_svc`` directly with a fake service so this never runs against live AWS.
    """
    global _svc
    if _svc is None:
        # One resolved provider ARN feeds BOTH collaborators: the bootstrap service creates
        # the provider it names, and the role service uses it as the Federated principal.
        # Resolving it twice could hand them different answers.
        oidc_provider_arn = _resolve_oidc_provider_arn()
        _svc = ConnectionService(
            table_name=settings.CONNECTIONS_TABLE_NAME,
            secret_prefix=settings.CONNECTIONS_SECRET_PREFIX,
            region=settings.AWS_REGION,
            ecr_push_role_service=_build_ecr_push_role_service(oidc_provider_arn),
            github_oidc_provider_service=_build_github_oidc_provider_service(oidc_provider_arn),
        )
    return _svc


def _resolve_oidc_provider_arn() -> str:
    """``GITHUB_OIDC_PROVIDER_ARN``, or the deterministic ARN derived from the account.

    Terraform passes the deterministic string (it has no provider RESOURCE to reference any
    more — the platform creates that on the first GitHub connection), so the env is normally
    set. The STS-derived fallback covers an env wired before that change and a bare local
    shell, so nothing downstream has to branch on whether the var was populated. Called from
    the lazy singleton, never at import — ``settings`` is built at import time and an STS
    call there would make module import depend on AWS reachability."""
    return resolve_github_oidc_provider_arn(
        settings.GITHUB_OIDC_PROVIDER_ARN, region=settings.AWS_REGION
    )


def _build_ecr_push_role_service(oidc_provider_arn: str) -> EcrPushRoleService:
    """Per-org GitHub-OIDC ECR-push role provisioner (E22 multi-org). Inert (no-op) unless
    all three wiring values are set — so an unconfigured env still allows connections and
    just falls back to the platform-default push role on materialized repos."""
    return EcrPushRoleService(
        role_name_prefix=settings.ECR_PUSH_ROLE_NAME_PREFIX,
        oidc_provider_arn=oidc_provider_arn,
        ecr_repository_arn=settings.AGENT_IMAGES_ECR_ARN,
        region=settings.AWS_REGION,
    )


def _build_github_oidc_provider_service(oidc_provider_arn: str) -> GitHubOidcProviderService:
    """Account-global GitHub Actions OIDC provider bootstrap. Terraform ships no GitHub
    artifacts, so the FIRST GitHub connection creates the provider if the account lacks it.
    Inert when the ARN could not be resolved."""
    return GitHubOidcProviderService(
        provider_arn=oidc_provider_arn,
        region=settings.AWS_REGION,
    )


def _materialized_repo_names(connection_id: str) -> set:
    """The names of the AGENT repos AGP materialized for ``connection_id``.

    The reconcile surface subtracts these from the adopt picker (D-C3): a materialized agent repo
    is something AGP already accounts for, and offering one for adoption would invite an operator
    to register a live agent's repository as a template — after which every materialize from it
    would ship that agent's code.

    Composed at the ROUTE level, like the delete guard below, and the import is local for the same
    reason: ``project_service`` depends on connections (a project names one), so a service-level
    collaborator here would be a cycle. A repo's connection is reached through its project, since
    the repository record names only its ``project_id``.

    This RAISES on a store failure rather than returning an empty set — the reconcile service
    fails closed on it, because an under-subtracted picker is the failure this exists to prevent.
    """
    from api.routes.projects import get_project_service

    project_svc = get_project_service()
    project_ids = {
        p.id for p in project_svc.list_projects() if p.connection_id == connection_id
    }
    return {
        r.name for r in project_svc.list_repositories() if r.project_id in project_ids
    }


def get_rollout_service() -> RolloutService:
    """Lazy ``RolloutService`` singleton built from ``settings``.

    Seed sources: ``AGENT_TEMPLATES_DIR`` (base-template scaffolds, shipped in the image)
    and the ``agentcore_runtime`` Terraform module zip staged in S3
    (``RUNTIME_MODULE_BUCKET`` / ``RUNTIME_MODULE_KEY``, uploaded by ``terraform apply``).
    The image does NOT ship the ``infrastructure/`` tree, so the infra rollout downloads
    the module from S3 via a boto3 client rather than reading a local dir. Shares the
    connection service so both surfaces resolve one org/token seam. Tests patch
    ``_rollout_svc`` directly so this never runs live.

    ``GitHubRepoService`` is injected as the ``RepoProvider`` (E28C/T1): reconcile uses its three
    reads and the seed push uses ``create_repo``/``commit_files``. No ``ZipService`` any more —
    E28C/T3 deleted the dict→zip→dict round trip along with the destructive overwrite it served.
    """
    global _rollout_svc
    if _rollout_svc is None:
        _rollout_svc = RolloutService(
            GitHubRepoService(),
            get_connection_service(),
            agent_templates_dir=settings.AGENT_TEMPLATES_DIR,
            s3_client=boto3.client("s3", region_name=settings.AWS_REGION),
            runtime_module_bucket=settings.RUNTIME_MODULE_BUCKET,
            runtime_module_key=settings.RUNTIME_MODULE_KEY,
            # The template catalog (E28B/T2) — the `template` partition of the EXISTING
            # projects table. A rolled-out scaffold becomes a template by being registered
            # here, now that the GitHub-only `is_template` flip is gone.
            template_registry=TemplateRegistry(
                table_name=settings.PROJECTS_TABLE_NAME,
                region=settings.AWS_REGION,
            ),
            known_repo_names=_materialized_repo_names,
        )
    return _rollout_svc


# RolloutError.kind → HTTP status + FIXED detail literal (same secret-leak guard).
# "validation" → 422: malformed caller input (a bad connection id / template name) is
# PERMANENT, so it must not share the retryable 502 that a genuine rollout fault gets.
#
# E28C/T3 adds the two kinds the adopt verb needs, and both are DISTINCT facts rather than
# convenient reuses of what was already here:
#   repo_not_found — the REPO is not in the org. Answering the rollout path's "Unknown base
#                    template" would point an operator who mistyped a repo name at AGP's seed
#                    list, which has nothing to do with their mistake.
#   conflict       — already registered. There was no 409 kind at all, so "this is already a
#                    template" had nowhere honest to land and would have arrived as a 400.
_ROLLOUT_ERROR_STATUS = {
    "not_found": 404,
    "repo_not_found": 404,
    "conflict": 409,
    "rollout_error": 502,
    "validation": 422,
}
_ROLLOUT_ERROR_DETAIL = {
    "not_found": "Unknown base template",
    "repo_not_found": "Repository not found in the org",
    "conflict": "Template already registered",
    "rollout_error": "Template rollout failed",
    "validation": "Invalid template name or connection id",
}


def _raise_rollout_error(err: RolloutError) -> None:
    status = _ROLLOUT_ERROR_STATUS.get(err.kind, 400)
    detail = _ROLLOUT_ERROR_DETAIL.get(err.kind, "Template rollout failed")
    raise HTTPException(status_code=status, detail=detail)


def _raise_connection_error(err: ConnectionError) -> None:
    """Map a ConnectionError to an HTTPException with a FIXED detail literal.

    ``verify_failed`` surfaces the curated ``err.message`` (the §4 reason — safe); every
    other kind uses a fixed literal so a raw store/exception value never reaches the client.
    """
    status = _ERROR_STATUS.get(err.kind, 400)
    if err.kind == "verify_failed":
        detail = err.message
    else:
        detail = _ERROR_DETAIL.get(err.kind, "Connection operation failed")
    raise HTTPException(status_code=status, detail=detail)


@router.get("", response_model=List[Connection])
async def list_connections(
    principal: Principal = RBACDepends(current_principal),
    # OPERATOR-read: operators consume the trust boundary (pick a connection + resolve
    # org names to provision projects). The read model carries NO secret. Mutations below
    # stay ADMIN.
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    svc = get_connection_service()
    return svc.list_connections()


@router.post("", response_model=Connection, status_code=201)
async def create_connection(
    body: ConnectionCreate,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    svc = get_connection_service()
    try:
        return svc.create_connection(body, created_by=principal.email)
    except ConnectionError as err:
        _raise_connection_error(err)


@router.post("/{id}/test", response_model=Connection)
async def test_connection(
    id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    svc = get_connection_service()
    try:
        return svc.test_connection(id)
    except ConnectionError as err:
        _raise_connection_error(err)


@router.put("/{id}/token", response_model=Connection)
async def replace_token(
    id: str,
    body: ConnectionTokenReplace,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    svc = get_connection_service()
    try:
        return svc.replace_token(id, body.token)
    except ConnectionError as err:
        _raise_connection_error(err)


@router.put("/{id}/key", response_model=Connection)
async def replace_key(
    id: str,
    body: ConnectionKeyReplace,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    svc = get_connection_service()
    try:
        return svc.replace_key(id, body.private_key)
    except ConnectionError as err:
        _raise_connection_error(err)


@router.put("/{id}/oauth-client", response_model=Connection)
async def set_connection_oauth_client(
    id: str,
    body: ConnectionOAuthClient,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """Store the App's OAuth client pair so humans in this org can link their GitHub accounts.

    The one-time per-org paste (see the module docstring). ``client_secret`` is write-only: the
    response is the ordinary ``Connection`` read model, which carries ``client_id`` +
    ``has_oauth_client`` and no secret. The service verifies ``client_id`` against ``GET /app``
    BEFORE writing, so a pair from a different App is refused with the stored secret unchanged.

    A GitHub Enterprise connection is REFUSED rather than stored. ``get_oauth_client_credentials``
    declines every ``base_url`` connection (the OAuth legs are github.com-only), so a stored GHE
    pair would satisfy nothing while ``has_oauth_client`` — and the linkable-connections view's
    ``oauth_client_ready`` — reported the org as ready. Since the App-via-manifest capture path is
    github.com-only by construction (``register_url`` targets ``https://github.com/organizations/…``),
    this route is the only reachable way to set that flag, which makes it the right place to close
    the dead end: the admin gets a clear refusal instead of spending their one look at the secret.
    """
    svc = get_connection_service()
    try:
        # Pre-check on the record, not the body: the refusal must land BEFORE the secret is
        # persisted, and it costs one read the paste path can well afford.
        if svc.get_connection(id).base_url:
            _raise_connection_error(
                ConnectionError(
                    "Per-user GitHub linking is available for github.com connections only",
                    kind="verify_failed",
                )
            )
        return svc.set_oauth_client(id, body.client_id, body.client_secret)
    except ConnectionError as err:
        _raise_connection_error(err)


def _link_callback_for(redirect_url: str) -> str:
    """Derive the end-user link callback from the SPA-supplied admin ``redirect_url``.

    Same origin, path swapped for the pinned ``LINK_CALLBACK_PATH``; query and fragment dropped
    (``_validate_redirect_uri`` refuses a ``redirect_uri`` carrying either, so registering one
    that did could never be matched at link time). The path is IMPORTED, never re-typed: GitHub
    matches a ``redirect_uri`` against a registered callback byte-for-byte, so a drifted literal
    would break every link with a provider-side rejection. Imported locally, the idiom the delete
    guard below already uses for a cross-module reach.
    """
    from services.github_user_link import LINK_CALLBACK_PATH

    parts = urlsplit(redirect_url)
    return urlunsplit((parts.scheme, parts.netloc, LINK_CALLBACK_PATH, "", ""))


@router.post("/manifest/start", response_model=ManifestStartResponse)
async def manifest_start(
    body: ManifestStart,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """Begin the App-via-Manifest handshake: issue a CSRF ``state`` and return the GitHub
    registration ``post_url`` + the pre-filled manifest. Only GitHub is supported.

    The manifest registers TWO different redirect slots and both must keep working:
    ``redirect_url`` is where the ADMIN lands after CREATING the App (it carries the one-time
    conversion ``code`` back to ``/ops/connections/callback``), and ``callback_urls`` is where an
    END USER lands after AUTHORIZING it (``/ops/github-link/callback``, E27B). ``callback_urls`` is
    settable at App-creation time only — on an existing App it is org-admin UI-only — so it has to
    be seeded here or per-user linking needs manual org work. It is derived from the same
    SPA-supplied origin rather than configured separately, so one console deployment cannot end up
    with its two callbacks on different hosts."""
    if body.provider != Provider.GITHUB:
        _raise_connection_error(ConnectionError("only GitHub is supported", kind="bad_request"))
    svc = get_connection_service()
    try:
        state = svc.create_manifest_state(body.org, body.base_url, created_by=principal.email)
    except ConnectionError as err:
        _raise_connection_error(err)
    return ManifestStartResponse(
        post_url=register_url(body.org, state),
        manifest=build_manifest(
            body.org,
            body.redirect_url,
            callback_urls=[_link_callback_for(body.redirect_url)],
        ),
        state=state,
    )


@router.post("/manifest/callback", response_model=ManifestCallbackResponse)
async def manifest_callback(
    body: ManifestCallback,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """Finish the App-via-Manifest handshake: exchange the one-time ``code`` and either
    connect (installed) or return the install step (``needs_install`` + ``install_url``)."""
    svc = get_connection_service()
    try:
        connection, needs_install, install_url = svc.complete_manifest(body.code, body.state)
    except ConnectionError as err:
        _raise_connection_error(err)
    return ManifestCallbackResponse(
        connection=connection, needs_install=needs_install, install_url=install_url
    )


@router.post("/{id}/finalize", response_model=Connection)
async def finalize_connection(
    id: str,
    body: ConnectionFinalize,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """Finalize a PENDING App connection: resolve the installation (or use the supplied id),
    verify, and flip to CONNECTED."""
    svc = get_connection_service()
    try:
        return svc.finalize_app_connection(id, body.installation_id)
    except ConnectionError as err:
        _raise_connection_error(err)


@router.delete("/{id}", status_code=204)
async def delete_connection(
    id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    # Referential-integrity guard (E23/T10): a connection cannot be deleted while any
    # project references it — deleting it would strand the project (its repo's GitHub
    # becomes unreachable, an orphan the platform can't tear down). The check lives at the
    # route level, which composes both services: connection_service is deliberately
    # project-unaware (projects depend on connections, not the reverse — a service-level
    # guard would be a circular dependency). Import is local to avoid a module-load cycle.
    from api.routes.projects import get_project_service

    project_svc = get_project_service()
    if any(p.connection_id == id for p in project_svc.list_projects()):
        raise HTTPException(
            status_code=409, detail="Connection has projects; delete them first"
        )
    svc = get_connection_service()
    try:
        svc.delete_connection(id)
    except ConnectionError as err:
        _raise_connection_error(err)
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# The RECONCILE surface (E22/T5, rebuilt in E28C/T3 — design D-C3)
#
# One place where AGP's template registry and the org's actual repositories are compared, plus
# the three verbs that act on what it found: seed/re-push (``POST /{id}/rollout``), adopt
# (``POST /{id}/templates/adopt``), and the FORCED infra repo (ensured by every rollout).
# --------------------------------------------------------------------------- #


class ReconcileItemView(BaseModel):
    """One reconcile row on the wire. BINDING for the frontend (T6).

    ``exists_in_org`` and ``selectable`` are DELETED, not renamed: the boolean was answered from
    AGP's DDB catalog, which is evidence about AGP's store and never about the org, and a client
    that kept reading a boolean would keep rendering the same two wrong answers (a registered
    template whose repo is gone shown as in-sync; an existing repo shown as creatable). ``state``
    is the replacement and it has four values, so there is nothing for a boolean to carry.

    ``head_sha``/``default_branch`` are null whenever there was nothing to read — an absent repo,
    an org-origin row (present by construction from the listing, deliberately not re-probed), or a
    repository that exists with no commit yet.
    """

    name: str
    origin: str  # "seed" | "org" | "registry"
    state: str   # registered_present | registered_missing | unregistered_present | seed_absent
    default_branch: Optional[str] = None
    head_sha: Optional[str] = None


class ReconcileViewResponse(BaseModel):
    """``infra_repo`` stays a SEPARATE field rather than a flagged row in ``templates``: that is
    how "FORCED — always ensured, never a choice" survives now that ``selectable`` is gone. A
    structural separation cannot be flipped by a client."""

    templates: List[ReconcileItemView]
    infra_repo: ReconcileItemView


class RolloutRequest(BaseModel):
    """``overwrite`` and ``overwrite_infra`` are SEPARATE consents (E28D). Both default to
    ``False``, which is the safe default and a deliberate NARROWING: a payload that sets only
    ``overwrite`` re-pushes the selected templates and no longer authorizes pushing AGP's Terraform
    module over an existing ``agp-runtime-infra``. Creating that repo when it is ABSENT stays
    unconditional either way."""

    template_names: List[str] = []
    overwrite: bool = False          # templates only, from here on
    overwrite_infra: bool = False    # the forced infra repo's OWN consent


class AdoptRequest(BaseModel):
    """Adopt one existing org repository as a template. ``description`` is optional and is the
    only metadata the confirm collects — everything else is editable afterwards via
    ``PATCH /templates/{name}``.

    There is deliberately no ``created_by``: the actor comes from the validated principal."""

    repo_name: str
    description: Optional[str] = None


class RolloutResultItemView(BaseModel):
    name: str
    # "created" | "overwritten" | "recreated" | "skipped" | "adopted". BINDING for T6.
    #
    # Each word is derived from the OBSERVED state, so it never overstates what happened:
    # "overwritten" = a re-push on top of a registered template (E28C/T3 deleted delete+recreate,
    # so nothing is destroyed), "recreated" = the record existed but the repo was gone and was
    # rebuilt from seed, "created" = genuinely new. A "skipped" row's ``reason`` is operator-facing
    # prose and, for a repo that exists but is not a registered template, it points at ADOPT — that
    # is the only honest action for that state.
    action: str
    reason: Optional[str] = None


class RolloutResultResponse(BaseModel):
    items: List[RolloutResultItemView]


@router.get("/{id}/rollout/reconcile", response_model=ReconcileViewResponse)
async def reconcile_rollout(
    id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """Compare AGP's registry against the org's repositories; return one row per name.

    THE ONLY READ SURFACE THAT CALLS THE PROVIDER (the ruled cost model, D-C3): one paginated
    ``list_repos``, one registry read, and a bounded set of ``read_repo`` probes. No list/page
    route gains a provider call, so the Templates page stays registry-only and instant. (The
    rollout and adopt POSTs probe too — they must, to decide safely — but those are explicit
    operator actions, not page loads.)

    Admin-gated: reconcile is the read half of a trust-boundary write surface.
    """
    svc = get_rollout_service()
    try:
        view = svc.reconcile(id)
    except ConnectionError as err:
        _raise_connection_error(err)
    except RolloutError as err:
        # E28C/T3 — this used to be `except (RolloutError, GitHubRepoError)` answering 502 for
        # BOTH, so a malformed connection id (permanent, unretryable) told the console to retry.
        # The kind map is the authority now, exactly as on the rollout POST below.
        _raise_rollout_error(err)
    except GitHubRepoError:
        raise HTTPException(status_code=502, detail="Template rollout failed")
    return ReconcileViewResponse(
        templates=[ReconcileItemView(**vars(i)) for i in view.templates],
        infra_repo=ReconcileItemView(**vars(view.infra_repo)),
    )


@router.post("/{id}/rollout", response_model=RolloutResultResponse)
async def rollout_templates(
    id: str,
    body: RolloutRequest,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """Roll out the selected base templates as GitHub template repos + ALWAYS ensure the
    private per-org runtime-infra repo. Admin-gated."""
    svc = get_rollout_service()
    try:
        result = svc.rollout(
            id,
            template_names=body.template_names,
            overwrite=body.overwrite,
            overwrite_infra=body.overwrite_infra,
        )
    except ConnectionError as err:
        _raise_connection_error(err)
    except RolloutError as err:
        _raise_rollout_error(err)
    except GitHubRepoError:
        raise HTTPException(status_code=502, detail="Template rollout failed")
    return RolloutResultResponse(
        items=[RolloutResultItemView(**vars(i)) for i in result.items]
    )


@router.post("/{id}/templates/adopt", response_model=TemplateView)
async def adopt_template(
    id: str,
    body: AdoptRequest,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """ADOPT an existing org repository as one of this org's templates (E28C/T3, D-C4).

    Register-as-is: a governance statement ("this repo is our template"), never a content check
    and never a push. It is always an explicit human click — nothing adopts automatically — which
    is why it is a verb of its own rather than a branch inside rollout.

    Responds with the ordinary ``TemplateView`` the Templates page already renders, so an adopted
    template is indistinguishable from an uploaded one on every downstream surface. 404 = no such
    repo in the org, 409 = already registered, 422 = an illegal name. ``created_by`` comes from the
    validated ``principal.email``, NEVER from the body — the same rule ``create_connection``
    follows.
    """
    svc = get_rollout_service()
    try:
        return svc.adopt(
            id,
            repo_name=body.repo_name,
            description=body.description,
            created_by=principal.email,
        )
    except ConnectionError as err:
        _raise_connection_error(err)
    except RolloutError as err:
        _raise_rollout_error(err)
    except GitHubRepoError:
        raise HTTPException(status_code=502, detail="Template rollout failed")
