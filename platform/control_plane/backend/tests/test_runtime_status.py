"""Tests for the E28/T5 runtime-status read surface (design D9, contract C2).

Two layers, both fully mocked (no live AWS):

  1. ``AgentIdentityService.runtime_status`` — the boto3 ``get_agent_runtime`` status →
     C2 enum mapping. The control client is a ``MagicMock`` exactly as in
     ``test_agent_identity_service.py``.
  2. ``GET /agents/{agent_id}/runtime`` — the route, on the SAME tenant + VIEWER gate as
     the per-agent metrics/traces routes (``agents.py`` — reads are tenant-wide by design,
     with NO project-role gate). Harness mirrors ``test_observability_routes.py``: a
     minimal FastAPI app with ONLY the agents router, the REAL ``require_role`` +
     ``current_principal`` path against a mocked ``verify_entra_token``, a mocked
     ``AgentRegistryService`` and a seeded tenant-resolver stub.

The load-bearing invariants:
  - every native ``AgentRuntimeStatus`` value maps to a C2 slot;
  - ``ResourceNotFoundException`` → ``not_deployed`` (definitively gone);
  - ANY other ``ClientError`` (incl. the AMBIGUOUS AccessDenied) and any transport error →
    ``unknown``, NEVER ``failed`` — an unreachable control plane is not a broken runtime;
  - no ARN, no AWS account id, no credential ever appears in ``detail``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi import FastAPI
from fastapi.testclient import TestClient

# A realistic runtime ARN. The account-id segment is what must never reach a response
# field other than ``runtime_arn`` itself (which is ``agent.agent_arn``, already public
# on every agent route).
ACCOUNT_SEGMENT = "123456789012"
RUNTIME_RID = "agent_governance_test_agent-wdOmsREOEj"
RUNTIME_ARN = (
    f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT_SEGMENT}:runtime/{RUNTIME_RID}"
)
# An ECR container URI — it CARRIES the account id, so only the image TAG may ever be
# surfaced from it.
CONTAINER_URI = f"{ACCOUNT_SEGMENT}.dkr.ecr.us-east-1.amazonaws.com/agp-agent:sha-abc123"


# ---------------------------------------------------------------------------
# Layer 1 — service: the boto3 → C2 mapping
# ---------------------------------------------------------------------------

def _make_agent(*, agent_arn: str | None = RUNTIME_ARN, agent_id: str = "rec-abc123"):
    from models.agent import Agent, AuthType, LifecycleState, Platform

    now = datetime.now(timezone.utc)
    return Agent(
        id=agent_id,
        name="Claims Triage DE",
        purpose="Triage inbound motor claims",
        lifecycle_state=LifecycleState.APPROVED,
        platform=Platform.AWS_BEDROCK,
        auth_type=AuthType.ENTRA,
        agent_arn=agent_arn,
        tenant_id="ten-1",
        created_at=now,
        updated_at=now,
    )


def _svc(control):
    from services.agent_identity_service import AgentIdentityService

    return AgentIdentityService(
        graph=MagicMock(),
        registry=MagicMock(),
        control_client=control,
        region="us-east-1",
        tenant_id="00000000-0000-0000-0000-000000000001",
        login_base="https://login.microsoftonline.com",
    )


def _client_error(code: str, message: str = "boom"):
    return ClientError(
        {"Error": {"Code": code, "Message": message}}, "GetAgentRuntime"
    )


@pytest.mark.parametrize(
    "native,expected",
    [
        ("READY", "ready"),
        ("CREATING", "creating"),
        ("UPDATING", "updating"),
        ("CREATE_FAILED", "failed"),
        ("UPDATE_FAILED", "failed"),
        ("DELETE_FAILED", "failed"),
        ("FAILED", "failed"),
    ],
)
def test_every_native_status_maps_to_its_c2_slot(native, expected):
    control = MagicMock()
    control.get_agent_runtime.return_value = {"status": native}

    result = _svc(control).runtime_status(_make_agent())

    assert result.status == expected
    # RID-not-ARN: the control call takes the ARN's last "/"-segment.
    assert control.get_agent_runtime.call_args.kwargs == {
        "agentRuntimeId": RUNTIME_RID
    }


def test_native_status_enum_is_fully_covered():
    """Every value botocore declares for ``AgentRuntimeStatus`` has an explicit mapping.

    This is the guard against a silent gap: a new native status added by AgentCore must
    fail HERE (loudly, offline) rather than degrade a live runtime to "unknown" in the UI.
    A SUBSET check, not equality — the map also carries the pre-T5 defensive
    ``DELETE_FAILED``/``FAILED`` that botocore does not declare.
    """
    from botocore.loaders import Loader

    from services.agent_identity_service import _NATIVE_TO_RUNTIME_STATUS

    model = Loader().load_service_model("bedrock-agentcore-control", "service-2")
    native = set(model["shapes"]["AgentRuntimeStatus"]["enum"])

    assert native <= set(_NATIVE_TO_RUNTIME_STATUS), (
        f"unmapped native status(es): {sorted(native - set(_NATIVE_TO_RUNTIME_STATUS))}"
    )


def test_mapping_agrees_with_the_pre_existing_failed_set():
    """The provisioning path's ``_FAILED_STATUSES`` and the T5 map must not disagree about
    what "failed" means — two sets naming the same concept differently is how a status ends
    up fatal to a deploy but green in the UI."""
    from services.agent_identity_service import (
        _FAILED_STATUSES,
        _NATIVE_TO_RUNTIME_STATUS,
        _READY_STATUS,
    )

    for native in _FAILED_STATUSES:
        assert _NATIVE_TO_RUNTIME_STATUS[native] == "failed"
    assert _NATIVE_TO_RUNTIME_STATUS[_READY_STATUS] == "ready"


def test_every_mapped_value_is_in_the_closed_union():
    """No producer may emit a status the C2/C3 union does not contain — the frontend's
    lookup tables have no default branch, so an unlisted value would render nothing."""
    from models.agent import RUNTIME_STATUSES
    from services.agent_identity_service import _NATIVE_TO_RUNTIME_STATUS

    assert set(_NATIVE_TO_RUNTIME_STATUS.values()) <= set(RUNTIME_STATUSES)


def test_deleting_is_unknown_not_failed():
    """``DELETING`` has no slot in the C2 closed union (adding a 7th would break the C3
    frontend pin), so it degrades to ``unknown`` with a truthful hint — never ``failed``,
    which would report an intentional teardown as a fault."""
    control = MagicMock()
    control.get_agent_runtime.return_value = {"status": "DELETING"}

    result = _svc(control).runtime_status(_make_agent())

    assert result.status == "unknown"
    assert result.detail and "deleting" in result.detail.lower()


def test_unrecognized_status_is_unknown():
    control = MagicMock()
    control.get_agent_runtime.return_value = {"status": "SOMETHING_NEW"}

    result = _svc(control).runtime_status(_make_agent())

    assert result.status == "unknown"


def test_missing_status_key_is_unknown():
    control = MagicMock()
    control.get_agent_runtime.return_value = {}

    result = _svc(control).runtime_status(_make_agent())

    assert result.status == "unknown"


def test_resource_not_found_is_not_deployed():
    control = MagicMock()
    control.get_agent_runtime.side_effect = _client_error("ResourceNotFoundException")

    result = _svc(control).runtime_status(_make_agent())

    assert result.status == "not_deployed"


def test_no_agent_arn_is_not_deployed_without_calling_aws():
    control = MagicMock()

    result = _svc(control).runtime_status(_make_agent(agent_arn=None))

    assert result.status == "not_deployed"
    control.get_agent_runtime.assert_not_called()


@pytest.mark.parametrize(
    "code",
    ["AccessDeniedException", "ThrottlingException", "ValidationException",
     "InternalServerException"],
)
def test_other_client_errors_are_unknown_never_failed(code):
    """AccessDenied is AMBIGUOUS — a LIVE runtime behind an IAM/SCP/region misconfig
    returns it too (``runtime_exists``'s three-way rationale). Reporting a probe failure
    as a runtime failure would make a governance product state a wrong conclusion."""
    control = MagicMock()
    control.get_agent_runtime.side_effect = _client_error(code)

    result = _svc(control).runtime_status(_make_agent())

    assert result.status == "unknown"
    assert result.status != "failed"
    assert result.detail


def test_unreachable_control_plane_is_unknown():
    control = MagicMock()
    control.get_agent_runtime.side_effect = EndpointConnectionError(
        endpoint_url="https://bedrock-agentcore-control.us-east-1.amazonaws.com/"
    )

    result = _svc(control).runtime_status(_make_agent())

    assert result.status == "unknown"
    assert result.detail


def test_client_error_detail_leaks_no_arn_account_or_credential():
    """The upstream message is NEVER passed through. A real AgentCore AccessDenied body
    names the assumed-role ARN (account id included) — a hard project rule forbids an
    account id anywhere, so only a conservative error CODE may be surfaced."""
    control = MagicMock()
    control.get_agent_runtime.side_effect = _client_error(
        "AccessDeniedException",
        f"User: arn:aws:sts::{ACCOUNT_SEGMENT}:assumed-role/agp-backend/x is not "
        f"authorized to perform: bedrock-agentcore:GetAgentRuntime on {RUNTIME_ARN} "
        "(session token AQoDYXdzEJr...<REDACTED>)",
    )

    result = _svc(control).runtime_status(_make_agent())

    assert result.status == "unknown"
    detail = result.detail or ""
    assert ACCOUNT_SEGMENT not in detail
    assert "arn:aws" not in detail
    assert "assumed-role" not in detail
    assert "AQoDYXdz" not in detail


def test_hostile_error_code_is_not_echoed():
    """A code is only surfaced when it looks like a code. Anything else falls back to a
    fixed hint, so the upstream can never smuggle a payload through the ``Code`` field."""
    control = MagicMock()
    control.get_agent_runtime.side_effect = _client_error(
        f"arn:aws:sts::{ACCOUNT_SEGMENT}:assumed-role/x denied"
    )

    result = _svc(control).runtime_status(_make_agent())

    assert result.status == "unknown"
    assert ACCOUNT_SEGMENT not in (result.detail or "")
    assert "arn:aws" not in (result.detail or "")


def test_image_tag_is_the_tag_only_never_the_account_bearing_uri():
    control = MagicMock()
    control.get_agent_runtime.return_value = {
        "status": "READY",
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": CONTAINER_URI}},
    }

    result = _svc(control).runtime_status(_make_agent())

    assert result.image_tag == "sha-abc123"
    assert ACCOUNT_SEGMENT not in (result.image_tag or "")


def test_untagged_container_uri_yields_no_image_tag():
    """An untagged URI must yield ``None``, not the registry host (which carries the
    account id)."""
    control = MagicMock()
    control.get_agent_runtime.return_value = {
        "status": "READY",
        "agentRuntimeArtifact": {
            "containerConfiguration": {
                "containerUri": f"{ACCOUNT_SEGMENT}.dkr.ecr.us-east-1.amazonaws.com/agp"
            }
        },
    }

    result = _svc(control).runtime_status(_make_agent())

    assert result.image_tag is None


def test_missing_artifact_yields_no_image_tag():
    control = MagicMock()
    control.get_agent_runtime.return_value = {"status": "READY"}

    result = _svc(control).runtime_status(_make_agent())

    assert result.image_tag is None


def test_shape_carries_agent_id_stage_and_checked_at():
    control = MagicMock()
    control.get_agent_runtime.return_value = {"status": "READY"}

    result = _svc(control).runtime_status(_make_agent())

    assert result.agent_id == "rec-abc123"
    assert result.runtime_arn == RUNTIME_ARN
    # ``stage`` is free-form (D8). This agent is a LEGACY record — one ``agent_arn``, no
    # ``agent_arns`` map — so the probed runtime cannot be attributed to a stage, and reporting
    # one would be a fabrication. E28A/T1 gave a map-bearing record a real stage; see the
    # per-stage block at the end of this file.
    assert result.stage == "unknown"
    # ISO-8601 UTC, parseable.
    datetime.fromisoformat(result.checked_at.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Layer 2 — route: GET /agents/{agent_id}/runtime
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_modules():
    """Drop cached auth/config/route modules so monkeypatched env is honored."""
    import sys

    for mod in [
        "core.rbac",
        "core.security_entra",
        "core.config",
        "api.routes.agents",
        "api.routes.users",
    ]:
        sys.modules.pop(mod, None)
    yield


@pytest.fixture
def entra_settings(monkeypatch):
    monkeypatch.setenv("AUTH_PROVIDER", "entra")
    monkeypatch.setenv("USE_DEV_AUTH", "False")
    monkeypatch.setenv("DEBUG", "False")
    monkeypatch.setenv("ENTRA_TENANT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("ENTRA_AUDIENCE", "api://agp")
    monkeypatch.setenv("ENTRA_ROLE_ADMIN", "Platform.Admin")
    monkeypatch.setenv("ENTRA_ROLE_OPERATOR", "Platform.Operator")
    monkeypatch.setenv("ENTRA_ROLE_VIEWER", "Platform.Viewer")


def _context(*, is_global=False, tenant_ids=()):
    from services.tenant_resolver import TenantContext

    return TenantContext(
        is_global=is_global, tenant_ids=frozenset(tenant_ids), tenants=()
    )


class _FakeResolver:
    def __init__(self, ctx):
        self._ctx = ctx

    async def resolve(self, principal):
        return self._ctx


def _build_client(mock_svc, ctx, mock_identity=None):
    """Minimal app with ONLY the agents router + mocked registry/identity/resolver."""
    import api.routes.agents as agents_module
    import api.routes.users as users_module

    agents_module._svc = mock_svc
    if mock_identity is not None:
        agents_module._identity_svc = mock_identity
    users_module._tenant_resolver = _FakeResolver(ctx)

    app = FastAPI()
    app.include_router(agents_module.router, prefix="/api/v1")
    return TestClient(app), agents_module


def _claims_for(role: str):
    role_app = {
        "viewer": "Platform.Viewer",
        "operator": "Platform.Operator",
        "admin": "Platform.Admin",
    }[role]
    return {
        "oid": f"{role}-oid",
        "preferred_username": f"{role}.user@example.com",
        "roles": [role_app],
    }


def _headers():
    return {"Authorization": "Bearer fake-token"}


def _runtime_status(**overrides):
    """What the probe hands the route. ``RuntimeStatusWithEnv`` — the service's real return type
    since E36/T12 — so the handler's access to the live env is exercised, not stubbed around."""
    from services.agent_identity_service import RuntimeStatusWithEnv

    base = dict(
        agent_id="own",
        stage="unknown",
        status="ready",
        runtime_arn=RUNTIME_ARN,
        image_tag="sha-abc123",
        checked_at="2026-07-31T00:00:00+00:00",
    )
    base.update(overrides)
    return RuntimeStatusWithEnv(**base)


def test_route_returns_runtime_status_for_a_visible_agent(entra_settings):
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(agent_id="own")
    mock_identity = MagicMock()
    mock_identity.runtime_status.return_value = _runtime_status()
    client, _ = _build_client(mock_svc, _context(tenant_ids=["ten-1"]), mock_identity)

    with patch(
        "core.security_entra.verify_entra_token", return_value=_claims_for("viewer")
    ):
        resp = client.get("/api/v1/agents/own/runtime", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["agent_id"] == "own"
    assert body["stage"] == "unknown"
    assert body["image_tag"] == "sha-abc123"
    # The route handed the RESOLVED agent to the service (not a raw id).
    assert mock_identity.runtime_status.call_args[0][0].id == "own"


def test_route_foreign_tenant_404_is_byte_identical_to_missing(entra_settings):
    """Same visibility gate as ``/metrics``: a foreign tenant's agent must look absent,
    and the probe must not run before the gate."""
    mock_svc = MagicMock()
    mock_svc.get.side_effect = lambda agent_id: (
        _make_agent(agent_id="foreign") if agent_id == "foreign" else None
    )
    # A foreign agent: visible tenant set is ten-1, the record's is ten-2.
    foreign = _make_agent(agent_id="foreign")
    foreign.tenant_id = "ten-2"
    mock_svc.get.side_effect = lambda agent_id: (
        foreign if agent_id == "foreign" else None
    )
    mock_identity = MagicMock()
    mock_identity.runtime_status.return_value = _runtime_status()
    client, _ = _build_client(mock_svc, _context(tenant_ids=["ten-1"]), mock_identity)

    with patch(
        "core.security_entra.verify_entra_token", return_value=_claims_for("viewer")
    ):
        foreign_resp = client.get("/api/v1/agents/foreign/runtime", headers=_headers())
        missing_resp = client.get(
            "/api/v1/agents/truly-missing/runtime", headers=_headers()
        )

    assert foreign_resp.status_code == 404
    assert missing_resp.status_code == 404
    assert foreign_resp.json() == missing_resp.json()
    assert foreign_resp.json()["detail"] == "Agent not found"
    mock_identity.runtime_status.assert_not_called()


def test_route_is_viewer_gated_not_operator_gated(entra_settings):
    """VIEWER suffices — this is the SAME kind of read as ``/metrics``, and inventing a
    stricter gate here would make one read surface disagree with its sibling."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(agent_id="own")
    mock_identity = MagicMock()
    mock_identity.runtime_status.return_value = _runtime_status()
    client, _ = _build_client(mock_svc, _context(tenant_ids=["ten-1"]), mock_identity)

    with patch(
        "core.security_entra.verify_entra_token", return_value=_claims_for("viewer")
    ):
        resp = client.get("/api/v1/agents/own/runtime", headers=_headers())

    assert resp.status_code == 200


def test_route_requires_a_validated_token(entra_settings):
    """No token → 401, and the probe never runs.

    Note this route has no 403 case, by design and NOT by omission: ``_role_from_entra_claims``
    defaults an unmatched ``roles`` claim to least-privilege VIEWER (``rbac.py``), so any
    validated caller clears a VIEWER gate. The refusal boundary for a tenant-wide read is
    therefore authentication + the visibility 404 — exactly as it is for ``/metrics``."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(agent_id="own")
    mock_identity = MagicMock()
    client, _ = _build_client(mock_svc, _context(tenant_ids=["ten-1"]), mock_identity)

    resp = client.get("/api/v1/agents/own/runtime")

    assert resp.status_code == 401
    mock_identity.runtime_status.assert_not_called()


def test_route_never_500s_when_the_probe_degrades(entra_settings):
    """The service degrades rather than raising, so the route is a plain pass-through: an
    unreachable control plane surfaces as a 200 ``unknown``, not a 5xx."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(agent_id="own")
    mock_identity = MagicMock()
    mock_identity.runtime_status.return_value = _runtime_status(
        status="unknown", detail="control plane unreachable", image_tag=None
    )
    client, _ = _build_client(mock_svc, _context(tenant_ids=["ten-1"]), mock_identity)

    with patch(
        "core.security_entra.verify_entra_token", return_value=_claims_for("viewer")
    ):
        resp = client.get("/api/v1/agents/own/runtime", headers=_headers())

    assert resp.status_code == 200
    assert resp.json()["status"] == "unknown"


def test_route_response_body_contains_no_account_id_outside_the_arn(entra_settings):
    """``runtime_arn`` is ``agent.agent_arn`` — already public on every agent route — but
    no OTHER field may carry the account id."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(agent_id="own")
    mock_identity = MagicMock()
    mock_identity.runtime_status.return_value = _runtime_status(
        status="unknown", detail="probe failed: AccessDeniedException"
    )
    client, _ = _build_client(mock_svc, _context(tenant_ids=["ten-1"]), mock_identity)

    with patch(
        "core.security_entra.verify_entra_token", return_value=_claims_for("viewer")
    ):
        resp = client.get("/api/v1/agents/own/runtime", headers=_headers())

    body = resp.json()
    for key, value in body.items():
        if key == "runtime_arn" or not isinstance(value, str):
            continue
        assert ACCOUNT_SEGMENT not in value
        assert "arn:aws" not in value


# ---------------------------------------------------------------------------
# E28A/T1 — `stage` reports a REAL stage once the map can attribute the runtime
# ---------------------------------------------------------------------------
# This is the function the pre-E28A comment predicted would change ("when a per-stage ARN
# lands, this is the one function that changes"). It hardcoded `stage="unknown"` because the
# record named ONE runtime and could not say which stage owned it.
#
# FRONTEND CONTRACT (do not break): `runtimeScope` in the ops repo-detail tabs returns
# `kind: 'stage'` the moment a probe names a real stage, which is what stops the environment
# strip saying "not attributable per stage". So a stage name here is EVIDENCE, and a guessed
# one would manufacture per-stage evidence out of an agent-level fact — the exact error that
# module refuses. A legacy record must therefore still answer `unknown`.

DEV_RID = "agent_governance_test_agent_dev-aaaaaaaaaa"
PROD_RID = "agent_governance_test_agent_prod-bbbbbbbbbb"
DEV_ARN = f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT_SEGMENT}:runtime/{DEV_RID}"
PROD_ARN = f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT_SEGMENT}:runtime/{PROD_RID}"


