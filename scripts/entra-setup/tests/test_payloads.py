"""Tests for the pure application-registration payload builders.

These are the highest-value tests in the script: the builders produce the bodies
of two ``POST /applications`` calls, and several of the fields cannot be repaired
after the fact. An application's exposed scope and app roles are not edited on a
re-run — doing so would invalidate the assignments already made against them — so
a field missing from the CREATE body stays missing until someone deletes the
registration and starts over.

No network, no Azure CLI: the builders are pure functions over their arguments.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

# The script is a standalone single file, not an installed package: put its
# directory on the import path so the tests run the same way regardless of the
# working directory pytest was invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import setup_entra  # noqa: E402  (import follows the sys.path bootstrap above)


# The six application-permission identifiers, transcribed a SECOND time — from the
# published Microsoft Graph permission table's "Application" column, not from the
# module. Two independent transcriptions of the same source is the entire value
# here: a slip has to be made twice, in two files, to reach a tenant. Comparing
# the payload against the module's own table (which the rest of this file does)
# cannot catch a wrong identifier, because the payload is built from that table.
GRAPH_APPLICATION_PERMISSION_IDS = {
    "Application.ReadWrite.All": "1bfefb4e-e0b5-418b-a88f-73c46d2cc8e9",
    "AppRoleAssignment.ReadWrite.All": "06b708a9-e830-4db3-a914-8e69da51d44f",
    "DelegatedPermissionGrant.ReadWrite.All": "8e8e4742-1d95-4f68-9d56-6ee75648c72a",
    "User.Read.All": "df021288-bdef-4463-88db-98f22de89214",
    "Group.Read.All": "5b567255-7703-4780-807c-7be8301ae99b",
    "Directory.Read.All": "7ab1d382-f21e-4acd-a863-ba3e13f7da61",
}

# The DELEGATED identifiers for those same six permissions, from the other column
# of that table. They are here to be excluded: the realistic slip is reading
# across the row and taking a permission's delegated identifier for its
# application one, which leaves all six names correct and all six values distinct
# — so neither the name assertion nor the distinctness assertion below can see it.
DELEGATED_PERMISSION_IDS = frozenset(
    {
        "bdfbf15f-ee85-4955-8675-146e8e5296b5",  # Application.ReadWrite.All
        "84bccea3-f856-4a8a-967b-dbe0a3d53a64",  # AppRoleAssignment.ReadWrite.All
        "41ce6ca6-6826-4807-84f1-1c82854f7ee5",  # DelegatedPermissionGrant.ReadWrite.All
        "a154be20-db9c-4678-8ab7-66f6cc099a59",  # User.Read.All
        "5f8c59db-677d-491f-a6b8-5f174b11ec1d",  # Group.Read.All
        "06da0dbc-49e2-44d2-8312-53f166ab848a",  # Directory.Read.All
    }
)

SCOPE_ID = "aaaaaaaa-0000-0000-0000-000000000001"
BACKEND_APP_ID = "bbbbbbbb-0000-0000-0000-000000000002"
ROLE_IDS = {
    "Platform.Admin": "cccccccc-0000-0000-0000-000000000003",
    "Platform.Operator": "cccccccc-0000-0000-0000-000000000004",
    "Platform.Viewer": "cccccccc-0000-0000-0000-000000000005",
}


def backend_payload() -> dict:
    return setup_entra.backend_app_payload(
        name="AGP - Backend Graph Client",
        audience="api://agp",
        scope_id=SCOPE_ID,
        role_ids=ROLE_IDS,
        permission_ids=dict(setup_entra.GRAPH_APP_PERMISSIONS),
    )


# ===========================================================================
# Shared identity constants
# ===========================================================================
def test_six_graph_application_permissions_are_declared() -> None:
    """The backend needs exactly these six application permissions, and every
    GUID is a distinct one — a copy-paste slip here grants the wrong permission
    silently, because Graph validates the GUID but not the operator's intent."""
    assert len(setup_entra.GRAPH_APP_PERMISSIONS) == 6
    assert len(set(setup_entra.GRAPH_APP_PERMISSIONS.values())) == 6
    assert set(setup_entra.GRAPH_APP_PERMISSIONS) == {
        "Application.ReadWrite.All",
        "AppRoleAssignment.ReadWrite.All",
        "DelegatedPermissionGrant.ReadWrite.All",
        "User.Read.All",
        "Group.Read.All",
        "Directory.Read.All",
    }


def test_graph_application_permission_identifiers_match_the_published_table() -> None:
    """Every one of the six identifiers, pinned to its published value.

    This is the only assertion in the suite that can catch a wrong identifier, and
    it is why the literals above are transcribed from the source rather than read
    off the module. A wrong identifier is silently accepted by Microsoft Graph —
    it validates the format, not the intent — and it goes into the ONE create call
    that is never repaired afterwards, so the registration ends up declaring a
    permission nobody asked for and the consent step then grants it tenant-wide.
    """
    assert setup_entra.GRAPH_APP_PERMISSIONS == GRAPH_APPLICATION_PERMISSION_IDS
    # None of the six is a delegated identifier wearing an application name.
    assert not set(setup_entra.GRAPH_APP_PERMISSIONS.values()) & DELEGATED_PERMISSION_IDS


def test_new_guid_mints_a_fresh_random_identifier() -> None:
    """Single mint point, so a test or a re-run path can substitute it wholesale."""
    first, second = setup_entra.new_guid(), setup_entra.new_guid()

    assert first != second
    assert uuid.UUID(first).version == 4


