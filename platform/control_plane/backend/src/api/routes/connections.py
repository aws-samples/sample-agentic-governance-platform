"""Git org connections admin API: CRUD, the App-via-Manifest handshake, the OAuth client paste
and the template reconcile surface.

Operators read the list to pick a connection when provisioning a project; every mutation is
ADMIN. No credential crosses the read boundary: the service returns ``Connection`` without a
credential field, and credentials arrive only through the write-only create and replace bodies.
``created_by`` is the validated principal's identity. ``ConnectionError``, ``GitHubRepoError`` and
``RolloutError`` render through their tables.

``PUT /{id}/oauth-client`` exists because GitHub exposes an App's OAuth ``client_secret``
through no API once the App is created: an org whose App predates AGP's capture at manifest
conversion enables per-user linking by pasting the pair once. GitHub Enterprise connections are
refused there, since the OAuth legs are github.com-only.
"""

import logging
from typing import List, Optional
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Response
from fastapi import Depends as RBACDepends
from pydantic import Field

from api.routes.github_templates import joined_template_view
from core import aws_clients
from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
from models.base import WireModel
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
from services.github_repo_service import GitHubRepoService
from services.github_template_service import TemplateView
from services.infra_template_registry import InfraTemplateRegistry
from services.template_registry import TemplateRegistry
from services.template_rollout_service import ReconcileOrigin, ReconcileState, RolloutService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/connections", tags=["connections"])

_svc: Optional[ConnectionService] = None
_rollout_svc: Optional[RolloutService] = None


def get_connection_service() -> ConnectionService:
    global _svc
    if _svc is None:
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
    """``GITHUB_OIDC_PROVIDER_ARN``, or the ARN derived from the account through STS. Resolved
    once here so the bootstrap service and the role service name the same provider."""
    return resolve_github_oidc_provider_arn(settings.GITHUB_OIDC_PROVIDER_ARN, region=settings.AWS_REGION)


def _build_ecr_push_role_service(oidc_provider_arn: str) -> EcrPushRoleService:
    return EcrPushRoleService(
        role_name_prefix=settings.ECR_PUSH_ROLE_NAME_PREFIX,
        oidc_provider_arn=oidc_provider_arn,
        ecr_repository_arn=settings.AGENT_IMAGES_ECR_ARN,
        region=settings.AWS_REGION,
    )


def _build_github_oidc_provider_service(oidc_provider_arn: str) -> GitHubOidcProviderService:
    return GitHubOidcProviderService(
        provider_arn=oidc_provider_arn,
        region=settings.AWS_REGION,
    )


def _materialized_repo_names(connection_id: str) -> set:
    """The names of the agent repos AGP materialized for ``connection_id``, which the reconcile
    surface withholds from the adopt picker. Raises on a store failure so reconcile fails closed."""
    from api.routes.projects import get_project_service

    project_svc = get_project_service()
    project_ids = {p.id for p in project_svc.list_projects() if p.connection_id == connection_id}
    return {r.name for r in project_svc.list_repositories() if r.project_id in project_ids}


def get_rollout_service() -> RolloutService:
    global _rollout_svc
    if _rollout_svc is None:
        _rollout_svc = RolloutService(
            GitHubRepoService(),
            get_connection_service(),
            agent_templates_dir=settings.AGENT_TEMPLATES_DIR,
            s3_client=aws_clients.client("s3", region=settings.AWS_REGION),
            runtime_module_bucket=settings.RUNTIME_MODULE_BUCKET,
            runtime_module_key=settings.RUNTIME_MODULE_KEY,
            template_registry=TemplateRegistry(
                table_name=settings.PROJECTS_TABLE_NAME,
                region=settings.AWS_REGION,
            ),
            infra_template_registry=InfraTemplateRegistry(
                table_name=settings.PROJECTS_TABLE_NAME,
                region=settings.AWS_REGION,
            ),
            known_repo_names=_materialized_repo_names,
        )
    return _rollout_svc