def _mapped_agent(**overrides):
    agent = _make_agent(agent_arn=overrides.pop("agent_arn", PROD_ARN))
    agent.agent_arns = overrides.pop("agent_arns", {"dev": DEV_ARN, "prod": PROD_ARN})
    return agent


def test_stage_names_the_real_stage_when_the_map_attributes_the_probed_runtime():
    """The scalar mirrors whichever stage deployed last (C-A2), so the map key naming it IS
    the probed runtime's stage — a fact, not a guess."""
    control = MagicMock()
    control.get_agent_runtime.return_value = {"status": "READY"}

    result = _svc(control).runtime_status(_mapped_agent())

    assert result.stage == "prod"
    assert result.runtime_arn == PROD_ARN
    # The SAME runtime is probed as before — this changes attribution, not which ARN is read.
    assert control.get_agent_runtime.call_args.kwargs["agentRuntimeId"] == PROD_RID


def test_an_explicit_stage_probes_THAT_stages_runtime():
    """With a map the caller can ask for a specific stage — the read the `/runtime` route's
    optional `?stage=` exposes."""
    control = MagicMock()
    control.get_agent_runtime.return_value = {"status": "READY"}

    result = _svc(control).runtime_status(_mapped_agent(), stage="dev")

    assert result.stage == "dev"
    assert result.runtime_arn == DEV_ARN
    assert control.get_agent_runtime.call_args.kwargs["agentRuntimeId"] == DEV_RID


