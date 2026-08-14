"""Shared pytest fixtures for the control-plane backend test suite.

Established by Epic 4 (Agent Registry). Keep these fixtures *additive and harmless* to the
existing suite: no global state mutation, and the ONE autouse fixture is the outbound-network
guard below, which is a no-op for every test that does not open a socket.

The original "no autouse fixtures" rule was written when no test consumed conftest at all; it
protected against fixtures that CHANGE behaviour. ``_no_outbound_network`` does not — it only
fails a test that was already reaching the internet, which is never correct in this suite.

The Agent Registry service wraps ONE boto3 client (E32):
  - control plane: ``agent-registry-control`` (CRUD + lifecycle)
It used to also hold a reserved ``bedrock-agentcore`` data-plane client, which was never
called; the new namespace's data plane is a different API surface
(``SearchDiscoverableRegistryRecords``), so the parameter is gone. ``mock_registry_clients``
still yields a ``(ctl, data)`` tuple purely so its many consumers keep unpacking two values.

Tests inject ``unittest.mock.MagicMock`` clients (no moto — research §10) shaped
to mimic the verified response shapes from research §6.
"""

from __future__ import annotations

import json
import socket
from unittest.mock import MagicMock

import pytest


def app_route_paths(app) -> set[str]:
    """Every path registered on the app, with include-time prefixes applied.

    Starlette 1.x stopped flattening included routers into ``app.routes``: each
    ``include_router`` call now appears as a single ``fastapi.routing._IncludedRouter``
    wrapper whose ``path`` is ``None``. The registered paths live on
    ``wrapper.original_router.routes``, relative to ``wrapper.include_context.prefix``.
    Route-introspection tests must walk that structure instead of reading ``app.routes``
    flat — plain ``Route`` objects (app-level ``@app.get`` registrations) still carry
    their full path and are collected as-is.
    """
    paths: set[str] = set()

    def _walk(routes, prefix: str) -> None:
        for r in routes:
            inner = getattr(r, "original_router", None)
            if inner is not None:
                ctx_prefix = getattr(getattr(r, "include_context", None), "prefix", "") or ""
                _walk(inner.routes, prefix + ctx_prefix)
            else:
                path = getattr(r, "path", None)
                if path is not None:
                    paths.add(prefix + path)

    _walk(app.routes, "")
    return paths

# ===========================================================================
# Outbound-network guard (E27 fix pass)
#
# D8 recorded a regression where two test files constructed a REAL ProjectResolver +
# GraphService and POSTed to login.microsoftonline.com once per request — 96 live Entra
# calls, with a GREEN suite. It was fixed by seeding the resolver in those files, but
# nothing PREVENTED it: the next route to adopt a resolver dependency reintroduces live
# calls, and the suite still passes (slowly, and only while the network happens to be up).
#
# This guard makes that class of mistake VISIBLE. It fails only on a connect to a
# non-loopback address, so a correct test — every test in this suite mocks or injects its
# clients — never notices it. Loopback stays open because TestClient/uvicorn and any local
# fixture server need it.
# ===========================================================================

_ALLOWED_HOSTS = {"localhost", "::1", "0.0.0.0"}


def _is_loopback(host: object) -> bool:
    """Is this address local? Compared textually — a DNS lookup here would itself be a
    network call, and any name that needs resolving is by definition not loopback."""
    if not isinstance(host, str):
        return False
    if host in _ALLOWED_HOSTS or host.startswith("127.") or host.startswith("::ffff:127."):
        return True
    return host.endswith(".localhost")


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch, request):
    """FAIL any test that opens a TCP connection to a non-loopback address.

    Patches ``socket.socket.connect``/``connect_ex`` — the single chokepoint every HTTP client
    in this suite (httpx, requests, botocore, msal) ultimately funnels through, so one guard
    covers all of them without knowing which library a test uses.

    Unix-domain sockets and any non-``(host, port)`` address family pass through untouched.

    Escape hatch: ``@pytest.mark.allow_network`` on a test that genuinely must reach out. No
    test in the suite uses it today; it exists so the guard is never a reason to delete
    coverage."""
    if request.node.get_closest_marker("allow_network"):
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _check(address):
        if isinstance(address, tuple) and address:
            host = address[0]
            if not _is_loopback(host):
                raise AssertionError(
                    f"This test tried to open a network connection to {host!r}. Tests must "
                    "never reach a live service — inject a mock/fake client or seed the "
                    "relevant module singleton (see decisions D8). If the call is genuinely "
                    "required, mark the test with @pytest.mark.allow_network."
                )

    def guarded_connect(self, address):
        _check(address)
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        _check(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)


