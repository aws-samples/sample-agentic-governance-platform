"""Automates the Microsoft Entra ID app registrations the platform needs.

This script performs, against the Microsoft Graph REST API, the same work the
manual identity-setup guide describes by hand: it creates the backend and
single-page-application registrations with everything they carry, grants the
admin consents, assigns the operator running it to the administrator role, and
prints the configuration values to paste into the deployment. It writes nothing
to disk and never changes anything outside the two app registrations.

Standalone by design: the Python standard library plus ``requests``, no imports
from the platform's own source tree, and its own virtual environment. An operator
can copy this directory out of the repository and it still runs.

Authentication is delegated to the Azure CLI: ``az account get-access-token``
returns an access token for the signed-in administrator, and every mutation is a
plain Graph REST call made with it. The CLI is never used for the mutations
themselves, so what the script does is visible in this file rather than hidden in
CLI behaviour that changes between releases.

Three layers, in order:
  1. token acquisition and the HTTP transport (``get_az_token``, ``GraphClient``);
  2. pure payload builders (``backend_app_payload``, ``spa_app_payload``);
  3. the orchestrated flow that calls them.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import requests

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Microsoft Graph's application id, the same in every tenant; its service principal's object id is
# per-tenant and is looked up:
# https://learn.microsoft.com/en-us/troubleshoot/azure/entra/entra-id/governance/verify-first-party-apps-sign-in
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"

# The role values the backend reads out of the access token: a contract between the two, not a preference.
PLATFORM_ROLES = ("Platform.Admin", "Platform.Operator", "Platform.Viewer")

# The one delegated scope the backend exposes and the frontend requests.
SCOPE_NAME = "Access.Default"

DEFAULT_AUDIENCE = "api://agp"
DEFAULT_BACKEND_NAME = "AGP - Backend Graph Client"
DEFAULT_SPA_NAME = "AGP - Frontend"

DEFAULT_SPA_NAME_VARIANT = "AGP — Frontend"

GRAPH_APP_PERMISSIONS: dict[str, str] = {
    "Application.ReadWrite.All": "1bfefb4e-e0b5-418b-a88f-73c46d2cc8e9",
    "AppRoleAssignment.ReadWrite.All": "06b708a9-e830-4db3-a914-8e69da51d44f",
    "DelegatedPermissionGrant.ReadWrite.All": "8e8e4742-1d95-4f68-9d56-6ee75648c72a",
    "User.Read.All": "df021288-bdef-4463-88db-98f22de89214",
    "Group.Read.All": "5b567255-7703-4780-807c-7be8301ae99b",
    "Directory.Read.All": "7ab1d382-f21e-4acd-a863-ba3e13f7da61",
}

# Graph answers a duplicate consent or role assignment with a 400 whose message is the only discriminator.
ALREADY_EXISTS_SUBSTRINGS = ("already exists", "permission entry already")

_ALREADY_EXISTS_STATUSES = frozenset({400, 409})

_SECRET_DISPLAY_NAME = "agp-backend (24 months)"  # noqa: S105 — the secret's display name in the portal, not a credential; expires 2027-10-23; added E49A-50A/T19

_ROLE_DETAILS: dict[str, tuple[str, str]] = {
    "Platform.Admin": ("Platform Admin", "Full platform administration."),
    "Platform.Operator": (
        "Platform Operator",
        "Create, edit and operate agents and MCP servers.",
    ),
    "Platform.Viewer": ("Platform Viewer", "Read-only access."),
}

# ``--resource-type ms-graph`` resolves the Graph endpoint per cloud, so no hostname is hardcoded:
# https://learn.microsoft.com/en-us/cli/azure/account#az-account-get-access-token
_AZ_TOKEN_COMMAND = (
    "az",
    "account",
    "get-access-token",
    "--resource-type",
    "ms-graph",
    "-o",
    "json",
)

_AZ_TIMEOUT_SECONDS = 60

_UNREADABLE_TOKEN_GUIDANCE = (
    "The Azure CLI's token response is missing or could not be read: it needs an "  # noqa: S105 — an operator message, not a credential; expires 2027-10-23; added E49A-50A/T19
    "access token, a tenant id, and a numeric 'expires_on'. Confirm 'az account "
    "show' reports the expected tenant, then re-run."
)

_RETRY_ATTEMPTS = 11
_RETRY_BASE_DELAY_SECONDS = 1.0
_RETRY_MAX_DELAY_SECONDS = 8.0

_RETRY_AFTER_MAX_SECONDS = 60.0

# 400 is retried because Graph answers a create that references a not-yet-replicated application
# with 400, not only 404; 403 is absent, a missing directory role is never transient.
_RETRIABLE_STATUSES = frozenset({400, 404, 429, 500, 502, 503, 504})

_HTTP_TIMEOUT_SECONDS = 30

_DIRECTORY_ROLE_GUIDANCE = (
    "Microsoft Entra refused the request. The signed-in account is missing a "
    "required DIRECTORY ROLE: creating app registrations and granting tenant-wide "
    "admin consent needs Application Administrator (or Cloud Application "
    "Administrator) together with Privileged Role Administrator, or the Global "
    "Administrator role on its own. Have a tenant administrator assign one of "
    "those directory roles to the account, sign in again, and re-run."
)


class PreflightError(RuntimeError):
    """The script cannot start: the Azure CLI is missing, nobody is signed in, or
    its output could not be read. Distinct from a Graph failure because there is
    nothing to retry and nothing has been changed in the tenant yet."""


class GraphError(RuntimeError):
    """A Microsoft Graph call failed. Carries the HTTP status, the Graph error
    code, and the error message.

    The message is safe to surface: Graph's resource endpoints never echo the
    request's Authorization header or body, so an error message from one cannot
    leak the access token or the generated client secret. Nothing in this script
    ever puts a token or a secret into an exception.
    """

    def __init__(self, status: int, code: str, message: str = "") -> None:
        self.status = status
        self.code = code
        self.message = message
        detail = f"status={status}, code={code or 'unknown'}"
        super().__init__(f"{message} ({detail})" if message else f"Graph error ({detail})")


class GraphAuthError(GraphError):
    """Graph refused the call with 403. Never retried: retrying a permissions
    problem for the length of the replication window turns a clear, immediately
    actionable error into a mysterious hang."""


class AlreadyExists(GraphError):
    """The object the call was creating is already there.

    Raised for a duplicate consent grant or role assignment, where the existing
    object IS the desired end state — so callers treat this as success rather than
    as a failure. Never retried.
    """


@dataclass
class AzToken:
    """An access token for Microsoft Graph, plus the two facts that come free with
    it: the tenant it was issued for (so the run needs no extra lookup and cannot
    target the wrong tenant) and its expiry as a POSIX timestamp."""

    access_token: str
    tenant_id: str
    expires_on: int


def get_az_token() -> AzToken:
    """Get a Graph access token for the signed-in administrator from the Azure CLI.

    Every failure mode becomes a ``PreflightError`` whose message says what to do,
    because all of them are the operator's environment rather than the tenant: the
    CLI is not installed, nobody is signed in, it never answered, or it answered
    something unreadable.

    The POSIX ``expires_on`` field is used rather than the human-readable
    ``expiresOn``, which is local time and would misread in any non-UTC timezone.
    """
    try:
        completed = subprocess.run(  # noqa: S603 — fixed argv from _AZ_TOKEN_COMMAND, az resolved with shutil.which; expires 2027-10-23; added E49A-50A/T19
            _az_command(),
            capture_output=True,
            text=True,
            check=False,
            timeout=_AZ_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as err:
        raise PreflightError(
            "The Azure CLI ('az') is not installed or is not on PATH. Install it "
            "from https://learn.microsoft.com/cli/azure/install-azure-cli, then "
            "sign in with 'az login' as a tenant administrator."
        ) from err
    except subprocess.TimeoutExpired:
        raise PreflightError(
            f"The Azure CLI did not answer within {_AZ_TIMEOUT_SECONDS} seconds. "
            "Run 'az account get-access-token --resource-type ms-graph -o json' by "
            "hand to see what it is waiting for, then re-run."
        ) from None

    if completed.returncode != 0:
        raise PreflightError(
            "Could not get an access token from the Azure CLI. Sign in as a "
            "tenant administrator with 'az login' (add '--tenant <tenant-id>' if "
            "your default tenant is not the target one) and re-run. "
            f"The Azure CLI reported: {completed.stderr.strip() or 'no details'}"
        )

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as err:
        raise PreflightError(
            "The Azure CLI returned output that is not JSON. Re-run "
            "'az account get-access-token --resource-type ms-graph -o json' by "
            "hand to see what it says."
        ) from err

    try:
        access_token = payload["accessToken"]
        tenant_id = payload["tenant"]
        expires_on = int(payload["expires_on"])
    except (TypeError, KeyError, ValueError):
        raise PreflightError(_UNREADABLE_TOKEN_GUIDANCE) from None

    if not access_token or not tenant_id:
        raise PreflightError(_UNREADABLE_TOKEN_GUIDANCE)

    return AzToken(access_token=access_token, tenant_id=tenant_id, expires_on=expires_on)


def _az_command() -> tuple[str, ...]:
    """The token command, with ``az`` resolved to a real executable path.

    Resolving it is not cosmetic. On Windows the Azure CLI ships as ``az.cmd``, and
    ``subprocess.run`` without a shell goes through ``CreateProcess``, which appends
    ``.exe`` but does not apply ``PATHEXT`` — so a bare ``"az"`` as argv[0] raises
    ``FileNotFoundError`` on a machine where ``az`` works in every shell, and the
    operator is told the Azure CLI is not installed when it plainly is. That is the
    one error message there is no way to act on. ``shutil.which`` applies ``PATHEXT``
    and finds it.

    When nothing is found the unresolved name is kept deliberately, so that "there is
    no Azure CLI here" has exactly one message and one place it comes from: the
    ``FileNotFoundError`` arm above, which says how to install it and sign in.
    """
    return (shutil.which("az") or _AZ_TOKEN_COMMAND[0], *_AZ_TOKEN_COMMAND[1:])


class GraphClient:
    """A small synchronous Microsoft Graph client with one shared retry policy.

    Every request goes through ``_request``, so the replication retry, the 403
    fail-fast, and the already-exists classification apply identically to every
    call in the script. There is no way to reach Graph while bypassing them.

    The session is injectable so the tests can drive the whole policy offline with
    a fake that replays queued responses.
    """

    def __init__(self, token: AzToken, session: Any | None = None) -> None:
        self._token = token
        self._session = session if session is not None else requests.Session()

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET ``path`` and return the parsed body."""
        return _json_body(self._request("GET", path, params=params))

    def post(self, path: str, payload: dict) -> dict:
        """POST ``payload`` to ``path`` and return the parsed body.

        Creates answer with the new object, which is the only chance to read
        server-generated fields such as a secret's text — so the body is always
        handed back rather than discarded.
        """
        return _json_body(self._request("POST", path, json_body=payload))

    def patch(self, path: str, payload: dict) -> None:
        """PATCH ``payload`` onto ``path``.

        Returns nothing: Graph answers a successful PATCH with 204 and an empty
        body, so there is nothing for a caller to inspect.
        """
        self._request("PATCH", path, json_body=payload)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        """Issue one Graph request, retrying only the transient shapes.

        Each response is classified in this order, before any retry decision:

        1. **Already-exists first.** A duplicate grant arrives as a 400, so if the
           retry decision came first it would retry an operation that has already
           succeeded for the full 52-second window and then report failure.
        2. **403 next, immediately fatal.** A missing directory role is not going
           to appear halfway through a retry loop.
        3. **Then the transient set**, with a bounded linear backoff. Throttling
           waits as long as Graph asked, up to its own longer ceiling; everything
           else waits its attempt number, capped.

        Anything unrecognised fails on the first response. Guessing that an
        unknown 4xx might be transient is how a real misconfiguration becomes a
        minute of silence.

        A transport failure — no HTTP response at all — joins the transient set:
        the same interruption expressed as a 503 would be retried, so a dropped
        connection or a timeout gets the same window rather than escaping as a raw
        traceback that leaves the tenant half-configured.
        """
        url = f"{GRAPH_BASE}{path}"
        headers = {
            "Authorization": f"Bearer {self._token.access_token}",
            "Content-Type": "application/json",
        }

        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            try:
                response = self._session.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    params=params,
                    timeout=_HTTP_TIMEOUT_SECONDS,
                )
            except requests.exceptions.RequestException as err:
                if attempt == _RETRY_ATTEMPTS:
                    raise GraphError(
                        0,
                        "transport_failure",
                        "Could not reach Microsoft Graph after "
                        f"{_RETRY_ATTEMPTS} attempts ({type(err).__name__}). "
                        "Check the network connection, any proxy, and the VPN, "
                        "then re-run.",
                    ) from None
                time.sleep(_scheduled_delay(attempt))
                continue

            status = response.status_code
            if 200 <= status < 300:
                return response

            code, message = _graph_error_fields(response)

            if status in _ALREADY_EXISTS_STATUSES and _is_already_exists(message):
                raise AlreadyExists(status, code, message)

            if status == 403:
                raise GraphAuthError(
                    status,
                    code,
                    f"{_DIRECTORY_ROLE_GUIDANCE} Microsoft Graph reported: {message or 'no details'}",
                )

            if status not in _RETRIABLE_STATUSES or attempt == _RETRY_ATTEMPTS:
                raise GraphError(status, code, message)

            time.sleep(_retry_delay(attempt, response))

        raise GraphError(500, "retry_exhausted")