def test_an_unknown_stage_is_not_deployed_rather_than_a_silent_fallback():
    """A stage the agent has no runtime for is `not_deployed` WITHOUT an AWS call. Falling back
    to another stage's runtime would answer a question the caller did not ask, and the caller
    would read it as being about the stage they named."""
    control = MagicMock()

    result = _svc(control).runtime_status(_mapped_agent(), stage="uat")

    assert result.status == "not_deployed"
    assert result.stage == "uat"
    assert result.runtime_arn is None
    control.get_agent_runtime.assert_not_called()


def test_a_legacy_record_still_answers_unknown():
    """The load-bearing backward-compatibility case. A scalar-only record genuinely cannot
    attribute its runtime, so it must keep saying so — the frontend treats any other value as
    per-stage evidence."""
    control = MagicMock()
    control.get_agent_runtime.return_value = {"status": "READY"}

    result = _svc(control).runtime_status(_make_agent())

    assert result.stage == "unknown"
    assert result.runtime_arn == RUNTIME_ARN


def test_a_legacy_record_asked_for_a_stage_does_not_pretend_to_have_one():
    """An explicit `?stage=dev` against a record with no map must NOT return the scalar's
    runtime captioned `dev` — that is the fabrication `runtimeScope` exists to prevent. The
    record cannot attribute its runtime, so no stage can be confirmed."""
    control = MagicMock()

    result = _svc(control).runtime_status(_make_agent(), stage="dev")

    assert result.status == "not_deployed"
    control.get_agent_runtime.assert_not_called()