# ===========================================================================
# Backend application
# ===========================================================================
def test_backend_payload_requests_version_two_tokens() -> None:
    """``requestedAccessTokenVersion: 2`` must ride along in the CREATE body.

    It is what makes the short ``api://`` identifier URI acceptable, and it is
    what makes the issued tokens the version the frontend's library expects. Sent
    later, it is too late: the identifier URI has already been rejected.
    """
    payload = backend_payload()

    assert payload["api"]["requestedAccessTokenVersion"] == 2
    assert payload["identifierUris"] == ["api://agp"]
    assert payload["signInAudience"] == "AzureADMyOrg"
    assert payload["displayName"] == "AGP - Backend Graph Client"


def test_backend_payload_exposes_the_delegated_scope() -> None:
    """One scope, consentable by admins AND users, enabled from the start."""
    scopes = backend_payload()["api"]["oauth2PermissionScopes"]

    assert len(scopes) == 1
    scope = scopes[0]
    assert scope["id"] == SCOPE_ID
    assert scope["value"] == setup_entra.SCOPE_NAME == "Access.Default"
    # "User" is the portal's "Admins and users"; "Admin" would lock users out.
    assert scope["type"] == "User"
    assert scope["isEnabled"] is True


def test_backend_payload_declares_all_three_platform_roles() -> None:
    """The role VALUES are the contract the backend authorizes against, so they
    are asserted by value rather than by position."""
    roles = backend_payload()["appRoles"]

    assert [role["value"] for role in roles] == list(setup_entra.PLATFORM_ROLES)
    assert {role["value"] for role in roles} == {
        "Platform.Admin",
        "Platform.Operator",
        "Platform.Viewer",
    }
    for role in roles:
        assert role["id"] == ROLE_IDS[role["value"]]
        # "User" is the portal's "Users/Groups": these roles are assigned to people.
        assert role["allowedMemberTypes"] == ["User"]
        assert role["isEnabled"] is True
        assert role["displayName"]
        assert role["description"]


def test_backend_payload_requests_six_application_permissions() -> None:
    """``type: "Role"`` is an APPLICATION permission; ``"Scope"`` would be
    delegated and would leave the backend unable to act on its own."""
    resource_access = backend_payload()["requiredResourceAccess"]

    assert len(resource_access) == 1
    assert resource_access[0]["resourceAppId"] == setup_entra.GRAPH_APP_ID
    entries = resource_access[0]["resourceAccess"]
    assert len(entries) == 6
    assert all(entry["type"] == "Role" for entry in entries)
    assert {entry["id"] for entry in entries} == set(
        setup_entra.GRAPH_APP_PERMISSIONS.values()
    )


def test_backend_payload_uses_resolved_permission_guids_when_supplied() -> None:
    """Permission GUIDs are preferably read back from the tenant's own Microsoft
    Graph service principal; the built-in table is only the fallback."""
    resolved = dict(setup_entra.GRAPH_APP_PERMISSIONS)
    resolved["User.Read.All"] = "dddddddd-0000-0000-0000-000000000006"

    payload = setup_entra.backend_app_payload(
        name="a name",
        audience="api://agp",
        scope_id=SCOPE_ID,
        role_ids=ROLE_IDS,
        permission_ids=resolved,
    )

    entries = payload["requiredResourceAccess"][0]["resourceAccess"]
    assert "dddddddd-0000-0000-0000-000000000006" in {entry["id"] for entry in entries}


def test_backend_payload_asks_for_the_secret_inline() -> None:
    """Requesting the password in the same POST is the only way to ever read it:
    the generated value comes back in the create response and can never be
    retrieved again. No ``endDateTime`` — Graph's default is two years, which is
    the lifetime the setup guide assumes."""
    credentials = backend_payload()["passwordCredentials"]

    assert len(credentials) == 1
    assert credentials[0]["displayName"]
    assert "endDateTime" not in credentials[0]


# ===========================================================================
# Single-page application
# ===========================================================================
def test_spa_payload_registers_the_spa_platform_explicitly() -> None:
    """The empty ``spa`` object is load-bearing, not a placeholder.

    It selects the authorization-code-with-PKCE flow and the no-secret client
    that the frontend needs. Omitting it leaves the registration with no platform
    at all, and the deployment's callback URL then has nowhere to be added.
    """
    payload = setup_entra.spa_app_payload(
        name="AGP - Frontend", backend_app_id=BACKEND_APP_ID, scope_id=SCOPE_ID
    )

    assert payload["spa"] == {"redirectUris": []}
    assert payload["signInAudience"] == "AzureADMyOrg"
    assert payload["displayName"] == "AGP - Frontend"


def test_spa_payload_requests_the_backend_scope_as_delegated() -> None:
    """The SPA calls the backend AS THE SIGNED-IN USER, so the requested access
    is ``type: "Scope"``. The resource is the backend's client id — its object id
    or its identifier URI would both be rejected here."""
    payload = setup_entra.spa_app_payload(
        name="AGP - Frontend", backend_app_id=BACKEND_APP_ID, scope_id=SCOPE_ID
    )

    resource_access = payload["requiredResourceAccess"]
    assert len(resource_access) == 1
    assert resource_access[0]["resourceAppId"] == BACKEND_APP_ID
    assert resource_access[0]["resourceAccess"] == [{"id": SCOPE_ID, "type": "Scope"}]


def test_spa_payload_carries_no_secret_and_no_identifier_uri() -> None:
    """A public client cannot keep a secret, and the SPA exposes no API of its
    own — so both fields have to be absent, not empty."""
    payload = setup_entra.spa_app_payload(
        name="AGP - Frontend", backend_app_id=BACKEND_APP_ID, scope_id=SCOPE_ID
    )

    assert "passwordCredentials" not in payload
    assert "identifierUris" not in payload
    assert "appRoles" not in payload
    assert "api" not in payload