def _scheduled_delay(attempt: int) -> float:
    """The policy's own delay for this attempt: linear in the attempt number, capped."""
    return min(_RETRY_BASE_DELAY_SECONDS * attempt, _RETRY_MAX_DELAY_SECONDS)


def _retry_delay(attempt: int, response: Any) -> float:
    """How long to wait before the next attempt.

    Throttling is the one case where the server knows better than the policy, so a
    usable ``Retry-After`` wins outright, up to its own ``_RETRY_AFTER_MAX_SECONDS``
    ceiling rather than the schedule's shorter cap. Clamping it to the schedule
    would be a false economy: every retry would then land inside the window the
    server asked the client to wait out, failing a call that waiting would have
    completed. The ceiling keeps the wait bounded all the same — ten such waits at
    most, 600 seconds. Anything unusable — an HTTP-date, junk, or a negative number,
    which ``time.sleep`` would reject outright — falls back to the schedule, so the
    returned delay is never negative.
    """
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        try:
            requested = int(retry_after)
        except (TypeError, ValueError):
            return _scheduled_delay(attempt)
        if requested >= 0:
            return min(float(requested), _RETRY_AFTER_MAX_SECONDS)
    return _scheduled_delay(attempt)


def _graph_error_fields(response: Any) -> tuple[str, str]:
    """Pull ``(code, message)` out of a Graph error body, tolerating its absence.

    An error response is not guaranteed to carry a JSON body at all (a gateway can
    answer a 502 with HTML), and a transport failure must not be masked by a
    parsing failure while the client is working out what went wrong.
    """
    try:
        error = response.json().get("error") or {}
    except (ValueError, AttributeError):
        return "", ""
    if not isinstance(error, dict):
        return "", ""
    return str(error.get("code") or ""), str(error.get("message") or "")


def _is_already_exists(message: str) -> bool:
    """True if a Graph error message says the object is already there."""
    lowered = message.lower()
    return any(substring in lowered for substring in ALREADY_EXISTS_SUBSTRINGS)


def _json_body(response: Any) -> dict:
    """The response body as a dict, or an empty dict if it has none.

    Some successful Graph writes answer 204 with no body. A caller that only
    needs the write to have happened should not have to guard against that.
    """
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def new_guid() -> str:
    """Mint an identifier for a scope or an app role.

    The single mint point in the script, so a caller that must reuse an existing
    identifier has exactly one function to avoid and the tests have exactly one
    thing to substitute.
    """
    return str(uuid.uuid4())