@router.get("", response_model=List[Connection])
async def list_connections(
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    return get_connection_service().list_connections()


@router.post("", response_model=Connection, status_code=201)
async def create_connection(
    body: ConnectionCreate,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    return get_connection_service().create_connection(body, created_by=principal.email or principal.oid or "")


@router.post("/{id}/test", response_model=Connection)
async def test_connection(
    id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    return get_connection_service().test_connection(id)


@router.put("/{id}/token", response_model=Connection)
async def replace_token(
    id: str,
    body: ConnectionTokenReplace,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    return get_connection_service().replace_token(id, body.token)


@router.put("/{id}/key", response_model=Connection)
async def replace_key(
    id: str,
    body: ConnectionKeyReplace,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    return get_connection_service().replace_key(id, body.private_key)


@router.put("/{id}/oauth-client", response_model=Connection)
async def set_connection_oauth_client(
    id: str,
    body: ConnectionOAuthClient,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """Store the App's OAuth client pair so humans in this org can link their GitHub accounts.
    ``client_secret`` is write-only; the response is the ordinary read model. A GitHub Enterprise
    connection is refused before anything is written."""
    svc = get_connection_service()
    if svc.get_connection(id).base_url:
        raise ConnectionError(
            "Per-user GitHub linking is available for github.com connections only",
            kind="verify_failed",
        )
    return svc.set_oauth_client(id, body.client_id, body.client_secret)


def _link_callback_for(redirect_url: str) -> str:
    """The end-user link callback on the SPA-supplied origin: same origin, the pinned
    ``LINK_CALLBACK_PATH``, query and fragment dropped."""
    from services.github_user_link import LINK_CALLBACK_PATH

    parts = urlsplit(redirect_url)
    return urlunsplit((parts.scheme, parts.netloc, LINK_CALLBACK_PATH, "", ""))


@router.post("/manifest/start", response_model=ManifestStartResponse)
async def manifest_start(
    body: ManifestStart,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """Begin the App-via-Manifest handshake: issue a CSRF ``state`` and return GitHub's
    registration ``post_url`` with the pre-filled manifest. The manifest registers the admin's
    ``redirect_url`` and, derived from the same origin, the end-user link ``callback_urls``,
    which GitHub only accepts at App creation."""
    if body.provider != Provider.GITHUB:
        raise ConnectionError("only GitHub is supported", kind="bad_request")
    state = get_connection_service().create_manifest_state(
        body.org, body.base_url, created_by=principal.email or principal.oid or ""
    )
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
    """Finish the handshake: exchange the one-time ``code`` and either connect or return the
    install step (``needs_install`` with ``install_url``)."""
    connection, needs_install, install_url = get_connection_service().complete_manifest(body.code, body.state)
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
    return get_connection_service().finalize_app_connection(id, body.installation_id)


@router.delete("/{id}", status_code=204)
async def delete_connection(
    id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """A connection with projects is not deleted: the check composes the project service here,
    since the connection service is project-unaware."""
    from api.routes.projects import get_project_service

    if any(p.connection_id == id for p in get_project_service().list_projects()):
        raise ConnectionError(f"connection {id} still has projects", kind="has_projects")
    get_connection_service().delete_connection(id)
    return Response(status_code=204)


class ReconcileItemView(WireModel):
    """One reconcile row. ``head_sha`` and ``default_branch`` are null whenever there was nothing
    to read: an absent repo, an org-origin row, or a repository with no commit yet."""

    name: str
    origin: ReconcileOrigin = Field(description='Where the row comes from: "seed", "org" or "registry".')
    state: ReconcileState = Field(
        description=(
            "registered_present, registered_missing, unregistered_present or seed_absent; the four states "
            "AGP's registry and the org's repositories can disagree in."
        )
    )
    default_branch: Optional[str] = None
    head_sha: Optional[str] = None
    infra_template_id: Optional[str] = Field(
        default=None, description="A seed row's declared infra template; null for every other origin."
    )


class InfraSeedView(WireModel):
    """One infra-catalog seed as the roll-out upserts it: the console's infra template row
    without ``module_source``."""

    id: str
    name: str
    description: str = ""
    platform: str
    iac_type: str
    build_kind: str
    repo_name: str


class ReconcileViewResponse(WireModel):
    """``infra_repo`` is a separate field, not a flagged row in ``templates``: it is always
    ensured and never a choice, and a structural separation cannot be flipped by a client."""

    templates: List[ReconcileItemView]
    infra_repo: ReconcileItemView
    infra_seeds: List[InfraSeedView] = Field(default_factory=list)


class RolloutRequest(WireModel):
    template_names: List[str] = Field(default_factory=list)
    overwrite: bool = Field(default=False, description="Re-push the selected templates over existing repos.")
    overwrite_infra: bool = Field(
        default=False,
        description=(
            "A separate consent: push AGP's Terraform module over an existing runtime-infra repo. "
            "Creating that repo when absent needs no consent."
        ),
    )


class AdoptRequest(WireModel):
    """Adopt one existing org repository as a template. The actor comes from the principal."""

    repo_name: str
    description: Optional[str] = None


class RolloutResultItemView(WireModel):
    name: str
    action: str = Field(
        description=(
            'One of "created", "overwritten", "recreated", "skipped", "adopted" or "failed", derived from '
            'the observed state; "skipped" means nothing was needed and "failed" means a push was '
            "attempted and did not happen."
        )
    )
    reason: Optional[str] = None


class RolloutResultResponse(WireModel):
    items: List[RolloutResultItemView]


@router.get("/{id}/rollout/reconcile", response_model=ReconcileViewResponse)
async def reconcile_rollout(
    id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """Compare AGP's registry against the org's repositories, one row per name: the only read
    surface that calls the provider."""
    view = get_rollout_service().reconcile(id)
    return ReconcileViewResponse(
        templates=[ReconcileItemView(**vars(i)) for i in view.templates],
        infra_repo=ReconcileItemView(**vars(view.infra_repo)),
        infra_seeds=[InfraSeedView(**seed) for seed in view.infra_seeds],
    )


@router.post("/{id}/rollout", response_model=RolloutResultResponse)
async def rollout_templates(
    id: str,
    body: RolloutRequest,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """Roll out the selected base templates as GitHub template repos and always ensure the
    private per-org runtime-infra repo."""
    result = get_rollout_service().rollout(
        id,
        template_names=body.template_names,
        overwrite=body.overwrite,
        overwrite_infra=body.overwrite_infra,
    )
    return RolloutResultResponse(items=[RolloutResultItemView(**vars(i)) for i in result.items])


@router.post("/{id}/templates/adopt", response_model=TemplateView)
async def adopt_template(
    id: str,
    body: AdoptRequest,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """Register an existing org repository as one of this org's templates, as is: no content
    check, no push. Answers the joined ``TemplateView`` the Templates page renders. 404 when the
    repo is not in the org, 409 when it is already registered, 422 for an illegal name."""
    view = get_rollout_service().adopt(
        id,
        repo_name=body.repo_name,
        description=body.description,
        created_by=principal.email or principal.oid or "",
    )
    return joined_template_view(id, view)
