"""Pydantic models for Git org Connections (E19 + E20/T9 GitHub App auth).

Plain ``BaseModel``s with ``str, Enum`` enums — the ``marketplace`` idiom.

SECRET SEAM: no credential secret ever lives on a read model. ``Connection`` carries
only ``secret_arn``/``has_secret`` (the credential sits in Secrets Manager) plus
NON-secret display metadata. Credentials flow in *only* through the write-only
``ConnectionCreate``/``ConnectionTokenReplace``/``ConnectionKeyReplace`` inputs.

AUTH TYPES (``ConnectionCreate`` write-shape contract):
  - ``pat`` (default): a Personal/Group Access Token. REQUIRES ``token``. The secret
    stored is ``{"token": <value>}``.
  - ``github_app``: an org-installed GitHub App. REQUIRES ``app_id`` +
    ``installation_id`` + ``private_key`` (PEM). ``app_id``/``installation_id`` are
    NON-secret and live on the read model; the PRIVATE KEY is a secret and is stored
    as ``{"private_key": <PEM>}`` in Secrets Manager only. AGP mints a fresh, scoped,
    short-lived installation access token per operation (see ``github_app_auth``).
    The secret body may carry a THIRD key, ``client_secret`` — the App's OAuth client
    secret, used only by the E27B per-user link flow. Its partner ``client_id`` is
    NON-secret and therefore lives on the read model beside ``app_id``; the read model
    exposes only ``has_oauth_client`` to say whether the secret half is present.

DynamoDB serialization and Secrets Manager I/O live in the connection service, NOT
here. No boto3, no I/O, no AWS imports — pure models.
"""

import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel, field_validator, model_validator

# GitHub org/user LOGIN rules: alphanumerics with SINGLE internal hyphens, no leading or
# trailing hyphen, 1-39 characters. The lookahead is what forbids `a--b` and `a-` while
# still allowing `AgenticOps-Platform`.
#
# THIS IS A SECURITY BOUNDARY, NOT A COSMETIC ONE, and that is why the pattern lives in
# this dependency-free module rather than beside the IAM code that needs it (which imports
# boto3 — see this module's "pure models" rule). ``org`` is interpolated into an IAM trust
# policy's ``sub`` ``StringLike`` pattern (``ecr_push_role_service._trust_policy``), where
# `*` and `?` are WILDCARD METACHARACTERS: an unvalidated ``org="*"`` mints
# ``repo:*/*:*``, i.e. a role ANY GitHub repo on the internet may assume — and that role's
# ARN is published to third-party repos by design as the ``AWS_ECR_PUSH_ROLE_ARN`` Actions
# var. The character class below STRUCTURALLY EXCLUDES every character that could widen
# that pattern — `*`, `?`, `/`, `:` are all outside `[a-zA-Z0-9-]` — so a value that
# matches this cannot be a trust widening. That exclusion is the whole point; do not
# "relax" it to admit `.`, `_` or `/` without re-reading `_trust_policy`.
#
# Applied to GITLAB orgs too, deliberately: ``connection_service._ensure_ecr_push_role``
# calls ``ensure_role(org)`` for EVERY provider, so a GitLab group name reaches the same
# trust document. GitLab paths admit `.`/`_`/`/` that this rejects, which narrows the
# accepted GitLab group names — an accepted trade, since AGP has no GitLab repo provider
# and a wildcard reaching IAM must not depend on which provider enum was sent.
ORG_LOGIN_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$")

# The message is a CURATED LITERAL (no interpolated input echoed back) — same rule the
# connection/ECR error messages follow.
ORG_LOGIN_ERROR = (
    "org must be 1-39 alphanumerics with single internal hyphens "
    "(no leading/trailing hyphen, no '/', ':', '*' or '?')"
)


def validate_org_login(org: str) -> str:
    """Return ``org`` if it is a legal login, else raise ``ValueError``. Shared by every
    write model that carries an org which reaches an IAM trust policy."""
    # fullmatch, not match: `$` in match() tolerates a trailing newline, and an org
    # of "acme\n" reaching IAM bricks the shared push role permanently (trust is
    # adopt-don't-retrust, and there is deliberately no UpdateAssumeRolePolicy grant).
    if not ORG_LOGIN_RE.fullmatch(org or ""):
        raise ValueError(ORG_LOGIN_ERROR)
    return org


class Provider(str, Enum):
    GITHUB = "github"
    GITLAB = "gitlab"


class AuthType(str, Enum):
    PAT = "pat"
    GITHUB_APP = "github_app"


class ConnStatus(str, Enum):
    CONNECTED = "connected"
    ERROR = "error"
    PENDING = "pending"  # App-via-Manifest: created but not yet installed/verified (E20/U2)