def backend_app_payload(
    name: str,
    audience: str,
    scope_id: str,
    role_ids: dict[str, str],
    permission_ids: dict[str, str],
) -> dict:
    """Build the create body for the backend app registration.

    Everything the registration needs goes in this ONE request, and that is a
    requirement rather than an optimisation. An existing registration's exposed
    scope and app roles are never edited afterwards — editing them would break the
    role assignments already made against them — so a field missing here stays
    missing until somebody deletes the registration and starts over.

    Args:
        name: the registration's display name, as it appears in the portal.
        audience: the identifier URI clients request tokens for. Must be unique in
            the tenant and must not end in a slash.
        scope_id: identifier for the exposed delegated scope. The
            single-page-application registration has to request this exact
            identifier, so on a re-run it is read back off the existing
            registration rather than minted again.
        role_ids: app role value -> identifier, for all three platform roles.
        permission_ids: Microsoft Graph permission name -> identifier, ideally
            resolved from the tenant's own Graph service principal;
            ``GRAPH_APP_PERMISSIONS`` supplies any that are missing.
    """
    return {
        "displayName": name,
        "signInAudience": "AzureADMyOrg",
        "identifierUris": [audience],
        "api": {
            "requestedAccessTokenVersion": 2,
            "oauth2PermissionScopes": [
                {
                    "id": scope_id,
                    "value": SCOPE_NAME,
                    # "User" is the portal's "Admins and users":
                    # https://learn.microsoft.com/en-us/graph/api/resources/permissionscope
                    "type": "User",
                    "isEnabled": True,
                    "adminConsentDisplayName": "Access AGP",
                    "adminConsentDescription": (
                        "Allows the app to call the AGP API on behalf of the signed-in user."
                    ),
                    "userConsentDisplayName": "Access AGP",
                    "userConsentDescription": ("Allows the app to call the AGP API on your behalf."),
                }
            ],
        },
        "appRoles": [
            {
                "id": role_ids[value],
                "value": value,
                "displayName": _ROLE_DETAILS[value][0],
                "description": _ROLE_DETAILS[value][1],
                # "User" is the portal's "Users/Groups", people rather than applications:
                # https://learn.microsoft.com/en-us/graph/api/resources/approle
                "allowedMemberTypes": ["User"],
                "isEnabled": True,
            }
            for value in PLATFORM_ROLES
        ],
        # "Role" is an application permission and "Scope" a delegated one:
        # https://learn.microsoft.com/en-us/graph/api/resources/resourceaccess
        "requiredResourceAccess": [
            {
                "resourceAppId": GRAPH_APP_ID,
                "resourceAccess": [
                    {
                        "id": permission_ids.get(permission) or fallback_id,
                        "type": "Role",
                    }
                    for permission, fallback_id in GRAPH_APP_PERMISSIONS.items()
                ],
            }
        ],
        "passwordCredentials": [{"displayName": _SECRET_DISPLAY_NAME}],
    }


def spa_app_payload(name: str, backend_app_id: str, scope_id: str) -> dict:
    """Build the create body for the single-page-application registration.

    Deliberately minimal, and the absences matter as much as the contents: no
    client secret (a browser cannot keep one), no identifier URI and no app roles
    (this registration exposes no API of its own), and no Microsoft Graph
    permissions (it only ever calls the platform's backend).

    Args:
        name: the registration's display name, as it appears in the portal.
        backend_app_id: the backend registration's CLIENT id. Its directory object
            id and its identifier URI are both rejected here.
        scope_id: the identifier of the backend's delegated scope, which must be
            the very one the backend registration exposes.
    """
    return {
        "displayName": name,
        "signInAudience": "AzureADMyOrg",
        "spa": {"redirectUris": []},
        "requiredResourceAccess": [
            {
                "resourceAppId": backend_app_id,
                "resourceAccess": [{"id": scope_id, "type": "Scope"}],
            }
        ],
    }


BACKEND_APP_PLACEHOLDER = "<backend-app-id>"
BACKEND_APP_OBJECT_PLACEHOLDER = "<backend-app-object-id>"
BACKEND_SP_PLACEHOLDER = "<backend-sp-id>"
SPA_APP_PLACEHOLDER = "<spa-app-id>"
SPA_APP_OBJECT_PLACEHOLDER = "<spa-app-object-id>"
SPA_SP_PLACEHOLDER = "<spa-sp-id>"

UNKNOWN_UNTIL_REAL_RUN = "<created-on-real-run>"

EXISTING_SECRET = "<existing secret — cannot be re-read>"  # noqa: S105 — the output marker for a secret no run can re-read, not a credential; expires 2027-10-23; added E49A-50A/T19

OUTCOME_CREATED = "created"
OUTCOME_ROTATED = "rotated"
OUTCOME_EXISTING = "existing"

_APPLICATION_SELECT = "id,appId,displayName,identifierUris,api,appRoles,requiredResourceAccess,spa"

_SERVICE_PRINCIPAL_SELECT = "id,appId,appRoleAssignmentRequired"


class Executor:
    """The seam that makes ``--dry-run`` a flag rather than a second code path.

    Every mutation in the flow goes through ``apply``. A real run sends it; a dry
    run prints it. The flow above is identical either way, which is the only way a
    dry run can be trusted to describe what a real run would do — a parallel
    "planning" implementation would drift from the real one on its first edit.

    Whether an already-exists answer is success is the CALLER's judgement, passed in
    per mutation, because the two halves of this flow disagree about it and getting
    it wrong in either direction is a lie to the operator. See ``tolerate_existing``.
    """

    def __init__(self, client: GraphClient | None, dry_run: bool) -> None:
        self._client = client
        self.dry_run = dry_run

    def apply(
        self,
        description: str,
        method: str,
        path: str,
        payload: dict,
        *,
        tolerate_existing: bool = False,
    ) -> dict | None:
        """Perform one mutation, or describe it.

        Returns the created object on a real run. ``None`` means "there is nothing
        to read back", which covers all three of: a dry run, an object that was
        already there and was tolerated, and a PATCH — Graph answers a successful
        PATCH with 204 and an empty body, so ``apply`` hands back an empty dict for
        one rather than pretending it produced something.

        ``tolerate_existing`` says whether an already-exists answer is the desired
        end state, and it defaults to NO because the dangerous half is the quiet
        one. For a consent or a role assignment the existing object IS what this run
        wanted, nothing is read back from the response, and re-sending is cheaper
        than a pre-check — so those pass ``True`` and the answer is logged as
        success. For a CREATE it is the exact opposite: the response is the only
        source of the new object's id, its client id and, for the backend, its
        client secret. Treating that as success leaves the rest of the run with
        nothing to reference, so it fails here, naming the cause.
        """
        if self.dry_run:
            print(f"WOULD {method} {path} — {description}")
            print(_indented(json.dumps(payload, indent=2, ensure_ascii=False)))
            return None

        print(f"{method} {path} — {description}")
        try:
            if method == "POST":
                return self._client.post(path, payload)
            if method == "PATCH":
                self._client.patch(path, payload)
                return {}
        except AlreadyExists as err:
            if not tolerate_existing:
                raise _create_conflict(description, err) from err
            print(f"  already present — {description}")
            return None
        raise ValueError(f"unsupported method: {method}")


def _create_conflict(description: str, err: AlreadyExists) -> GraphError:
    """An already-exists answer to a call whose response was the only source of truth.

    Kept as a message rather than a silent success because of what it means here:
    this run looked for the object moments earlier and did not find it, so whatever
    is blocking the create is something the lookup cannot see. Naming that, and the
    one thing it usually is, is the difference between a five-minute fix in the
    admin center and an operator staring at a duplicate-value error.
    """
    return GraphError(
        err.status,
        err.code,
        f"Could not {description}. Microsoft Entra reports that the object already "
        "exists, but the lookup this run performed first found nothing — so what is "
        "blocking the create is a registration this script cannot see, and there is "
        "no object id, no client id and no client secret to carry on with. The usual "
        "cause is a registration holding the same identifier URI or the same display "
        "name that has been DELETED: Microsoft Entra keeps a deleted app "
        "registration for 30 days, still holding both, and does not list it among "
        "the live ones. Look under App registrations, Deleted applications, in the "
        "Microsoft Entra admin center, and either restore it (a re-run then reuses "
        "it) or delete it permanently; or re-run against a different name or a "
        f"different --audience. Microsoft Graph reported: {err.message or 'no details'}",
    )