def pytest_configure(config):
    """Register the guard's opt-out marker so ``-W error::PytestUnknownMarkWarning`` is safe."""
    config.addinivalue_line(
        "markers", "allow_network: this test may open a non-loopback TCP connection"
    )


# Registry id used across the service tests.
REGISTRY_ID = "reg-test"
RECORD_ID = "rec-123"
RECORD_ARN = (
    "arn:aws:agent-registry:us-east-1:123456789012:"
    f"registry/{REGISTRY_ID}/record/{RECORD_ID}"
)


def _sample_envelope(**overrides) -> dict:
    """A research-§4 governance envelope (the Custom-record ``data`` dict)."""
    env = {
        "schema_version": 1,
        "agent_id": RECORD_ID,
        "sponsor_oid": "maria-oid",
        "sponsor_email": "maria.bauer@example.com",
        "business_unit": "Claims",
        "region": "DE",
        "data_classification": "Confidential",
        "platform": "aws_bedrock",
        "framework": "langgraph",
        "mcp_server_ids": [],
        "entra_app_id": None,
        "entra_api_app_id": None,
        "origin": "Registered",
        "created_by": "maria.bauer@example.com",
    }
    env.update(overrides)
    return env


def _sample_record(**overrides) -> dict:
    """A GetRegistryRecord-style response (research §6) with the envelope inline."""
    envelope = overrides.pop("_envelope", None) or _sample_envelope()
    record = {
        "recordId": RECORD_ID,
        "recordArn": RECORD_ARN,
        "name": "claims-triage-de",
        "displayName": "claims-triage-de",
        "description": "Triage inbound motor claims for the DE market",
        "recordType": "CUSTOM",
        "recordVersion": "1.0.0",
        "status": "DRAFT",
        "createdAt": "2026-06-01T10:00:00+00:00",
        "updatedAt": "2026-06-01T10:00:00+00:00",
        "descriptors": {"custom": {"data": json.dumps(envelope)}},
    }
    record.update(overrides)
    return record


@pytest.fixture
def sample_envelope():
    return _sample_envelope


@pytest.fixture
def sample_record():
    return _sample_record


@pytest.fixture
def mock_registry_clients():
    """Return ``(ctl, data)`` MagicMocks shaped per research §6.

    Per-test overrides are expected (e.g. configuring ``list_registry_records``
    to return matching records for the name pre-check). Defaults are the common
    happy-path shapes.
    """
    ctl = MagicMock(name="agent-registry-control")
    data = MagicMock(name="agent-registry")

    # CreateRegistryRecord output is ONLY {recordArn, status} (research §6).
    ctl.create_registry_record.return_value = {
        "recordArn": RECORD_ARN,
        "status": "DRAFT",
    }
    # Default: no name collision on the pre-check; no records on list.
    ctl.list_registry_records.return_value = {"registryRecords": [], "nextToken": None}
    # Full record (with descriptors) for get/fan-out.
    ctl.get_registry_record.return_value = _sample_record()
    ctl.update_registry_record.return_value = {
        "recordArn": RECORD_ARN,
        "status": "DRAFT",
    }
    ctl.delete_registry_record.return_value = {}
    ctl.submit_registry_record_for_approval.return_value = {}
    ctl.update_registry_record_status.return_value = {}

    return ctl, data


