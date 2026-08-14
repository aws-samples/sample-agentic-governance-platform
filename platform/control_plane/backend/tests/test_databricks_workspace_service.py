"""The one Databricks REST client (E29/T2 — contract C-2).

Three properties this module exists to pin, none of which a generous fake can see:

**1. The request SHAPES are the product.** Databricks' OAuth surface fails in ways that read as
"auth is broken" rather than "your form is wrong": an exchange that carries a ``client_id`` is
*prohibited* on an account-wide federation policy (research §3.2), and a downscoped token needs
``authorization_details`` rather than ``scope`` (§3.7). So ``_FakeDatabricks`` is deliberately
STRICT — it answers 400 to any request whose form/method/headers drift from the documented shape.
A fake more generous than reality makes tests that cannot fail; several tests here call the fake
directly with a drifted form purely to prove the guard is real.

**2. Defensive reads.** The Apps response field names (``url``, ``oauth2_app_client_id``, the
dedicated service-principal id) are UNVERIFIED in Databricks' docs (§2.1 — the REST reference page
would not load). The adapter therefore keys on ``name`` only and passes everything else through
untouched; a partial record must survive, and a nameless one must be skipped rather than crash a
whole discovery listing.

**3. Secrets are parameters, never logs, never error text.** ``_SECRET`` and ``_JWT`` appear in
this module only as FORBIDDEN substrings of messages, mirroring ``test_repo_provider``'s idiom.

Transport is ``httpx.MockTransport`` (as in ``test_repo_provider`` / ``test_langfuse_metrics_service``)
with an ordered ``(method, path)`` log, because idempotence claims — "the second
``ensure_federation_audience`` issues NO PATCH" — are counts, not states. The repo is not in
pytest-asyncio auto mode, so every async test carries ``@pytest.mark.asyncio``.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from urllib.parse import parse_qs, quote, unquote

import httpx
import pytest

from services.databricks_workspace_service import (
    DatabricksError,
    DatabricksWorkspaceService,
)

_WS = "https://dbc-test.cloud.databricks.com"
_ACCOUNT_HOST = "https://accounts-test.cloud.databricks.com"
_ACCOUNT_ID = "11111111-2222-3333-4444-555555555555"
_CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_SECRET = "dose_never_in_a_message"
_JWT = "eyJhbGciOiJSUzI1NiJ9.entra.jwt_never_in_a_message"
_TOKEN = "dapi_minted_token_never_in_a_message"
# The SCIM id and the application id of the same principal — DIFFERENT identifiers, and only
# the first addresses the secrets sub-resource (E29/T6).
_SCIM_SP_ID = "7788990011"
_SP_APPLICATION_ID = "ffffffff-0000-1111-2222-333333333333"
_SP_SECRET = "dose_sp_secret_never_in_a_message"
_SAFE_KIND = re.compile(r"^[A-Za-z_]{1,64}$")


# =========================================================================== #
# The strict fake
# =========================================================================== #
def _form(req: httpx.Request) -> dict:
    return {k: v[0] for k, v in parse_qs(req.content.decode()).items()}


def _bad(code: str) -> httpx.Response:
    """The fake's own rejection — shaped like a real Databricks OIDC/REST error body."""
    return httpx.Response(400, json={"error": code, "error_code": code.upper()})


