"""Tests for the orchestrated flow: ordering, idempotency, dry-run, and the CLI.

Fully offline. The Azure CLI step and the Graph transport are both replaced: the
token comes from a stub and ``GraphClient`` is swapped for ``FakeClient``, a tiny
in-memory tenant that answers the reads out of its own state and records every
write. So these tests assert on the SEQUENCE of mutations, which is the part of
this script that cannot be verified any other way — Microsoft Graph's ordering
constraints are dependencies, not preferences, and a run that gets them wrong
fails in the middle with a tenant half-configured.

Five behaviours are pinned here because each one is a documented way to lose data
or waste an operator's afternoon:

1. **The order.** A service principal cannot be created before its application, a
   consent cannot name a principal that does not exist yet, and the frontend
   registration has to reference the backend's scope identifier.
2. **Re-runs create nothing.** The backend is found by its identifier URI and the
   frontend by display name; the consents are re-POSTed and their already-exists
   answer is success.
3. **Existing registrations are never edited.** Rewriting an app's roles re-mints
   their identifiers and silently orphans every assignment made against them, so
   drift is reported and left alone.
4. **The client secret survives a failure.** Microsoft Entra discloses it exactly
   once, in the response that creates it. A run that dies afterwards must still
   print it, or the operator's only copy is gone.
5. **Dry-run writes nothing at all.** It is the safe first run against a
   configured tenant, so "zero mutations" is a test, not a promise.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

# The script is a standalone single file, not an installed package: put its
# directory on the import path so the tests run the same way regardless of the
# working directory pytest was invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import setup_entra  # noqa: E402  (import follows the sys.path bootstrap above)


# ===========================================================================
# The scripted tenant
# ===========================================================================
TENANT_ID = "11111111-1111-1111-1111-111111111111"
GRAPH_SP_ID = "22222222-2222-2222-2222-222222222222"
ME_ID = "33333333-3333-3333-3333-333333333333"
ME_UPN = "admin@contoso.onmicrosoft.com"
INITIAL_DOMAIN = "contoso.onmicrosoft.com"

# Identifiers on the ALREADY-EXISTING registrations, deliberately unlike anything
# the fake mints for a create: a re-run that quietly re-minted a scope or role
# identifier would show up as a changed value here rather than as a pass.
EXISTING_SCOPE_ID = "aaaaaaaa-0000-0000-0000-000000000001"
EXISTING_ROLE_IDS = {
    "Platform.Admin": "aaaaaaaa-0000-0000-0000-000000000002",
    "Platform.Operator": "aaaaaaaa-0000-0000-0000-000000000003",
    "Platform.Viewer": "aaaaaaaa-0000-0000-0000-000000000004",
}
EXISTING_BACKEND_OBJECT_ID = "existing-backend-object-id"
EXISTING_BACKEND_CLIENT_ID = "existing-backend-client-id"
EXISTING_SPA_OBJECT_ID = "existing-spa-object-id"
EXISTING_SPA_CLIENT_ID = "existing-spa-client-id"

# What THIS tenant says the six Microsoft Graph permissions are called, deliberately
# unlike the module's fallback table. A fake whose Graph principal is built from that
# table cannot tell a tenant-resolved identifier from a hardcoded one, which makes
# the resolution itself untestable — the very feature that exists so a renamed or
# unavailable permission fails loudly instead of granting the wrong thing quietly.
TENANT_PERMISSION_IDS = {
    name: f"tenant-permission-{index}"
    for index, name in enumerate(setup_entra.GRAPH_APP_PERMISSIONS, start=1)
}


@dataclass
class Call:
    """One recorded call. ``payload`` carries the body for a write and the query
    parameters for a read, because both are things a test needs to assert on."""

    method: str
    path: str
    payload: dict | None


def graph_service_principal(*, app_roles: bool = True) -> dict:
    """Microsoft Graph's own service principal, as the tenant returns it.

    ``app_roles=False`` models a tenant whose Graph principal does not expose the
    ``appRoles`` collection the script prefers to resolve permission identifiers
    from, which is what the hardcoded fallback table exists for.
    """
    roles = [
        {"id": permission_id, "value": name, "isEnabled": True}
        for name, permission_id in setup_entra.GRAPH_APP_PERMISSIONS.items()
    ]
    return {
        "id": GRAPH_SP_ID,
        "appId": setup_entra.GRAPH_APP_ID,
        "displayName": "Microsoft Graph",
        "appRoles": roles if app_roles else [],
    }


def graph_service_principal_with(permission_ids: dict[str, str]) -> dict:
    """Microsoft Graph's service principal as a tenant that reports its OWN ids."""
    return dict(
        graph_service_principal(),
        appRoles=[
            {"id": permission_id, "value": name, "isEnabled": True}
            for name, permission_id in permission_ids.items()
        ],
    )


def organization(*, verified_domains: list[dict] | None = None) -> dict:
    """The tenant's organization object.

    The initial ``*.onmicrosoft.com`` domain is deliberately NOT first in the
    list: the value the frontend needs is the one flagged ``isInitial``, and a
    reader that takes the first entry would pass every other assertion in this
    file while printing a custom domain the sign-in flow cannot use.
    """
    if verified_domains is None:
        verified_domains = [
            {"name": "contoso.com", "isInitial": False, "isDefault": True},
            {"name": INITIAL_DOMAIN, "isInitial": True, "isDefault": False},
        ]
    return {
        "id": TENANT_ID,
        "displayName": "Contoso",
        "verifiedDomains": verified_domains,
    }


def existing_backend_application(
    *,
    roles: tuple[str, ...] = setup_entra.PLATFORM_ROLES,
    token_version: Any = 2,
    audience: str = setup_entra.DEFAULT_AUDIENCE,
    scope: bool = True,
    permissions: bool = True,
) -> dict:
    """A backend registration already present in the tenant, as Graph returns it.

    Every keyword is a drift lever, so one fixture covers the clean re-run and
    each individual thing the script must report rather than repair.
    """
    scopes = (
        [
            {
                "id": EXISTING_SCOPE_ID,
                "value": setup_entra.SCOPE_NAME,
                "type": "User",
                "isEnabled": True,
            }
        ]
        if scope
        else []
    )
    resource_access = [
        {"id": permission_id, "type": "Role"}
        for permission_id in setup_entra.GRAPH_APP_PERMISSIONS.values()
    ]
    return {
        "id": EXISTING_BACKEND_OBJECT_ID,
        "appId": EXISTING_BACKEND_CLIENT_ID,
        "displayName": setup_entra.DEFAULT_BACKEND_NAME,
        "identifierUris": [audience],
        "api": {
            "requestedAccessTokenVersion": token_version,
            "oauth2PermissionScopes": scopes,
        },
        "appRoles": [
            {
                "id": EXISTING_ROLE_IDS[value],
                "value": value,
                "isEnabled": True,
                "allowedMemberTypes": ["User"],
            }
            for value in roles
        ],
        "requiredResourceAccess": [
            {
                "resourceAppId": setup_entra.GRAPH_APP_ID,
                "resourceAccess": resource_access if permissions else [],
            }
        ],
    }


def existing_spa_application(
    *,
    name: str | None = None,
    platform: bool = True,
    backend_app_id: str = EXISTING_BACKEND_CLIENT_ID,
    scope_id: str = EXISTING_SCOPE_ID,
) -> dict:
    """A frontend registration already present in the tenant.

    The three levers model the reason this registration is compared at all: it is
    matched on display name alone, so an application that merely shares the name is
    adopted as the deployment's frontend. ``platform=False`` and a foreign
    ``backend_app_id``/``scope_id`` are what such an application looks like.
    """
    app = {
        "id": EXISTING_SPA_OBJECT_ID,
        "appId": EXISTING_SPA_CLIENT_ID,
        "displayName": name or setup_entra.DEFAULT_SPA_NAME,
        "requiredResourceAccess": [
            {
                "resourceAppId": backend_app_id,
                "resourceAccess": [{"id": scope_id, "type": "Scope"}],
            }
        ],
    }
    if platform:
        app["spa"] = {"redirectUris": []}
    return app


def service_principal_for(
    app_id: str, *, assignment_required: bool = True, sp_id: str | None = None
) -> dict:
    """A service principal already present in the tenant, keyed by its app id."""
    return {
        "id": sp_id or f"sp-of-{app_id}",
        "appId": app_id,
        "appRoleAssignmentRequired": assignment_required,
    }