@pytest.fixture
def service(mock_registry_clients):
    """Construct an AgentRegistryService with an injected mock control client.

    E32: the service no longer takes ``data_client`` — the reserved bedrock-agentcore
    data-plane client was never called, and the new namespace's data plane is a
    different API surface (``SearchDiscoverableRegistryRecords``). The
    ``mock_registry_clients`` fixture still yields the ``(ctl, data)`` tuple so its many
    consumers are unaffected; the unused ``data`` half is simply not passed on.
    """
    from services.agent_registry_service import AgentRegistryService

    ctl, _ = mock_registry_clients
    return AgentRegistryService(registry_id=REGISTRY_ID, control_client=ctl)


# ===========================================================================
# Epic 5 — MCP Server registry fixtures (add-only; parallel to the agent ones)
#
# An MCP server is an ``MCP``-type registry record whose payload is
# ``descriptors.mcpServer.data`` (a stringified server.json) + an optional
# ``descriptors.mcpServer.additionalData.tools.data`` (a stringified
# ``{"tools": [...]}``). Governance rides inside ``server.json
# _meta["com.agp/governance"]`` (research §2, §3). The mocks mimic the verified
# live shapes (research §6, §8).
#
# E32 renamed all three legs of that payload: the union arm ``mcp`` became
# ``mcpServer``, its ``server`` leaf collapsed INTO the arm itself (``data`` +
# ``dataSchemaVersion``), and the ``tools`` leaf moved one level down under
# ``additionalData``. ``schemaVersion``/``protocolVersion``/``inlineContent`` are all
# gone, consolidated into ``dataSchemaVersion``/``data``.
# ===========================================================================

MCP_RECORD_ID = "mcp-rec-123"
MCP_RECORD_ARN = (
    "arn:aws:agent-registry:us-east-1:123456789012:"
    f"registry/{REGISTRY_ID}/record/{MCP_RECORD_ID}"
)


def _mcp_sample_envelope(**overrides) -> dict:
    """A research-§3.1 governance envelope (the server.json ``_meta`` dict)."""
    env = {
        "schema_version": 1,
        "mcp_server_id": MCP_RECORD_ID,
        "kind": "standard",
        "owner_oid": "maria-oid",
        "owner_email": "maria.bauer@example.com",
        "business_unit": "Claims",
        "region": "DE",
        "data_classification": "Confidential",
        "entra_app_id": None,
        "gateway_arn": None,
        "created_by": "lars.svensson@example.com",
    }
    env.update(overrides)
    return env


def _mcp_sample_server_json(**overrides) -> dict:
    """A research-§3 server.json with the governance envelope under ``_meta``."""
    envelope = overrides.pop("_envelope", None) or _mcp_sample_envelope()
    server = {
        "name": "agp/internal-claims-mcp",
        "description": "Read-only access to motor and property claims records for the DE market.",
        "version": "1.0.0",
        "remotes": [
            {"type": "streamable-http", "url": "https://mcp.claims.acme.internal/mcp"}
        ],
        "_meta": {"com.agp/governance": envelope},
    }
    server.update(overrides)
    return server


def _mcp_sample_tools() -> list:
    """A research-§3 tools list (1-2 sample tools)."""
    return [
        {
            "name": "get_claim",
            "description": "Fetch a single claim by its claim number.",
            "inputSchema": {
                "type": "object",
                "properties": {"claim_number": {"type": "string"}},
                "required": ["claim_number"],
            },
        },
        {
            "name": "search_claims",
            "description": "Search claims by policy holder, status, or date range.",
            "inputSchema": {
                "type": "object",
                "properties": {"policy_holder": {"type": "string"}},
            },
        },
    ]