def _indented(text: str, prefix: str = "  ") -> str:
    """Shift a block right, so a printed payload reads as part of its plan line."""
    return "\n".join(f"{prefix}{line}" for line in text.splitlines())


@dataclass
class Context:
    """The facts about the tenant and the operator that the rest of the run needs.

    Read once, at the start, because every one of them is referenced by several
    later calls and because reading them first is what makes a misconfigured
    environment fail before anything has been created.
    """

    me_id: str
    me_upn: str
    tenant_id: str
    tenant_domain: str
    graph_sp_id: str
    permission_ids: dict[str, str]


def resolve_context(client: GraphClient) -> Context:
    """Read the runner, the tenant, and Microsoft Graph's own service principal.

    The tenant id comes from the directory's own organization object rather than
    from the token, so the value printed for the deployment is the one Graph
    reports for the objects this run actually touched.

    The six permission identifiers are resolved from the tenant's Graph service
    principal, whose ``appRoles`` collection maps each permission's name to its
    identifier, and fall back PER ENTRY to the hardcoded table. Per entry rather
    than wholesale: a tenant that exposes five of the six should still get five
    resolved values, and the sixth is exactly the one worth being loud about.
    """
    me = client.get("/me")
    me_id = str(me.get("id") or "")
    me_upn = str(me.get("userPrincipalName") or "")
    if not me_id:
        raise PreflightError(
            "Microsoft Graph did not report who is signed in, so this run cannot "
            "assign the administrator role to anybody. Confirm 'az account show' "
            "reports the expected user and tenant, then re-run."
        )

    organization = _first(client.get("/organization", params={"$select": "id,displayName,verifiedDomains"}))
    graph_sp = _first(
        client.get(
            "/servicePrincipals",
            params={
                "$filter": f"appId eq '{GRAPH_APP_ID}'",
                "$select": "id,displayName,appRoles",
            },
        )
    )
    graph_sp_id = str(graph_sp.get("id") or "")
    if not graph_sp_id:
        raise PreflightError(
            "Microsoft Graph's own service principal could not be found in this "
            "tenant, and admin consent cannot be granted without it. This is "
            "usually a sign the account is signed in to a different tenant than "
            "the intended one: check 'az account show' and re-run."
        )

    return Context(
        me_id=me_id,
        me_upn=me_upn,
        tenant_id=str(organization.get("id") or ""),
        tenant_domain=_initial_domain(organization, me_upn),
        graph_sp_id=graph_sp_id,
        permission_ids=_permission_ids(graph_sp),
    )


def _initial_domain(organization: dict, me_upn: str) -> str:
    """The tenant's own ``*.onmicrosoft.com`` domain, which the frontend displays.

    The entry flagged ``isInitial`` is the one, and it is neither necessarily the
    first in the list nor the default: a tenant with a custom domain usually has
    that one marked default and listed first. Taking either would print a domain
    that is real but not the one asked for.

    The last resort is the signed-in account's own domain suffix, which is right
    for a member administrator and wrong for a guest — hence last.
    """
    domains = [domain for domain in (organization.get("verifiedDomains") or []) if isinstance(domain, dict)]
    for domain in domains:
        if domain.get("isInitial") and domain.get("name"):
            return str(domain["name"])
    for domain in domains:
        name = str(domain.get("name") or "")
        if name.endswith(".onmicrosoft.com"):
            return name
    return me_upn.partition("@")[2]


def _permission_ids(graph_sp: dict) -> dict[str, str]:
    """Map each of the six permission names to its identifier in THIS tenant."""
    by_value = {
        str(role.get("value")): str(role.get("id"))
        for role in graph_sp.get("appRoles") or []
        if isinstance(role, dict) and role.get("id") and role.get("value")
    }
    return {name: by_value.get(name) or fallback for name, fallback in GRAPH_APP_PERMISSIONS.items()}


def find_backend(client: GraphClient, audience: str) -> dict | None:
    """The existing backend registration, matched on its identifier URI.

    The identifier URI is unique tenant-wide; a display name is not. Matching the
    backend on its name would let a second registration that happens to share the
    name shadow the real one, and the whole deployment reads its client id from
    whichever this function returned.
    """
    matches = _values(
        client.get(
            "/applications",
            params={
                "$filter": f"identifierUris/any(u:u eq '{_odata_literal(audience)}')",
                "$select": _APPLICATION_SELECT,
            },
        )
    )
    return matches[0] if matches else None


def find_spa(client: GraphClient, name: str, *, variants: tuple[str, ...] = ()) -> dict | None:
    """The existing frontend registration, matched on its display name.

    The display name is all there is: this registration exposes no API, so it has
    no identifier URI. Display names are not unique, so more than one match is
    refused rather than resolved by picking the first — silently choosing would
    point the deployment at a registration nobody named.

    ``variants`` are additional spellings of the same intended name, queried after
    it. They exist because the manual setup guide's examples use an em dash where
    this script's default uses a plain hyphen, and a tenant built by following that
    guide has to be recognised rather than duplicated. Each spelling is a separate
    ``displayName eq`` query, so their results are disjoint by construction and a
    match under any spelling counts towards the same ambiguity check: two
    registrations that both mean "the frontend" are exactly as unresolvable as two
    that share one name.
    """
    matches: list[dict] = []
    for candidate in dict.fromkeys((name, *variants)):
        matches.extend(
            _values(
                client.get(
                    "/applications",
                    params={
                        "$filter": f"displayName eq '{_odata_literal(candidate)}'",
                        "$select": _APPLICATION_SELECT,
                    },
                )
            )
        )
    if len(matches) > 1:
        found = ", ".join(sorted({repr(str(app.get("displayName") or "")) for app in matches}))
        raise PreflightError(
            f"{len(matches)} app registrations in this tenant answer to the frontend "
            f"name {name!r} ({found}), and a display name is the only way to identify "
            "the frontend one. Rename or remove the duplicates in the Microsoft Entra "
            "admin center, or point this run at a single one with --spa-name."
        )
    return matches[0] if matches else None


def _odata_literal(value: str) -> str:
    """Escape a value for a single-quoted filter literal by doubling its quotes.

    A single quote would otherwise close the literal early and turn a lookup into
    a malformed query. Reachable in practice: the audience and both display names
    arrive from the command line.
    """
    return value.replace("'", "''")


def _values(body: dict) -> list[dict]:
    """The ``value`` collection of a Graph list response, tolerating its absence."""
    values = body.get("value")
    if not isinstance(values, list):
        return []
    return [item for item in values if isinstance(item, dict)]


def _first(body: dict) -> dict:
    """The first entry of a Graph list response, or an empty dict."""
    values = _values(body)
    return values[0] if values else {}


def compare_backend(app: dict, audience: str) -> list[str]:
    """Compare an existing backend registration against what this setup expects.

    Returns one human line per difference; an empty list means there is nothing to
    report. Nothing here repairs anything, and that is the design: ``appRoles`` and
    ``api.oauth2PermissionScopes`` are collection properties that a PATCH replaces
    wholesale, so "fixing" one re-mints the identifiers of the entries it rewrites.
    Assignments are keyed on those identifiers, so every user already assigned to a
    role would keep an assignment pointing at a role that no longer exists — and
    nothing would error. Their roles claim would simply stop appearing.

    The permission comparison uses this module's table rather than the identifiers
    resolved from the tenant, because the registration under comparison was built
    from that table.
    """
    lines: list[str] = []

    uris = [str(uri) for uri in app.get("identifierUris") or []]
    if audience not in uris:
        lines.append(f"identifier URI: expected {audience}, found {', '.join(uris) if uris else 'none'}")
    elif len(uris) > 1:
        others = ", ".join(uri for uri in uris if uri != audience)
        lines.append(
            f"identifier URI: expected only {audience}, found {len(uris)} on the "
            f"registration ({others} as well) — a token issued for one of the others "
            "is refused by the backend as a wrong audience"
        )

    api = app.get("api") if isinstance(app.get("api"), dict) else {}
    version = api.get("requestedAccessTokenVersion")
    if version != 2:
        lines.append(
            "api.requestedAccessTokenVersion: expected 2, found "
            f"{version if version is not None else 'none'} — the frontend's "
            "library expects version 2 tokens"
        )

    scopes = {
        str(scope.get("value")): scope
        for scope in api.get("oauth2PermissionScopes") or []
        if isinstance(scope, dict)
    }
    lines.extend(_compare_entry("delegated scope", SCOPE_NAME, scopes.get(SCOPE_NAME)))

    roles = {str(role.get("value")): role for role in app.get("appRoles") or [] if isinstance(role, dict)}
    for value in PLATFORM_ROLES:
        lines.extend(_compare_entry("app role", value, roles.get(value)))

    declared: set[str] = set()
    for entry in app.get("requiredResourceAccess") or []:
        if isinstance(entry, dict) and entry.get("resourceAppId") == GRAPH_APP_ID:
            declared.update(
                str(item.get("id")) for item in entry.get("resourceAccess") or [] if isinstance(item, dict)
            )
    for name, permission_id in GRAPH_APP_PERMISSIONS.items():
        if permission_id not in declared:
            lines.append(f"Microsoft Graph permission {name}: expected in requiredResourceAccess, found none")

    return lines