class FakeClient:
    """An in-memory stand-in for ``GraphClient`` over a scripted tenant.

    Reads are answered from the tenant state, so the same object models a fresh
    tenant, a fully configured one, and a drifted one purely by what it is
    constructed with. Writes are recorded and applied to that state, which is
    what lets a test assert both the ORDER of the mutations and that a re-run
    against the resulting state creates nothing.

    ``failures`` maps ``(method, path)`` to what should happen instead of the
    normal answer. A list is consumed one entry per matching call, which is the
    only way to distinguish the two ``POST /applications`` calls; ``None`` inside
    a list means "behave normally this time". A callable is invoked with
    ``(client, path, payload)`` and may mutate the tenant before raising, which
    is how a create that LANDED but whose response was lost is modelled.
    """

    def __init__(
        self,
        *,
        applications: list[dict] | None = None,
        service_principals: list[dict] | None = None,
        me: dict | None = None,
        org: dict | None = None,
        graph_sp: dict | None = None,
        failures: dict[tuple[str, str], Any] | None = None,
    ) -> None:
        self.applications = [dict(app) for app in (applications or [])]
        self.service_principals = [dict(sp) for sp in (service_principals or [])]
        self.service_principals.append(graph_sp or graph_service_principal())
        self.me = me or {"id": ME_ID, "userPrincipalName": ME_UPN, "displayName": "Admin"}
        self.org = org or organization()
        self._failures = {
            key: value if isinstance(value, list) else [value] * 1000
            for key, value in (failures or {}).items()
        }
        self.calls: list[Call] = []
        self._app_count = 0
        self._sp_count = 0

    # --- what the tests assert on -----------------------------------------
    @property
    def mutations(self) -> list[Call]:
        return [call for call in self.calls if call.method in ("POST", "PATCH")]

    @property
    def mutation_sequence(self) -> list[tuple[str, str]]:
        return [(call.method, call.path) for call in self.mutations]

    def sent(self, method: str, path: str) -> list[dict]:
        """Every body sent to one endpoint, in order."""
        return [
            call.payload or {}
            for call in self.calls
            if call.method == method and call.path == path
        ]

    # --- the GraphClient surface ------------------------------------------
    def get(self, path: str, params: dict | None = None) -> dict:
        self.calls.append(Call("GET", path, params))
        scripted = self._scripted("GET", path, params)
        if scripted is not _NORMAL:
            return scripted
        if path == "/me":
            return dict(self.me)
        if path == "/organization":
            return {"value": [dict(self.org)]}
        if path == "/applications":
            return {"value": self._filter_applications((params or {}).get("$filter", ""))}
        if path == "/servicePrincipals":
            return {
                "value": self._filter_service_principals(
                    (params or {}).get("$filter", "")
                )
            }
        raise AssertionError(f"the flow read an endpoint the fake does not model: {path}")

    def post(self, path: str, payload: dict) -> dict:
        self.calls.append(Call("POST", path, payload))
        scripted = self._scripted("POST", path, payload)
        if scripted is not _NORMAL:
            return scripted
        if path == "/applications":
            return self.store_application(payload)
        if path == "/servicePrincipals":
            return self.store_service_principal(payload)
        if path.endswith("/addPassword"):
            return {"secretText": "rotated-secret", "keyId": "rotated-key-id"}
        if path.endswith("/appRoleAssignedTo") or path == "/oauth2PermissionGrants":
            return dict(payload, id="assignment-id")
        raise AssertionError(f"the flow wrote an endpoint the fake does not model: {path}")

    def patch(self, path: str, payload: dict) -> None:
        self.calls.append(Call("PATCH", path, payload))
        scripted = self._scripted("PATCH", path, payload)
        if scripted is not _NORMAL:
            return None
        for sp in self.service_principals:
            if path == f"/servicePrincipals/{sp['id']}":
                sp.update(payload)
        return None

    # --- tenant state ------------------------------------------------------
    def store_application(self, payload: dict) -> dict:
        """Apply a ``POST /applications`` to the tenant and answer with the object.

        The generated secret text is attached here because that is where it comes
        from in reality: the create response is the only place it ever appears.
        """
        self._app_count += 1
        created = dict(payload)
        created["id"] = f"created-app-object-{self._app_count}"
        created["appId"] = f"created-app-client-{self._app_count}"
        if created.get("passwordCredentials"):
            created["passwordCredentials"] = [
                dict(
                    credential,
                    secretText=f"generated-secret-{self._app_count}",
                    keyId=f"created-key-{self._app_count}",
                )
                for credential in created["passwordCredentials"]
            ]
        self.applications.append(created)
        return created

    def store_service_principal(self, payload: dict) -> dict:
        self._sp_count += 1
        created = {
            "id": f"created-sp-{self._sp_count}",
            "appId": payload["appId"],
            "appRoleAssignmentRequired": False,
        }
        self.service_principals.append(created)
        return created

    # --- internals ---------------------------------------------------------
    def _scripted(self, method: str, path: str, payload: dict | None) -> Any:
        queued = self._failures.get((method, path))
        if not queued:
            return _NORMAL
        entry = queued.pop(0)
        if entry is None:
            return _NORMAL
        if isinstance(entry, Exception):
            raise entry
        if callable(entry):
            return entry(self, path, payload)
        return entry

    def _filter_applications(self, filter_value: str) -> list[dict]:
        if filter_value.startswith("identifierUris/any"):
            wanted = _quoted_literal(filter_value)
            return [
                dict(app)
                for app in self.applications
                if wanted in (app.get("identifierUris") or [])
            ]
        if filter_value.startswith("displayName eq"):
            wanted = _quoted_literal(filter_value)
            return [
                dict(app) for app in self.applications if app.get("displayName") == wanted
            ]
        raise AssertionError(f"unmodelled application filter: {filter_value!r}")

    def _filter_service_principals(self, filter_value: str) -> list[dict]:
        if not filter_value.startswith("appId eq"):
            raise AssertionError(f"unmodelled principal filter: {filter_value!r}")
        wanted = _quoted_literal(filter_value)
        return [dict(sp) for sp in self.service_principals if sp.get("appId") == wanted]


# Sentinel meaning "no failure was scripted for this call", so that a scripted
# answer of ``None`` (a PATCH's empty body) stays distinguishable from it.
_NORMAL = object()


def _status_codes(text: str) -> list[str]:
    """Every number the text presents as an HTTP status, however it is phrased."""
    return re.findall(r"(?:status[= ]|HTTP )(\d+)", text, re.IGNORECASE)


def _quoted_literal(filter_value: str) -> str:
    """The single-quoted OData literal out of a filter, un-escaping doubled quotes.

    Mirrors what Graph itself does, so a filter the script forgot to escape comes
    back as a lookup that does not match rather than as a silent pass.
    """
    start = filter_value.index("'") + 1
    end = filter_value.rindex("'")
    return filter_value[start:end].replace("''", "'")


@pytest.fixture
def run(monkeypatch) -> Callable[..., int]:
    """Run ``main`` against a ``FakeClient``, with the Azure CLI step stubbed out.

    Everything above the transport is the real code path: argument parsing, the
    flow, the drift report, and the printed output block.
    """

    def _run(client: FakeClient, argv: list[str] | None = None) -> int:
        token = setup_entra.AzToken(
            access_token="fake-access-token",
            tenant_id=TENANT_ID,
            expires_on=4102444800,
        )
        monkeypatch.setattr(setup_entra, "get_az_token", lambda: token)
        monkeypatch.setattr(
            setup_entra, "GraphClient", lambda *args, **kwargs: client
        )
        return setup_entra.main(argv or [])

    return _run


def fresh_tenant(**kwargs) -> FakeClient:
    """A tenant with neither registration in it."""
    return FakeClient(**kwargs)


def configured_tenant(*, backend: dict | None = None, **kwargs) -> FakeClient:
    """A tenant where a previous run already created everything."""
    backend_app = backend if backend is not None else existing_backend_application()
    return FakeClient(
        applications=[backend_app, existing_spa_application()],
        service_principals=[
            service_principal_for(EXISTING_BACKEND_CLIENT_ID),
            service_principal_for(EXISTING_SPA_CLIENT_ID),
        ],
        **kwargs,
    )