def test_stage_stays_reported_when_the_probe_degrades_to_unknown_status():
    """`status` and `stage` are independent facts. An unreachable control plane means the
    STATUS is unknown; WHICH runtime was probed is still known from the record."""
    control = MagicMock()
    control.get_agent_runtime.side_effect = _client_error("AccessDeniedException")

    result = _svc(control).runtime_status(_mapped_agent(), stage="dev")

    assert result.status == "unknown"
    assert result.stage == "dev"


def test_no_runtime_at_all_is_not_deployed_with_the_unknown_stage():
    """Nothing provisioned (E20 pre-registration): no map, no scalar, no AWS call."""
    control = MagicMock()

    result = _svc(control).runtime_status(_make_agent(agent_arn=None))

    assert result.status == "not_deployed"
    assert result.stage == "unknown"
    control.get_agent_runtime.assert_not_called()


def test_route_forwards_an_explicit_stage_to_the_probe(entra_settings):
    """E28A/T1: `?stage=` is OPTIONAL and ADDITIVE. When given, the route hands it to the
    probe so the reading describes THAT stage's runtime."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(agent_id="own")
    mock_identity = MagicMock()
    mock_identity.runtime_status.return_value = _runtime_status(stage="prod")
    client, _ = _build_client(mock_svc, _context(tenant_ids=["ten-1"]), mock_identity)

    with patch(
        "core.security_entra.verify_entra_token", return_value=_claims_for("viewer")
    ):
        resp = client.get("/api/v1/agents/own/runtime?stage=prod", headers=_headers())

    assert resp.status_code == 200
    assert resp.json()["stage"] == "prod"
    # Positionally after the agent — the same shape the service signature takes.
    assert mock_identity.runtime_status.call_args[0][1] == "prod"


def test_route_omitting_the_stage_passes_None_not_a_guess(entra_settings):
    """The default must stay the pre-E28A behaviour: no stage named ⇒ probe the scalar's
    runtime. Defaulting to "dev" here would silently report on the wrong runtime."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(agent_id="own")
    mock_identity = MagicMock()
    mock_identity.runtime_status.return_value = _runtime_status()
    client, _ = _build_client(mock_svc, _context(tenant_ids=["ten-1"]), mock_identity)

    with patch(
        "core.security_entra.verify_entra_token", return_value=_claims_for("viewer")
    ):
        resp = client.get("/api/v1/agents/own/runtime", headers=_headers())

    assert resp.status_code == 200
    assert mock_identity.runtime_status.call_args[0][1] is None