def compare_spa(app: dict, backend_app_id: str, scope_id: str | None) -> list[str]:
    """Compare an existing frontend registration against what this setup expects.

    This one earns its place precisely because the lookup behind it is weak. The
    frontend registration is matched on display name alone, so an unrelated
    application that happens to carry that name is adopted, printed as the
    deployment's client id, and then fails at sign-in with a scope or redirect error
    that names neither this script nor the registration it picked. Comparing what
    was adopted is the only warning available.

    Two things are checked, and they are the two a browser sign-in cannot survive
    without: the single-page-application platform, which is what enables the
    authorization-code-with-PKCE flow and gives the deployment's redirect URI
    somewhere to live, and the declared access to the backend's own delegated scope.

    Reported, never repaired, for the same reason as the backend's comparison:
    ``requiredResourceAccess`` is a collection property that a PATCH replaces
    wholesale, and the operator's own reasons for the difference are not knowable
    from here.
    """
    lines: list[str] = []

    if not isinstance(app.get("spa"), dict):
        lines.append(
            "single-page-application platform: expected the registration to declare "
            "one, found none — without it the browser sign-in flow is not enabled and "
            "the deployment's redirect URI has nowhere to be registered"
        )

    resources: set[str] = set()
    granted: set[str] = set()
    for entry in app.get("requiredResourceAccess") or []:
        if not isinstance(entry, dict):
            continue
        resources.add(str(entry.get("resourceAppId") or ""))
        if entry.get("resourceAppId") == backend_app_id:
            granted.update(
                str(item.get("id")) for item in entry.get("resourceAccess") or [] if isinstance(item, dict)
            )

    if backend_app_id not in resources:
        lines.append(
            "backend API access: expected requiredResourceAccess to name the backend "
            f"client id {backend_app_id}, found "
            f"{', '.join(sorted(resources)) if resources else 'none'} — a frontend "
            "that does not request the backend's scope cannot get a token for it"
        )
    elif scope_id is not None and scope_id not in granted:
        lines.append(
            f"delegated scope {SCOPE_NAME}: expected requiredResourceAccess to name "
            f"the backend's scope {scope_id}, found "
            f"{', '.join(sorted(granted)) if granted else 'none'} — sign-in asks for "
            "a scope the backend does not expose and is refused"
        )

    return lines


def _compare_entry(kind: str, value: str, entry: dict | None) -> list[str]:
    """One drift line for a scope or an app role that is missing or turned off."""
    if entry is None:
        return [f"{kind} {value}: expected on the registration, found none"]
    if not entry.get("isEnabled"):
        return [f"{kind} {value}: expected enabled, found disabled"]
    return []


def render_output(
    *,
    tenant_id: str,
    tenant_domain: str,
    audience: str,
    backend_client_id: str,
    spa_client_id: str,
    secret: str | None,
    secret_outcome: str,
    me_upn: str,
    admin_assigned: bool,
    dry_run: bool,
) -> str:
    """Render the values the operator has to paste, and the caveats that go with them.

    This text is the script's only product — it writes nothing to disk — so the key
    names are a contract with the deployment's own variables and with the
    frontend's environment file, and are spelled here exactly as those files expect
    them. Paste-ready blocks first, caveats after, because an operator reads top to
    bottom and the two blocks are why they ran this.

    ``secret_outcome`` is ``OUTCOME_CREATED``, ``OUTCOME_ROTATED`` or ``OUTCOME_EXISTING`` — what this
    run did, or what a dry run has already worked out a real run WOULD do, about the
    backend's client secret. It is passed in rather than inferred from ``secret``
    because a dry run holds no secret in any of the three cases, and telling an
    operator to wait for a secret that a real run is never going to mint is how a
    working re-run gets read as a failure.
    """
    known_tenant_id = _printable(tenant_id)
    known_spa_client_id = _printable(spa_client_id)

    tfvars = {
        "entra_tenant_id": known_tenant_id,
        "entra_audience": audience,
        "entra_backend_client_id": _printable(backend_client_id),
        "entra_backend_client_secret": _secret_cell(secret, secret_outcome),
        "entra_spa_client_id": known_spa_client_id,
    }
    width = max(len(key) for key in tfvars)
    frontend = {
        "VITE_AUTH_PROVIDER": "entra",
        "VITE_ENTRA_TENANT_ID": known_tenant_id,
        "VITE_ENTRA_TENANT_DOMAIN": _printable(tenant_domain),
        "VITE_ENTRA_SPA_CLIENT_ID": known_spa_client_id,
        "VITE_ENTRA_SPA_SCOPE": f"{audience}/{SCOPE_NAME}",
    }

    rule = "=" * 78
    lines = [
        rule,
        " Configuration values",
        rule,
        "",
        "1. platform/control_plane/infrastructure/secrets.auto.tfvars",
        "",
    ]
    lines += [f'{key.ljust(width)} = "{value}"' for key, value in tfvars.items()]
    lines += [
        "",
        "2. platform/control_plane/frontend/.env",
        "",
    ]
    lines += [f"{key}={value}" for key, value in frontend.items()]
    lines += [
        "",
        "3. After you deploy",
        "",
        "   Two frontend values do not exist yet, because the addresses they hold do",
        "   not exist until the deployment does. Read them from 'terraform output'",
        "   after the first apply: VITE_API_URL from api_endpoint, and",
        "   VITE_ENTRA_SPA_REDIRECT_URI from the frontend address with",
        "   '/auth/callback' on the end.",
        "",
        "   Then register that same redirect URI on the frontend app registration in",
        "   Microsoft Entra as well, under Authentication. Setting it only in the",
        "   .env file is the step that silently breaks sign-in: the browser is sent",
        "   to an address the registration does not list, and the error names the",
        "   redirect URI rather than the file you have to change.",
        "",
        "4. The client secret",
        "",
    ]
    lines += [f"   {line}" for line in _secret_note(secret, secret_outcome, dry_run)]
    lines += [
        "",
        "5. What this run did for you",
        "",
    ]
    lines += [f"   {line}" for line in _runner_note(me_upn, admin_assigned, dry_run)]
    lines += ["", rule]
    return "\n".join(lines)


def _printable(value: str) -> str:
    """A value fit to paste, or a marker saying only a real run can produce it.

    Everything a dry run cannot know is named ``<something>``, so one prefix test
    covers all of them and no angle-bracketed identifier can reach a config file
    looking like a value.
    """
    return UNKNOWN_UNTIL_REAL_RUN if not value or value.startswith("<") else value


def _secret_cell(secret: str | None, secret_outcome: str) -> str:
    """What goes on the secret's line: the value, or why there is not one.

    Keyed on the outcome rather than on whether this is a dry run, because the two
    answer different questions and only the outcome answers the operator's. A dry
    run against a tenant whose backend registration already exists has already
    established that no secret is coming, in that run or in the real one after it.
    """
    if secret:
        return secret
    if secret_outcome == OUTCOME_EXISTING:
        return EXISTING_SECRET
    return UNKNOWN_UNTIL_REAL_RUN