def already_exists() -> setup_entra.AlreadyExists:
    return setup_entra.AlreadyExists(
        400, "Request_BadRequest", "Permission entry already exists."
    )


# ===========================================================================
# The Executor seam
# ===========================================================================
def test_executor_sends_a_post_and_hands_back_the_response() -> None:
    """A real run's ``apply`` is a thin pass-through, because the response is the
    only place server-generated fields such as a secret's text ever appear."""
    client = fresh_tenant()
    executor = setup_entra.Executor(client, dry_run=False)

    created = executor.apply(
        "create the backend app registration", "POST", "/applications", {"displayName": "x"}
    )

    assert created is not None
    assert created["appId"] == "created-app-client-1"
    assert client.mutation_sequence == [("POST", "/applications")]


def test_executor_treats_a_tolerated_already_exists_as_success(capsys) -> None:
    """A duplicate consent IS the desired end state, so it is logged and the flow
    continues. Letting it escape would turn every re-run into a failure."""
    client = FakeClient(
        failures={("POST", "/oauth2PermissionGrants"): already_exists()}
    )
    executor = setup_entra.Executor(client, dry_run=False)

    result = executor.apply(
        "grant admin consent",
        "POST",
        "/oauth2PermissionGrants",
        {"scope": setup_entra.SCOPE_NAME},
        tolerate_existing=True,
    )

    assert result is None
    assert "already present" in capsys.readouterr().out


def test_executor_refuses_an_untolerated_already_exists() -> None:
    """The opposite judgement, and it is the default because the dangerous half is
    the quiet one.

    For a CREATE the response is the only source of the new object's id, its client
    id and its secret. An already-exists answer means there is none of that, so
    calling it success hands the rest of the run nothing to reference — and the
    lookup that ran moments earlier found nothing, so what is blocking the create is
    something this script cannot see. The message has to say so, and name the reason
    it usually is.
    """
    client = FakeClient(failures={("POST", "/applications"): already_exists()})
    executor = setup_entra.Executor(client, dry_run=False)

    with pytest.raises(setup_entra.GraphError) as excinfo:
        executor.apply(
            "create the backend app registration",
            "POST",
            "/applications",
            {"displayName": "x"},
        )

    message = str(excinfo.value)
    assert not isinstance(excinfo.value, setup_entra.AlreadyExists)
    assert "create the backend app registration" in message
    assert "30 days" in message
    assert "Deleted applications" in message


def test_executor_in_dry_run_prints_the_plan_and_sends_nothing(capsys) -> None:
    """The dry-run seam is the whole reason the flow has one: the flow logic is
    identical, and only the execution of a mutation is swapped for printing it."""
    client = fresh_tenant()
    executor = setup_entra.Executor(client, dry_run=True)

    result = executor.apply(
        "create the backend app registration",
        "POST",
        "/applications",
        {"displayName": "AGP - Backend Graph Client"},
    )

    out = capsys.readouterr().out
    assert result is None
    assert client.calls == []
    assert "WOULD POST /applications — create the backend app registration" in out
    # The payload is printed too: a plan the operator cannot read is not a plan.
    assert '"displayName": "AGP - Backend Graph Client"' in out


# ===========================================================================
# Reads
# ===========================================================================
def test_resolve_context_reads_the_runner_tenant_and_permissions() -> None:
    """The four facts every later call needs, in one place."""
    client = fresh_tenant()

    context = setup_entra.resolve_context(client)

    assert context.me_id == ME_ID
    assert context.me_upn == ME_UPN
    assert context.tenant_id == TENANT_ID
    assert context.graph_sp_id == GRAPH_SP_ID
    assert context.permission_ids == dict(setup_entra.GRAPH_APP_PERMISSIONS)


def test_resolve_context_prefers_the_initial_domain() -> None:
    """The frontend needs the tenant's own ``*.onmicrosoft.com`` domain, which is
    the entry flagged ``isInitial`` — not the first one and not the default one."""
    client = fresh_tenant()

    assert setup_entra.resolve_context(client).tenant_domain == INITIAL_DOMAIN


def test_resolve_context_falls_back_per_permission() -> None:
    """A tenant whose Graph principal does not list the roles still gets the six
    identifiers, from the hardcoded table, one entry at a time."""
    client = fresh_tenant(graph_sp=graph_service_principal(app_roles=False))

    context = setup_entra.resolve_context(client)

    assert context.permission_ids == dict(setup_entra.GRAPH_APP_PERMISSIONS)


def test_resolve_context_prefers_the_tenants_own_permission_ids() -> None:
    """The tenant's answer wins over this module's table, and the difference has to be
    observable to be worth anything.

    So this fixture's identifiers deliberately look nothing like
    ``GRAPH_APP_PERMISSIONS``: a lookup replaced by ``dict(GRAPH_APP_PERMISSIONS)``
    passes every test whose fake was built FROM that table, while shipping a script
    that grants whatever a stale hardcoded GUID happens to name in a tenant that
    reports something else.
    """
    client = fresh_tenant(graph_sp=graph_service_principal_with(TENANT_PERMISSION_IDS))

    context = setup_entra.resolve_context(client)

    assert context.permission_ids == TENANT_PERMISSION_IDS
    assert context.permission_ids != dict(setup_entra.GRAPH_APP_PERMISSIONS)


def test_resolve_context_falls_back_only_for_the_permissions_the_tenant_omits() -> None:
    """Per entry, not wholesale.

    A tenant that reports five of the six should get five resolved values and one
    fallback; discarding all five because one is missing would grant five permissions
    by a hardcoded GUID the tenant may not agree with.
    """
    partial = dict(TENANT_PERMISSION_IDS)
    del partial["Directory.Read.All"]
    client = fresh_tenant(graph_sp=graph_service_principal_with(partial))

    resolved = setup_entra.resolve_context(client).permission_ids

    assert resolved["User.Read.All"] == TENANT_PERMISSION_IDS["User.Read.All"]
    assert (
        resolved["Directory.Read.All"]
        == setup_entra.GRAPH_APP_PERMISSIONS["Directory.Read.All"]
    )


# ===========================================================================
# Existence lookups
# ===========================================================================
def test_find_backend_matches_on_the_identifier_uri() -> None:
    """The identifier URI is unique tenant-wide; the display name is not. Matching
    the backend on the name would let a second app with the same name shadow it."""
    client = configured_tenant()

    found = setup_entra.find_backend(client, setup_entra.DEFAULT_AUDIENCE)

    assert found is not None
    assert found["appId"] == EXISTING_BACKEND_CLIENT_ID
    assert client.calls[-1].payload["$filter"] == (
        f"identifierUris/any(u:u eq '{setup_entra.DEFAULT_AUDIENCE}')"
    )


def test_find_backend_escapes_a_quote_in_the_audience() -> None:
    """A single quote closes an OData literal early. Doubling it is the escape, and
    the flag it arrives on is operator-supplied, so this is reachable."""
    client = fresh_tenant()

    setup_entra.find_backend(client, "api://o'brien")

    assert client.calls[-1].payload["$filter"] == (
        "identifierUris/any(u:u eq 'api://o''brien')"
    )


def test_find_backend_returns_none_on_a_fresh_tenant() -> None:
    assert setup_entra.find_backend(fresh_tenant(), setup_entra.DEFAULT_AUDIENCE) is None


def test_find_spa_matches_on_the_display_name() -> None:
    """The frontend registration exposes no API, so it has no identifier URI and
    the display name is the only key there is."""
    client = configured_tenant()

    found = setup_entra.find_spa(client, setup_entra.DEFAULT_SPA_NAME)

    assert found is not None
    assert found["appId"] == EXISTING_SPA_CLIENT_ID
    assert client.calls[-1].payload["$filter"] == (
        f"displayName eq '{setup_entra.DEFAULT_SPA_NAME}'"
    )


def test_find_spa_refuses_to_guess_between_two_matches() -> None:
    """Display names are not unique. Picking the first match would silently point
    the deployment at whichever registration Graph happened to list first."""
    client = FakeClient(
        applications=[existing_spa_application(), existing_spa_application()]
    )

    with pytest.raises(setup_entra.PreflightError) as excinfo:
        setup_entra.find_spa(client, setup_entra.DEFAULT_SPA_NAME)

    assert setup_entra.DEFAULT_SPA_NAME in str(excinfo.value)
    assert "--spa-name" in str(excinfo.value)