# ---------------------------------------------------------------------------
# E36/T12 — reconcile-on-read: the probe's live env feeds the MCP heal
# ---------------------------------------------------------------------------
# A runtime replacement wipes the backend-injected `MCP_SERVERS`, and the platform gets no
# signal. This read already fetches the live `environmentVariables` and used to discard them, so
# the detection is free — the probe now CARRIES the env to the handler, which hands it to
# `reconcile_runtime_mcp_env`. A write on a GET is an accepted, explicit spec decision.
#
# SECURITY: the env is an INTERNAL hand-off. It is never serialized — `response_model`
# is still `RuntimeStatus`, which has no such field, and this route is VIEWER-gated.


def _granted_agent(agent_id: str = "own"):
    agent = _make_agent(agent_id=agent_id)
    agent.mcp_server_ids = ["CZR2"]
    return agent


def test_route_hands_the_live_env_to_the_reconciler(entra_settings):
    """The hook point: whatever the probe read is what the heal decides on. Passing the live env
    is what keeps the detection free — re-reading it here would double the AWS calls on a read
    route to learn a fact we already hold."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _granted_agent()
    mock_identity = MagicMock()
    live_env = {"CREDENTIAL_PROVIDER_NAME": "agp-agent-obo-own"}
    mock_identity.runtime_status.return_value = _runtime_status(environment_variables=live_env)
    client, agents_module = _build_client(
        mock_svc, _context(tenant_ids=["ten-1"]), mock_identity
    )

    with patch(
        "core.security_entra.verify_entra_token", return_value=_claims_for("viewer")
    ), patch.object(
        agents_module, "reconcile_runtime_mcp_env", new_callable=AsyncMock
    ) as reconcile:
        resp = client.get("/api/v1/agents/own/runtime", headers=_headers())

    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"
    called_agent, called_env = reconcile.call_args[0]
    assert called_agent.id == "own"  # the RESOLVED agent, as the probe got
    assert called_env == live_env


def test_route_never_serializes_the_live_env(entra_settings):
    """THE security constraint. A runtime env can carry an audience, an ARN, a provider name —
    and this route is VIEWER-gated, the loosest gate in the product. The env exists on the
    carrier ONLY for the internal hand-off: `response_model=RuntimeStatus` has no such field, so
    FastAPI drops it, and no value may appear anywhere in the body."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _granted_agent()
    mock_identity = MagicMock()
    mock_identity.runtime_status.return_value = _runtime_status(
        environment_variables={
            "MCP_SERVERS": '[{"id":"CZR2","audience":"api://agp-mcp-CZR2"}]',
            "SOME_TOKEN": "super-secret-value",
        }
    )
    client, agents_module = _build_client(
        mock_svc, _context(tenant_ids=["ten-1"]), mock_identity
    )

    with patch(
        "core.security_entra.verify_entra_token", return_value=_claims_for("viewer")
    ), patch.object(agents_module, "reconcile_runtime_mcp_env", new_callable=AsyncMock):
        resp = client.get("/api/v1/agents/own/runtime", headers=_headers())

    assert resp.status_code == 200
    assert "environment_variables" not in resp.json()
    assert "environmentVariables" not in resp.text
    assert "super-secret-value" not in resp.text
    assert "api://agp-mcp-CZR2" not in resp.text


