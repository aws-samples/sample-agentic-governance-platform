"""``graph_service_from_settings`` under and without ``GRAPH_IN_MEMORY``, and the backend over the
in-memory directory: a harness agent provisioned through ``AgentIdentityService`` beside the in-memory
registry with a control client that records no call, a refused application create persisting
``failed``, and one dev-auth run over the agents, grants, entra, tenants and users-admin routers from
register through grant and revoke to delete, leaving nothing of the agent in the directory.
"""

from __future__ import annotations

import ast
import asyncio
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import aws_clients, registry_memory
from core.config import Settings
from models.agent import AgentAuthType, AgentCreate, IdentityStatus, Platform
from services import graph_memory, graph_service
from services.agent_identity_service import AgentIdentityService, ProvisioningError
from services.agent_registry_service import AgentRegistryService
from services.graph_memory import (
    LOCAL_BACKEND_APP_ID,
    LOCAL_CLIENT_CREDENTIAL,
    LOCAL_GROUP_ID,
    LOCAL_PRINCIPAL,
    LOCAL_TENANT_ID,
    MemoryDirectory,
    MemoryGraphTransport,
)
from services.graph_service import GraphService, graph_service_from_settings

REGION = "us-east-1"
AGENTS = "agp-agents"
RUN = "e2e-graph-routes"
HARNESS_ARN = f"arn:aws:bedrock-agentcore:us-east-1:111122223333:harness/{RUN}"
OTHER_TENANT_ID = "99999999-0000-0000-0000-000000000000"
OTHER_BACKEND_CLIENT_ID = "99999999-0000-0000-0000-000000000001"
ENTRA_IDENTITY = (
    "ENTRA_TENANT_ID",
    "ENTRA_BACKEND_CLIENT_ID",
    "ENTRA_SPA_CLIENT_ID",
    "ENTRA_PLATFORM_APP_CLIENT_ID",
)
DEV_ADMIN = {"x-user-email": "dev@example.com"}
REPO_ROOT = (
    Path(os.environ["AGP_REPO_ROOT"])
    if "AGP_REPO_ROOT" in os.environ
    else Path(__file__).resolve().parents[4]
)
SRC = REPO_ROOT / "platform/control_plane/backend/src"
FACTORY = ("platform/control_plane/backend/src/services/graph_service.py", "graph_service_from_settings")
BUILD_THROUGH_THE_FACTORY = (
    "build the adapter through services.graph_service.graph_service_from_settings(), the one constructor "
    "that reads GRAPH_IN_MEMORY; a GraphService(...) anywhere else reaches Entra from a laptop"
)


@pytest.fixture
def in_memory_graph(monkeypatch):
    monkeypatch.setattr(graph_service.settings, "GRAPH_IN_MEMORY", True)
    monkeypatch.setattr(graph_memory, "_DIRECTORY", None)


@pytest.fixture
def both_doubles(in_memory_graph, monkeypatch):
    monkeypatch.setattr(aws_clients.settings, "REGISTRY_IN_MEMORY", True)
    monkeypatch.setattr(aws_clients.settings, "AGENT_REGISTRY_NAME", AGENTS)
    monkeypatch.setattr(registry_memory, "_INSTANCE", None)


def _directory() -> MemoryDirectory:
    directory = graph_memory._DIRECTORY
    assert directory is not None
    return directory


def _traces_of(directory: MemoryDirectory, *, app_id: str, sp_id: str, audience: str) -> list[dict[str, Any]]:
    return [
        *(a for a in directory.applications.values() if audience in a.get("identifierUris", [])),
        *(p for p in directory.service_principals.values() if p["appId"] == app_id),
        *(a for a in directory.assignments.values() if sp_id in (a["principalId"], a["resourceId"])),
        *(g for g in directory.grants.values() if sp_id in (g["clientId"], g["resourceId"])),
    ]