# ===========================================================================
# Drift comparison
# ===========================================================================
def test_compare_backend_is_silent_on_a_matching_registration() -> None:
    """No news is the clean result: an empty list means nothing to report."""
    assert (
        setup_entra.compare_backend(
            existing_backend_application(), setup_entra.DEFAULT_AUDIENCE
        )
        == []
    )


@pytest.mark.parametrize(
    "app, expected",
    [
        (
            existing_backend_application(roles=("Platform.Admin", "Platform.Viewer")),
            "Platform.Operator",
        ),
        (existing_backend_application(token_version=1), "requestedAccessTokenVersion"),
        (existing_backend_application(scope=False), setup_entra.SCOPE_NAME),
        (existing_backend_application(permissions=False), "Directory.Read.All"),
        (existing_backend_application(audience="api://something-else"), "api://agp"),
    ],
)
def test_compare_backend_names_what_is_missing(app: dict, expected: str) -> None:
    """Each drift line names the thing an operator has to go and fix, because the
    script deliberately will not fix it: rewriting an existing registration's
    roles or scopes re-mints their identifiers and orphans every assignment."""
    lines = setup_entra.compare_backend(app, setup_entra.DEFAULT_AUDIENCE)

    assert any(expected in line for line in lines), lines


def test_compare_backend_reports_a_second_identifier_uri() -> None:
    """Both URIs are legal and only one is the audience this deployment is told to
    use, so the other is a token that the backend refuses with an error naming a URI
    nobody typed. Silence here is what makes that failure unattributable."""
    app = existing_backend_application()
    app["identifierUris"] = ["api://agp-legacy", setup_entra.DEFAULT_AUDIENCE]

    lines = setup_entra.compare_backend(app, setup_entra.DEFAULT_AUDIENCE)

    assert any("api://agp-legacy" in line for line in lines), lines


def test_compare_spa_is_silent_on_the_registration_this_script_creates() -> None:
    """The frontend registration this script writes has to compare clean against
    itself, or every re-run reports drift it caused."""
    assert (
        setup_entra.compare_spa(
            existing_spa_application(), EXISTING_BACKEND_CLIENT_ID, EXISTING_SCOPE_ID
        )
        == []
    )


@pytest.mark.parametrize(
    "app, expected",
    [
        (existing_spa_application(platform=False), "single-page-application platform"),
        (
            existing_spa_application(backend_app_id="an-unrelated-application"),
            "an-unrelated-application",
        ),
        (existing_spa_application(scope_id="an-unrelated-scope"), "an-unrelated-scope"),
    ],
)
def test_compare_spa_names_what_a_browser_sign_in_cannot_survive(
    app: dict, expected: str
) -> None:
    """The frontend registration is matched on display name and nothing else, so an
    application that merely shares that name is adopted, printed as the deployment's
    client id, and fails at sign-in with a scope or redirect error that names neither
    this script nor the registration it picked. This comparison is the only warning
    there is — and, like the backend's, it reports rather than repairs."""
    lines = setup_entra.compare_spa(app, EXISTING_BACKEND_CLIENT_ID, EXISTING_SCOPE_ID)

    assert any(expected in line for line in lines), lines


# ===========================================================================
# The fresh-tenant run
# ===========================================================================
def test_fresh_run_performs_the_documented_mutation_sequence(run, capsys) -> None:
    """The ordering is a dependency chain, not a preference.

    A service principal cannot be created before its application exists, a consent
    cannot name a principal that does not exist yet, and the frontend registration
    has to carry the scope identifier the backend exposes. A run that reorders
    these fails halfway with a tenant that is half-configured.
    """
    client = fresh_tenant()

    exit_code = run(client)

    assert exit_code == 0, capsys.readouterr()
    backend_sp = "created-sp-1"
    consent = f"/servicePrincipals/{GRAPH_SP_ID}/appRoleAssignedTo"
    assert client.mutation_sequence == [
        ("POST", "/applications"),
        ("POST", "/servicePrincipals"),
        ("PATCH", f"/servicePrincipals/{backend_sp}"),
        *[("POST", consent)] * 6,
        ("POST", "/applications"),
        ("POST", "/servicePrincipals"),
        ("POST", "/oauth2PermissionGrants"),
        ("POST", f"/servicePrincipals/{backend_sp}/appRoleAssignedTo"),
    ]


def test_fresh_run_sends_the_payload_fields_that_cannot_be_repaired(run) -> None:
    """The fields verified here are the ones a later PATCH cannot fix.

    ``requestedAccessTokenVersion`` has to travel in the same call as the
    identifier URI — it is what keeps a bare ``api://agp`` legal — and the empty
    ``spa`` object is what gives the frontend registration a platform at all.
    """
    client = fresh_tenant()

    assert run(client) == 0

    backend, spa = client.sent("POST", "/applications")
    assert backend["identifierUris"] == [setup_entra.DEFAULT_AUDIENCE]
    assert backend["api"]["requestedAccessTokenVersion"] == 2
    assert backend["displayName"] == setup_entra.DEFAULT_BACKEND_NAME
    assert backend["passwordCredentials"]  # the one chance to read a secret
    assert spa["spa"] == {"redirectUris": []}
    assert spa["displayName"] == setup_entra.DEFAULT_SPA_NAME

    # The hardest coupling in the script: the frontend references the backend's
    # scope BY IDENTIFIER, so the two values have to be the same GUID.
    scope_id = backend["api"]["oauth2PermissionScopes"][0]["id"]
    assert spa["requiredResourceAccess"][0]["resourceAccess"] == [
        {"id": scope_id, "type": "Scope"}
    ]
    assert spa["requiredResourceAccess"][0]["resourceAppId"] == "created-app-client-1"


def test_fresh_run_requires_assignment_and_consents_every_permission(run) -> None:
    """Assignment-required is what makes an unassigned user fail at sign-in instead
    of getting a token that quietly refuses every call later."""
    client = fresh_tenant()

    assert run(client) == 0

    assert client.sent("PATCH", "/servicePrincipals/created-sp-1") == [
        {"appRoleAssignmentRequired": True}
    ]
    consents = client.sent("POST", f"/servicePrincipals/{GRAPH_SP_ID}/appRoleAssignedTo")
    assert [consent["appRoleId"] for consent in consents] == list(
        setup_entra.GRAPH_APP_PERMISSIONS.values()
    )
    assert {consent["principalId"] for consent in consents} == {"created-sp-1"}
    assert {consent["resourceId"] for consent in consents} == {GRAPH_SP_ID}


def test_fresh_run_grants_the_delegated_scope_by_name(run) -> None:
    """The grant names the scope by its VALUE, not by the identifier URI form, and
    both sides are service principal OBJECT ids rather than client ids."""
    client = fresh_tenant()

    assert run(client) == 0

    assert client.sent("POST", "/oauth2PermissionGrants") == [
        {
            "clientId": "created-sp-2",
            "consentType": "AllPrincipals",
            "resourceId": "created-sp-1",
            "scope": setup_entra.SCOPE_NAME,
        }
    ]


def test_fresh_run_assigns_the_runner_on_the_backend_principal(run) -> None:
    """This must target the BACKEND principal. The same call against the frontend
    one answers a perfectly happy 201 and is a silent no-op — the role never
    reaches any token."""
    client = fresh_tenant()

    assert run(client) == 0

    backend = client.sent("POST", "/applications")[0]
    admin_role_id = next(
        role["id"] for role in backend["appRoles"] if role["value"] == "Platform.Admin"
    )
    assert client.sent("POST", "/servicePrincipals/created-sp-1/appRoleAssignedTo") == [
        {
            "principalId": ME_ID,
            "resourceId": "created-sp-1",
            "appRoleId": admin_role_id,
        }
    ]