def _secret_note(secret: str | None, secret_outcome: str, dry_run: bool) -> list[str]:
    """The paragraph that decides whether the secret above gets copied or scrolled past."""
    if secret:
        return [
            "The value above is shown here and nowhere else. Microsoft Entra returns",
            "a client secret only in the response that creates it and cannot",
            "disclose it again, so no later run of this script can recover it. Copy",
            "it into secrets.auto.tfvars now.",
        ]
    if dry_run and secret_outcome == OUTCOME_EXISTING:
        return [
            "The backend app registration already exists, so a real run will NOT",
            "create a secret — and the existing one cannot be read back, by this",
            "script or by anybody else. Keep the value already in",
            "secrets.auto.tfvars. To add a second, valid alongside the first, re-run",
            "with --rotate-secret.",
        ]
    if dry_run and secret_outcome == OUTCOME_ROTATED:
        return [
            "Nothing was created, so there is no secret yet. A real run with",
            "--rotate-secret adds a SECOND secret to the existing backend",
            "registration, valid alongside the one already in use, and prints it",
            "exactly once.",
        ]
    if dry_run:
        return [
            "Nothing was created, so there is no secret yet. A real run creates one",
            "with the backend registration and prints it exactly once.",
        ]
    return [
        "The backend app registration already existed, so no secret was created —",
        "and the existing one cannot be read back, by this script or by anybody",
        "else. Keep the value already in secrets.auto.tfvars. To add a second,",
        "valid alongside the first, re-run with --rotate-secret.",
    ]


def _runner_note(me_upn: str, admin_assigned: bool, dry_run: bool) -> list[str]:
    """What the run did for the person who ran it — or did not do, and why."""
    if not admin_assigned:
        return [
            f"{me_upn} was NOT assigned to Platform.Admin: the backend registration",
            "does not expose that role. Until it does, no sign-in reaches the",
            "platform with administrator rights.",
        ]
    if dry_run:
        return [
            f"A real run would assign {me_upn} to Platform.Admin on",
            "the backend service principal, which is what puts the role in that",
            "account's token.",
        ]
    return [
        f"This run assigned {me_upn} to Platform.Admin on the",
        "backend service principal, which is what puts the role in that account's",
        "token. An access token issued before now still carries the old, roleless",
        "claim until it expires, so sign out and back in if a session is already",
        "open.",
    ]


@dataclass
class RunState:
    """What the run has done so far, so a failure can still report it.

    The sharpest reason is the client secret: it is disclosed exactly once, in the
    response that creates the backend registration, so a run that dies after that
    response and before printing it destroys the operator's only copy and leaves
    deleting the registration as the only repair.

    The created-object flags are the second reason, and they are separate from the
    objects themselves on purpose. A found registration and a created one are the
    same shape of dict, and only this run knows which is which — telling an operator
    it created something it merely reused would send them looking for a mess that
    is not there, and staying silent about a registration created seconds before a
    failure leaves one they do not know to look for.
    """

    dry_run: bool = False
    backend_app: dict | None = None
    backend_created: bool = False
    secret: str | None = None
    backend_sp_id: str | None = None
    backend_sp_created: bool = False
    spa_app: dict | None = None
    spa_created: bool = False
    spa_sp_id: str | None = None
    spa_sp_created: bool = False
    admin_assigned: bool = False
    drift: list[str] = field(default_factory=list)


def _run(client: GraphClient, executor: Executor, args: argparse.Namespace, state: RunState) -> int:
    """Do the whole setup, in the one order the dependencies allow.

    The order is not a preference. A service principal cannot be created before the
    application it belongs to, a consent cannot name a principal that does not
    exist yet, and the frontend registration has to carry the identifier of the
    scope the backend exposes. Every step reads what it needs from the objects the
    previous ones returned, never from the arguments.
    """
    context = resolve_context(client)
    print(f"Signed in as {context.me_upn or 'an unknown account'}, in tenant {context.tenant_id}.")
    if executor.dry_run:
        print(
            "Dry run: reading the tenant and printing every change a real run would make. Nothing is written."
        )

    scope_id, role_ids, secret_outcome = _ensure_backend(client, executor, args, state, context)

    backend = state.backend_app or {}
    backend_app_id = str(backend.get("appId") or BACKEND_APP_PLACEHOLDER)
    audience = _matched_uri(backend, args.audience)

    state.backend_sp_id, existing_sp = _ensure_service_principal(
        executor,
        client,
        backend_app_id,
        "backend service principal",
        BACKEND_SP_PLACEHOLDER,
        lookup=not state.backend_created,
    )
    state.backend_sp_created = existing_sp is None
    if existing_sp is None:
        executor.apply(
            "require role assignment on the backend service principal",
            "PATCH",
            f"/servicePrincipals/{state.backend_sp_id}",
            {"appRoleAssignmentRequired": True},
        )
    elif not existing_sp.get("appRoleAssignmentRequired"):
        state.drift.append(
            "backend service principal: expected appRoleAssignmentRequired true, "
            "found false — anyone in the tenant can sign in until it is set"
        )

    for name, permission_id in context.permission_ids.items():
        executor.apply(
            f"grant admin consent for {name}",
            "POST",
            f"/servicePrincipals/{context.graph_sp_id}/appRoleAssignedTo",
            {
                "principalId": state.backend_sp_id,
                "resourceId": context.graph_sp_id,
                "appRoleId": permission_id,
            },
            tolerate_existing=True,
        )

    spa = find_spa(
        client,
        args.spa_name,
        variants=() if args.spa_name_given else (DEFAULT_SPA_NAME_VARIANT,),
    )
    if spa is not None:
        print(f"reusing the frontend app registration {spa.get('displayName')!r}")
        state.spa_app = spa
        state.drift.extend(compare_spa(spa, backend_app_id, scope_id))
        spa_lookup = True
    else:
        if scope_id is None:
            raise PreflightError(
                f"The backend app registration {backend.get('displayName')!r} does "
                f"not expose the {SCOPE_NAME} delegated scope, so a frontend "
                "registration cannot reference it. This script never edits an "
                "existing registration's exposed scope — rewriting it re-mints the "
                "identifier and orphans every assignment made against it — so add "
                "the scope in the Microsoft Entra admin center, or delete the "
                "registration and re-run to have it created whole."
            )
        payload = spa_app_payload(args.spa_name, backend_app_id, scope_id)
        created, landed = _create_spa(executor, client, args.spa_name, payload)
        state.spa_created = True
        state.spa_app = _created_or_planned(
            executor,
            created,
            payload,
            SPA_APP_OBJECT_PLACEHOLDER,
            SPA_APP_PLACEHOLDER,
            "frontend app registration",
        )
        spa_lookup = not landed

    spa_app = state.spa_app or {}
    spa_client_id = str(spa_app.get("appId") or SPA_APP_PLACEHOLDER)
    state.spa_sp_id, existing_spa_sp = _ensure_service_principal(
        executor,
        client,
        spa_client_id,
        "frontend service principal",
        SPA_SP_PLACEHOLDER,
        lookup=spa_lookup,
    )
    state.spa_sp_created = existing_spa_sp is None

    # Both ids are service principal object ids and the scope goes by its value:
    # https://learn.microsoft.com/en-us/graph/api/resources/oauth2permissiongrant
    executor.apply(
        "grant admin consent for the frontend's delegated scope",
        "POST",
        "/oauth2PermissionGrants",
        {
            "clientId": state.spa_sp_id,
            "consentType": "AllPrincipals",
            "resourceId": state.backend_sp_id,
            "scope": SCOPE_NAME,
        },
        tolerate_existing=True,
    )

    # On the backend principal: the same call on the frontend's answers 201 and the role reaches no token.
    admin_role_id = role_ids.get("Platform.Admin")
    if admin_role_id:
        executor.apply(
            f"assign {context.me_upn} to Platform.Admin",
            "POST",
            f"/servicePrincipals/{state.backend_sp_id}/appRoleAssignedTo",
            {
                "principalId": context.me_id,
                "resourceId": state.backend_sp_id,
                "appRoleId": admin_role_id,
            },
            tolerate_existing=True,
        )
        state.admin_assigned = True
    else:
        state.drift.append(
            "app role Platform.Admin: expected on the registration, found none — "
            "so the account running this was not assigned to it"
        )

    _report_drift(state.drift)
    print()
    print(
        render_output(
            tenant_id=context.tenant_id,
            tenant_domain=context.tenant_domain,
            audience=audience,
            backend_client_id=backend_app_id,
            spa_client_id=spa_client_id,
            secret=state.secret,
            secret_outcome=secret_outcome,
            me_upn=context.me_upn,
            admin_assigned=state.admin_assigned,
            dry_run=executor.dry_run,
        )
    )
    return 0