def _mcp_sample_record(**overrides) -> dict:
    """A GetRegistryRecord-style MCP response (research §8) with inline payloads.

    Supports ``_server_json``/``_envelope``/``_tools`` override hooks (mirrors the
    agent ``_sample_record`` ``_envelope`` hook).
    """
    server_json = overrides.pop("_server_json", None)
    envelope = overrides.pop("_envelope", None)
    tools = overrides.pop("_tools", None)
    if tools is None:
        tools = _mcp_sample_tools()
    if server_json is None:
        if envelope is not None:
            server_json = _mcp_sample_server_json(_envelope=envelope)
        else:
            server_json = _mcp_sample_server_json()

    mcp_server_descriptor = {
        "data": json.dumps(server_json),
        "dataSchemaVersion": "2025-12-11",
    }
    if tools:
        mcp_server_descriptor["additionalData"] = {
            "tools": {
                "data": json.dumps({"tools": tools}),
                "dataSchemaVersion": "2025-11-25",
            }
        }

    record = {
        "recordId": MCP_RECORD_ID,
        "recordArn": MCP_RECORD_ARN,
        "name": "internal-claims-mcp",
        # E32: displayName is the new human-facing label, and reads PREFER it over the
        # `name` dedup key. Mirrors `name` here because that is the post-create shape our
        # own writes produce (both carry the one name the API offers).
        "displayName": "internal-claims-mcp",
        "description": "Read-only access to motor and property claims records for the DE market.",
        "recordType": "MCP",
        "recordVersion": "1.0.0",
        "status": "DRAFT",
        "createdAt": "2026-06-02T10:00:00+00:00",
        "updatedAt": "2026-06-02T10:00:00+00:00",
        "descriptors": {"mcpServer": mcp_server_descriptor},
    }
    record.update(overrides)
    return record


@pytest.fixture
def mcp_sample_envelope():
    return _mcp_sample_envelope


@pytest.fixture
def mcp_sample_server_json():
    return _mcp_sample_server_json


@pytest.fixture
def mcp_sample_record():
    return _mcp_sample_record


@pytest.fixture
def mcp_mock_registry_clients():
    """Return ``(ctl, data)`` MagicMocks shaped per research §6/§8.

    ``create_registry_record`` returns ``status="CREATING"`` so the service's
    poll-to-DRAFT runs; ``get_registry_record`` returns a DRAFT record so the poll
    terminates on the first poll. Per-test overrides are expected.
    """
    ctl = MagicMock(name="agent-registry-control")
    data = MagicMock(name="agent-registry")

    # CreateRegistryRecord output is ONLY {recordArn, status} (research §6/§8).
    # CREATING (not DRAFT) so the poll-to-DRAFT path is exercised.
    ctl.create_registry_record.return_value = {
        "recordArn": MCP_RECORD_ARN,
        "status": "CREATING",
    }
    # Default: no name collision on the pre-check; no records on list.
    ctl.list_registry_records.return_value = {"registryRecords": [], "nextToken": None}
    # Full DRAFT record (with descriptors) for get/fan-out + the poll exit.
    ctl.get_registry_record.return_value = _mcp_sample_record()
    ctl.update_registry_record.return_value = {
        "recordArn": MCP_RECORD_ARN,
        "status": "DRAFT",
    }
    ctl.delete_registry_record.return_value = {}
    ctl.submit_registry_record_for_approval.return_value = {}
    ctl.update_registry_record_status.return_value = {}

    return ctl, data


@pytest.fixture
def mcp_service(mcp_mock_registry_clients):
    """Construct an McpServerRegistryService with an injected mock control client.

    E32: the service no longer takes ``data_client`` — the reserved bedrock-agentcore
    data-plane client was never called, and the new namespace's data plane is a different
    API surface (``SearchDiscoverableRegistryRecords``). Same change Task 2 made to the
    agent ``service`` fixture; ``mcp_mock_registry_clients`` still yields the
    ``(ctl, data)`` tuple so its many consumers keep unpacking two values, and the unused
    ``data`` half is simply not passed on.
    """
    from services.mcp_server_service import McpServerRegistryService

    ctl, _ = mcp_mock_registry_clients
    return McpServerRegistryService(
        registry_id=REGISTRY_ID,
        control_client=ctl,
    )