# ===========================================================================
# The idempotent re-run
# ===========================================================================
def test_rerun_creates_nothing_and_succeeds(run, capsys) -> None:
    """A second run against a configured tenant is the normal case, not an edge
    case: the operator re-runs it to check the setup or after changing a flag.

    Both registrations are found, both principals are reused, and the consents are
    re-POSTed only because their already-exists answer is cheaper and more certain
    than a pre-check. Nothing is created and nothing is edited.
    """
    consent_path = f"/servicePrincipals/{GRAPH_SP_ID}/appRoleAssignedTo"
    client = configured_tenant(
        failures={
            ("POST", consent_path): [already_exists()] * 6,
            ("POST", "/oauth2PermissionGrants"): already_exists(),
            (
                "POST",
                f"/servicePrincipals/sp-of-{EXISTING_BACKEND_CLIENT_ID}/appRoleAssignedTo",
            ): already_exists(),
        }
    )

    exit_code = run(client)
    out = capsys.readouterr().out

    assert exit_code == 0, out
    assert client.sent("POST", "/applications") == []
    assert client.sent("POST", "/servicePrincipals") == []
    assert [call for call in client.mutations if call.method == "PATCH"] == []
    assert out.count("already present") == 8
    # Nothing was created, so the secret cannot be shown again.
    assert "<existing secret — cannot be re-read>" in out


def test_rerun_reads_the_identifiers_off_the_found_registration(run) -> None:
    """Re-minting them is the sharpest hazard in the script.

    Assignments are keyed on the role's identifier, so a fresh GUID for the same
    role name leaves every assigned user holding a role that no longer exists, with
    nothing erroring. The same is true of the scope: the frontend references it by
    identifier, so a fresh one points at nothing. This tenant has the backend but
    not the frontend, which is the case that forces both to be read back.
    """
    client = FakeClient(
        applications=[existing_backend_application()],
        service_principals=[service_principal_for(EXISTING_BACKEND_CLIENT_ID)],
    )

    assert run(client) == 0

    spa = client.sent("POST", "/applications")[0]
    assert spa["requiredResourceAccess"][0]["resourceAccess"] == [
        {"id": EXISTING_SCOPE_ID, "type": "Scope"}
    ]
    assert spa["requiredResourceAccess"][0]["resourceAppId"] == EXISTING_BACKEND_CLIENT_ID
    grants = client.sent(
        "POST", f"/servicePrincipals/sp-of-{EXISTING_BACKEND_CLIENT_ID}/appRoleAssignedTo"
    )
    assert grants[0]["appRoleId"] == EXISTING_ROLE_IDS["Platform.Admin"]


def test_the_printed_audience_is_read_back_from_the_created_registration(
    run, capsys
) -> None:
    """The audience in the block is a fact about the tenant, not a restatement of the
    flag.

    A directory that stores an identifier URI differently from the way it was asked
    for is the case that separates the two: echoing ``--audience`` prints a value the
    tenant does not hold, and every API call then fails as ``401 Invalid audience``
    against an identifier nobody can find.
    """
    stored_uri = "api://agp-as-the-directory-stored-it"

    def create_and_store_a_different_uri(
        client: FakeClient, path: str, payload: dict
    ) -> dict:
        created = client.store_application(payload)
        created["identifierUris"] = [stored_uri]
        return created

    client = fresh_tenant(
        failures={("POST", "/applications"): [create_and_store_a_different_uri]}
    )

    assert run(client) == 0
    out = capsys.readouterr().out

    assert re.search(rf'^entra_audience +\= "{re.escape(stored_uri)}"$', out, re.MULTILINE)
    assert f"VITE_ENTRA_SPA_SCOPE={stored_uri}/{setup_entra.SCOPE_NAME}" in out
    assert f"{setup_entra.DEFAULT_AUDIENCE}/{setup_entra.SCOPE_NAME}" not in out


def test_the_printed_audience_is_the_one_the_lookup_matched(run, capsys) -> None:
    """A tenant that renamed its audience keeps the old identifier URI beside the new
    one, and an application may carry several.

    Only one of them is the audience this deployment is being configured for — the
    one the lookup matched — and printing any other is the shape that fails as
    ``401 Invalid audience`` on every single API call. So neither the first entry nor
    the last is good enough; the matched one is. The others are reported as drift,
    because a registration with two audiences is a fact worth knowing.
    """
    backend = existing_backend_application()
    backend["identifierUris"] = [
        "api://agp-legacy",
        setup_entra.DEFAULT_AUDIENCE,
        "api://agp-newer",
    ]
    client = configured_tenant(backend=backend)

    assert run(client) == 0
    out = capsys.readouterr().out

    matched = re.escape(setup_entra.DEFAULT_AUDIENCE)
    assert re.search(rf'^entra_audience +\= "{matched}"$', out, re.MULTILINE)
    assert (
        f"VITE_ENTRA_SPA_SCOPE={setup_entra.DEFAULT_AUDIENCE}/{setup_entra.SCOPE_NAME}"
        in out
    )
    assert "api://agp-legacy/" not in out
    assert "api://agp-newer/" not in out
    # ...and the extra URIs are named, once, as drift.
    assert "identifier URI: expected only" in out


def test_a_backend_without_the_admin_role_assigns_nobody_and_says_so(
    run, capsys
) -> None:
    """The receipt has to match what happened.

    A registration that predates the role cannot have anybody assigned to it, and a
    block claiming otherwise is a sign-in failure with a reassuring explanation
    attached: the operator has been told they are an administrator and their token
    carries no roles claim at all.
    """
    client = configured_tenant(
        backend=existing_backend_application(
            roles=("Platform.Operator", "Platform.Viewer")
        )
    )

    exit_code = run(client)
    out = capsys.readouterr().out

    assert exit_code == 0, out
    assert (
        client.sent(
            "POST",
            f"/servicePrincipals/sp-of-{EXISTING_BACKEND_CLIENT_ID}/appRoleAssignedTo",
        )
        == []
    )
    assert f"{ME_UPN} was NOT assigned to Platform.Admin" in out
    assert f"This run assigned {ME_UPN}" not in out
    assert "app role Platform.Admin" in out


def test_a_backend_without_the_delegated_scope_refuses_to_create_the_frontend(
    run, capsys
) -> None:
    """The one guard between a missing scope and an unusable frontend registration.

    Without it the frontend is created asking for scope id ``null``: Microsoft Entra
    accepts that, the run exits 0, and sign-in fails on a scope that references
    nothing. Repairing the backend's exposed scope is not an option either — a PATCH
    replaces the collection wholesale and re-mints the identifier every existing
    assignment is keyed on — so the run stops and says where to fix it.
    """
    client = FakeClient(
        applications=[existing_backend_application(scope=False)],
        service_principals=[service_principal_for(EXISTING_BACKEND_CLIENT_ID)],
    )

    exit_code = run(client)
    captured = capsys.readouterr()

    assert exit_code == 2
    assert setup_entra.SCOPE_NAME in captured.err
    assert client.sent("POST", "/applications") == []


def test_the_default_run_reuses_a_frontend_named_with_an_em_dash(run, capsys) -> None:
    """A tenant built by following the manual guide has to be recognised.

    The guide's examples spell the frontend registration's name with an em dash and
    this script's default uses a plain hyphen. The registration is matched on display
    name and nothing else, so without the second spelling a run against a hand-built
    tenant silently creates a SECOND frontend registration beside the first — after
    which the display-name lookup cannot tell them apart at all.
    """
    client = FakeClient(
        applications=[
            existing_backend_application(),
            existing_spa_application(name=setup_entra.DEFAULT_SPA_NAME_VARIANT),
        ],
        service_principals=[
            service_principal_for(EXISTING_BACKEND_CLIENT_ID),
            service_principal_for(EXISTING_SPA_CLIENT_ID),
        ],
    )

    exit_code = run(client)
    out = capsys.readouterr().out

    assert exit_code == 0, out
    assert client.sent("POST", "/applications") == []
    assert setup_entra.DEFAULT_SPA_NAME_VARIANT in out
    assert re.search(
        rf'^entra_spa_client_id +\= "{re.escape(EXISTING_SPA_CLIENT_ID)}"$',
        out,
        re.MULTILINE,
    )


def test_a_frontend_name_the_operator_passed_is_looked_up_verbatim(run) -> None:
    """A name somebody chose gets no second spelling guessed for it.

    The em-dash lookup exists to recognise this script's OWN default as the guide
    spells it. Applying it to an operator's name would adopt a registration they never
    named, on the strength of a punctuation guess — so the name carries the same
    hyphen the variant rule rewrites, and this tenant holds the rewritten spelling.
    One query goes out, and a new registration is created under the name as typed.
    """
    client = FakeClient(
        applications=[
            existing_backend_application(),
            existing_spa_application(name="TEST — Frontend"),
        ],
        service_principals=[service_principal_for(EXISTING_BACKEND_CLIENT_ID)],
    )

    assert run(client, ["--spa-name", "TEST - Frontend"]) == 0

    name_filters = [
        (call.payload or {}).get("$filter")
        for call in client.calls
        if call.path == "/applications"
        and str((call.payload or {}).get("$filter", "")).startswith("displayName")
    ]
    assert name_filters == ["displayName eq 'TEST - Frontend'"]
    assert client.sent("POST", "/applications")[0]["displayName"] == "TEST - Frontend"


