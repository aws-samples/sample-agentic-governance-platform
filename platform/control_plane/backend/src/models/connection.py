"""Pydantic models for Git org Connections: a PAT or an org-installed GitHub App.

No credential ever lives on a read model: ``Connection`` carries ``secret_arn`` / ``has_secret`` (the
credential sits in Secrets Manager) plus non-secret metadata, and the read models' inherited
``extra="ignore"`` discards an undeclared key rather than retaining it. Credentials flow in only
through the write-only ``ConnectionCreate``, ``ConnectionTokenReplace``, ``ConnectionKeyReplace`` and
``ConnectionOAuthClient`` inputs. For ``auth_type=pat`` the stored secret is ``{"token": ...}``; for
``github_app`` it is ``{"private_key": <PEM>}``, with ``app_id`` and ``installation_id`` non-secret on
the read model and AGP minting a scoped, short-lived installation token per operation. The secret body
may carry a third key, ``client_secret``, the App's OAuth client secret for the per-user link flow;
its partner ``client_id`` is non-secret and lives on the read model, which exposes only
``has_oauth_client`` for the secret half.

DynamoDB serialization and Secrets Manager I/O live in the connection service. No boto3, no I/O here.
"""

import re
from enum import Enum
from typing import Optional

from pydantic import Field, PrivateAttr, field_validator, model_validator

from models.base import WireModel

ORG_LOGIN_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$")

ORG_LOGIN_ERROR = (
    "org must be 1-39 alphanumerics with single internal hyphens "
    "(no leading/trailing hyphen, no '/', ':', '*' or '?')"
)


def validate_org_login(org: str) -> str:
    """Return ``org`` if it is a legal GitHub login, else raise ``ValueError`` with a curated literal.

    A security boundary, not a format check. ``org`` is interpolated into an IAM trust policy's ``sub``
    ``StringLike`` pattern (``ecr_push_role_service._trust_policy``), where ``*`` and ``?`` are
    wildcards: an unvalidated ``org="*"`` mints ``repo:*/*:*``, a role any GitHub repository may assume,
    and that role's ARN is published to third-party repositories by design. ``ORG_LOGIN_RE`` admits
    alphanumerics with single internal hyphens, 1 to 39 characters, so ``*``, ``?``, ``/`` and ``:`` are
    structurally excluded; ``fullmatch``, not ``match``, because ``$`` tolerates a trailing newline and
    ``"acme\\n"`` reaching IAM bricks the shared push role (trust is adopt-don't-retrust, with no
    ``UpdateAssumeRolePolicy`` grant). The pattern lives in this dependency-free module rather than
    beside the IAM code, which imports boto3. Applied to GitLab orgs too, since every provider reaches
    the same trust document; the GitLab names this narrows are an accepted trade.
    ``tests/test_connection_model.py`` pins the boundary over hostile inputs.
    """
    if not ORG_LOGIN_RE.fullmatch(org or ""):
        raise ValueError(ORG_LOGIN_ERROR)
    return org


class Provider(str, Enum):
    GITHUB = "github"
    GITLAB = "gitlab"


class ConnectionAuthType(str, Enum):
    PAT = "pat"
    GITHUB_APP = "github_app"


class ConnStatus(str, Enum):
    CONNECTED = "connected"
    ERROR = "error"
    PENDING = "pending"