def _assert_over_the_double(svc: GraphService) -> None:
    assert isinstance(svc._injected_client, httpx.AsyncClient)
    assert isinstance(svc._injected_client._transport, MemoryGraphTransport)
    assert svc._tenant_id == LOCAL_TENANT_ID
    assert svc._backend_client_id == LOCAL_BACKEND_APP_ID
    assert svc._client_secret_loader() == LOCAL_CLIENT_CREDENTIAL


def _harness_agent(registry: AgentRegistryService, name: str):
    return registry.create(
        AgentCreate(
            name=name,
            purpose="prove the directory",
            tenant_id="default",
            platform=Platform.AWS_BEDROCK,
            auth_type=AgentAuthType.ENTRA,
            agent_arn=HARNESS_ARN,
        ),
        created_by="tester",
    )


def _identity_service(registry: AgentRegistryService, control: MagicMock) -> AgentIdentityService:
    return AgentIdentityService(
        graph=graph_service_from_settings(),
        registry=registry,
        control_client=control,
        tenant_id=LOCAL_TENANT_ID,
        login_base=graph_service.settings.ENTRA_LOGIN_BASE,
    )


def test_the_factory_under_the_switch_builds_the_adapter_over_the_double_and_two_calls_share_one_directory(
    in_memory_graph,
):
    first = graph_service_from_settings()
    second = graph_service_from_settings()
    assert second is not first
    _assert_over_the_double(first)
    _assert_over_the_double(second)

    created = asyncio.run(first.create_agent_app("factory-agent", "factory agent"))
    object_id = asyncio.run(second.get_application_object_id(created["app_id"]))
    assert _directory().applications[object_id]["appId"] == created["app_id"]


def test_the_factory_under_the_switch_ignores_the_configured_entra_values(in_memory_graph, monkeypatch):
    monkeypatch.setattr(graph_service.settings, "ENTRA_TENANT_ID", OTHER_TENANT_ID)
    monkeypatch.setattr(graph_service.settings, "ENTRA_BACKEND_CLIENT_ID", OTHER_BACKEND_CLIENT_ID)

    svc = graph_service_from_settings()

    _assert_over_the_double(svc)
    assert svc._token_endpoint.endswith(f"/{LOCAL_TENANT_ID}/oauth2/v2.0/token")
    hits = asyncio.run(svc.search_principals("alex"))
    assert [hit["displayName"] for hit in hits] == [LOCAL_PRINCIPAL]


def test_the_factory_without_the_switch_builds_today_s_adapter_from_settings(monkeypatch):
    settings = graph_service.settings
    monkeypatch.setattr(settings, "GRAPH_IN_MEMORY", False)
    monkeypatch.setattr(settings, "ENTRA_TENANT_ID", OTHER_TENANT_ID)
    monkeypatch.setattr(settings, "ENTRA_BACKEND_CLIENT_ID", OTHER_BACKEND_CLIENT_ID)
    monkeypatch.setattr(graph_memory, "_DIRECTORY", None)

    svc = graph_service_from_settings()

    assert svc._injected_client is None
    assert svc._tenant_id == settings.ENTRA_TENANT_ID
    assert svc._backend_client_id == settings.ENTRA_BACKEND_CLIENT_ID
    assert svc._login_base == settings.ENTRA_LOGIN_BASE.rstrip("/")
    assert svc._graph_base == settings.GRAPH_API_BASE.rstrip("/")
    assert svc._audience_prefix == settings.AGENT_APP_AUDIENCE_PREFIX
    assert svc._client_secret_loader is graph_service.load_entra_backend_client_secret
    assert len(svc._clients_by_loop) == 0
    assert graph_memory._DIRECTORY is None