def _ensure_backend(
    client: GraphClient, executor: Executor, args: argparse.Namespace, state: RunState, context: Context
) -> tuple[str | None, dict[str, str], str]:
    """Find or create the backend registration and record it on ``state``.

    Returns the delegated scope's identifier (``None`` when an existing registration does not expose
    it), the app role identifiers by value, and the secret outcome, decided before anything is sent so
    a dry run predicts the branch a real run would take: ``OUTCOME_CREATED`` with the secret read off
    the create response, ``OUTCOME_ROTATED`` when ``--rotate-secret`` adds one to a found registration,
    ``OUTCOME_EXISTING`` otherwise.
    """
    found = find_backend(client, args.audience)
    if found is None and args.rotate_secret:
        raise PreflightError(
            "--rotate-secret adds a secret to a backend app registration that "
            f"already exists, and no registration in this tenant answers to "
            f"{args.audience}. Re-run without --rotate-secret to create it: the "
            "create response is where a first secret comes from."
        )

    if found is not None:
        print(f"reusing the backend app registration {found.get('displayName')!r}")
        state.backend_app = found
        state.drift.extend(compare_backend(found, args.audience))
        scope_id = _scope_id(found)
        role_ids = _role_ids(found)
        secret_outcome = OUTCOME_ROTATED if args.rotate_secret else OUTCOME_EXISTING
        if args.rotate_secret:
            state.secret = _add_secret(executor, found)
    else:
        secret_outcome = OUTCOME_CREATED
        scope_id = new_guid()
        role_ids = {value: new_guid() for value in PLATFORM_ROLES}
        payload = backend_app_payload(
            args.backend_name,
            args.audience,
            scope_id,
            role_ids,
            context.permission_ids,
        )
        created = executor.apply("create the backend app registration", "POST", "/applications", payload)
        state.backend_created = True
        state.backend_app = _created_or_planned(
            executor,
            created,
            payload,
            BACKEND_APP_OBJECT_PLACEHOLDER,
            BACKEND_APP_PLACEHOLDER,
            "backend app registration",
        )
        state.secret = _secret_text(created)
    return scope_id, role_ids, secret_outcome


def _ensure_service_principal(
    executor: Executor,
    client: GraphClient,
    app_id: str,
    description: str,
    placeholder: str,
    *,
    lookup: bool,
) -> tuple[str, dict | None]:
    """Find or create the service principal of one application.

    ``lookup`` is false on the path that just created the application: there is
    nothing to find, and in a dry run the application's id is a placeholder that no
    filter could match. Returns the principal's id and, when it was already there,
    the principal itself — so the caller can compare it without reading it twice.
    ``None`` in that second slot means "this run created it", which is what gates
    the one PATCH this script ever sends.
    """
    if lookup:
        matches = _values(
            client.get(
                "/servicePrincipals",
                params={
                    "$filter": f"appId eq '{_odata_literal(app_id)}'",
                    "$select": _SERVICE_PRINCIPAL_SELECT,
                },
            )
        )
        if matches:
            existing = matches[0]
            print(f"reusing the {description} ({existing.get('id')})")
            return str(existing.get("id") or placeholder), existing

    created = executor.apply(f"create the {description}", "POST", "/servicePrincipals", {"appId": app_id})
    sp_id = str((created or {}).get("id") or "")
    if sp_id:
        return sp_id, None
    if not executor.dry_run:
        raise _unreadable_create(description)
    return placeholder, None


def _create_spa(
    executor: Executor, client: GraphClient, name: str, payload: dict
) -> tuple[dict | None, bool]:
    """Create the frontend registration, recovering a create whose answer was lost.

    The frontend registration has no unique key — no identifier URI, and display
    names are not unique — so a create that reached Microsoft Entra but whose
    response never came back is indistinguishable from one that never happened.
    Reporting the failure without looking again would leave the registration
    behind, and the next run would create a SECOND one with the same name, at which
    point the lookup refuses to guess between them and the operator has to clean up
    by hand.

    So a transport failure is followed by exactly one more lookup. Only a transport
    failure: it is the one shape that means "no answer" rather than "no".
    """
    try:
        return (
            executor.apply("create the frontend app registration", "POST", "/applications", payload),
            True,
        )
    except GraphError as err:
        if err.status != 0 or err.code != "transport_failure":
            raise
        print(
            "The frontend app registration's create call got no answer. Looking "
            "again before reporting it: a create whose answer was lost cannot be "
            "told apart from one that never happened, and this registration has no "
            "unique key to check it by."
        )
        recovered = find_spa(client, name)
        if recovered is None:
            raise
        print(f"the create had landed after all: reusing {name!r}")
        return recovered, False


def _add_secret(executor: Executor, app: dict) -> str | None:
    """Add a client secret to an existing registration.

    Adds, never replaces: the credentials already on the registration keep working,
    which is what makes a rotation deployable without an outage. Delete the old one
    only once the deployment is healthy on the new one.
    """
    response = executor.apply(
        "add a client secret to the backend app registration",
        "POST",
        f"/applications/{app.get('id')}/addPassword",
        {"passwordCredential": {"displayName": _SECRET_DISPLAY_NAME}},
    )
    return str((response or {}).get("secretText") or "") or None


def _secret_text(created: dict | None) -> str | None:
    """The generated secret out of a create response — the only place it appears."""
    for credential in (created or {}).get("passwordCredentials") or []:
        if isinstance(credential, dict) and credential.get("secretText"):
            return str(credential["secretText"])
    return None


def _planned(payload: dict, object_placeholder: str, app_placeholder: str) -> dict:
    """The object a dry run would have created, as far as it can be known.

    Built from the payload that was about to be sent, so the identifiers minted for
    the scope and the roles are the ones the rest of the plan then references —
    which is what makes a dry run's later payloads internally consistent instead of
    full of unrelated placeholders.
    """
    return dict(payload, id=object_placeholder, appId=app_placeholder)


def _created_or_planned(
    executor: Executor,
    created: dict | None,
    payload: dict,
    object_placeholder: str,
    app_placeholder: str,
    description: str,
) -> dict:
    """The registration that was created — or, in a DRY RUN ONLY, the planned one.

    The gate is the whole point of this function. A placeholder object substituted
    into a real run is the worst failure this script has: the flow carries on in a
    live tenant against ``<backend-app-id>``, the calls that reference it are
    rejected with messages naming a value the operator has never seen, and the run
    can still reach a printed output block full of markers that read as success. So
    on a real run an unusable create response ends the run instead, with the one
    thing an operator can act on — that the registration may exist and a re-run will
    adopt it.
    """
    if created and created.get("id") and created.get("appId"):
        return created
    if not executor.dry_run:
        raise _unreadable_create(description)
    return _planned(payload, object_placeholder, app_placeholder)


def _unreadable_create(description: str) -> GraphError:
    """A create whose response carried no identifiers to carry on with.

    Status 0 for the same reason a transport failure uses it: there is no HTTP
    status to report — the call succeeded — so rendering one would send the operator
    looking up a code that does not exist.
    """
    return GraphError(
        0,
        "unreadable_create_response",
        f"Microsoft Entra accepted the {description} but answered with no object id "
        "or client id, so there is nothing for the rest of this run to reference and "
        "nothing further was attempted. The object is probably there: re-run with the "
        "same arguments and the lookup will find and reuse it.",
    )