def test_route_still_200s_when_the_heal_fails(entra_settings):
    """A failed heal must never degrade the read. The REAL reconciler runs here (only the
    rebuild is stubbed to raise, as an unreachable or cross-account runtime does), so this pins
    the end-to-end swallow: the status is still reported, and the caller is not told about a
    background best-effort that did not land."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _granted_agent()
    mock_identity = MagicMock()
    mock_identity.runtime_status.return_value = _runtime_status(environment_variables={"OTHER": "x"})
    client, _ = _build_client(mock_svc, _context(tenant_ids=["ten-1"]), mock_identity)

    with patch(
        "core.security_entra.verify_entra_token", return_value=_claims_for("viewer")
    ), patch(
        "services.agent_mcp_env.rebuild_runtime_mcp_env",
        new_callable=AsyncMock,
        side_effect=RuntimeError("AccessDenied"),
    ) as rebuild:
        resp = client.get("/api/v1/agents/own/runtime", headers=_headers())

    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"
    rebuild.assert_awaited_once()


def test_route_does_not_reconcile_an_agent_without_grants(entra_settings):
    """The self-limiting half of the trigger, at the route level: no grants ⇒ the read stays a
    pure read. This is what keeps write-on-a-GET confined to the wiped-runtime case."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _make_agent(agent_id="own")  # no mcp_server_ids
    mock_identity = MagicMock()
    mock_identity.runtime_status.return_value = _runtime_status(environment_variables=None)
    client, _ = _build_client(mock_svc, _context(tenant_ids=["ten-1"]), mock_identity)

    with patch(
        "core.security_entra.verify_entra_token", return_value=_claims_for("viewer")
    ), patch(
        "services.agent_mcp_env.rebuild_runtime_mcp_env", new_callable=AsyncMock
    ) as rebuild:
        resp = client.get("/api/v1/agents/own/runtime", headers=_headers())

    assert resp.status_code == 200
    rebuild.assert_not_awaited()