# ===========================================================================
# The drift run
# ===========================================================================
def test_drift_run_reports_and_changes_nothing(run, capsys) -> None:
    """Drift is reported, never repaired — and the run still succeeds, because the
    operator asked what the state was, not for it to be rewritten."""
    client = configured_tenant(
        backend=existing_backend_application(
            roles=("Platform.Admin", "Platform.Viewer")
        )
    )
    for sp in client.service_principals:
        if sp["appId"] == EXISTING_BACKEND_CLIENT_ID:
            sp["appRoleAssignmentRequired"] = False

    exit_code = run(client)
    out = capsys.readouterr().out

    assert exit_code == 0, out
    assert "Platform.Operator" in out
    assert "appRoleAssignmentRequired" in out
    assert [call for call in client.mutations if call.method == "PATCH"] == []
    assert client.sent("POST", "/applications") == []


def test_an_unrelated_registration_with_the_frontend_name_is_reported_as_drift(
    run, capsys
) -> None:
    """Adopting a registration is not the same as verifying it.

    The frontend is matched on display name alone, so an application that merely
    shares the name is adopted verbatim and printed as the deployment's client id. If
    it declares no single-page-application platform and requests some other API's
    scope, sign-in fails with a redirect or scope error that names neither this script
    nor the registration it chose. Reported, never repaired: the operator's reasons
    for that registration are not knowable from here.
    """
    client = FakeClient(
        applications=[
            existing_backend_application(),
            existing_spa_application(
                platform=False,
                backend_app_id="an-unrelated-application",
                scope_id="an-unrelated-scope",
            ),
        ],
        service_principals=[
            service_principal_for(EXISTING_BACKEND_CLIENT_ID),
            service_principal_for(EXISTING_SPA_CLIENT_ID),
        ],
    )

    exit_code = run(client)
    out = capsys.readouterr().out

    assert exit_code == 0, out
    assert "single-page-application platform" in out
    assert "an-unrelated-application" in out
    assert client.sent("POST", "/applications") == []
    assert [call for call in client.mutations if call.method == "PATCH"] == []


# ===========================================================================
# Dry run
# ===========================================================================
def test_dry_run_reads_only(run, capsys) -> None:
    """The safe first run against a configured tenant. Zero writes is a test."""
    client = fresh_tenant()

    exit_code = run(client, ["--dry-run"])
    out = capsys.readouterr().out

    assert exit_code == 0, out
    assert client.mutations == []
    assert {call.method for call in client.calls} == {"GET"}


def test_dry_run_prints_every_mutation_it_would_send(run, capsys) -> None:
    """All nine kinds of write are planned, and the ones whose ids do not exist yet
    are rendered as named placeholders rather than as a crash or a blank."""
    client = fresh_tenant()

    assert run(client, ["--dry-run"]) == 0
    out = capsys.readouterr().out

    assert "WOULD POST /applications" in out
    assert "WOULD POST /servicePrincipals" in out
    assert "WOULD PATCH /servicePrincipals/<backend-sp-id>" in out
    assert f"WOULD POST /servicePrincipals/{GRAPH_SP_ID}/appRoleAssignedTo" in out
    assert "WOULD POST /oauth2PermissionGrants" in out
    assert "WOULD POST /servicePrincipals/<backend-sp-id>/appRoleAssignedTo" in out
    assert '"principalId": "<backend-sp-id>"' in out
    assert '"clientId": "<spa-sp-id>"' in out
    # Values that only a real run can produce say so, rather than printing an
    # angle-bracketed id the operator might paste into a config file.
    assert "<created-on-real-run>" in out


def test_the_dry_run_plan_is_internally_consistent(run, capsys) -> None:
    """The plan's parts have to agree with each other to be worth reading.

    The identifiers the backend plan mints for its scope and its administrator role
    are the ones the frontend plan and the role assignment then reference. A plan
    whose payloads name unrelated identifiers describes a run that could not
    possibly work, and hides the one coupling most worth checking before a real
    run: the frontend references the backend's scope BY identifier.
    """
    client = fresh_tenant()

    assert run(client, ["--dry-run"]) == 0
    out = capsys.readouterr().out

    backend_plan = out.split("WOULD POST /applications")[1]
    spa_plan = out.split("WOULD POST /applications")[2]
    scope_id = re.search(
        rf'"id": "([0-9a-f-]{{36}})",\s*\n\s*"value": "{setup_entra.SCOPE_NAME}"',
        backend_plan,
    )
    admin_role_id = re.search(
        r'"id": "([0-9a-f-]{36})",\s*\n\s*"value": "Platform\.Admin"', backend_plan
    )
    assert scope_id and admin_role_id, backend_plan

    assert f'"id": "{scope_id.group(1)}"' in spa_plan
    # The assignment plan is the one addressed to the BACKEND principal; the six
    # consent plans are addressed to Microsoft Graph's.
    assignment_plan = out.split(
        f"WOULD POST /servicePrincipals/{setup_entra.BACKEND_SP_PLACEHOLDER}"
        "/appRoleAssignedTo"
    )[-1]
    assert f'"appRoleId": "{admin_role_id.group(1)}"' in assignment_plan


def test_dry_run_against_a_configured_tenant_plans_only_the_consents(run, capsys) -> None:
    """Nothing to create, so the plan is the consents and the assignment — which is
    exactly the reassurance the flag exists to give before a real run."""
    client = configured_tenant()

    assert run(client, ["--dry-run"]) == 0
    out = capsys.readouterr().out

    assert client.mutations == []
    assert "WOULD POST /applications" not in out
    assert "WOULD PATCH" not in out
    assert f"WOULD POST /servicePrincipals/{GRAPH_SP_ID}/appRoleAssignedTo" in out


def test_a_dry_run_predicts_that_a_reused_backend_yields_no_secret(run, capsys) -> None:
    """The dry run has to predict the run that follows it, and this is the run the
    guide calls the safe first one: a dry run against a configured tenant.

    Nothing is going to create a secret — not this run and not the real one after it,
    because the backend registration already exists and its secret cannot be re-read.
    Promising ``<created-on-real-run>`` sends the operator to wait for a value that
    never arrives, and the real run's honest "cannot be re-read" then reads as a
    failure.
    """
    client = configured_tenant()

    assert run(client, ["--dry-run"]) == 0
    out = capsys.readouterr().out

    assert "<existing secret — cannot be re-read>" in out
    assert "will NOT" in out
    assert "--rotate-secret" in out


def test_a_dry_run_with_rotation_predicts_the_secret_it_would_mint(run, capsys) -> None:
    """The mirror image: with ``--rotate-secret`` a real run WOULD mint one, so the
    same dry run must not say nothing is coming."""
    client = configured_tenant()

    assert run(client, ["--dry-run", "--rotate-secret"]) == 0
    out = capsys.readouterr().out

    assert client.mutations == []
    assert (
        f"WOULD POST /applications/{EXISTING_BACKEND_OBJECT_ID}/addPassword" in out
    )
    assert "<existing secret — cannot be re-read>" not in out
    assert "second secret" in out.lower()


def test_a_dry_run_on_a_fresh_tenant_still_promises_the_secret_it_will_create(
    run, capsys
) -> None:
    """The third branch, unchanged: a tenant with no backend registration gets one on
    a real run, and its secret is printed exactly once."""
    client = fresh_tenant()

    assert run(client, ["--dry-run"]) == 0
    out = capsys.readouterr().out

    assert setup_entra.UNKNOWN_UNTIL_REAL_RUN in out
    assert "<existing secret — cannot be re-read>" not in out
    assert "A real run creates one" in out


# ===========================================================================
# Failure paths
# ===========================================================================
def test_an_ambiguous_frontend_name_exits_two(run, capsys) -> None:
    """A preflight-class problem: nothing has been changed and there is nothing to
    retry, so it gets its own exit code and names the flag that resolves it."""
    client = FakeClient(
        applications=[existing_spa_application(), existing_spa_application()]
    )

    exit_code = run(client)

    assert exit_code == 2
    assert "--spa-name" in capsys.readouterr().err


