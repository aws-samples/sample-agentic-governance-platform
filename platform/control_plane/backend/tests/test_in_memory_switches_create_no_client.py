"""The in-memory switches build no botocore client. Every boto3 client and resource, whatever its service,
ends in ``botocore.session.Session.create_client``, and the registry double loads its service model
without it, so each test runs one switch's path with that method raising and then requires it uncalled,
because the registry writer logs and swallows a failed read: the registry provider under
``REGISTRY_IN_MEMORY``, the register path through ``AgentRegistryService`` over it, ``StateStore("")``,
and ``graph_service_from_settings`` under ``GRAPH_IN_MEMORY`` with a Secrets Manager secret configured, so a
service built over the default secret loader would build one.
"""

import asyncio
from contextlib import contextmanager
from unittest.mock import patch

from botocore.session import Session

from core import aws_clients, registry_memory, secrets_loader
from core.registry_memory import MemoryRegistryClient
from core.state_store import StateStore
from models.agent import AgentCreate, Platform
from services import graph_memory, graph_service
from services.agent_registry_service import AgentRegistryService

REGION = "us-east-1"
NO_CLIENT = "a botocore client was created under an in-memory switch"


@contextmanager
def no_botocore_client():
    with patch.object(Session, "create_client", side_effect=AssertionError(NO_CLIENT)) as create:
        yield
    assert create.call_count == 0, NO_CLIENT


def test_the_registry_provider_under_the_switch_creates_no_botocore_client(monkeypatch):
    monkeypatch.setattr(aws_clients.settings, "REGISTRY_IN_MEMORY", True)
    monkeypatch.setattr(registry_memory, "_INSTANCE", None)
    with no_botocore_client():
        provided = aws_clients.registry_control_client(REGION)
        page = next(iter(provided.get_paginator("list_registries").paginate()))
    assert isinstance(provided, MemoryRegistryClient)
    assert page["registries"]


def test_the_register_path_under_the_switch_creates_no_botocore_client(monkeypatch):
    monkeypatch.setattr(aws_clients.settings, "REGISTRY_IN_MEMORY", True)
    monkeypatch.setattr(registry_memory, "_INSTANCE", None)
    with no_botocore_client():
        service = AgentRegistryService(
            registry_name=registry_memory.settings.AGENT_REGISTRY_NAME, region=REGION
        )
        agent = service.create(
            AgentCreate(
                name="switch-agent",
                purpose="prove the switch",
                platform=Platform.AWS_BEDROCK,
                tenant_id="default",
            ),
            created_by="tester",
        )
        read = service.get(agent.id)
        deleted = service.delete(agent.id)
        gone = service.get(agent.id)
    assert read is not None and read.name == "switch-agent"
    assert deleted is not None
    assert gone is None


def test_the_empty_table_name_creates_no_botocore_client():
    with no_botocore_client():
        store = StateStore("")
        written = store.put_new({"pk": "a", "sk": "b"})
        read = store.get(("a", "b"))
    assert written is True
    assert read == {"pk": "a", "sk": "b"}


def test_the_graph_switch_creates_no_botocore_client(monkeypatch):
    monkeypatch.setattr(graph_service.settings, "GRAPH_IN_MEMORY", True)
    monkeypatch.setattr(graph_memory, "_DIRECTORY", None)
    monkeypatch.setattr(secrets_loader, "_cached_secret", None)
    monkeypatch.setenv("ENTRA_BACKEND_CLIENT_SECRET_ARN", "agp-backend-client-secret")
    with no_botocore_client():
        svc = graph_service.graph_service_from_settings()
        created = asyncio.run(svc.create_agent_app("switch-agent", "Switch Agent"))
        asyncio.run(svc.delete_agent_app(entra_app_id=created["app_id"], entra_sp_id=created["sp_id"]))
    assert created["app_uri"].endswith("switch-agent")