def test_a_harness_agent_provisions_to_provisioned_over_both_doubles_with_no_control_call(both_doubles):
    registry = AgentRegistryService(registry_name=AGENTS, region=REGION)
    control = MagicMock()
    identity = _identity_service(registry, control)
    agent = _harness_agent(registry, "harness-agent")
    assert agent.identity_status == IdentityStatus.PENDING

    asyncio.run(identity.provision(agent))

    record = registry.get(agent.id)
    audience = f"api://agp-agent-{agent.id}"
    assert record is not None
    assert record.identity_status == IdentityStatus.PROVISIONED
    assert record.entra_app_audience == audience
    assert record.entra_app_id and record.entra_sp_id
    principal = _directory().service_principals[record.entra_sp_id]
    assert principal["appId"] == record.entra_app_id
    assert principal["appRoleAssignmentRequired"] is True
    assert {role["id"] for role in principal["appRoles"]} == {record.invoker_role_id, record.admin_role_id}
    assert control.method_calls == []

    asyncio.run(identity.delete_identity(record, include_obo_provider=False))

    traces = _traces_of(_directory(), app_id=record.entra_app_id, sp_id=record.entra_sp_id, audience=audience)
    assert traces == []
    assert control.method_calls == []


def test_a_refused_application_create_persists_failed_and_raises(both_doubles, monkeypatch):
    def refuse(_transport, _call):
        raise graph_memory._Refusal(
            403, "Authorization_RequestDenied", "Insufficient privileges to complete the operation."
        )

    monkeypatch.setitem(graph_memory._ROUTES, ("POST", ("applications",)), refuse)
    registry = AgentRegistryService(registry_name=AGENTS, region=REGION)
    control = MagicMock()
    identity = _identity_service(registry, control)
    agent = _harness_agent(registry, "refused-agent")

    with pytest.raises(ProvisioningError):
        asyncio.run(identity.provision(agent))

    record = registry.get(agent.id)
    assert record is not None
    assert record.identity_status == IdentityStatus.FAILED
    assert record.entra_sp_id is None
    audience = f"api://agp-agent-{agent.id}"
    assert not [a for a in _directory().applications.values() if audience in a.get("identifierUris", [])]
    assert control.method_calls == []