class Connection(WireModel):
    """Read model: no secret field, ever."""

    id: str
    provider: Provider
    org: str
    base_url: Optional[str] = None
    auth_type: ConnectionAuthType = ConnectionAuthType.PAT
    app_id: Optional[str] = Field(default=None, description="GitHub App id; non-secret")
    installation_id: Optional[str] = Field(default=None, description="GitHub App installation id; non-secret")
    client_id: Optional[str] = Field(
        default=None,
        description="The App's OAuth client id for the per-user link flow; non-secret, distinct from app_id",
    )
    has_oauth_client: bool = Field(
        default=False,
        description=(
            "Whether the connection's secret body holds the OAuth client secret; until the one-time admin "
            "paste it does not"
        ),
    )
    status: ConnStatus = Field(
        description="pending is an App-via-Manifest connection created but not yet installed"
    )
    status_detail: Optional[str] = None
    account_login: Optional[str] = None
    secret_arn: str
    has_secret: bool = True
    ecr_push_role_arn: Optional[str] = Field(
        default=None,
        description=(
            "The per-org GitHub-OIDC ECR-push role, provisioned on connect and written as the "
            "AWS_ECR_PUSH_ROLE_ARN repository variable; None when provisioning is inert, and materialize "
            "uses the platform default"
        ),
    )
    last_verified_at: Optional[str] = None
    created_by: str
    created_at: str
    updated_at: str

    _row_version: int | None = PrivateAttr(default=None)


class ConnectionCreate(WireModel):
    """Write-only input: creates a connection and carries the credential.

    ``pat`` (the default) requires ``token``; ``github_app`` requires ``app_id``, ``installation_id`` and
    ``private_key`` (PEM). No credential field appears on the ``Connection`` read model.
    """

    provider: Provider
    org: str
    base_url: Optional[str] = None
    auth_type: ConnectionAuthType = ConnectionAuthType.PAT
    token: Optional[str] = None
    app_id: Optional[str] = None
    installation_id: Optional[str] = None
    private_key: Optional[str] = None

    @field_validator("org")
    @classmethod
    def _validate_org(cls, v: str) -> str:
        """The trust-policy input gate: ``org`` flows through ``_ensure_ecr_push_role`` into an IAM
        ``StringLike`` sub pattern (``validate_org_login``)."""
        return validate_org_login(v)

    @model_validator(mode="after")
    def _require_credential_for_auth_type(self) -> "ConnectionCreate":
        if self.auth_type == ConnectionAuthType.GITHUB_APP:
            missing = [f for f in ("app_id", "installation_id", "private_key") if not getattr(self, f)]
            if missing:
                raise ValueError(f"auth_type 'github_app' requires: {', '.join(missing)}")
        elif not self.token:
            raise ValueError("auth_type 'pat' requires: token")
        return self


class ConnectionTokenReplace(WireModel):
    """Write-only input: rotates the PAT."""

    token: str


class ConnectionKeyReplace(WireModel):
    """Write-only input: rotates the App private key."""

    private_key: str


class ConnectionOAuthClient(WireModel):
    """Write-only input: the admin-supplied OAuth client of a ``github_app`` connection. ``client_id``
    is echoed on the ``Connection`` read model; ``client_secret`` is merged into the existing Secrets
    Manager body and never leaves it."""

    client_id: str
    client_secret: str


class ManifestStart(WireModel):
    """Begin the App-via-Manifest handshake. No credential crosses the manifest models: the ``code`` is
    exchanged server-side and the private key never appears here."""

    org: str
    provider: Provider = Provider.GITHUB
    base_url: Optional[str] = None
    redirect_url: str

    @field_validator("org")
    @classmethod
    def _validate_org(cls, v: str) -> str:
        """The second door to the same IAM trust policy: this org is stashed in the manifest state,
        carried through ``complete_manifest`` into ``create_pending_app_connection`` and reaches
        ``_ensure_ecr_push_role`` on ``finalize_app_connection`` without a ``ConnectionCreate``."""
        return validate_org_login(v)


class ManifestStartResponse(WireModel):
    """Where to POST the manifest, plus the CSRF state."""

    post_url: str
    manifest: dict
    state: str


class ManifestCallback(WireModel):
    """Finish the manifest handshake."""

    code: str
    state: str


class ManifestCallbackResponse(WireModel):
    """The pending or connected connection, and whether the App still needs installing."""

    connection: Connection
    needs_install: bool
    install_url: Optional[str] = None


class ConnectionFinalize(WireModel):
    """Finalize a pending App connection."""

    installation_id: Optional[str] = None