def test_route_does_not_reconcile_off_a_degraded_read(entra_settings):
    """End to end: a grant-carrying agent whose probe FAILED (throttled, denied, unreachable, or
    simply gone) hands `None` to the reconciler, and no write happens. Without this the busiest
    failure modes turned every read into a doomed write — and the fleet page reads 1+N times per
    load, so it never backed off."""
    mock_svc = MagicMock()
    mock_svc.get.return_value = _granted_agent()
    mock_identity = MagicMock()
    mock_identity.runtime_status.return_value = _runtime_status(
        status="unknown", detail="the control plane could not be reached",
        environment_variables=None,
    )
    client, _ = _build_client(mock_svc, _context(tenant_ids=["ten-1"]), mock_identity)

    with patch(
        "core.security_entra.verify_entra_token", return_value=_claims_for("viewer")
    ), patch(
        "services.agent_mcp_env.rebuild_runtime_mcp_env", new_callable=AsyncMock
    ) as rebuild:
        resp = client.get("/api/v1/agents/own/runtime", headers=_headers())

    assert resp.status_code == 200
    assert resp.json()["status"] == "unknown"  # the read is still reported, unchanged
    rebuild.assert_not_awaited()


def test_probe_carries_the_live_env_it_already_read():
    """Service layer: `get_agent_runtime` returns `environmentVariables`, and this read used to
    throw them away. The carrier is what makes reconcile-on-read free of a second AWS call."""
    control = MagicMock()
    control.get_agent_runtime.return_value = {
        "status": "READY",
        "environmentVariables": {"MCP_SERVERS": "[]", "FOO": "bar"},
    }

    result = _svc(control).runtime_status(_make_agent())

    assert result.status == "ready"  # the C2 contract is untouched
    assert result.environment_variables == {"MCP_SERVERS": "[]", "FOO": "bar"}