class _FakeDatabricks:
    """A strict Databricks. Rejects wrong-shaped requests; records every call."""

    def __init__(self, *, policies: list[dict] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.forms: list[dict] = []
        self.bodies: list[dict] = []
        # Federation policies live in the fake so idempotence is observable as state.
        self.policies = policies if policies is not None else []
        self.apps_pages: list[dict] = [{"apps": []}]
        self.endpoint_pages: list[dict] = [{"endpoints": []}]
        self.token_status = 200
        self.raise_transport = False
        # Per-SP secret minting (E29/T6): a status override for the failure paths and a body
        # override so a malformed response (no ``secret``) is exercisable.
        self.sp_secret_status = 200
        self.sp_secret_body: dict | None = None
        # The app's access-control list, in the RESPONSE shape (E29/T13a): a principal key plus
        # ``all_permissions[].permission_level``. Held as state so assert-by-PUT is observable as
        # a read-back, not merely as a recorded body.
        self.app_acl: list[dict] = [
            {"group_name": "admins", "all_permissions": [{"permission_level": "CAN_MANAGE"}]}
        ]

    # -- the transport handler ------------------------------------------- #
    def handler(self, req: httpx.Request) -> httpx.Response:
        if self.raise_transport:
            raise httpx.ConnectError("no route to host", request=req)
        self.calls.append((req.method, req.url.path))
        path = req.url.path
        if path == "/oidc/v1/token":
            return self._token(req, account=False)
        if path == f"/oidc/accounts/{_ACCOUNT_ID}/v1/token":
            return self._token(req, account=True)
        if path == "/api/2.0/apps":
            return self._page(req, self.apps_pages)
        if path == "/api/2.0/serving-endpoints":
            return self._page(req, self.endpoint_pages)
        if path.startswith("/api/2.0/permissions/apps/"):
            return self._permissions(req)
        if path == "/api/2.0/preview/scim/v2/ServicePrincipals":
            return self._scim(req)
        if path.startswith("/api/2.0/accounts/servicePrincipals/"):
            # Pinned live 2026-08-11 (B2.5): the workspace host serves the SP secret mint under
            # an ``/accounts/`` prefix; the un-prefixed form 404s (the fake's default arm).
            return self._sp_secret(req)
        if path == f"/api/2.0/accounts/{_ACCOUNT_ID}/federationPolicies":
            return self._policies_list(req)
        if path.startswith(f"/api/2.0/accounts/{_ACCOUNT_ID}/federationPolicies/"):
            return self._policy_patch(req)
        return httpx.Response(404, json={"error_code": "ENDPOINT_NOT_FOUND"})

    # -- /oidc/v1/token: the two grants, each strictly shaped ------------- #
    def _token(self, req: httpx.Request, *, account: bool) -> httpx.Response:
        if req.method != "POST":
            return _bad("invalid_request")
        form = _form(req)
        self.forms.append(form)
        if self.token_status != 200:
            return httpx.Response(
                self.token_status, json={"error": "server_error", "message": "boom"}
            )
        grant = form.get("grant_type")

        if grant == "client_credentials":
            # Credentials go as HTTP Basic (research §3.7's ``--user`` form), never in the body.
            if not req.headers.get("authorization", "").startswith("Basic "):
                return _bad("invalid_client")
            if "client_secret" in form:
                return _bad("invalid_request")
            if form.get("scope") != "all-apis":
                return _bad("invalid_scope")
            return httpx.Response(
                200, json={"access_token": _TOKEN, "token_type": "Bearer", "expires_in": 3600}
            )

        if grant == "urn:ietf:params:oauth:grant-type:token-exchange":
            # Account-wide policy ⇒ client_id is PROHIBITED (research §3.2).
            if "client_id" in form:
                return _bad("invalid_request")
            if not form.get("subject_token"):
                return _bad("invalid_request")
            if form.get("subject_token_type") != "urn:ietf:params:oauth:token-type:jwt":
                return _bad("invalid_request")
            if form.get("scope") != "all-apis":
                return _bad("invalid_scope")
            return httpx.Response(
                200, json={"access_token": _TOKEN, "token_type": "Bearer", "expires_in": 3600}
            )

        return _bad("unsupported_grant_type")

    # -- paginated GET listings ------------------------------------------ #
    def _page(self, req: httpx.Request, pages: list[dict]) -> httpx.Response:
        if req.method != "GET":
            return _bad("invalid_request")
        if req.headers.get("authorization") != f"Bearer {_TOKEN}":
            return httpx.Response(401, json={"error_code": "UNAUTHENTICATED"})
        token = req.url.params.get("page_token")
        index = 0 if not token else int(token)
        if index >= len(pages):
            return httpx.Response(400, json={"error_code": "INVALID_PARAMETER_VALUE"})
        body = dict(pages[index])
        if index + 1 < len(pages):
            body["next_page_token"] = str(index + 1)
        return httpx.Response(200, json=body)

    # -- permissions: GET read, additive PATCH, replacing PUT ------------- #
    def _permissions(self, req: httpx.Request) -> httpx.Response:
        """GET reads the ACL, PATCH is additive, **PUT REPLACES** it (E29/T13a, design §3A).

        PUT used to be refused outright here, on the argument that replacing the ACL would
        silently revoke the customer's own grants. §3A inverts that DELIBERATELY: on an
        AGP-governed app the ACL is asserted, so replace is the intent. The strictness stays,
        aimed at the new hazard instead — a PUT whose list does not carry ``admins``
        ``CAN_MANAGE`` is a 400, mirroring the client-side guard so a composer bug is caught at
        BOTH layers rather than locking the platform admins out of a real workspace."""
        if req.headers.get("authorization") != f"Bearer {_TOKEN}":
            return httpx.Response(401, json={"error_code": "UNAUTHENTICATED"})
        if req.method == "GET":
            return httpx.Response(200, json={"access_control_list": list(self.app_acl)})
        if req.method not in ("PATCH", "PUT"):
            return _bad("invalid_request")
        body = json.loads(req.content.decode())
        self.bodies.append(body)
        acl = body.get("access_control_list")
        if not isinstance(acl, list) or not acl:
            return _bad("invalid_request")
        for entry in acl:
            if entry.get("permission_level") not in ("CAN_USE", "CAN_MANAGE"):
                return _bad("invalid_parameter_value")
            if not ({"service_principal_name", "group_name", "user_name"} & set(entry)):
                return _bad("invalid_request")
        if req.method == "PUT":
            if not any(
                e.get("group_name") == "admins" and e.get("permission_level") == "CAN_MANAGE"
                for e in acl
            ):
                return _bad("invalid_request")
            # A PUT is a REPLACE, so naming the same principal twice has no defined meaning —
            # upstream would either 400 or silently pick one (a downgrade). The fake refuses, so a
            # client that re-PUTs a flattened read cannot pass its tests (E29/T13a-FIX, F1).
            named = [
                (key, entry[key])
                for entry in acl
                for key in ("service_principal_name", "group_name", "user_name")
                if key in entry
            ]
            if len(named) != len(set(named)):
                return _bad("invalid_request")
            self.app_acl = [
                {
                    **{k: v for k, v in e.items() if k != "permission_level"},
                    "all_permissions": [{"permission_level": e["permission_level"]}],
                }
                for e in acl
            ]
        return httpx.Response(200, json={"access_control_list": acl})

    # -- SCIM service principals ----------------------------------------- #
    def _scim(self, req: httpx.Request) -> httpx.Response:
        if req.method != "POST":
            return _bad("invalid_request")
        body = json.loads(req.content.decode())
        self.bodies.append(body)
        if "urn:ietf:params:scim:schemas:core:2.0:ServicePrincipal" not in body.get("schemas", []):
            return _bad("invalid_request")
        if not body.get("displayName"):
            return _bad("invalid_request")
        return httpx.Response(
            201,
            json={
                "id": "7788990011",
                "applicationId": "ffffffff-0000-1111-2222-333333333333",
                "displayName": body["displayName"],
                "active": True,
            },
        )

    # -- per-SP OAuth secrets (E29/T6) ----------------------------------- #
    def _sp_secret(self, req: httpx.Request) -> httpx.Response:
        """Strict: POST only, bearer-authenticated, and addressed by the SCIM **id**.

        The application id is REJECTED with a 404, which is what the real API does and what
        makes "pass the SCIM id, not the application id" a testable claim rather than a comment.
        The id is read from the RAW path (see ``_policy_patch``) so a traversal attempt cannot be
        normalised into looking legitimate."""
        if req.method != "POST":
            return _bad("invalid_request")
        if req.headers.get("authorization") != f"Bearer {_TOKEN}":
            return httpx.Response(401, json={"error_code": "UNAUTHENTICATED"})
        raw = req.url.raw_path.decode().split("?")[0]
        segment = raw[len("/api/2.0/accounts/servicePrincipals/"):].split("/")[0]
        sp_id = unquote(segment)
        if self.sp_secret_status != 200:
            return httpx.Response(
                self.sp_secret_status, json={"error_code": "PERMISSION_DENIED", "message": "boom"}
            )
        if self.sp_secret_body is not None:
            return httpx.Response(200, json=self.sp_secret_body)
        if sp_id != _SCIM_SP_ID:
            # Addressed by application_id (or anything else) → the principal is not found here.
            return httpx.Response(404, json={"error_code": "RESOURCE_DOES_NOT_EXIST"})
        return httpx.Response(
            200,
            json={"id": "secret-rec-1", "secret": _SP_SECRET, "secret_hash": "sha256:abc"},
        )

    # -- account federation policies ------------------------------------- #
    def _policies_list(self, req: httpx.Request) -> httpx.Response:
        if req.method != "GET":
            return _bad("invalid_request")
        return httpx.Response(200, json={"policies": self.policies})

    def _policy_patch(self, req: httpx.Request) -> httpx.Response:
        if req.method != "PATCH":
            return _bad("invalid_request")
        if not req.url.params.get("update_mask"):
            # Databricks ignores a maskless PATCH silently; the fake refuses so we can't ship one.
            return _bad("invalid_request")
        body = json.loads(req.content.decode())
        self.bodies.append(body)
        # A real server routes on the RAW path and percent-decodes ONE segment at a time — which
        # is exactly why quoting works. Decoding the whole path (httpx's ``.path``) would let
        # ``..%2F..`` look like traversal again, so the fake must not take that shortcut.
        policy_id = unquote(req.url.raw_path.decode().split("?")[0].rsplit("/", 1)[-1])
        for policy in self.policies:
            if policy.get("policy_id") == policy_id:
                policy.setdefault("oidc_policy", {})["audiences"] = list(
                    body.get("oidc_policy", {}).get("audiences", [])
                )
                return httpx.Response(200, json=policy)
        return httpx.Response(404, json={"error_code": "RESOURCE_DOES_NOT_EXIST"})


def _svc(fake: _FakeDatabricks) -> DatabricksWorkspaceService:
    return DatabricksWorkspaceService(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        account_host=_ACCOUNT_HOST,
    )


def _entra_policy(*, audiences: list[str], policy_id: str = "pol-1") -> dict:
    return {
        "policy_id": policy_id,
        "oidc_policy": {
            "issuer": "https://login.microsoftonline.com/tenant-guid/v2.0",
            "audiences": list(audiences),
            "subject_claim": "sub",
        },
    }


# =========================================================================== #
# mint_m2m_token
# =========================================================================== #
@pytest.mark.asyncio
async def test_mint_m2m_token_sends_the_documented_client_credentials_form():
    fake = _FakeDatabricks()
    token = await _svc(fake).mint_m2m_token(_WS, _CLIENT_ID, _SECRET)

    assert token == _TOKEN
    assert fake.calls == [("POST", "/oidc/v1/token")]
    form = fake.forms[0]
    assert form["grant_type"] == "client_credentials"
    assert form["scope"] == "all-apis"
    # The secret travels as HTTP Basic (fake enforces), never as a form field.
    assert "client_secret" not in form


@pytest.mark.asyncio
async def test_the_fake_rejects_a_drifted_grant_type():
    """Strictness proof: a wrong grant_type is a 400 at the fake, so a drifted service fails."""
    fake = _FakeDatabricks()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as client:
        resp = await client.post(
            f"{_WS}/oidc/v1/token",
            data={"grant_type": "password", "scope": "all-apis"},
            auth=(_CLIENT_ID, _SECRET),
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_mint_m2m_token_failure_surfaces_a_safe_kind_and_never_the_secret():
    fake = _FakeDatabricks()
    fake.token_status = 500
    with pytest.raises(DatabricksError) as excinfo:
        await _svc(fake).mint_m2m_token(_WS, _CLIENT_ID, _SECRET)

    err = excinfo.value
    assert _SAFE_KIND.match(err.kind), err.kind
    assert _SECRET not in err.message and _SECRET not in str(err)
    assert _CLIENT_ID not in err.message
    # The upstream body ("boom") never leaks either.
    assert "boom" not in str(err)


@pytest.mark.asyncio
async def test_a_400_from_databricks_maps_to_a_safe_kind():
    """A rejected form (unsupported grant) → DatabricksError whose kind matches the regex."""
    fake = _FakeDatabricks()
    svc = _svc(fake)
    # Drive the rejection through the service by asking for an exchange with an empty JWT,
    # which the fake refuses as invalid_request.
    with pytest.raises(DatabricksError) as excinfo:
        await svc.exchange_federated_token(_WS, "")
    assert _SAFE_KIND.match(excinfo.value.kind), excinfo.value.kind


# =========================================================================== #
# exchange_federated_token — the NO-client_id property
# =========================================================================== #
@pytest.mark.asyncio
async def test_exchange_federated_token_sends_no_client_id():
    fake = _FakeDatabricks()
    token = await _svc(fake).exchange_federated_token(_WS, _JWT)

    assert token == _TOKEN
    form = fake.forms[0]
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"
    assert form["subject_token"] == _JWT
    assert form["subject_token_type"] == "urn:ietf:params:oauth:token-type:jwt"
    assert form["scope"] == "all-apis"
    # THE pinned property: an account-wide federation policy PROHIBITS client_id (§3.2).
    assert "client_id" not in form


@pytest.mark.asyncio
async def test_the_fake_rejects_an_exchange_that_carries_a_client_id():
    """Strictness proof for the property above — adding client_id is a 400, not a shrug."""
    fake = _FakeDatabricks()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as client:
        resp = await client.post(
            f"{_WS}/oidc/v1/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token": _JWT,
                "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
                "scope": "all-apis",
                "client_id": _CLIENT_ID,
            },
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_exchange_failure_never_echoes_the_subject_jwt():
    fake = _FakeDatabricks()
    fake.token_status = 403
    with pytest.raises(DatabricksError) as excinfo:
        await _svc(fake).exchange_federated_token(_WS, _JWT)
    assert _JWT not in str(excinfo.value)
    assert _SAFE_KIND.match(excinfo.value.kind)


# =========================================================================== #
# list_apps / list_serving_endpoints — pagination + defensive reads
# =========================================================================== #
@pytest.mark.asyncio
async def test_list_apps_follows_next_page_token_and_concatenates():
    fake = _FakeDatabricks()
    fake.apps_pages = [
        {"apps": [{"name": "a1", "url": "https://a1.databricksapps.com"}]},
        {"apps": [{"name": "a2", "url": "https://a2.databricksapps.com"}]},
    ]
    apps = await _svc(fake).list_apps(_WS, _TOKEN)

    assert [a["name"] for a in apps] == ["a1", "a2"]
    assert fake.calls.count(("GET", "/api/2.0/apps")) == 2


@pytest.mark.asyncio
async def test_list_apps_reads_defensively_partial_kept_nameless_skipped():
    """The unverified field names (§2.1) must never be indexed: a record missing ``url`` and
    ``oauth2_app_client_id`` survives untouched; one missing ``name`` is skipped, not a KeyError."""
    fake = _FakeDatabricks()
    fake.apps_pages = [
        {
            "apps": [
                {"name": "partial"},  # no url, no oauth2_app_client_id, no SP id
                {"url": "https://orphan.databricksapps.com"},  # no name at all
                {"name": "full", "url": "https://f.databricksapps.com",
                 "oauth2_app_client_id": "oauth-client-1"},
            ]
        }
    ]
    apps = await _svc(fake).list_apps(_WS, _TOKEN)

    assert [a["name"] for a in apps] == ["partial", "full"]
    assert "url" not in apps[0]  # passed through as-is, nothing invented
    assert apps[1]["oauth2_app_client_id"] == "oauth-client-1"


@pytest.mark.asyncio
async def test_list_serving_endpoints_paginates_and_skips_nameless():
    fake = _FakeDatabricks()
    fake.endpoint_pages = [
        {"endpoints": [{"name": "e1"}, {"id": "no-name"}]},
        {"endpoints": [{"name": "e2", "state": {"ready": "READY"}}]},
    ]
    endpoints = await _svc(fake).list_serving_endpoints(_WS, _TOKEN)

    assert [e["name"] for e in endpoints] == ["e1", "e2"]
    assert fake.calls.count(("GET", "/api/2.0/serving-endpoints")) == 2


@pytest.mark.asyncio
async def test_list_apps_unauthorized_surfaces_a_safe_kind():
    fake = _FakeDatabricks()
    with pytest.raises(DatabricksError) as excinfo:
        await _svc(fake).list_apps(_WS, "wrong-token")
    assert _SAFE_KIND.match(excinfo.value.kind)
    assert "wrong-token" not in str(excinfo.value)


# =========================================================================== #
# grant_app_can_use
# =========================================================================== #
@pytest.mark.asyncio
async def test_grant_app_can_use_patches_additively_with_can_use():
    fake = _FakeDatabricks()
    sp = "12345678-90ab-cdef-1234-567890abcdef"
    await _svc(fake).grant_app_can_use(_WS, _TOKEN, "my-app", sp)

    assert fake.calls == [("PATCH", "/api/2.0/permissions/apps/my-app")]
    entry = fake.bodies[0]["access_control_list"][0]
    assert entry["permission_level"] == "CAN_USE"
    assert entry["service_principal_name"] == sp


@pytest.mark.asyncio
async def test_grant_app_can_use_treats_a_non_uuid_principal_as_a_group():
    fake = _FakeDatabricks()
    await _svc(fake).grant_app_can_use(_WS, _TOKEN, "my-app", "agp-operators")
    entry = fake.bodies[0]["access_control_list"][0]
    assert entry["group_name"] == "agp-operators"
    assert "service_principal_name" not in entry


@pytest.mark.asyncio
async def test_grant_app_can_use_with_kind_user_keys_the_entry_by_user_name():
    """§3A mirrors each Entra assignment as a PER-USER ``CAN_USE`` entry, and a user is keyed by
    ``user_name`` (a UPN/email — never UUID-shaped, so the default heuristic would have called it
    a group and granted nothing to the person)."""
    fake = _FakeDatabricks()
    await _svc(fake).grant_app_can_use(
        _WS, _TOKEN, "my-app", "lars.svensson@example.com", kind="user"
    )

    assert fake.calls == [("PATCH", "/api/2.0/permissions/apps/my-app")]
    entry = fake.bodies[0]["access_control_list"][0]
    assert entry["user_name"] == "lars.svensson@example.com"
    assert entry["permission_level"] == "CAN_USE"
    assert "group_name" not in entry and "service_principal_name" not in entry


@pytest.mark.asyncio
async def test_grant_app_can_use_kind_service_principal_is_explicit_not_guessed():
    """A non-UUID service principal (a name, not an application id) is unreachable by the
    heuristic; ``kind`` makes it addressable."""
    fake = _FakeDatabricks()
    await _svc(fake).grant_app_can_use(
        _WS, _TOKEN, "my-app", "agp-agent-sp", kind="service_principal"
    )
    entry = fake.bodies[0]["access_control_list"][0]
    assert entry["service_principal_name"] == "agp-agent-sp"


# =========================================================================== #
# get_app_permissions / set_app_permissions / revoke_app_can_use (E29/T13a, §3A)
# =========================================================================== #
@pytest.mark.asyncio
async def test_get_app_permissions_normalizes_all_three_principal_kinds():
    """One flat ``{principal, kind, level, inherited}`` per (principal, level) — the shape §3A's
    drift comparison diffs against the Entra assignment list. A record carrying two levels yields
    two entries; inherited permissions are reported as they arrive, because an entry AGP cannot
    remove is still an entry that grants access — but FLAGGED, because a write path must drop them
    rather than materialize them as permanent direct grants."""
    fake = _FakeDatabricks()
    fake.app_acl = [
        {"group_name": "admins", "all_permissions": [{"permission_level": "CAN_MANAGE"}]},
        {
            "user_name": "lars.svensson@example.com",
            "all_permissions": [{"permission_level": "CAN_USE"}],
        },
        {
            "service_principal_name": _SP_APPLICATION_ID,
            "all_permissions": [
                {"permission_level": "CAN_MANAGE"},
                {"permission_level": "CAN_USE", "inherited": True},
            ],
        },
    ]
    entries = await _svc(fake).get_app_permissions(_WS, _TOKEN, "my-app")

    assert fake.calls == [("GET", "/api/2.0/permissions/apps/my-app")]
    assert entries == [
        {"principal": "admins", "kind": "group", "level": "CAN_MANAGE", "inherited": False},
        {
            "principal": "lars.svensson@example.com",
            "kind": "user",
            "level": "CAN_USE",
            "inherited": False,
        },
        {
            "principal": _SP_APPLICATION_ID,
            "kind": "service_principal",
            "level": "CAN_MANAGE",
            "inherited": False,
        },
        {
            "principal": _SP_APPLICATION_ID,
            "kind": "service_principal",
            "level": "CAN_USE",
            "inherited": True,
        },
    ]


@pytest.mark.asyncio
async def test_get_app_permissions_reads_defensively_and_dedupes():
    """The same defensive-read rule as the listings: a record with no recognised principal key,
    no readable levels, or a non-dict shape is SKIPPED rather than crashing the read — a drift
    probe that dies on one odd ACL record reports nothing about the rest. Duplicate
    (principal, level) pairs collapse, so a repeated level cannot inflate the diff."""
    fake = _FakeDatabricks()
    fake.app_acl = [
        {"all_permissions": [{"permission_level": "CAN_USE"}]},  # no principal key
        {"group_name": "orphan"},  # no all_permissions
        {"user_name": "u@example.com", "all_permissions": "nonsense"},  # wrong shape
        {"user_name": "u@example.com", "all_permissions": [{"permission_level": ""}]},  # empty
        "not-a-dict",
        {
            "group_name": "dupes",
            "all_permissions": [
                {"permission_level": "CAN_USE"},
                {"permission_level": "CAN_USE"},
            ],
        },
    ]
    entries = await _svc(fake).get_app_permissions(_WS, _TOKEN, "my-app")

    assert entries == [
        {"principal": "dupes", "kind": "group", "level": "CAN_USE", "inherited": False}
    ]


@pytest.mark.asyncio
async def test_a_level_reported_both_directly_and_inherited_collapses_to_direct():
    """The direct grant is the one AGP can strip, so the collapsed entry must say ``inherited:
    False`` regardless of which report arrived first — otherwise a strippable grant would be
    dropped from every re-assert as if it were untouchable."""
    fake = _FakeDatabricks()
    fake.app_acl = [
        {
            "user_name": "u@example.com",
            "all_permissions": [
                {"permission_level": "CAN_USE", "inherited": True},
                {"permission_level": "CAN_USE"},
            ],
        }
    ]
    assert await _svc(fake).get_app_permissions(_WS, _TOKEN, "my-app") == [
        {"principal": "u@example.com", "kind": "user", "level": "CAN_USE", "inherited": False}
    ]


@pytest.mark.asyncio
async def test_an_unreadable_acl_record_is_logged_at_warning(caplog):
    """A skipped record is a record a re-assert would DELETE — ``get`` cannot return a skip count
    without breaking the pinned signature, so the one thing it owes the operator is a loud line."""
    fake = _FakeDatabricks()
    fake.app_acl = [{"group_name": "orphan"}]
    with caplog.at_level(logging.WARNING):
        assert await _svc(fake).get_app_permissions(_WS, _TOKEN, "my-app") == []
    assert any(
        rec.levelno == logging.WARNING and "unreadable ACL record" in rec.getMessage()
        for rec in caplog.records
    )
    # The record itself (a customer principal) never reaches the log line.
    assert "orphan" not in caplog.text


@pytest.mark.asyncio
async def test_get_app_permissions_missing_acl_is_an_empty_list():
    """An app with no ACL block is an app with no entries — not an error, and not a KeyError."""
    fake = _FakeDatabricks()
    fake.app_acl = []
    assert await _svc(fake).get_app_permissions(_WS, _TOKEN, "my-app") == []


@pytest.mark.asyncio
async def test_set_app_permissions_puts_exactly_the_composed_list():
    """Assert = REPLACE, so the request is a PUT and the body is the caller's list verbatim,
    denormalized back to Databricks' write shape (one key per principal kind)."""
    fake = _FakeDatabricks()
    await _svc(fake).set_app_permissions(
        _WS,
        _TOKEN,
        "my-app",
        [
            {"principal": "admins", "kind": "group", "level": "CAN_MANAGE"},
            {"principal": _SP_APPLICATION_ID, "kind": "service_principal", "level": "CAN_MANAGE"},
            {"principal": "lars.svensson@example.com", "kind": "user", "level": "CAN_USE"},
        ],
    )

    assert fake.calls == [("PUT", "/api/2.0/permissions/apps/my-app")]
    assert fake.bodies[0] == {
        "access_control_list": [
            {"group_name": "admins", "permission_level": "CAN_MANAGE"},
            {"service_principal_name": _SP_APPLICATION_ID, "permission_level": "CAN_MANAGE"},
            {"user_name": "lars.svensson@example.com", "permission_level": "CAN_USE"},
        ]
    }
    # And the ACL really was replaced — the read-back shows exactly the asserted list, every entry
    # now DIRECT (an asserted entry is one AGP can strip again).
    assert await _svc(fake).get_app_permissions(_WS, _TOKEN, "my-app") == [
        {"principal": "admins", "kind": "group", "level": "CAN_MANAGE", "inherited": False},
        {
            "principal": _SP_APPLICATION_ID,
            "kind": "service_principal",
            "level": "CAN_MANAGE",
            "inherited": False,
        },
        {
            "principal": "lars.svensson@example.com",
            "kind": "user",
            "level": "CAN_USE",
            "inherited": False,
        },
    ]


@pytest.mark.asyncio
async def test_set_app_permissions_refuses_a_level_outside_the_closed_vocabulary():
    """``CAN_USE``/``CAN_MANAGE`` is the whole vocabulary (research §4). A composer bug that
    invented a level is refused BEFORE the wire, same philosophy as the admins guard."""
    fake = _FakeDatabricks()
    with pytest.raises(DatabricksError) as excinfo:
        await _svc(fake).set_app_permissions(
            _WS,
            _TOKEN,
            "my-app",
            [
                {"principal": "admins", "kind": "group", "level": "CAN_MANAGE"},
                {"principal": "u@example.com", "kind": "user", "level": "CAN_EDIT"},
            ],
        )
    assert excinfo.value.kind == "acl_entry_invalid"
    assert fake.calls == []


@pytest.mark.asyncio
async def test_set_app_permissions_refuses_a_list_without_the_admins_grant():
    """THE guard: a composed list missing ``admins`` ``CAN_MANAGE`` would PUT the platform
    admins out of their own app — unrecoverable through AGP, since the next write needs
    CAN_MANAGE. Refused BEFORE the request (call count 0), not reported after the damage."""
    fake = _FakeDatabricks()
    with pytest.raises(DatabricksError) as excinfo:
        await _svc(fake).set_app_permissions(
            _WS,
            _TOKEN,
            "my-app",
            [{"principal": "lars.svensson@example.com", "kind": "user", "level": "CAN_USE"}],
        )

    assert excinfo.value.kind == "acl_missing_admins"
    assert _SAFE_KIND.match(excinfo.value.kind)
    assert fake.calls == []


@pytest.mark.asyncio
async def test_set_app_permissions_refuses_admins_at_a_lower_level():
    """``admins`` present but only ``CAN_USE`` is the same lockout — the guard is on the level,
    not on the name appearing somewhere in the list."""
    fake = _FakeDatabricks()
    with pytest.raises(DatabricksError) as excinfo:
        await _svc(fake).set_app_permissions(
            _WS,
            _TOKEN,
            "my-app",
            [{"principal": "admins", "kind": "group", "level": "CAN_USE"}],
        )
    assert excinfo.value.kind == "acl_missing_admins"
    assert fake.calls == []


@pytest.mark.asyncio
async def test_the_fake_refuses_a_put_without_the_admins_grant():
    """Strictness proof for the guard above: the boundary rejects it too, so the client-side
    check is a fast, honest refusal rather than the only thing standing in the way."""
    fake = _FakeDatabricks()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as client:
        resp = await client.put(
            f"{_WS}/api/2.0/permissions/apps/my-app",
            json={"access_control_list": [{"user_name": "u@example.com",
                                           "permission_level": "CAN_USE"}]},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_the_fake_refuses_a_put_that_names_the_same_principal_twice():
    """A PUT is a REPLACE, so two records for one principal have no defined meaning: upstream
    either 400s (revoke unusable) or picks one (a silent DOWNGRADE). Refused at the boundary too,
    so a client that re-PUTs a flattened read cannot pass."""
    fake = _FakeDatabricks()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as client:
        resp = await client.put(
            f"{_WS}/api/2.0/permissions/apps/my-app",
            json={
                "access_control_list": [
                    {"group_name": "admins", "permission_level": "CAN_MANAGE"},
                    {"service_principal_name": _SP_APPLICATION_ID,
                     "permission_level": "CAN_MANAGE"},
                    {"service_principal_name": _SP_APPLICATION_ID, "permission_level": "CAN_USE"},
                ]
            },
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_set_app_permissions_refuses_a_list_naming_the_same_principal_twice():
    """MEDIUM-1: the client was LESS strict than its own fake. A composed list (the path T13b
    uses) naming one principal twice has no defined upstream meaning — last-wins could seat that
    person at whichever record arrives last, or 400 the whole assert and leave the app unasserted.
    Refusing is not rewriting, so the verbatim-assert contract survives; the request never goes."""
    fake = _FakeDatabricks()
    with pytest.raises(DatabricksError) as excinfo:
        await _svc(fake).set_app_permissions(
            _WS,
            _TOKEN,
            "my-app",
            [
                {"principal": "admins", "kind": "group", "level": "CAN_MANAGE"},
                {"principal": "lars@example.com", "kind": "user", "level": "CAN_USE"},
                {"principal": "lars@example.com", "kind": "user", "level": "CAN_USE"},
            ],
        )
    assert excinfo.value.kind == "acl_entry_invalid"
    assert _SAFE_KIND.match(excinfo.value.kind)
    assert fake.calls == []


@pytest.mark.asyncio
async def test_set_app_permissions_duplicate_refusal_folds_case_for_users_only():
    """The duplicate check uses the SAME identity notion as the revoke match: two differently-cased
    records for one user are one principal upstream, while two differently-cased GROUPS are two
    groups and must still be transmittable."""
    fake = _FakeDatabricks()
    admins = {"principal": "admins", "kind": "group", "level": "CAN_MANAGE"}
    with pytest.raises(DatabricksError) as excinfo:
        await _svc(fake).set_app_permissions(
            _WS,
            _TOKEN,
            "my-app",
            [
                admins,
                {"principal": "Lars.Svensson@example.com", "kind": "user", "level": "CAN_MANAGE"},
                {"principal": "lars.svensson@example.com", "kind": "user", "level": "CAN_USE"},
            ],
        )
    assert excinfo.value.kind == "acl_entry_invalid"
    assert fake.calls == []

    # Groups keep case-sensitive identity — this list is legitimate and goes through.
    await _svc(fake).set_app_permissions(
        _WS,
        _TOKEN,
        "my-app",
        [
            admins,
            {"principal": "Analysts", "kind": "group", "level": "CAN_USE"},
            {"principal": "analysts", "kind": "group", "level": "CAN_USE"},
        ],
    )
    assert fake.calls == [("PUT", "/api/2.0/permissions/apps/my-app")]


@pytest.mark.asyncio
async def test_revoke_app_can_use_drops_only_the_named_principal_and_keeps_admins():
    """Read-modify-PUT, because the permissions API has no single-entry DELETE. Only the
    (principal, kind) pair asked for disappears: the other user's grant, the tenant SP's
    CAN_MANAGE, and the admins entry all survive."""
    fake = _FakeDatabricks()
    fake.app_acl = [
        {"group_name": "admins", "all_permissions": [{"permission_level": "CAN_MANAGE"}]},
        {"service_principal_name": _SP_APPLICATION_ID,
         "all_permissions": [{"permission_level": "CAN_MANAGE"}]},
        {"user_name": "lars.svensson@example.com",
         "all_permissions": [{"permission_level": "CAN_USE"}]},
        {"user_name": "other@example.com", "all_permissions": [{"permission_level": "CAN_USE"}]},
    ]
    svc = _svc(fake)
    await svc.revoke_app_can_use(
        _WS, _TOKEN, "my-app", "lars.svensson@example.com", "user"
    )

    assert fake.calls == [
        ("GET", "/api/2.0/permissions/apps/my-app"),
        ("PUT", "/api/2.0/permissions/apps/my-app"),
    ]
    assert await svc.get_app_permissions(_WS, _TOKEN, "my-app") == [
        {"principal": "admins", "kind": "group", "level": "CAN_MANAGE", "inherited": False},
        {
            "principal": _SP_APPLICATION_ID,
            "kind": "service_principal",
            "level": "CAN_MANAGE",
            "inherited": False,
        },
        {
            "principal": "other@example.com",
            "kind": "user",
            "level": "CAN_USE",
            "inherited": False,
        },
    ]


@pytest.mark.asyncio
async def test_revoke_collapses_a_multi_level_and_inherited_read_before_putting_it():
    """F1: the read is flattening and inherited-INCLUSIVE by design (it feeds the drift diff), so
    re-PUTting it verbatim would name the tenant SP twice in one body — the fake 400s that, and
    upstream might silently downgrade it to the inherited CAN_USE, costing AGP the CAN_MANAGE its
    next write needs. The PUT carries ONE entry per (principal, kind) at the strongest DIRECT
    level, and the inherited grant is not materialized as a permanent direct one."""
    fake = _FakeDatabricks()
    fake.app_acl = [
        {"group_name": "admins", "all_permissions": [{"permission_level": "CAN_MANAGE"}]},
        {
            "service_principal_name": _SP_APPLICATION_ID,
            "all_permissions": [
                {"permission_level": "CAN_MANAGE"},
                {"permission_level": "CAN_USE", "inherited": True},
            ],
        },
        {"group_name": "inherited-only",
         "all_permissions": [{"permission_level": "CAN_USE", "inherited": True}]},
        {"user_name": "lars@example.com", "all_permissions": [{"permission_level": "CAN_USE"}]},
        {"user_name": "other@example.com", "all_permissions": [{"permission_level": "CAN_USE"}]},
    ]
    svc = _svc(fake)
    await svc.revoke_app_can_use(_WS, _TOKEN, "my-app", "lars@example.com", "user")

    assert fake.bodies[0] == {
        "access_control_list": [
            {"group_name": "admins", "permission_level": "CAN_MANAGE"},
            {"service_principal_name": _SP_APPLICATION_ID, "permission_level": "CAN_MANAGE"},
            {"user_name": "other@example.com", "permission_level": "CAN_USE"},
        ]
    }


@pytest.mark.asyncio
async def test_revoke_app_can_use_matches_a_user_case_insensitively():
    """F2: Entra's ``mail`` is ``Lars.Svensson@example.com`` where Databricks reports the lowercased
    form. Exact equality made the revoke a SILENT SUCCESS that left the person holding CAN_USE —
    §3A's whole reason for existing is that this path closes."""
    fake = _FakeDatabricks()
    fake.app_acl = [
        {"group_name": "admins", "all_permissions": [{"permission_level": "CAN_MANAGE"}]},
        {"user_name": "lars.svensson@example.com",
         "all_permissions": [{"permission_level": "CAN_USE"}]},
    ]
    svc = _svc(fake)
    await svc.revoke_app_can_use(_WS, _TOKEN, "my-app", "Lars.Svensson@EXAMPLE.com", "user")

    assert await svc.get_app_permissions(_WS, _TOKEN, "my-app") == [
        {"principal": "admins", "kind": "group", "level": "CAN_MANAGE", "inherited": False}
    ]


@pytest.mark.asyncio
async def test_a_group_revoke_stays_case_sensitive():
    """Only usernames are case-insensitive upstream. Folding a GROUP name would let one revoke
    strip a differently-cased group nobody named — the widening the (principal, kind) match exists
    to prevent."""
    fake = _FakeDatabricks()
    fake.app_acl = [
        {"group_name": "admins", "all_permissions": [{"permission_level": "CAN_MANAGE"}]},
        {"group_name": "Analysts", "all_permissions": [{"permission_level": "CAN_USE"}]},
    ]
    svc = _svc(fake)
    await svc.revoke_app_can_use(_WS, _TOKEN, "my-app", "analysts", "group")

    assert await svc.get_app_permissions(_WS, _TOKEN, "my-app") == [
        {"principal": "admins", "kind": "group", "level": "CAN_MANAGE", "inherited": False},
        {"principal": "Analysts", "kind": "group", "level": "CAN_USE", "inherited": False},
    ]


@pytest.mark.asyncio
async def test_a_revoke_that_matches_nothing_warns(caplog):
    """Idempotence is correct; a BLIND success is not. One warning distinguishes "already gone"
    from "never matched" — the case-mismatch bypass above looked exactly like a clean revoke."""
    fake = _FakeDatabricks()
    svc = _svc(fake)
    with caplog.at_level(logging.WARNING):
        await svc.revoke_app_can_use(_WS, _TOKEN, "my-app", "nobody@example.com", "user")

    assert any(
        rec.levelno == logging.WARNING and "matched no removable" in rec.getMessage()
        for rec in caplog.records
    )
    # The principal is customer data and never reaches the line.
    assert "nobody@example.com" not in caplog.text


@pytest.mark.asyncio
async def test_an_inherited_only_match_is_not_a_removable_revoke(caplog):
    """An inherited grant is precisely what AGP cannot strip. Reporting that revoke as a clean
    success would be the drift report's comfortable lie moved one layer down."""
    fake = _FakeDatabricks()
    fake.app_acl = [
        {"group_name": "admins", "all_permissions": [{"permission_level": "CAN_MANAGE"}]},
        {"user_name": "u@example.com",
         "all_permissions": [{"permission_level": "CAN_USE", "inherited": True}]},
    ]
    svc = _svc(fake)
    with caplog.at_level(logging.WARNING):
        await svc.revoke_app_can_use(_WS, _TOKEN, "my-app", "u@example.com", "user")

    assert any("matched no removable" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_an_inherited_admins_grant_does_not_trip_the_added_admins_warning(caplog):
    """MEDIUM-2: the admins group's CAN_MANAGE is commonly reported as INHERITED, and the write
    collapse drops inherited entries — so deciding the "ADDED a grant nobody asked for" warning
    from the collapsed list fired it on EVERY revoke, drowning the one event it exists for. The
    decision is made from the PRE-collapse read; the entry is still asserted in the body, because
    ``set_app_permissions``' guard requires it there regardless."""
    fake = _FakeDatabricks()
    fake.app_acl = [
        {"group_name": "admins",
         "all_permissions": [{"permission_level": "CAN_MANAGE", "inherited": True}]},
        {"user_name": "lars@example.com", "all_permissions": [{"permission_level": "CAN_USE"}]},
    ]
    svc = _svc(fake)
    with caplog.at_level(logging.WARNING):
        await svc.revoke_app_can_use(_WS, _TOKEN, "my-app", "lars@example.com", "user")

    assert not any("ADDED the missing admins" in rec.getMessage() for rec in caplog.records)
    assert fake.bodies[0] == {
        "access_control_list": [{"group_name": "admins", "permission_level": "CAN_MANAGE"}]
    }


@pytest.mark.asyncio
async def test_a_workspace_that_really_stripped_admins_still_warns(caplog):
    """The other half of MEDIUM-2: the warning must still fire for the genuine event — a read
    reporting NO admins CAN_MANAGE at all, direct or inherited — because there this revoke really
    does add a grant nobody asked for on a customer workspace."""
    fake = _FakeDatabricks()
    fake.app_acl = [
        {"user_name": "lars@example.com", "all_permissions": [{"permission_level": "CAN_USE"}]},
    ]
    svc = _svc(fake)
    with caplog.at_level(logging.WARNING):
        await svc.revoke_app_can_use(_WS, _TOKEN, "my-app", "lars@example.com", "user")

    assert any("ADDED the missing admins" in rec.getMessage() for rec in caplog.records)
    assert fake.bodies[0] == {
        "access_control_list": [{"group_name": "admins", "permission_level": "CAN_MANAGE"}]
    }


@pytest.mark.asyncio
async def test_an_unknown_level_in_the_read_is_dropped_at_collapse_not_raised_later(caplog):
    """LOW-3: an out-of-vocabulary level read from a workspace used to survive the collapse and then
    raise ``acl_entry_invalid`` in ``set_app_permissions`` — so every revoke on that app failed
    FOREVER, blaming the caller's composition for something the workspace reported. A replace
    cannot preserve a level the client may not transmit, so it is dropped where the write shape is
    decided, loudly and with a count, and the revoke completes."""
    fake = _FakeDatabricks()
    fake.app_acl = [
        {"group_name": "admins", "all_permissions": [{"permission_level": "CAN_MANAGE"}]},
        {"group_name": "odd-level", "all_permissions": [{"permission_level": "CAN_EDIT"}]},
        {"user_name": "lars@example.com", "all_permissions": [{"permission_level": "CAN_USE"}]},
    ]
    svc = _svc(fake)
    # The READ still reports it: the drift probe must keep seeing what the workspace says.
    assert {
        "principal": "odd-level",
        "kind": "group",
        "level": "CAN_EDIT",
        "inherited": False,
    } in await svc.get_app_permissions(_WS, _TOKEN, "my-app")

    with caplog.at_level(logging.WARNING):
        await svc.revoke_app_can_use(_WS, _TOKEN, "my-app", "lars@example.com", "user")

    assert fake.bodies[0] == {
        "access_control_list": [{"group_name": "admins", "permission_level": "CAN_MANAGE"}]
    }
    assert any(
        "may not transmit" in rec.getMessage() and rec.levelno == logging.WARNING
        for rec in caplog.records
    )
    # A count and a safe label only — the dropped entry named a customer principal.
    assert "odd-level" not in caplog.text


@pytest.mark.asyncio
async def test_revoke_app_can_use_matches_on_kind_not_only_on_the_name():
    """A group and a user can share a string. Revoking the user must not strip the group of the
    same name — that would silently widen the revoke to everyone in it."""
    fake = _FakeDatabricks()
    fake.app_acl = [
        {"group_name": "admins", "all_permissions": [{"permission_level": "CAN_MANAGE"}]},
        {"group_name": "ambiguous", "all_permissions": [{"permission_level": "CAN_USE"}]},
        {"user_name": "ambiguous", "all_permissions": [{"permission_level": "CAN_USE"}]},
    ]
    svc = _svc(fake)
    await svc.revoke_app_can_use(_WS, _TOKEN, "my-app", "ambiguous", "user")

    assert await svc.get_app_permissions(_WS, _TOKEN, "my-app") == [
        {"principal": "admins", "kind": "group", "level": "CAN_MANAGE", "inherited": False},
        {"principal": "ambiguous", "kind": "group", "level": "CAN_USE", "inherited": False},
    ]


@pytest.mark.asyncio
async def test_revoke_app_can_use_is_idempotent_and_still_asserts():
    """Revoking an entry that is already gone re-PUTs the same list rather than erroring: the
    caller's desired state (this principal has no grant) already holds, and §3A's revoke is
    retryable after a half-completed write."""
    fake = _FakeDatabricks()
    svc = _svc(fake)
    await svc.revoke_app_can_use(_WS, _TOKEN, "my-app", "nobody@example.com", "user")

    assert ("PUT", "/api/2.0/permissions/apps/my-app") in fake.calls
    assert await svc.get_app_permissions(_WS, _TOKEN, "my-app") == [
        {"principal": "admins", "kind": "group", "level": "CAN_MANAGE", "inherited": False}
    ]


@pytest.mark.asyncio
async def test_revoke_app_can_use_restores_an_unreported_admins_entry():
    """An ACL read that reports no ``admins`` entry (a workspace that stripped it, or a shape the
    read skipped) must not make revoke unusable — the guard would refuse every re-PUT. The
    admins CAN_MANAGE entry is put back, which is the §3A invariant anyway."""
    fake = _FakeDatabricks()
    fake.app_acl = [
        {"user_name": "lars.svensson@example.com",
         "all_permissions": [{"permission_level": "CAN_USE"}]}
    ]
    svc = _svc(fake)
    await svc.revoke_app_can_use(_WS, _TOKEN, "my-app", "lars.svensson@example.com", "user")

    assert await svc.get_app_permissions(_WS, _TOKEN, "my-app") == [
        {"principal": "admins", "kind": "group", "level": "CAN_MANAGE", "inherited": False}
    ]


@pytest.mark.asyncio
async def test_permissions_methods_never_leak_the_token_on_a_rejection():
    """401/403 on any of the three surfaces the mapped safe kind only — never the bearer token,
    never the upstream body."""
    fake = _FakeDatabricks()
    svc = _svc(fake)
    for call in (
        svc.get_app_permissions(_WS, _TOKEN + "-wrong", "my-app"),
        svc.set_app_permissions(
            _WS,
            _TOKEN + "-wrong",
            "my-app",
            [{"principal": "admins", "kind": "group", "level": "CAN_MANAGE"}],
        ),
        svc.revoke_app_can_use(_WS, _TOKEN + "-wrong", "my-app", "u@example.com", "user"),
    ):
        with pytest.raises(DatabricksError) as excinfo:
            await call
        err = excinfo.value
        assert _SAFE_KIND.match(err.kind), err.kind
        assert _TOKEN not in str(err) and "wrong" not in str(err)


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile_name", ["../../secrets", "a?x=1", "a%2fb"])
async def test_the_new_permissions_methods_quote_the_app_name(hostile_name):
    """Same hazard as ``grant_app_can_use``: the app name is upstream-controlled, so every new
    method must keep it inside ONE path segment."""
    raw_paths: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        raw_paths.append(req.url.raw_path)
        return httpx.Response(200, json={"access_control_list": []})

    svc = DatabricksWorkspaceService(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        account_host=_ACCOUNT_HOST,
    )
    await svc.get_app_permissions(_WS, _TOKEN, hostile_name)
    await svc.set_app_permissions(
        _WS, _TOKEN, hostile_name,
        [{"principal": "admins", "kind": "group", "level": "CAN_MANAGE"}],
    )
    await svc.revoke_app_can_use(_WS, _TOKEN, hostile_name, "u@example.com", "user")

    prefix = b"/api/2.0/permissions/apps/"
    assert len(raw_paths) == 4  # get, set, and revoke's read-modify-write pair
    for raw in raw_paths:
        assert raw.startswith(prefix)
        tail = raw[len(prefix):]
        assert b"/" not in tail and b"?" not in tail and b"#" not in tail
        assert unquote(tail.decode()) == hostile_name


# =========================================================================== #
# create_service_principal
# =========================================================================== #
@pytest.mark.asyncio
async def test_create_service_principal_returns_id_and_application_id():
    fake = _FakeDatabricks()
    out = await _svc(fake).create_service_principal(_WS, _TOKEN, "agp-agent-x")

    assert out["id"] == "7788990011"
    assert out["application_id"] == "ffffffff-0000-1111-2222-333333333333"
    body = fake.bodies[0]
    assert body["displayName"] == "agp-agent-x"
    assert "urn:ietf:params:scim:schemas:core:2.0:ServicePrincipal" in body["schemas"]


@pytest.mark.asyncio
async def test_create_service_principal_reads_a_partial_response_defensively():
    """A SCIM response missing ``applicationId`` must not KeyError — the caller sees ""."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": "555"})

    svc = DatabricksWorkspaceService(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        account_host=_ACCOUNT_HOST,
    )
    out = await svc.create_service_principal(_WS, _TOKEN, "agp-agent-y")
    assert out == {"id": "555", "application_id": ""}


# =========================================================================== #
# ensure_federation_audience — idempotent BOTH directions
# =========================================================================== #
@pytest.mark.asyncio
async def test_ensure_federation_audience_present_twice_yields_one_entry_and_one_patch():
    fake = _FakeDatabricks(policies=[_entra_policy(audiences=["databricks"])])
    svc = _svc(fake)

    await svc.ensure_federation_audience(
        _ACCOUNT_HOST, _ACCOUNT_ID, _TOKEN, "api://agp", present=True
    )
    await svc.ensure_federation_audience(
        _ACCOUNT_HOST, _ACCOUNT_ID, _TOKEN, "api://agp", present=True
    )

    assert fake.policies[0]["oidc_policy"]["audiences"] == ["databricks", "api://agp"]
    patches = [c for c in fake.calls if c[0] == "PATCH"]
    assert len(patches) == 1, "the second call must be a read-only no-op"


@pytest.mark.asyncio
async def test_ensure_federation_audience_absent_remove_is_a_silent_no_op():
    fake = _FakeDatabricks(policies=[_entra_policy(audiences=["databricks"])])
    await _svc(fake).ensure_federation_audience(
        _ACCOUNT_HOST, _ACCOUNT_ID, _TOKEN, "api://never-added", present=False
    )
    assert fake.policies[0]["oidc_policy"]["audiences"] == ["databricks"]
    assert [c for c in fake.calls if c[0] == "PATCH"] == []


@pytest.mark.asyncio
async def test_ensure_federation_audience_removes_an_existing_audience():
    fake = _FakeDatabricks(policies=[_entra_policy(audiences=["databricks", "api://agp"])])
    await _svc(fake).ensure_federation_audience(
        _ACCOUNT_HOST, _ACCOUNT_ID, _TOKEN, "api://agp", present=False
    )
    assert fake.policies[0]["oidc_policy"]["audiences"] == ["databricks"]


@pytest.mark.asyncio
async def test_ensure_federation_audience_removes_several_forms_in_one_list_and_one_patch():
    """E29 livefix-7: teardown removes BOTH audience forms (GUID + legacy api:// URI) in ONE
    call — one policy list, one PATCH. Two back-to-back single-audience calls tripped the
    account API's rate limit live (the second list 429'd AFTER the entry was already gone,
    so teardown reported failure over a removal that had succeeded)."""
    fake = _FakeDatabricks(
        policies=[_entra_policy(audiences=["databricks", "agent-guid", "api://agp-agent-x"])]
    )
    await _svc(fake).ensure_federation_audience(
        _ACCOUNT_HOST, _ACCOUNT_ID, _TOKEN, ["agent-guid", "api://agp-agent-x"], present=False
    )
    assert fake.policies[0]["oidc_policy"]["audiences"] == ["databricks"]
    assert len([c for c in fake.calls if c[0] == "GET"]) == 1
    assert len([c for c in fake.calls if c[0] == "PATCH"]) == 1


@pytest.mark.asyncio
async def test_ensure_federation_audience_multi_remove_with_only_one_present_patches_once():
    """The common post-livefix-6 shape: the GUID is on the policy, the legacy URI never was —
    one PATCH dropping the present one, the absent one a silent part of the same call."""
    fake = _FakeDatabricks(policies=[_entra_policy(audiences=["databricks", "agent-guid"])])
    await _svc(fake).ensure_federation_audience(
        _ACCOUNT_HOST, _ACCOUNT_ID, _TOKEN, ["agent-guid", "api://agp-agent-x"], present=False
    )
    assert fake.policies[0]["oidc_policy"]["audiences"] == ["databricks"]
    assert len([c for c in fake.calls if c[0] == "PATCH"]) == 1


@pytest.mark.asyncio
async def test_ensure_federation_audience_multi_remove_of_absent_forms_is_a_read_only_no_op():
    fake = _FakeDatabricks(policies=[_entra_policy(audiences=["databricks"])])
    await _svc(fake).ensure_federation_audience(
        _ACCOUNT_HOST, _ACCOUNT_ID, _TOKEN, ["agent-guid", "api://agp-agent-x"], present=False
    )
    assert fake.policies[0]["oidc_policy"]["audiences"] == ["databricks"]
    assert [c for c in fake.calls if c[0] == "PATCH"] == []


@pytest.mark.asyncio
async def test_ensure_federation_audience_with_no_policy_raises_a_safe_kind():
    """This method cannot invent an issuer, so "add an audience" with no policy is an error —
    but "remove" is still a no-op."""
    fake = _FakeDatabricks(policies=[])
    svc = _svc(fake)

    await svc.ensure_federation_audience(
        _ACCOUNT_HOST, _ACCOUNT_ID, _TOKEN, "api://agp", present=False
    )
    with pytest.raises(DatabricksError) as excinfo:
        await svc.ensure_federation_audience(
            _ACCOUNT_HOST, _ACCOUNT_ID, _TOKEN, "api://agp", present=True
        )
    assert _SAFE_KIND.match(excinfo.value.kind)


# =========================================================================== #
# FIX round 1 — the six defects, each reproduced before it was fixed
# =========================================================================== #
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed",
    [
        {"issuer": "https://login.microsoftonline.com/t/v2.0", "audience": "databricks"},
        {"issuer": "https://login.microsoftonline.com/t/v2.0", "audiences": "databricks"},
        {"issuer": "https://login.microsoftonline.com/t/v2.0", "audiences": [1, 2]},
        {"issuer": "https://login.microsoftonline.com/t/v2.0"},
    ],
)
async def test_an_unreadable_audience_list_refuses_to_write(malformed):
    """CRITICAL. The ``audiences`` key is INFERRED from CLI examples, not read off a loaded
    reference page. When the real shape differs — ``audience`` singular, a bare string, non-strings
    — treating it as empty turned this method into "replace the customer's whole audience list with
    one element": silent deletion of account-level trust state that other tenants share. Refusing
    to write is the only safe reading of a shape we cannot parse."""
    fake = _FakeDatabricks(policies=[{"policy_id": "pol-1", "oidc_policy": dict(malformed)}])
    with pytest.raises(DatabricksError) as excinfo:
        await _svc(fake).ensure_federation_audience(
            _ACCOUNT_HOST, _ACCOUNT_ID, _TOKEN, "api://agp", present=True
        )
    assert excinfo.value.kind == "federation_policy_unreadable"
    assert _SAFE_KIND.match(excinfo.value.kind)
    # The point of the fix: nothing was written.
    assert [c for c in fake.calls if c[0] == "PATCH"] == []


@pytest.mark.asyncio
async def test_the_patch_targets_the_entra_policy_not_the_first_one():
    """CRITICAL. An enterprise plausibly federates Okta for humans AND Entra for workloads (up to
    20 policies per account, §3.2). ``policies[0]`` wrote AGP's audience onto whichever came first
    — editing Okta's trust statement while ``probe_capabilities`` reported on the Entra one. Both
    now discriminate on the same issuer marker, so they cannot disagree."""
    okta = {
        "policy_id": "okta-pol",
        "oidc_policy": {"issuer": "https://okta.example/oidc", "audiences": ["okta-aud"]},
    }
    entra = _entra_policy(audiences=["databricks"], policy_id="entra-pol")
    fake = _FakeDatabricks(policies=[okta, entra])  # Okta FIRST, deliberately

    await _svc(fake).ensure_federation_audience(
        _ACCOUNT_HOST, _ACCOUNT_ID, _TOKEN, "api://agp", present=True
    )

    patches = [p for m, p in fake.calls if m == "PATCH"]
    assert patches == [f"/api/2.0/accounts/{_ACCOUNT_ID}/federationPolicies/entra-pol"]
    assert fake.policies[1]["oidc_policy"]["audiences"] == ["databricks", "api://agp"]
    assert fake.policies[0]["oidc_policy"]["audiences"] == ["okta-aud"], "Okta untouched"


@pytest.mark.asyncio
async def test_multiple_entra_policies_refuse_to_guess():
    """Two Entra policies and no subject-claim or audience hint to choose on: editing shared trust
    state on a coin flip is worse than refusing."""
    fake = _FakeDatabricks(
        policies=[
            _entra_policy(audiences=["a"], policy_id="e1"),
            _entra_policy(audiences=["b"], policy_id="e2"),
        ]
    )
    with pytest.raises(DatabricksError) as excinfo:
        await _svc(fake).ensure_federation_audience(
            _ACCOUNT_HOST, _ACCOUNT_ID, _TOKEN, "api://agp", present=True
        )
    assert excinfo.value.kind == "federation_policy_ambiguous"
    assert [c for c in fake.calls if c[0] == "PATCH"] == []


@pytest.mark.asyncio
async def test_an_okta_only_account_reports_no_entra_policy():
    """A non-Entra account is ``federation_policy_missing``, not "use the Okta one"."""
    fake = _FakeDatabricks(
        policies=[
            {"policy_id": "okta", "oidc_policy": {"issuer": "https://okta.example/oidc",
                                                  "audiences": ["x"]}}
        ]
    )
    svc = _svc(fake)
    # remove is still a silent no-op — AGP's audience is not on any Entra policy by definition.
    await svc.ensure_federation_audience(
        _ACCOUNT_HOST, _ACCOUNT_ID, _TOKEN, "api://agp", present=False
    )
    with pytest.raises(DatabricksError) as excinfo:
        await svc.ensure_federation_audience(
            _ACCOUNT_HOST, _ACCOUNT_ID, _TOKEN, "api://agp", present=True
        )
    assert excinfo.value.kind == "federation_policy_missing"
    assert [c for c in fake.calls if c[0] == "PATCH"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hostile_name",
    ["../../secrets", "a?x=1", "app/../../../api/2.0/secrets", "x#frag", "a b", "a%2fb"],
)
async def test_a_hostile_app_name_cannot_escape_its_path_segment(hostile_name):
    """An app name is UPSTREAM-controlled — it arrives from a discovery listing, not from AGP.
    Unquoted, ``"../../secrets"`` resolved to ``/api/2.0/secrets`` and ``"a?x=1"`` injected a query
    string: a name in a customer's listing became a choice of endpoint."""
    seen: list[httpx.URL] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url)
        return httpx.Response(200, json={})

    svc = DatabricksWorkspaceService(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        account_host=_ACCOUNT_HOST,
    )
    await svc.grant_app_can_use(_WS, _TOKEN, hostile_name, "agp-operators")

    url = seen[0]
    # Assert on ``raw_path`` — the bytes actually sent. ``url.path`` is httpx's percent-DECODED
    # convenience view, so it still reads ``/../../secrets`` even when the wire is correctly
    # escaped; asserting on it would fail a correct implementation (and pass a broken one whose
    # escaping was cosmetic).
    prefix = b"/api/2.0/permissions/apps/"
    raw = url.raw_path
    assert raw.startswith(prefix)
    tail = raw[len(prefix):]
    # Exactly ONE path segment, with no smuggled query or fragment.
    assert b"/" not in tail
    assert b"?" not in tail and b"#" not in tail
    assert not url.query
    # And the decoded segment is still the app the caller asked for — escaped, not mangled.
    assert unquote(tail.decode()) == hostile_name


@pytest.mark.asyncio
async def test_a_hostile_policy_id_cannot_escape_its_path_segment():
    """``policy_id`` is upstream-controlled too — it comes back from the policy listing."""
    raw_paths: list[bytes] = []
    fake = _FakeDatabricks(policies=[_entra_policy(audiences=["databricks"], policy_id="../../x")])

    def handler(req: httpx.Request) -> httpx.Response:
        raw_paths.append(req.url.raw_path)
        return fake.handler(req)

    svc = DatabricksWorkspaceService(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        account_host=_ACCOUNT_HOST,
    )
    await svc.ensure_federation_audience(
        _ACCOUNT_HOST, _ACCOUNT_ID, _TOKEN, "api://agp", present=True
    )

    patched = [p for p in raw_paths if b"?" in p]  # the PATCH carries update_mask
    assert len(patched) == 1
    base = f"/api/2.0/accounts/{_ACCOUNT_ID}/federationPolicies/".encode()
    tail = patched[0][len(base):].split(b"?")[0]
    assert b"/" not in tail
    assert unquote(tail.decode()) == "../../x"


@pytest.mark.asyncio
async def test_a_constant_pagination_token_raises_instead_of_looping_forever():
    """The loop's exit condition is entirely upstream-controlled: a workspace echoing a CONSTANT
    ``next_page_token`` would spin this coroutine forever, holding a request slot and growing the
    record list without bound. A repeated token is caught on the second sighting."""
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200, json={"apps": [{"name": f"a{calls['n']}"}], "next_page_token": "SAME"}
        )

    svc = DatabricksWorkspaceService(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        account_host=_ACCOUNT_HOST,
    )
    with pytest.raises(DatabricksError) as excinfo:
        await svc.list_apps(_WS, _TOKEN)
    assert excinfo.value.kind == "pagination_overflow"
    assert calls["n"] <= 3, "must stop on the repeat, not grind to the page cap"


@pytest.mark.asyncio
async def test_an_endless_stream_of_fresh_tokens_hits_the_page_cap():
    """The slower cycle: every page hands out a NEW token, so the seen-guard never fires. The hard
    cap is the backstop, and it raises rather than truncating — an inventory silently cut short but
    presented as complete is a governance lie."""
    import services.databricks_workspace_service as mod

    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200, json={"endpoints": [{"name": f"e{calls['n']}"}],
                       "next_page_token": f"t{calls['n']}"}
        )

    svc = DatabricksWorkspaceService(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        account_host=_ACCOUNT_HOST,
    )
    with pytest.raises(DatabricksError) as excinfo:
        await svc.list_serving_endpoints(_WS, _TOKEN)
    assert excinfo.value.kind == "pagination_overflow"
    assert calls["n"] == mod._MAX_PAGES


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_url", ["", "not-a-url", "://missing-scheme", "ht tp://x"])
async def test_a_malformed_workspace_url_maps_to_a_safe_kind(bad_url):
    """``httpx.InvalidURL`` and the bare ``ValueError`` httpx raises for an unroutable URL are NOT
    ``httpx.HTTPError`` subclasses, so a half-configured tenant record escaped the error boundary
    raw as ``ValueError: unknown url type`` — a 500 carrying an httpx-internal message."""
    fake = _FakeDatabricks()
    with pytest.raises(DatabricksError) as excinfo:
        await _svc(fake).list_apps(bad_url, _TOKEN)
    assert excinfo.value.kind in ("bad_request_url", "unreachable")
    assert _SAFE_KIND.match(excinfo.value.kind)


@pytest.mark.asyncio
async def test_the_patch_body_carries_the_freshly_read_list_plus_one_delta():
    """The lost-update mitigation, stated as a test: the body is what was just READ plus this
    call's single addition — never a reconstructed or defaulted list. So a concurrent writer can
    lose ITS edit, but this call can never drop an audience it read."""
    fake = _FakeDatabricks(
        policies=[_entra_policy(audiences=["databricks", "other-tenant-aud"])]
    )
    await _svc(fake).ensure_federation_audience(
        _ACCOUNT_HOST, _ACCOUNT_ID, _TOKEN, "api://agp", present=True
    )
    sent = fake.bodies[0]["oidc_policy"]["audiences"]
    assert sent == ["databricks", "other-tenant-aud", "api://agp"]


# =========================================================================== #
# FIX round 2 — a non-dict oidc_policy, and the account_id path segment
# =========================================================================== #
_MALFORMED_OIDC = ["not-a-dict", [1, 2, 3], 42, None, True]


@pytest.mark.asyncio
@pytest.mark.parametrize("junk", _MALFORMED_OIDC)
async def test_a_non_dict_oidc_policy_is_skipped_not_inspected(junk):
    """``(p.get("oidc_policy") or {}).get(...)`` raised a bare ``AttributeError`` on a string,
    list, or int — escaping ``ensure_federation_audience`` unmapped, BEFORE the isinstance guard
    further down could help. A shape we cannot parse is not evidence of Entra federation, so it is
    skipped: the real Entra policy beside it is still found and patched."""
    junk_policy = {"policy_id": "junk", "oidc_policy": junk}
    good = _entra_policy(audiences=["databricks"], policy_id="entra-pol")
    fake = _FakeDatabricks(policies=[junk_policy, good])  # junk FIRST

    await _svc(fake).ensure_federation_audience(
        _ACCOUNT_HOST, _ACCOUNT_ID, _TOKEN, "api://agp", present=True
    )

    patches = [p for m, p in fake.calls if m == "PATCH"]
    assert patches == [f"/api/2.0/accounts/{_ACCOUNT_ID}/federationPolicies/entra-pol"]
    assert fake.policies[1]["oidc_policy"]["audiences"] == ["databricks", "api://agp"]


@pytest.mark.asyncio
@pytest.mark.parametrize("junk", _MALFORMED_OIDC)
async def test_a_non_dict_oidc_policy_alone_raises_a_safe_kind_not_attributeerror(junk):
    """With ONLY a malformed policy there is no Entra policy, so this is the missing-policy path —
    a mapped DatabricksError, never an AttributeError."""
    fake = _FakeDatabricks(policies=[{"policy_id": "junk", "oidc_policy": junk}])
    with pytest.raises(DatabricksError) as excinfo:
        await _svc(fake).ensure_federation_audience(
            _ACCOUNT_HOST, _ACCOUNT_ID, _TOKEN, "api://agp", present=True
        )
    assert excinfo.value.kind == "federation_policy_missing"
    assert [c for c in fake.calls if c[0] == "PATCH"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("junk", _MALFORMED_OIDC)
async def test_probe_capabilities_survives_a_non_dict_oidc_policy(junk):
    """The nastier half of the same bug: the crash landed AFTER ``account_admin = True`` was set,
    so a malformed policy blew up a probe whose entire contract is "never raise" — and would have
    surfaced as a 500 on the connect-tenant screen."""
    fake = _FakeDatabricks(policies=[{"policy_id": "junk", "oidc_policy": junk}])
    fake.apps_pages = [{"apps": [{"name": "a1"}]}]

    caps = await _svc(fake).probe_capabilities(
        _WS, _ACCOUNT_ID, _CLIENT_ID, _SECRET,
        account_admin_client_id=_CLIENT_ID, account_admin_secret=_SECRET,
    )
    assert caps == {"can_discover": True, "account_admin": True, "user_sync": False}


def test_the_two_scan_sites_share_one_predicate():
    """``ensure_federation_audience`` and ``probe_capabilities`` MUST agree on which policy is the
    Entra one — when they disagreed, one wrote to a policy the other was not reporting on. Pinning
    the shared helper keeps a future edit from forking the logic again."""
    import services.databricks_workspace_service as mod

    assert mod._is_entra_policy(
        {"oidc_policy": {"issuer": "https://login.microsoftonline.com/t/v2.0"}}
    )
    assert not mod._is_entra_policy({"oidc_policy": {"issuer": "https://okta.example/oidc"}})
    for junk in _MALFORMED_OIDC:
        assert not mod._is_entra_policy({"oidc_policy": junk})
    assert not mod._is_entra_policy({})
    # Both call sites go through it — a re-inlined `.get("issuer")` scan is the regression.
    source = inspect.getsource(mod)
    assert source.count("_is_entra_policy(p)") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hostile_account",
    [
        "a1/../../accounts/other-acct/federationPolicies",
        "acct?x=1",
        "../../../secrets",
        "a/b",
    ],
)
async def test_a_hostile_account_id_cannot_retarget_account_level_calls(hostile_account):
    """``account_id`` is a stored tenant-record field, and unquoted it is a PATH, not an
    identifier: ``a1/../../accounts/other-acct/...`` retargeted the account-ADMIN token mint and
    the federation-policy reads at a different Databricks account. That is the highest-privilege
    call in the module being aimed by a config value."""
    seen: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.raw_path)
        if b"/v1/token" in req.url.raw_path:
            return httpx.Response(200, json={"access_token": _TOKEN})
        return httpx.Response(200, json={"policies": []})

    svc = DatabricksWorkspaceService(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        account_host=_ACCOUNT_HOST,
    )
    await svc._mint_account_token(hostile_account, _CLIENT_ID, _SECRET)
    await svc._list_federation_policies(_ACCOUNT_HOST, hostile_account, _TOKEN)

    mint, policies = seen[0].decode(), seen[1].decode()
    # The id occupies exactly ONE segment in each URL, and no other account is reachable.
    assert mint.startswith("/oidc/accounts/") and mint.endswith("/v1/token")
    mint_segment = mint[len("/oidc/accounts/"):-len("/v1/token")]
    assert unquote(mint_segment) == hostile_account
    assert "/" not in mint_segment, mint  # one segment, no traversal on the wire
    assert "?" not in mint_segment

    prefix = "/api/2.0/accounts/"
    assert policies.startswith(prefix) and policies.endswith("/federationPolicies")
    assert unquote(policies[len(prefix):-len("/federationPolicies")]) == hostile_account
    assert "other-acct" not in policies.replace(quote(hostile_account, safe=""), "")
    assert "?" not in policies


# =========================================================================== #
# probe_capabilities — plain values, fail-closed
# =========================================================================== #
@pytest.mark.asyncio
async def test_probe_capabilities_all_green():
    fake = _FakeDatabricks(policies=[_entra_policy(audiences=["databricks"])])
    fake.apps_pages = [{"apps": [{"name": "a1"}]}]
    caps = await _svc(fake).probe_capabilities(
        _WS, _ACCOUNT_ID, _CLIENT_ID, _SECRET,
        account_admin_client_id=_CLIENT_ID, account_admin_secret=_SECRET,
    )
    assert caps == {"can_discover": True, "account_admin": True, "user_sync": True}


@pytest.mark.asyncio
async def test_probe_capabilities_without_admin_creds_reports_discovery_only():
    fake = _FakeDatabricks(policies=[_entra_policy(audiences=["databricks"])])
    fake.apps_pages = [{"apps": [{"name": "a1"}]}]
    caps = await _svc(fake).probe_capabilities(_WS, _ACCOUNT_ID, _CLIENT_ID, _SECRET)
    assert caps == {"can_discover": True, "account_admin": False, "user_sync": False}


@pytest.mark.asyncio
async def test_probe_capabilities_fails_closed_on_a_token_error():
    fake = _FakeDatabricks()
    fake.token_status = 500
    caps = await _svc(fake).probe_capabilities(
        _WS, _ACCOUNT_ID, _CLIENT_ID, _SECRET,
        account_admin_client_id=_CLIENT_ID, account_admin_secret=_SECRET,
    )
    assert caps == {"can_discover": False, "account_admin": False, "user_sync": False}


@pytest.mark.asyncio
async def test_probe_capabilities_fails_closed_on_a_transport_blowup():
    fake = _FakeDatabricks()
    fake.raise_transport = True
    caps = await _svc(fake).probe_capabilities(
        _WS, _ACCOUNT_ID, _CLIENT_ID, _SECRET,
        account_admin_client_id=_CLIENT_ID, account_admin_secret=_SECRET,
    )
    assert caps == {"can_discover": False, "account_admin": False, "user_sync": False}


@pytest.mark.asyncio
async def test_probe_capabilities_discovery_falls_back_to_serving_endpoints():
    """``apps list`` visibility follows app permissions (§5.3), so a discovery SP with no app
    grants sees nothing — a readable serving-endpoint list still proves discovery works."""
    fake = _FakeDatabricks()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/2.0/apps":
            return httpx.Response(403, json={"error_code": "PERMISSION_DENIED"})
        return fake.handler(req)

    fake.endpoint_pages = [{"endpoints": [{"name": "e1"}]}]
    svc = DatabricksWorkspaceService(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        account_host=_ACCOUNT_HOST,
    )
    caps = await svc.probe_capabilities(_WS, _ACCOUNT_ID, _CLIENT_ID, _SECRET)
    assert caps["can_discover"] is True


@pytest.mark.asyncio
async def test_probe_capabilities_user_sync_false_for_a_non_entra_issuer():
    """``user_sync`` means "Entra identities are federated into this account" — an Okta-issued
    policy must not claim it."""
    fake = _FakeDatabricks(
        policies=[{"policy_id": "p", "oidc_policy": {"issuer": "https://okta.example/oidc",
                                                     "audiences": ["databricks"]}}]
    )
    fake.apps_pages = [{"apps": [{"name": "a1"}]}]
    caps = await _svc(fake).probe_capabilities(
        _WS, _ACCOUNT_ID, _CLIENT_ID, _SECRET,
        account_admin_client_id=_CLIENT_ID, account_admin_secret=_SECRET,
    )
    assert caps["account_admin"] is True
    assert caps["user_sync"] is False


# =========================================================================== #
# Contract C-2 shape + the T1-independence binding
# =========================================================================== #
def test_contract_c2_signatures():
    """C-2 is a cross-task contract: T3/T6/T7/T10 call these names positionally, so a renamed
    parameter is a build break in another wave, not a style nit."""
    expected = {
        "mint_m2m_token": ["self", "workspace_url", "client_id", "client_secret"],
        "exchange_federated_token": ["self", "workspace_url", "subject_jwt"],
        "list_apps": ["self", "workspace_url", "token"],
        "list_serving_endpoints": ["self", "workspace_url", "token"],
        # ``kind`` is APPENDED with a default (E29/T13a): the existing positional callers keep
        # working, which is the whole reason it goes last rather than beside ``app_name``.
        "grant_app_can_use": [
            "self", "workspace_url", "token", "app_name", "service_principal_or_group", "kind",
        ],
        "get_app_permissions": ["self", "workspace_url", "token", "app_name"],
        "set_app_permissions": ["self", "workspace_url", "token", "app_name", "entries"],
        "revoke_app_can_use": [
            "self", "workspace_url", "token", "app_name", "principal", "kind",
        ],
        "create_service_principal": ["self", "workspace_url", "token", "display_name"],
        "ensure_federation_audience": [
            "self", "account_host", "account_id", "token", "audience", "present",
        ],
        "probe_capabilities": [
            "self", "workspace_url", "account_id", "client_id", "client_secret",
            "account_admin_client_id", "account_admin_secret",
        ],
    }
    for name, params in expected.items():
        method = getattr(DatabricksWorkspaceService, name)
        assert inspect.iscoroutinefunction(method), f"{name} must be async"
        sig = inspect.signature(method)
        assert list(sig.parameters) == params, name

    probe = inspect.signature(DatabricksWorkspaceService.probe_capabilities).parameters
    assert probe["account_admin_client_id"].default is None
    assert probe["account_admin_secret"].default is None
    # ``present`` is keyword-only on ensure_federation_audience (C-2's ``*, present: bool``).
    fed = inspect.signature(DatabricksWorkspaceService.ensure_federation_audience).parameters
    assert fed["present"].kind is inspect.Parameter.KEYWORD_ONLY
    # ``kind`` defaults to "group" so every pre-T13a call site is byte-for-byte unchanged.
    grant = inspect.signature(DatabricksWorkspaceService.grant_app_can_use).parameters
    assert grant["kind"].default == "group"


def test_module_imports_no_tenant_or_agent_model():
    """BINDING (C-2): stage config arrives as plain values so T2 is independent of T1's models.
    An import here would couple the waves and is the exact thing the plan forbids."""
    source = inspect.getsource(
        __import__("services.databricks_workspace_service", fromlist=["x"])
    )
    assert "models.tenant" not in source
    assert "models.agent" not in source
    assert "from models" not in source


@pytest.mark.parametrize(
    "hostile_code",
    [
        # A real PERMISSION_DENIED body names the principal and the object path.
        "PERMISSION_DENIED: User aaaa-bbbb is not authorized on /Workspace/customer/secret",
        "INVALID <script>alert(1)</script>",
        "code with spaces",
        "x" * 200,  # over the 64-char cap
        "",
        "123-numeric",
    ],
)
def test_safe_kind_rejects_hostile_upstream_codes_by_execution(hostile_code):
    """The ``.kind`` regex is a security boundary, so it is verified by EXECUTION over hostile
    inputs (Global Constraints), not by reading it. Anything that is not a bare code falls back
    to a fixed per-status word — the upstream string never becomes the kind."""
    from services.databricks_workspace_service import _safe_kind

    resp = httpx.Response(403, json={"error_code": hostile_code})
    kind = _safe_kind(resp)
    assert kind == "forbidden"
    assert _SAFE_KIND.match(kind)


def test_safe_kind_keeps_a_bare_databricks_code():
    from services.databricks_workspace_service import _safe_kind

    assert _safe_kind(httpx.Response(403, json={"error_code": "PERMISSION_DENIED"})) == (
        "permission_denied"
    )


@pytest.mark.asyncio
async def test_no_secret_or_token_reaches_a_log_record(caplog):
    """Mirrors ``connection_service``'s pinned property: this module logs safe codes only. A
    grep-by-eye is not a guarantee; the log records are inspected."""
    caplog.set_level("DEBUG")
    fake = _FakeDatabricks()
    fake.token_status = 500
    with pytest.raises(DatabricksError):
        await _svc(fake).mint_m2m_token(_WS, _CLIENT_ID, _SECRET)

    fake2 = _FakeDatabricks()
    fake2.token_status = 403
    with pytest.raises(DatabricksError):
        await _svc(fake2).exchange_federated_token(_WS, _JWT)

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert blob, "the failures should have logged something"
    for forbidden in (_SECRET, _JWT, _TOKEN):
        assert forbidden not in blob
    # Nor the upstream RESPONSE BODY: a real Databricks error message names workspace paths and
    # principal ids, so ``resp.text`` in a log line is the same leak wearing a different hat.
    assert "boom" not in blob


def test_explicit_timeouts_are_configured():
    """httpx's default is no timeout on some transports; an unbounded Databricks call would hang
    a request thread. The module must name its own timeout."""
    import services.databricks_workspace_service as mod

    assert isinstance(mod._HTTP_TIMEOUT_SECONDS, (int, float))
    assert 0 < mod._HTTP_TIMEOUT_SECONDS <= 60


# =========================================================================== #
# E29/T6 additions — per-SP secret minting + the public account-token mint
# =========================================================================== #

@pytest.mark.asyncio
async def test_create_service_principal_secret_returns_the_credential():
    fake = _FakeDatabricks()
    out = await _svc(fake).create_service_principal_secret(_WS, _TOKEN, _SCIM_SP_ID)

    assert out == {"secret": _SP_SECRET, "secret_hash": "sha256:abc", "id": "secret-rec-1"}
    # Path pinned live 2026-08-11 (B2.5): the workspace host serves the secret mint under an
    # ``/accounts/`` prefix; the un-prefixed form 404s on a real workspace.
    assert ("POST", f"/api/2.0/accounts/servicePrincipals/{_SCIM_SP_ID}/credentials/secrets") in fake.calls


@pytest.mark.asyncio
async def test_the_secret_call_takes_the_scim_id_not_the_application_id():
    """The two ids are interchangeable-LOOKING and only one works. The fake 404s the
    application id exactly as the real API does, so this claim is enforced rather than
    commented."""
    fake = _FakeDatabricks()
    with pytest.raises(DatabricksError) as err:
        await _svc(fake).create_service_principal_secret(_WS, _TOKEN, _SP_APPLICATION_ID)

    assert _SAFE_KIND.match(err.value.kind)


@pytest.mark.asyncio
async def test_a_secret_response_without_a_secret_is_bad_response():
    """An empty secret persisted as though it were real produces a service principal that can
    never authenticate and a record claiming it can — so this must raise, not return ""."""
    fake = _FakeDatabricks()
    fake.sp_secret_body = {"id": "secret-rec-1", "secret_hash": "sha256:abc"}
    with pytest.raises(DatabricksError) as err:
        await _svc(fake).create_service_principal_secret(_WS, _TOKEN, _SCIM_SP_ID)

    assert err.value.kind == "bad_response"


@pytest.mark.asyncio
async def test_the_sp_id_is_quoted_into_one_segment():
    """A traversal-shaped SCIM id must not retarget the call. Quoted, it stays one segment and
    the fake 404s it; unquoted, ``../../secrets`` selected a different endpoint."""
    fake = _FakeDatabricks()
    with pytest.raises(DatabricksError):
        await _svc(fake).create_service_principal_secret(_WS, _TOKEN, "../../secrets")

    for _method, path in fake.calls:
        assert path.startswith("/api/2.0/accounts/servicePrincipals/")
        assert "/credentials/secrets" in path


@pytest.mark.asyncio
async def test_the_minted_secret_never_reaches_a_log_or_an_error(caplog):
    fake = _FakeDatabricks()
    fake.sp_secret_status = 403
    with caplog.at_level("DEBUG"):
        with pytest.raises(DatabricksError) as err:
            await _svc(fake).create_service_principal_secret(_WS, _TOKEN, _SCIM_SP_ID)

    blob = "\n".join(r.getMessage() for r in caplog.records)
    for forbidden in (_SP_SECRET, _TOKEN):
        assert forbidden not in blob
        assert forbidden not in err.value.message
    assert "boom" not in blob and "boom" not in err.value.message


@pytest.mark.asyncio
async def test_mint_account_token_is_public_and_the_old_private_name_still_works():
    fake = _FakeDatabricks()
    svc = _svc(fake)

    assert await svc.mint_account_token(_ACCOUNT_ID, _CLIENT_ID, _SECRET) == _TOKEN
    # The alias is the SAME function, so the two names cannot drift apart.
    assert svc._mint_account_token.__func__ is svc.mint_account_token.__func__


@pytest.mark.asyncio
async def test_mint_account_token_honours_a_per_call_account_host():
    """The account console host is per-cloud and not derivable from the workspace URL, so the
    caller that knows the tenant's cloud passes it — rather than every caller having to build a
    second client to reach Azure or GCP."""
    fake = _FakeDatabricks()
    # A host with a trailing slash, to prove ``_origin`` normalisation is applied.
    other_host = "https://accounts-azure-test.example/"
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    svc = DatabricksWorkspaceService(http_client=client, account_host=_ACCOUNT_HOST)

    await svc.mint_account_token(
        _ACCOUNT_ID, _CLIENT_ID, _SECRET, account_host=other_host
    )

    assert ("POST", f"/oidc/accounts/{_ACCOUNT_ID}/v1/token") in fake.calls
    # The instance default was NOT used for this call.
    assert svc._account_host == _ACCOUNT_HOST


@pytest.mark.asyncio
async def test_the_account_token_credential_never_reaches_a_log_or_an_error(caplog):
    fake = _FakeDatabricks()
    fake.token_status = 401
    with caplog.at_level("DEBUG"):
        with pytest.raises(DatabricksError) as err:
            await _svc(fake).mint_account_token(_ACCOUNT_ID, _CLIENT_ID, _SECRET)

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert _SECRET not in blob and _SECRET not in err.value.message