def _scope_id(app: dict) -> str | None:
    """The identifier of the delegated scope on an existing registration.

    Read back, never re-minted: the frontend registration references this scope by
    identifier, so a fresh one would point at nothing at all.
    """
    api = app.get("api") if isinstance(app.get("api"), dict) else {}
    for scope in api.get("oauth2PermissionScopes") or []:
        if isinstance(scope, dict) and scope.get("value") == SCOPE_NAME and scope.get("id"):
            return str(scope["id"])
    return None


def _role_ids(app: dict) -> dict[str, str]:
    """The app role identifiers on an existing registration, keyed by role value.

    Read back for the same reason as the scope, and a sharper one: an assignment
    names the role by identifier, so re-minting one leaves every user already
    assigned holding an assignment that points at nothing, with no error anywhere.
    """
    return {
        str(role["value"]): str(role["id"])
        for role in app.get("appRoles") or []
        if isinstance(role, dict) and role.get("id") and role.get("value")
    }


def _matched_uri(app: dict, requested: str) -> str:
    """The identifier URI this run MATCHED, read off the registration itself.

    Not an arbitrary entry of the collection. An application may carry several
    identifier URIs — a tenant that renamed its audience keeps the old one beside the
    new — and only one of them is the audience this deployment is being configured
    for. Printing another is the shape that fails as ``401 Invalid audience`` on
    every single API call, with an error naming a URI nobody typed.

    Nor is it the requested value echoed back, which would make the printed audience
    a restatement of the flag rather than a fact about the tenant: on the create path
    this is read out of the create response, so a directory that normalised the URI
    is reported as it stored it.
    """
    uris = [str(uri) for uri in app.get("identifierUris") or []]
    if requested in uris:
        return requested
    return uris[0] if uris else requested


def _report_drift(lines: list[str]) -> None:
    """Print the differences found on registrations that already existed."""
    if not lines:
        return
    print()
    print("Drift report — what this tenant already holds differs from what this setup expects:")
    for line in lines:
        print(f"  - {line}")
    print(
        "Nothing above was changed. Rewriting an existing registration's app roles "
        "or exposed scope re-mints their identifiers, which orphans every "
        "assignment already made against them and breaks sign-in for everyone who "
        "has one — so these are reported for you to settle in the Microsoft Entra "
        "admin center, or by deleting the registration and re-running."
    )


def _report_partial(state: RunState) -> None:
    """Print what a failed run found and created, before the error is reported.

    Drift first, because those lines were collected before the failure and are
    frequently WHY it happened: a missing app role or an unset
    ``appRoleAssignmentRequired`` is exactly the kind of difference that makes a
    later call behave in a way the error message does not explain. Discarding them
    on the one path where they are most useful would be perverse.

    Then everything this run created, whether or not it captured a secret. Two
    different things are being rescued here and only one of them is the secret: an
    app registration created seconds before a failure is invisible unless the
    operator is told it is there, and a reuse-path failure creates plenty (the
    frontend registration, its principal) while minting no secret at all. The secret
    paragraph is the conditional part, because there is only sometimes a secret, and
    it is the one thing no later run can ever recover.

    A dry run has nothing to report here: it created nothing and holds no secret.
    """
    _report_drift(state.drift)
    if state.dry_run:
        return

    created: list[tuple[str, str]] = []
    if state.backend_created:
        created.append(("backend app registration", _object_label(state.backend_app)))
    if state.backend_sp_created and state.backend_sp_id:
        created.append(("backend service principal", state.backend_sp_id))
    if state.spa_created:
        created.append(("frontend app registration", _object_label(state.spa_app)))
    if state.spa_sp_created and state.spa_sp_id:
        created.append(("frontend service principal", state.spa_sp_id))

    if not created and not state.secret:
        return

    rule = "=" * 78
    print()
    print(rule)
    print(" PARTIAL SETUP — read this before the error below")
    print(rule)
    if created:
        print()
        print("This run created the following before it stopped:")
        for label, detail in created:
            print(f"  {label:26} {detail}")
    if state.secret:
        print()
        print(f'entra_backend_client_secret = "{state.secret}"')
        print()
        print(
            "That secret cannot be read again by anybody: Microsoft Entra returns a "
            "client secret only in the response that created it. Copy it into "
            "platform/control_plane/infrastructure/secrets.auto.tfvars now, before "
            "anything else."
        )
    print()
    print(
        "Then re-run this script with the same arguments. It finds everything above "
        "and carries on from where this run stopped — it creates no second "
        "registration and mints no second secret."
    )
    print(rule)


def _object_label(app: dict | None) -> str:
    """A registration named the two ways an operator can go and look it up.

    An unreadable create response is the one case where neither is known: the
    registration exists and the answer describing it did not arrive. Saying that is
    more use than printing ``None`` twice.
    """
    app = app or {}
    return (
        f"{app.get('displayName') or '(display name not reported)'} "
        f"(client id {app.get('appId') or 'not reported'})"
    )


def _describe(err: GraphError) -> str:
    """One line for a Graph failure, without inventing an HTTP status.

    A transport failure carries status 0 as a sentinel — there was no response at
    all — so rendering it as "HTTP 0" would send the operator looking for a status
    code that does not exist.
    """
    message = err.message or "Microsoft Graph refused the call."
    if err.status == 0:
        return message
    detail = f"HTTP {err.status}" + (f", {err.code}" if err.code else "")
    return f"{message} ({detail})"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line.

    Five flags, and the defaults are contracts: the audience and both display names
    are the values the setup guide and the deployment's own variables use, so a run
    with no flags produces a paste-ready output block. ``--spa-name`` has no argparse default so that
    ``spa_name_given`` records whether the operator named the frontend registration; only the accepted
    default also matches the setup guide's em-dash spelling of the same name.
    """
    parser = argparse.ArgumentParser(
        prog="setup_entra.py",
        description=(
            "Create the two Microsoft Entra app registrations the platform needs, "
            "grant the admin consents, assign the account running this to the "
            "administrator role, and print the configuration values to paste into "
            "the deployment. Writes nothing to disk. Safe to re-run: existing "
            "registrations are reused and reported on, never edited."
        ),
        epilog=(
            "Sign in first with 'az login', as an account holding Application "
            "Administrator together with Privileged Role Administrator, or Global "
            "Administrator on its own. Run with --dry-run first."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "read the tenant and print every change a real run would make, "
            "with its payload, without making any of them"
        ),
    )
    parser.add_argument(
        "--audience",
        default=DEFAULT_AUDIENCE,
        help=(
            "the backend's application ID URI, which is also what the deployment "
            "and the frontend's scope have to name (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--backend-name",
        default=DEFAULT_BACKEND_NAME,
        help="display name for the backend app registration (default: %(default)s)",
    )
    parser.add_argument(
        "--spa-name",
        default=None,
        help=(f"display name for the frontend app registration (default: {DEFAULT_SPA_NAME})"),
    )
    parser.add_argument(
        "--rotate-secret",
        action="store_true",
        help=(
            "add a second client secret to an existing backend app registration. "
            "The current one keeps working; delete it in the portal once the "
            "deployment is healthy on the new one"
        ),
    )
    args = parser.parse_args(argv)
    args.spa_name_given = args.spa_name is not None
    if args.spa_name is None:
        args.spa_name = DEFAULT_SPA_NAME
    return args


def main(argv: list[str] | None = None) -> int:
    """Run the setup and return the process exit code.

    Three outcomes, and they are different on purpose. 0 means the tenant is in the
    intended state, drift lines included — they are a report, not a failure. 2 means
    nothing was attempted: the environment or the arguments are wrong, and there is
    nothing to retry. 1 means Microsoft Graph refused or could not be reached
    part-way through, which is the only outcome that can leave work half done — so
    it is also the one that prints what was created first.
    """
    args = parse_args(argv)
    state = RunState(dry_run=args.dry_run)
    try:
        client = GraphClient(get_az_token())
        return _run(client, Executor(client, dry_run=args.dry_run), args, state)
    except PreflightError as err:
        _report_partial(state)
        print(f"Setup stopped: {err}", file=sys.stderr)
        return 2
    except GraphError as err:
        _report_partial(state)
        print(f"Setup stopped: {_describe(err)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