def test_a_refusal_mid_flow_exits_one_naming_the_directory_role(run, capsys) -> None:
    """A 403 here is never about API scopes — the Azure CLI token already carries
    them. It is the signed-in account's DIRECTORY ROLE, and saying anything else
    sends the operator somewhere with no fix in it."""
    client = fresh_tenant(
        failures={
            ("POST", f"/servicePrincipals/{GRAPH_SP_ID}/appRoleAssignedTo"): (
                setup_entra.GraphAuthError(
                    403,
                    "Authorization_RequestDenied",
                    setup_entra._DIRECTORY_ROLE_GUIDANCE,
                )
            )
        }
    )

    exit_code = run(client)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "directory role" in captured.err.lower()


def test_a_failure_after_the_backend_create_still_prints_the_secret(run, capsys) -> None:
    """The single most expensive failure this script can have.

    Microsoft Entra discloses a client secret exactly once, in the response that
    creates it. If the run dies after that response and before printing it, the
    operator's only copy is gone and the only repair is deleting the registration
    and starting over. So the failure path prints it, says what was created, and
    still exits non-zero.
    """
    client = fresh_tenant(
        failures={
            ("POST", "/servicePrincipals"): setup_entra.GraphError(
                500, "InternalServerError", "the directory is having a bad day"
            )
        }
    )

    exit_code = run(client)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "generated-secret-1" in captured.out
    assert "created-app-client-1" in captured.out
    # The block has to be recognisable as partial, or it reads as a success.
    assert "partial" in captured.out.lower()
    # And it has to say the value cannot be recovered, or nobody copies it.
    assert "cannot" in captured.out.lower()


def test_a_backend_create_answered_already_exists_fails_and_prints_no_placeholders(
    run, capsys
) -> None:
    """The most expensive lie this script could tell, and it is one call away.

    An already-exists answer to a CREATE is not the desired end state: the response
    is the only source of the new registration's object id, client id and secret, and
    the lookup that ran moments earlier found nothing — so what is blocking the
    create is something this script cannot see. A soft-deleted registration is the
    usual reason: Microsoft Entra keeps one for 30 days, still holding its identifier
    URI, and does not list it among the live ones. Delete a registration and re-run
    inside that window, which this script's own drift report and README both invite,
    and this is exactly where the run lands.

    Treating it as success is what makes it expensive: the run continues in a real
    tenant against dry-run placeholders and can reach exit 0 with an output block full
    of ``<created-on-real-run>`` markers that read as a result. So: exit 1, a message
    naming the cause, and not one placeholder anywhere in what was printed.
    """
    client = fresh_tenant(failures={("POST", "/applications"): already_exists()})

    exit_code = run(client)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "already present" not in captured.out
    # The run stopped at the refused create. Nothing was attempted against a
    # placeholder id.
    assert client.mutation_sequence == [("POST", "/applications")]
    for marker in (
        setup_entra.BACKEND_APP_PLACEHOLDER,
        setup_entra.BACKEND_APP_OBJECT_PLACEHOLDER,
        setup_entra.BACKEND_SP_PLACEHOLDER,
        setup_entra.SPA_APP_PLACEHOLDER,
        setup_entra.SPA_SP_PLACEHOLDER,
        setup_entra.UNKNOWN_UNTIL_REAL_RUN,
    ):
        assert marker not in captured.out
    assert "Configuration values" not in captured.out
    # The message has to be actionable, which means naming where to look.
    assert "30 days" in captured.err
    assert "Deleted applications" in captured.err
    assert "--audience" in captured.err


def test_a_principal_create_answered_already_exists_fails_and_keeps_the_secret(
    run, capsys
) -> None:
    """The same classification, on the call that is likeliest to hit it.

    The service-principal create names a just-created application, so under eventual
    consistency the ``appId eq`` lookup can miss while the directory already holds the
    principal. Its response is the only source of the principal's object id — which
    eleven later calls address — so tolerating the answer would PATCH and grant
    against ``<backend-sp-id>``. The backend's secret, already captured by then, is
    still rescued.
    """
    client = fresh_tenant(
        failures={("POST", "/servicePrincipals"): already_exists()}
    )

    exit_code = run(client)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "already present" not in captured.out
    assert setup_entra.BACKEND_SP_PLACEHOLDER not in captured.out
    assert "generated-secret-1" in captured.out
    assert "Deleted applications" in captured.err


def test_a_create_whose_response_has_no_identifiers_stops_the_run(run, capsys) -> None:
    """The other door a placeholder could walk through on a real run.

    A create that succeeds but answers with nothing usable in it leaves the flow with
    no object id and no client id — and the fallback that a dry run legitimately uses
    is sitting right there. Taking it would send eleven later calls at
    ``<backend-sp-id>`` in a live tenant and could still print an output block. So the
    fallback is a dry-run-only path, and a real run stops here and says the
    registration is probably there and a re-run will adopt it.
    """
    client = fresh_tenant(
        failures={
            ("POST", "/applications"): {"displayName": setup_entra.DEFAULT_BACKEND_NAME}
        }
    )

    exit_code = run(client)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert client.mutation_sequence == [("POST", "/applications")]
    for marker in (
        setup_entra.BACKEND_APP_PLACEHOLDER,
        setup_entra.BACKEND_APP_OBJECT_PLACEHOLDER,
        setup_entra.BACKEND_SP_PLACEHOLDER,
        setup_entra.UNKNOWN_UNTIL_REAL_RUN,
    ):
        assert marker not in captured.out
    assert "Configuration values" not in captured.out
    assert "re-run" in captured.err.lower()


def test_a_principal_create_with_an_unreadable_answer_stops_the_run(run, capsys) -> None:
    """The same guard on the other create, and this is the one with eleven calls
    behind it.

    A service principal's object id comes from nowhere but its create response, and
    the PATCH, the six consents, the delegated grant and the runner assignment all
    address it. A create answered with an empty body — which is what a 204 or a
    stripped response looks like once it has been parsed — leaves that id unknown,
    and the dry-run placeholder is sitting right there. Taking it would PATCH
    ``/servicePrincipals/<backend-sp-id>`` in a live tenant and grant six admin
    consents to a principal id that is not one, then print an output block. So the
    run stops, and the secret captured a moment earlier is still rescued.
    """
    client = fresh_tenant(failures={("POST", "/servicePrincipals"): {}})

    exit_code = run(client)
    captured = capsys.readouterr()

    assert exit_code == 1
    # Nothing was addressed to a placeholder: the run got no further than the two
    # creates, so there is no PATCH and there are no consents.
    assert client.mutation_sequence == [
        ("POST", "/applications"),
        ("POST", "/servicePrincipals"),
    ]
    assert setup_entra.BACKEND_SP_PLACEHOLDER not in captured.out
    assert setup_entra.UNKNOWN_UNTIL_REAL_RUN not in captured.out
    assert "Configuration values" not in captured.out
    # The backend service principal is named, and so is the way out.
    assert "backend service principal" in captured.err
    assert "re-run" in captured.err.lower()
    # ...and the secret the backend create disclosed is not lost with the run.
    assert "generated-secret-1" in captured.out


def test_a_reuse_path_failure_still_names_what_it_created(run, capsys) -> None:
    """A failure after a REUSED backend creates plenty and mints no secret.

    The frontend registration and its service principal are both new, and the
    operator has to know: a registration created seconds before a failure is
    invisible unless somebody says it is there, and the re-run that adopts it can only
    be trusted if it is known to exist. What must NOT appear is the reused backend —
    claiming to have created it would send the operator looking for a mess that is not
    there.
    """
    client = FakeClient(
        applications=[existing_backend_application()],
        service_principals=[service_principal_for(EXISTING_BACKEND_CLIENT_ID)],
        failures={
            ("POST", "/oauth2PermissionGrants"): setup_entra.GraphError(
                500, "InternalServerError", "the directory is having a bad day"
            )
        },
    )

    exit_code = run(client)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "PARTIAL SETUP" in captured.out
    block = captured.out.split("PARTIAL SETUP")[1]
    assert "created-app-client-1" in block  # the new frontend registration
    assert "created-sp-1" in block  # and its service principal
    assert "frontend app registration" in block
    # The backend was reused, not created, and no secret was minted on this path.
    assert "backend app registration" not in block
    assert "entra_backend_client_secret" not in block