class Connection(BaseModel):                 # read-model — NO secret field, ever
    id: str
    provider: Provider
    org: str
    base_url: Optional[str] = None
    auth_type: AuthType = AuthType.PAT
    # GitHub App NON-secret metadata (safe to expose; the private key is NOT here).
    app_id: Optional[str] = None
    installation_id: Optional[str] = None
    # E27B — the App's OAuth client, used for the per-user GitHub link flow. ``client_id``
    # is NON-secret (distinct from ``app_id``); the client SECRET lives in the connection's
    # Secrets Manager body only, so this model carries just the presence flag — exactly how
    # ``has_secret`` stands in for the private key. Additive: pre-E27B items validate as
    # "no OAuth client yet" (the org still needs the one-time admin paste).
    client_id: Optional[str] = None
    has_oauth_client: bool = False
    status: ConnStatus
    status_detail: Optional[str] = None
    account_login: Optional[str] = None
    secret_arn: str
    has_secret: bool = True
    # Per-org GitHub-OIDC ECR-push role ARN (E22 multi-org): provisioned on connect,
    # written as the AWS_ECR_PUSH_ROLE_ARN repo var on this org's materialized repos so a
    # repo in one org can't assume another org's push role. None when the provisioning
    # service is inert (unconfigured) → materialize falls back to the platform default.
    ecr_push_role_arn: Optional[str] = None
    last_verified_at: Optional[str] = None
    created_by: str
    created_at: str
    updated_at: str


class ConnectionCreate(BaseModel):           # write-only input — carries the credential
    """Create a connection. The required credential fields depend on ``auth_type``:

    - ``pat`` (default): ``token`` is required.
    - ``github_app``: ``app_id`` + ``installation_id`` + ``private_key`` (PEM) required.

    Every credential field is write-only — none appears on the ``Connection`` read model.
    """

    provider: Provider
    org: str
    base_url: Optional[str] = None
    auth_type: AuthType = AuthType.PAT
    # PAT path:
    token: Optional[str] = None
    # GitHub App path (app_id/installation_id are non-secret; private_key is a secret):
    app_id: Optional[str] = None
    installation_id: Optional[str] = None
    private_key: Optional[str] = None

    # THE trust-policy input gate. ``org`` flows create_connection → _ensure_ecr_push_role
    # → ensure_role/ensure_shared_role → _trust_policy, where it lands inside an IAM
    # ``StringLike`` sub pattern. See ``ORG_LOGIN_RE``.
    @field_validator("org")
    @classmethod
    def _validate_org(cls, v: str) -> str:
        return validate_org_login(v)

    @model_validator(mode="after")
    def _require_credential_for_auth_type(self) -> "ConnectionCreate":
        if self.auth_type == AuthType.GITHUB_APP:
            missing = [
                f
                for f in ("app_id", "installation_id", "private_key")
                if not getattr(self, f)
            ]
            if missing:
                raise ValueError(
                    f"auth_type 'github_app' requires: {', '.join(missing)}"
                )
        elif not self.token:
            raise ValueError("auth_type 'pat' requires: token")
        return self


class ConnectionTokenReplace(BaseModel):     # write-only input — rotates the PAT
    token: str


class ConnectionKeyReplace(BaseModel):       # write-only input — rotates the App private key
    private_key: str


class ConnectionOAuthClient(BaseModel):      # write-only input — carries the client secret
    """Admin-supplied OAuth client for a ``github_app`` connection (E27B).

    ``client_id`` is echoed back on the ``Connection`` read model; ``client_secret`` is
    merged into the existing Secrets Manager body and never leaves it.
    """

    client_id: str
    client_secret: str


# --------------------------------------------------------------------------- #
# App-via-Manifest flow (E20/U2). No credential ever crosses these boundaries:
# the manifest ``code`` is exchanged server-side; the private key never appears here.
# --------------------------------------------------------------------------- #


class ManifestStart(BaseModel):              # request — begin the manifest handshake
    org: str
    provider: Provider = Provider.GITHUB
    base_url: Optional[str] = None
    redirect_url: str

    # The SECOND door to the same IAM trust policy, and it must not be forgotten: this org
    # is stashed in the manifest state, carried through ``complete_manifest`` into
    # ``create_pending_app_connection``, and reaches ``_ensure_ecr_push_role`` on
    # ``finalize_app_connection``. ``ConnectionCreate`` never validates it, because the
    # App-via-Manifest path never constructs one.
    @field_validator("org")
    @classmethod
    def _validate_org(cls, v: str) -> str:
        return validate_org_login(v)


class ManifestStartResponse(BaseModel):      # response — where to POST the manifest + CSRF state
    post_url: str
    manifest: dict
    state: str


class ManifestCallback(BaseModel):           # request — finish the manifest handshake
    code: str
    state: str


class ManifestCallbackResponse(BaseModel):   # response — the (pending or connected) connection
    connection: Connection
    needs_install: bool
    install_url: Optional[str] = None


class ConnectionFinalize(BaseModel):         # request — finalize a pending App connection
    installation_id: Optional[str] = None
