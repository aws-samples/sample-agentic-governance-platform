"""The grant mirror, tested where it lives.

The mirror's BEHAVIOR is already pinned end-to-end through the routes (``test_grants_routes.py``).
What this file pins is the property that made the extraction worth doing: the drift READ and
the re-assert WRITE agree about the baseline because they share ONE function, not two copies
of a rule. A second copy is invisible to route tests as long as both spell the rule the same
way — and becomes a permanent self-drift the day one of them is edited.

Offline: the service's collaborators are the app-ACL client and the Entra side, and both are
substituted directly on the instance (the module's own fake-and-stub idiom; no new libraries).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from services import grant_mirror_service as gms
from services.grant_mirror_service import (
    AclTarget,
    AssignedUsers,
    GrantMirrorService,
)

if TYPE_CHECKING:
    from services.databricks_workspace_service import DatabricksWorkspaceService


def _run(coro):
    return asyncio.run(coro)


def _acl(principal, kind="user", level="CAN_USE", inherited=False):
    return {"principal": principal, "kind": kind, "level": level, "inherited": inherited}


class _FakeAclClient:
    """The app-permissions half of ``DatabricksWorkspaceService`` — read + replacing PUT."""

    def __init__(self, entries):
        self.entries = list(entries)
        self.asserted = None

    async def get_app_permissions(self, workspace_url, token, app_name):
        return list(self.entries)

    async def set_app_permissions(self, workspace_url, token, app_name, desired):
        self.asserted = list(desired)


_AGENT = SimpleNamespace(
    id="rec-db-1",
    tenant_id="ten-1",
    entra_sp_id="sp-1",
    identity_status="provisioned",
    is_databricks_governed=True,
)


def _acl_target(databricks: object, stage: object, *, token: str = "", app_name: str = "") -> AclTarget:
    return AclTarget(
        databricks=cast("DatabricksWorkspaceService", databricks), stage=stage, token=token, app_name=app_name
    )


def _service(acl_entries, *, assigned=None):
    """A ``GrantMirrorService`` whose app target + Entra side are stubbed on the instance."""
    client = _FakeAclClient(acl_entries)
    target = _acl_target(
        client,
        SimpleNamespace(workspace_url="https://dbc.example.com", sp_client_id="sp-agp"),
        token="tok",
        app_name="agp-app",
    )
    svc = GrantMirrorService()

    async def _target(agent):
        return target

    async def _assigned(agent):
        return assigned or AssignedUsers(names={}, unresolved=[])

    svc.acl_target = _target
    svc.assigned_user_names = _assigned
    return svc, client


def test_the_drift_read_and_the_reassert_share_one_baseline_function(monkeypatch):
    """Patching the ONE baseline predicate must move BOTH answers.

    A rogue entry declared "owned" disappears from the drift read AND from the fresh drift the
    re-assert answers with. If either side carried its own copy of the rule, that side would
    still report the entry — which is exactly the shape of a repair that re-creates the drift
    it just fixed.
    """
    rogue = _acl("rogue@example.com", "user", "CAN_MANAGE")
    baseline = {
        ("admins", "group", "CAN_MANAGE"),
        ("sp-agp", "service_principal", "CAN_MANAGE"),
        ("rogue@example.com", "user", "CAN_MANAGE"),
    }
    calls = []

    def _spy(target):
        calls.append(target.app_name)
        return set(baseline)

    monkeypatch.setattr(gms, "_baseline_principals", _spy)

    svc, _ = _service([_acl("admins", "group", "CAN_MANAGE"), rogue])
    assert _run(svc.drift(_AGENT)).entries == []

    svc, _ = _service([_acl("admins", "group", "CAN_MANAGE"), rogue])
    assert _run(svc.reassert(_AGENT)).entries == []

    assert calls == ["agp-app", "agp-app"]


def test_without_the_baseline_the_same_two_paths_report_the_same_drift():
    """The control for the test above: unpatched, the rogue entry is drift on BOTH sides.

    The re-assert's answer is a FRESH read of the list it just replaced, so its own PUT is what
    clears the entry — the two paths agree before and after the repair, which is only true while
    the composer writes exactly what the baseline owns.
    """
    rogue = _acl("rogue@example.com", "user", "CAN_MANAGE")
    svc, _ = _service([_acl("admins", "group", "CAN_MANAGE"), rogue])
    assert [(e.principal, e.direction) for e in _run(svc.drift(_AGENT)).entries] == [
        ("rogue@example.com", "unauthorized_acl")
    ]

    svc, client = _service([_acl("admins", "group", "CAN_MANAGE"), rogue])
    _run(svc.reassert(_AGENT))
    assert client.asserted is not None
    assert all(e["principal"] != "rogue@example.com" for e in client.asserted)


def test_the_composed_list_is_exactly_what_the_baseline_owns():
    """The composer writes the baseline (admins + the workspace SP) plus one CAN_USE per named
    grantee — and nothing the baseline predicate would call drift."""
    svc, client = _service(
        [_acl("admins", "group", "CAN_MANAGE")],
        assigned=AssignedUsers(
            names={"lars.svensson@example.com": "lars.svensson@example.com"}, unresolved=[]
        ),
    )
    fresh = _run(svc.reassert(_AGENT))

    assert client.asserted == [
        {"principal": "admins", "kind": "group", "level": "CAN_MANAGE"},
        {"principal": "sp-agp", "kind": "service_principal", "level": "CAN_MANAGE"},
        {
            "principal": "lars.svensson@example.com",
            "kind": "user",
            "level": "CAN_USE",
        },
    ]
    baseline = gms._baseline_principals(_acl_target(None, SimpleNamespace(sp_client_id="sp-agp")))
    assert client.asserted is not None
    written = {(e["principal"].casefold(), e["kind"], e["level"]) for e in client.asserted}
    assert written - baseline == {
        ("lars.svensson@example.com", "user", "CAN_USE"),
    }
    assert isinstance(fresh.entries, list)


@pytest.mark.parametrize(
    "method,args",
    [
        ("drift", ()),
        ("reassert", ()),
        ("mirror_grant", ({"id": "a1", "principalType": "User", "principalId": "u1"},)),
        ("revoke_mirror_target", ("a1",)),
        ("mirror_revoke", ("lars.svensson@example.com",)),
    ],
)
def test_every_public_method_gates_itself_with_no_route_involved(method, args):
    """The gate is an INVARIANT of the boundary, not the caller's obligation (review I-3).

    The mirror is a module-level singleton importable from anywhere, so a future direct caller
    (the marketplace-integration epic) must not be able to reach a write by simply not knowing
    about ``gate()``. Each public method refuses the 409 itself — including ``mirror_grant``,
    where the check sits OUTSIDE the inner try/except so the 409 cannot be relabeled as a failed
    ACL write and roll a fresh assignment back.
    """
    svc, client = _service([])
    agentcore = SimpleNamespace(
        id="rec-aws-1",
        tenant_id="ten-1",
        entra_sp_id="sp-1",
        identity_status="provisioned",
        is_databricks_governed=False,
    )

    with pytest.raises(gms.GrantMirrorError) as ei:
        _run(getattr(svc, method)(agentcore, *args))

    assert ei.value.status_code == 409
    assert str(ei.value) == "not a Databricks-governed agent"
    assert client.asserted is None


def test_the_gate_refuses_a_non_databricks_agent_and_an_unprovisioned_one():
    """Both 409s the drift + re-assert routes share, as domain errors (the route maps status)."""
    svc = GrantMirrorService()

    with pytest.raises(gms.GrantMirrorError) as ei:
        svc.gate(
            SimpleNamespace(
                runtime_handle=None,
                auth_type=None,
                platform=None,
                identity_status="provisioned",
                entra_sp_id="sp-1",
                is_databricks_governed=False,
            )
        )
    assert ei.value.status_code == 409

    with pytest.raises(gms.GrantMirrorError) as ei:
        svc.gate(SimpleNamespace(is_databricks_governed=True, identity_status="none", entra_sp_id=None))
    assert ei.value.status_code == 409


def test_the_drift_read_reports_each_kind_the_acl_parser_yields_with_todays_bytes():
    svc, _ = _service(
        [
            _acl("rogue@example.com", "user", "CAN_MANAGE"),
            _acl("analysts", "group", "CAN_USE"),
            _acl("sp-rogue", "service_principal", "CAN_USE"),
        ]
    )

    assert [e.model_dump(mode="json") for e in _run(svc.drift(_AGENT)).entries] == [
        {
            "principal": "rogue@example.com",
            "kind": "user",
            "level": "CAN_MANAGE",
            "direction": "unauthorized_acl",
        },
        {"principal": "analysts", "kind": "group", "level": "CAN_USE", "direction": "unauthorized_acl"},
        {
            "principal": "sp-rogue",
            "kind": "service_principal",
            "level": "CAN_USE",
            "direction": "unauthorized_acl",
        },
    ]