@pytest.mark.usefixtures("reset_modules")
def test_the_grant_path_runs_over_the_double_from_register_to_delete(monkeypatch):
    for name in ("USE_DEV_AUTH", "REGISTRY_IN_MEMORY", "GRAPH_IN_MEMORY"):
        monkeypatch.setenv(name, "true")
    for name in (
        *(f for f in Settings.model_fields if f.endswith("_TABLE_NAME")),
        *ENTRA_IDENTITY,
        "LANGFUSE_HOST",
    ):
        monkeypatch.setenv(name, "")
    monkeypatch.delitem(sys.modules, "api.routes.users_admin", raising=False)
    monkeypatch.setattr(graph_service.settings, "GRAPH_IN_MEMORY", True)
    monkeypatch.setattr(aws_clients.settings, "REGISTRY_IN_MEMORY", True)
    monkeypatch.setattr("core.registry_memory._INSTANCE", None)
    monkeypatch.setattr("services.graph_memory._DIRECTORY", None)
    control = MagicMock()
    real_client = aws_clients.client

    def client_for(service: str, **kwargs: Any) -> Any:
        return control if service == "bedrock-agentcore-control" else real_client(service, **kwargs)

    monkeypatch.setattr(aws_clients, "client", client_for)

    import api.routes.agents as agents
    import api.routes.grants as grants
    import api.routes.tenants as tenants
    import api.routes.users as users
    import api.routes.users_admin as users_admin
    from core.errors import install_error_handlers

    monkeypatch.setattr(grants, "_graph_svc", None)
    monkeypatch.setattr(agents, "_svc", None)
    monkeypatch.setattr(agents, "_identity_svc", None)
    monkeypatch.setattr(users, "_tenant_resolver", None)
    app = FastAPI()
    install_error_handlers(app)
    for router in (agents.router, grants.router, grants.entra_router, tenants.router, users_admin.router):
        app.include_router(router, prefix="/api/v1")
    client = TestClient(app, headers=DEV_ADMIN)

    tenant = client.post(
        "/api/v1/admin/tenants",
        json={
            "name": f"{RUN}-tenant",
            "line_of_business": "e2e",
            "entra_group_ids": [LOCAL_GROUP_ID],
            "platform": "aws",
            "stages": {"dev": {"account_id": "111122223333", "region": "us-east-1"}},
        },
    )
    assert tenant.status_code == 201, tenant.text
    monkeypatch.setattr(users, "_tenant_resolver", None)

    created = client.post(
        "/api/v1/agents",
        json={
            "name": f"{RUN}-governed-agent",
            "purpose": "verify-agp proof",
            "tenant_id": tenant.json()["id"],
            "platform": "aws_bedrock",
            "auth_type": "entra",
            "agent_arn": HARNESS_ARN,
        },
    )
    assert created.status_code == 201, created.text
    agent_id = created.json()["id"]
    read = client.get(f"/api/v1/agents/{agent_id}")
    assert read.status_code == 200, read.text
    record = read.json()
    assert record["identity_status"] == "provisioned"

    hits = client.get("/api/v1/entra/principals/search", params={"q": "alex"})
    assert hits.status_code == 200, hits.text
    assert [(hit["display_name"], hit["type"]) for hit in hits.json()] == [(LOCAL_PRINCIPAL, "user")]
    grant_body = {"principal_id": hits.json()[0]["id"], "principal_type": "user", "role": "Invoker"}

    granted = client.post(f"/api/v1/agents/{agent_id}/grants", json=grant_body)
    assert granted.status_code == 201, granted.text
    grant = granted.json()
    assert (grant["principal_display"], grant["principal_type"], grant["role"]) == (
        LOCAL_PRINCIPAL,
        "User",
        "Invoker",
    )
    again = client.post(f"/api/v1/agents/{agent_id}/grants", json=grant_body)
    assert again.status_code == 409, again.text
    assert again.json()["detail"] == "principal is already assigned to this agent"
    listed = client.get(f"/api/v1/agents/{agent_id}/grants")
    assert listed.status_code == 200, listed.text
    assert [row["assignment_id"] for row in listed.json()] == [grant["assignment_id"]]

    platform_users = client.get("/api/v1/admin/users")
    assert platform_users.status_code == 502, platform_users.text
    assert platform_users.json()["detail"] == "Failed to read platform users"

    revoked = client.delete(f"/api/v1/agents/{agent_id}/grants/{grant['assignment_id']}")
    assert revoked.status_code == 204, revoked.text
    revoked_again = client.delete(f"/api/v1/agents/{agent_id}/grants/{grant['assignment_id']}")
    assert revoked_again.status_code == 404, revoked_again.text
    assert revoked_again.json()["detail"] == "grant not found"

    directory = _directory()
    assert any(p["appId"] == record["entra_app_id"] for p in directory.service_principals.values())
    deleted = client.delete(f"/api/v1/agents/{agent_id}")
    assert deleted.status_code == 200, deleted.text
    assert client.get(f"/api/v1/agents/{agent_id}").status_code == 404
    traces = _traces_of(
        directory,
        app_id=record["entra_app_id"],
        sp_id=record["entra_sp_id"],
        audience=record["entra_app_audience"],
    )
    assert traces == []
    assert control.method_calls == []


def _graph_service_constructions(node: ast.AST, function: str | None = None):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield from _graph_service_constructions(child, child.name)
            continue
        if isinstance(child, ast.Call):
            func = child.func
            if (isinstance(func, ast.Name) and func.id == "GraphService") or (
                isinstance(func, ast.Attribute) and func.attr == "GraphService"
            ):
                yield function, child.lineno
        yield from _graph_service_constructions(child, function)


def test_graph_service_is_constructed_only_in_the_factory():
    constructions = [
        (path.relative_to(REPO_ROOT).as_posix(), function, line)
        for path in sorted(SRC.rglob("*.py"))
        for function, line in _graph_service_constructions(ast.parse(path.read_text(), filename=str(path)))
    ]
    outside = [f"{file}:{line}" for file, function, line in constructions if (file, function) != FACTORY]
    assert outside == [], "\n".join(f"{site}: {BUILD_THROUGH_THE_FACTORY}" for site in outside)
    assert [function for _, function, _ in constructions] == [FACTORY[1]] * 2, (
        f"{FACTORY[0]}::{FACTORY[1]} builds GraphService once per branch; found {constructions}"
    )