def test_a_failed_run_still_reports_the_drift_it_found(run, capsys) -> None:
    """The failure path is where drift lines are worth the most.

    A missing app role or an unset ``appRoleAssignmentRequired`` is frequently WHY a
    later call behaved in a way its error message does not explain, and those lines
    were collected before the failure. Discarding them on the one path where they
    diagnose something is perverse.
    """
    client = configured_tenant(
        backend=existing_backend_application(
            roles=("Platform.Admin", "Platform.Viewer")
        ),
        failures={
            ("POST", f"/servicePrincipals/{GRAPH_SP_ID}/appRoleAssignedTo"): (
                setup_entra.GraphError(
                    500, "InternalServerError", "the directory is having a bad day"
                )
            )
        },
    )

    exit_code = run(client)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Drift report" in captured.out
    assert "Platform.Operator" in captured.out
    # Nothing was created and no secret was minted, so there is no partial block to
    # bury the error under.
    assert "PARTIAL SETUP" not in captured.out


def test_an_ambiguity_after_the_backend_create_still_prints_the_secret(
    run, capsys
) -> None:
    """The rescue is on BOTH failure arms, not just the Graph one.

    Exit 2 is not "nothing was attempted": a duplicated frontend name is only
    discovered after the backend registration and its secret already exist. The
    secret is disclosed exactly once, so an unguarded exit here destroys the
    operator's only copy just as thoroughly as a 500 would.
    """
    client = fresh_tenant(
        applications=[existing_spa_application(), existing_spa_application()]
    )

    exit_code = run(client)
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "PARTIAL SETUP" in captured.out
    assert "generated-secret-1" in captured.out
    assert "--spa-name" in captured.err


def test_a_lost_frontend_create_response_is_recovered_not_repeated(run, capsys) -> None:
    """The frontend registration has no unique key, so a create whose response was
    lost cannot be told apart from a create that never happened — except by looking
    again. Reporting failure without looking would leave a duplicate registration
    behind on the next run, with the deployment pointed at whichever one won.
    """

    def create_lands_but_the_answer_is_lost(client: FakeClient, path: str, payload: dict):
        client.store_application(payload)
        raise setup_entra.GraphError(
            0, "transport_failure", "Could not reach Microsoft Graph after 11 attempts"
        )

    client = fresh_tenant(
        failures={
            ("POST", "/applications"): [None, create_lands_but_the_answer_is_lost]
        }
    )

    exit_code = run(client)
    out = capsys.readouterr().out

    assert exit_code == 0, out
    # Exactly two create attempts: the backend, and the frontend one that landed.
    assert client.mutation_sequence.count(("POST", "/applications")) == 2
    assert len(client.applications) == 2


def test_a_frontend_create_that_really_failed_exits_one(run, capsys) -> None:
    """The other half of the recovery: looking again and finding nothing means the
    create really did fail, and that is reported rather than papered over."""
    client = fresh_tenant(
        failures={
            ("POST", "/applications"): [
                None,
                setup_entra.GraphError(
                    0, "transport_failure", "Could not reach Microsoft Graph"
                ),
            ]
        }
    )

    exit_code = run(client)
    captured = capsys.readouterr()

    assert exit_code == 1
    # A transport failure carries status 0 as a sentinel meaning "no response at
    # all". Rendering it as a status sends the operator looking up an HTTP code
    # that does not exist, so neither the raw form nor a dressed-up one appears.
    assert "status=0" not in captured.err
    assert "HTTP 0" not in captured.err
    assert "0" not in _status_codes(captured.err)
    assert "Could not reach Microsoft Graph" in captured.err
    # ...and the backend secret is still not lost.
    assert "generated-secret-1" in captured.out


def test_rotate_secret_without_a_backend_is_a_named_error(run, capsys) -> None:
    """There is nothing to rotate on a tenant with no backend registration, and
    guessing that a plain run was meant would mint a secret the operator did not
    ask for on an app they did not know would be created."""
    client = fresh_tenant()

    exit_code = run(client, ["--rotate-secret"])
    err = capsys.readouterr().err

    assert exit_code == 2
    assert "--rotate-secret" in err
    assert client.mutations == []


def test_rotate_secret_appends_on_an_existing_backend(run, capsys) -> None:
    """Rotation ADDS a credential; it never replaces one. The old secret keeps
    working, which is what makes a rotation deployable without downtime."""
    client = configured_tenant()

    exit_code = run(client, ["--rotate-secret"])
    out = capsys.readouterr().out

    assert exit_code == 0, out
    assert client.sent(
        "POST", f"/applications/{EXISTING_BACKEND_OBJECT_ID}/addPassword"
    ) == [{"passwordCredential": {"displayName": setup_entra._SECRET_DISPLAY_NAME}}]
    assert "rotated-secret" in out
    assert "<existing secret — cannot be re-read>" not in out


# ===========================================================================
# Preflight: finding the Azure CLI
# ===========================================================================
# These live here rather than with the transport tests because what they pin is a
# platform hazard in how the flow starts, not the token parsing those tests cover.
def test_the_azure_cli_is_invoked_at_its_resolved_path(monkeypatch) -> None:
    """On Windows the Azure CLI ships as ``az.cmd``.

    ``subprocess.run`` without a shell goes through ``CreateProcess``, which appends
    ``.exe`` but does not apply ``PATHEXT``, so a bare ``"az"`` as argv[0] raises
    ``FileNotFoundError`` on a machine where ``az`` works in every shell — and the
    operator is told the Azure CLI is not installed when it plainly is, which is the
    one error message there is no way to act on. Resolving the name first is the fix.
    """
    recorded: list[tuple[str, ...]] = []

    def record(command, **kwargs):
        recorded.append(tuple(command))
        return _completed_az_process()

    monkeypatch.setattr(setup_entra.shutil, "which", lambda name: r"C:\azure-cli\az.cmd")
    monkeypatch.setattr(setup_entra.subprocess, "run", record)

    token = setup_entra.get_az_token()

    assert token.access_token == "an-access-token"
    assert recorded[0][0] == r"C:\azure-cli\az.cmd"
    # The documented, cloud-aware token request is unchanged by the resolution.
    assert "ms-graph" in recorded[0]


def test_an_azure_cli_that_cannot_be_found_is_reported_as_not_installed(
    monkeypatch,
) -> None:
    """Nothing on PATH keeps the one message for it, from the one place it comes
    from: the arm that says how to install the CLI and how to sign in."""
    monkeypatch.setattr(setup_entra.shutil, "which", lambda name: None)

    def missing(command, **kwargs):
        raise FileNotFoundError(command)

    monkeypatch.setattr(setup_entra.subprocess, "run", missing)

    with pytest.raises(setup_entra.PreflightError) as excinfo:
        setup_entra.get_az_token()

    assert "azure cli" in str(excinfo.value).lower()
    assert "az login" in str(excinfo.value)


def _completed_az_process():
    """What a signed-in Azure CLI answers ``get-access-token`` with."""
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps(
            {
                "accessToken": "an-access-token",
                "expires_on": 4102444800,
                "tenant": TENANT_ID,
            }
        ),
        stderr="",
    )


# ===========================================================================
# The command line
# ===========================================================================
def test_the_flags_default_to_the_documented_values() -> None:
    """These four defaults are a contract with the setup guide and with
    ``secrets.auto.tfvars``; changing one silently breaks a paste-ready output."""
    args = setup_entra.parse_args([])

    assert args.audience == "api://agp"
    assert args.backend_name == "AGP - Backend Graph Client"
    assert args.spa_name == "AGP - Frontend"
    assert args.dry_run is False
    assert args.rotate_secret is False


def test_the_flags_are_honoured(run) -> None:
    """Custom names and audience are how the documented test-safely workflow builds
    a throwaway pair of registrations next to the real ones."""
    client = fresh_tenant()

    assert (
        run(
            client,
            [
                "--audience",
                "api://agp-test",
                "--backend-name",
                "TEST Backend",
                "--spa-name",
                "TEST Frontend",
            ],
        )
        == 0
    )

    backend, spa = client.sent("POST", "/applications")
    assert backend["identifierUris"] == ["api://agp-test"]
    assert backend["displayName"] == "TEST Backend"
    assert spa["displayName"] == "TEST Frontend"