def test_probe_env_is_none_when_the_read_never_reached_the_runtime():
    """A not-deployed read holds NO evidence about the runtime's env. `None` is that absence, and it
    must NOT be confused with "the runtime has an empty env": the reconciler heals on the second
    and not on the first, because only a read that reached the runtime can witness a wipe."""
    control = MagicMock()

    result = _svc(control).runtime_status(_make_agent(agent_arn=None))

    assert result.status == "not_deployed"
    assert result.environment_variables is None
    control.get_agent_runtime.assert_not_called()


def test_probe_env_is_an_empty_dict_when_the_runtime_carries_no_env():
    """The wipe's own signature: the read SUCCEEDED and the runtime has no `environmentVariables`
    at all. That is evidence — a dict — so the reconciler can act on it. Reporting it as `None`
    (the pre-fix behaviour) made it indistinguishable from a failed read."""
    control = MagicMock()
    control.get_agent_runtime.return_value = {"status": "READY"}

    result = _svc(control).runtime_status(_make_agent())

    assert result.status == "ready"
    assert result.environment_variables == {}


@pytest.mark.parametrize(
    "code", ["ThrottlingException", "AccessDeniedException", "ResourceNotFoundException"]
)
def test_probe_env_is_none_on_every_degraded_read(code):
    """Throttling, the AMBIGUOUS AccessDenied (which a cross-account runtime returns) and a
    genuine not-found all answer "no evidence about the env". Healing off these never converged:
    a throttle got answered with an EXTRA write to the throttled API, and an unreachable runtime
    got the same doomed write on every single read."""
    control = MagicMock()
    control.get_agent_runtime.side_effect = _client_error(code)

    result = _svc(control).runtime_status(_make_agent())

    assert result.status in ("unknown", "not_deployed")
    assert result.environment_variables is None


def test_probe_env_is_none_when_the_control_plane_is_unreachable():
    """The transport half of the same rule (`BotoCoreError`) — no answer, so no evidence."""
    control = MagicMock()
    control.get_agent_runtime.side_effect = EndpointConnectionError(
        endpoint_url="https://bedrock-agentcore-control.us-east-1.amazonaws.com/"
    )

    result = _svc(control).runtime_status(_make_agent())

    assert result.status == "unknown"
    assert result.environment_variables is None
