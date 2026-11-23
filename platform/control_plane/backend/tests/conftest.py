"""Shared fixtures for the control-plane backend suite.

`pytest_configure` runs before any test module is imported: `_hermetic_shell_environment` deletes what
the shell exported that the suite reads and applies `AWS_PINNED`, and botocore's send is replaced so a
request no stub answered raises `BotocoreSendRefused`. Four fixtures are autouse: `_no_outbound_network`
fails any test that connects to a non-loopback address, `_no_botocore_send` errors a test during which
botocore tried to send, even when the test swallowed the refusal, `_no_botocore_send_outside_a_test`,
session-scoped, errors the run's last test when a refusal was swallowed outside every test, and
`_app_modules_restored`, module-scoped,
returns `sys.modules` after every test module to the `APP_PACKAGES` entries it started with, so a module that
evicted the application leaves the next one the modules it bound at collection. Everything else is opt-in.
Registry clients are `unittest.mock.MagicMock`s shaped like the service responses; no moto.

A fixture two test modules share lives here and is never imported from a sibling module:
`reset_modules` (opt in with `pytestmark = pytest.mark.usefixtures("reset_modules")`),
`entra_settings`, `app_evicted_under_entra` that the two `test_users_me_*` modules opt into, the
promote harness `build_svc`, `role_svc`, `project_svc`, `client_factory`
that `test_promote_repo.py` and `test_promote_eval_gate.py` drive, and the project-service fakes
`scaffold_dir` and `project_service_fakes` that `test_project_service.py` and
`test_project_materialize.py` drive.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any, Final, NoReturn
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from services.project_service import ProjectService


def app_route_paths(app) -> set[str]:
    """Every path registered on the app, with include-time prefixes applied.

    Starlette 1.x stopped flattening included routers into ``app.routes``: each
    ``include_router`` call now appears as a single ``fastapi.routing._IncludedRouter``
    wrapper whose ``path`` is ``None``. The registered paths live on
    ``wrapper.original_router.routes``, relative to ``wrapper.include_context.prefix``.
    Route-introspection tests must walk that structure instead of reading ``app.routes``
    flat; plain ``Route`` objects (app-level ``@app.get`` registrations) still carry
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


_ALLOWED_HOSTS = {"localhost", "::1", "0.0.0.0"}  # noqa: S104 — an allowlist of local addresses, not a bind; expires 2027-10-23; added E49A-50A/T2


def _is_loopback(host: object) -> bool:
    """Is this address local? Compared textually: a DNS lookup here would itself be a network
    call, and any name that needs resolving is by definition not loopback."""
    if not isinstance(host, str):
        return False
    if host in _ALLOWED_HOSTS or host.startswith("127.") or host.startswith("::ffff:127."):
        return True
    return host.endswith(".localhost")


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch, request):
    """FAIL any test that opens a TCP connection to a non-loopback address.

    Patches ``socket.socket.connect``/``connect_ex``, the single chokepoint every HTTP client in
    this suite (httpx, botocore, msal) funnels through, so one guard covers all of them without
    knowing which library a test uses. Unix-domain sockets and any non-``(host, port)`` address
    family pass through untouched.

    Escape hatch: ``@pytest.mark.allow_network`` on a test that genuinely must reach out. No test
    in the suite uses it today; it exists so the guard is never a reason to delete coverage."""
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
                    "never reach a live service: inject a mock/fake client or seed the "
                    "relevant module singleton. If the call is genuinely required, mark the "
                    "test with @pytest.mark.allow_network."
                )

    def guarded_connect(self, address):
        _check(address)
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        _check(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)


AWS_PINNED: Final[dict[str, str]] = {
    "AWS_EC2_METADATA_DISABLED": "true",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_SHARED_CREDENTIALS_FILE": "/nonexistent/aws/credentials",
    "AWS_CONFIG_FILE": "/nonexistent/aws/config",
    "BOTO_CONFIG": "/nonexistent/boto.cfg",
    "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "true",
}

KEPT_SHELL_VARIABLES: Final = frozenset({"AGP_REPO_ROOT", "AGP_UPDATE_OPENAPI_SNAPSHOT"})

_SCRUBBED_OUTSIDE_SETTINGS: Final = frozenset({"BUILD_SHA", "FORCE_COLOR", "NO_COLOR", "CLICOLOR_FORCE"})


def _scrubbed_names(environ: Mapping[str, str], fields: Iterable[str]) -> dict[str, str]:
    named = set(fields) | _SCRUBBED_OUTSIDE_SETTINGS
    return {
        key: value
        for key, value in environ.items()
        if (key.startswith("AWS_") and key not in AWS_PINNED)
        or (key.startswith(("AGP_", "CI_")) and key not in KEPT_SHELL_VARIABLES)
        or key in named
    }


def _hermetic_shell_environment() -> None:
    """Delete what the shell exported that the suite reads, then apply `AWS_PINNED`, before collection
    imports `core.config`, `boto3` or `moto`: no `os.environ` read at import or inside a test, no
    `Settings` built from the environment or a dotenv file, and no child process a test spawns sees the
    developer's or the GitLab job's value; a test that needs one sets it itself.

    `ENV_FILE` is emptied first, so neither the class read here nor the one a test module imports reads a
    dotenv file. The field names come from a `core.config` imported for that read alone and dropped from
    `sys.modules` and from the `core` package after it, so the snapshot `settings` the suite reads through
    either import shape is built after the scrub.

    Two names are kept. `AGP_REPO_ROOT` is the copied-tree switch: the tree-scanning modules read it at
    import to run against another checkout. `AGP_UPDATE_OPENAPI_SNAPSHOT` is the regeneration switch
    `test_openapi_contract.py` reads inside a test."""
    os.environ["ENV_FILE"] = ""
    from core.config import Settings

    fields = frozenset(Settings.model_fields)
    sys.modules.pop("core.config")
    vars(sys.modules["core"]).pop("config", None)
    for name in _scrubbed_names(os.environ, fields):
        del os.environ[name]
    os.environ.update(AWS_PINNED)


class BotocoreSendRefused(AssertionError):
    """botocore reached its HTTP send: no moto backend and no `botocore.stub.Stubber` answered."""


_REFUSED_SENDS: list[str] = []


def _refuse_botocore_send(self: Any, request: Any) -> NoReturn:
    _REFUSED_SENDS.append(f"{request.method} {request.url}")
    raise BotocoreSendRefused(
        f"botocore tried to send {request.method} {request.url} past every stub: build the client under "
        "moto's mock_aws on its default endpoint, or inject a fake."
    )


def pytest_configure(config):
    """Register the network guard's opt-out marker so ``-W error::PytestUnknownMarkWarning`` is safe."""
    _hermetic_shell_environment()
    import botocore.endpoint

    botocore.endpoint.Endpoint._send = _refuse_botocore_send
    config.addinivalue_line("markers", "allow_network: this test may open a non-loopback TCP connection")


@pytest.fixture(autouse=True)
def _no_botocore_send():
    """A refusal the test or the code under test swallowed still fails the test."""
    start = len(_REFUSED_SENDS)
    yield
    sent = _REFUSED_SENDS[start:]
    del _REFUSED_SENDS[start:]
    assert sent == [], "botocore sent past every stub: " + "; ".join(sent)


@pytest.fixture(autouse=True, scope="session")
def _no_botocore_send_outside_a_test():
    """A refusal swallowed outside every test, at collection or in a module- or session-scoped fixture, errors
    the run's last test."""
    yield
    assert _REFUSED_SENDS == [], "botocore sent past every stub outside a test: " + "; ".join(_REFUSED_SENDS)


APP_PACKAGES: Final = ("api", "core", "models", "services", "main")


def _app_modules() -> dict[str, ModuleType]:
    return {name: module for name, module in sys.modules.items() if name.partition(".")[0] in APP_PACKAGES}


@pytest.fixture(autouse=True, scope="module")
def _app_modules_restored():
    """Delete the application modules a test module added, from `sys.modules` and from their kept parent
    package, and put back the ones it started with, each set again on its parent package: a fresh import of
    a child under a kept package rewrites the package attribute that `import a.b as m` and `from a import b`
    read."""
    before = _app_modules()
    yield
    for name in _app_modules().keys() - before.keys():
        del sys.modules[name]
        parent, _, leaf = name.rpartition(".")
        if parent in before:
            vars(before[parent]).pop(leaf, None)
    sys.modules.update(before)
    for name, module in before.items():
        parent, _, leaf = name.rpartition(".")
        if parent:
            setattr(before[parent], leaf, module)


RESET_CORE_MODULES: Final = ("core.rbac", "core.security_entra", "core.config")


@pytest.fixture
def reset_modules():
    """Drop every loaded route module and the `RESET_CORE_MODULES`, from `sys.modules` and from their loaded
    parent package, so a test's monkeypatched environment is read afresh on the next import of either shape,
    `import a.b` or `from a import b`. Opt in per module with
    ``pytestmark = pytest.mark.usefixtures("reset_modules")``."""
    for name in [
        loaded for loaded in sys.modules if loaded.startswith("api.routes.") or loaded in RESET_CORE_MODULES
    ]:
        del sys.modules[name]
        parent, _, leaf = name.rpartition(".")
        if parent in sys.modules:
            vars(sys.modules[parent]).pop(leaf, None)
    yield


SETTINGS_FREE_MODULES: Final = frozenset({"core.errors", "core.registry_resolver"})


def evict_the_app() -> None:
    """Drop every `api` and `core` module and `main` from `sys.modules`, so the next import builds the
    application under the environment as it stands. The `SETTINGS_FREE_MODULES` stay: they read no
    settings, and a fresh copy would re-create `AppError` and the resolver errors beneath the classes other
    modules already imported, so the handler, `pytest.raises` and identity checks would no longer match."""
    for name in list(sys.modules):
        if (
            (name.startswith("core.") and name not in SETTINGS_FREE_MODULES)
            or name.startswith("api.")
            or name == "main"
        ):
            sys.modules.pop(name, None)


@pytest.fixture
def app_evicted_under_entra(entra_settings):
    """`entra_settings`, then `evict_the_app`, so a test that imports `main` builds the whole application in
    Entra mode. Opt in per module with ``pytestmark = pytest.mark.usefixtures("app_evicted_under_entra")``."""
    evict_the_app()


@pytest.fixture
def entra_settings(monkeypatch):
    monkeypatch.setenv("AUTH_PROVIDER", "entra")
    monkeypatch.setenv("USE_DEV_AUTH", "False")
    monkeypatch.setenv("ENTRA_TENANT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("ENTRA_AUDIENCE", "api://agp")
    monkeypatch.setenv("ENTRA_BACKEND_CLIENT_ID", "backend-client-id")
    monkeypatch.setenv("ENTRA_SPA_CLIENT_ID", "spa-client-id")
    monkeypatch.setenv("ENTRA_ROLE_ADMIN", "Platform.Admin")
    monkeypatch.setenv("ENTRA_ROLE_OPERATOR", "Platform.Operator")
    monkeypatch.setenv("ENTRA_ROLE_VIEWER", "Platform.Viewer")


REGISTRY_ID = "reg-test"
RECORD_ID = "rec-123"
RECORD_ARN = f"arn:aws:agent-registry:us-east-1:123456789012:registry/{REGISTRY_ID}/record/{RECORD_ID}"


def _sample_envelope(**overrides) -> dict:
    """A governance envelope (the Custom-record ``data`` dict)."""
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
    """A GetRegistryRecord-style response with the envelope inline."""
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
    """Return ``(ctl, data)`` MagicMocks shaped like the control-plane responses: ``CreateRegistryRecord``
    answers only ``{recordArn, status}``, the list pre-check finds no name collision, and the full
    record (with descriptors) comes back from ``get_registry_record``. Per-test overrides are expected.
    """
    ctl = MagicMock(name="agent-registry-control")
    data = MagicMock(name="agent-registry")

    ctl.create_registry_record.return_value = {
        "recordArn": RECORD_ARN,
        "status": "DRAFT",
    }
    ctl.list_registry_records.return_value = {"registryRecords": [], "nextToken": None}
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
    """An ``AgentRegistryService`` with an injected mock control client. The ``data`` half of the tuple
    is not passed on: the service has no data-plane client."""
    from services.agent_registry_service import AgentRegistryService

    ctl, _ = mock_registry_clients
    return AgentRegistryService(registry_id=REGISTRY_ID, control_client=ctl)


MCP_RECORD_ID = "mcp-rec-123"
MCP_RECORD_ARN = (
    f"arn:aws:agent-registry:us-east-1:123456789012:registry/{REGISTRY_ID}/record/{MCP_RECORD_ID}"
)


def _mcp_sample_envelope(**overrides) -> dict:
    """A governance envelope (the server.json ``_meta`` dict)."""
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
    """A server.json with the governance envelope under ``_meta``."""
    envelope = overrides.pop("_envelope", None) or _mcp_sample_envelope()
    server = {
        "name": "agp/internal-claims-mcp",
        "description": "Read-only access to motor and property claims records for the DE market.",
        "version": "1.0.0",
        "remotes": [{"type": "streamable-http", "url": "https://mcp.claims.acme.internal/mcp"}],
        "_meta": {"com.agp/governance": envelope},
    }
    server.update(overrides)
    return server


def _mcp_sample_tools() -> list:
    """A tools list of two sample tools."""
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
    """A GetRegistryRecord-style MCP response with inline payloads: ``descriptors.mcpServer.data`` is
    the stringified server.json and ``additionalData.tools.data`` the stringified ``{"tools": [...]}``.
    Supports ``_server_json``/``_envelope``/``_tools`` override hooks (mirrors the agent
    ``_sample_record`` ``_envelope`` hook).
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

    mcp_server_descriptor: dict[str, Any] = {
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
    """Return ``(ctl, data)`` MagicMocks for the MCP registry. ``create_registry_record`` answers
    ``status="CREATING"`` so the service's poll-to-DRAFT runs, and ``get_registry_record`` returns a
    DRAFT record so the poll terminates on the first read. Per-test overrides are expected.
    """
    ctl = MagicMock(name="agent-registry-control")
    data = MagicMock(name="agent-registry")

    ctl.create_registry_record.return_value = {
        "recordArn": MCP_RECORD_ARN,
        "status": "CREATING",
    }
    ctl.list_registry_records.return_value = {"registryRecords": [], "nextToken": None}
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
    """A ``McpServerRegistryService`` with an injected mock control client; the ``data`` half of the
    tuple is not passed on."""
    from services.mcp_server_service import McpServerRegistryService

    ctl, _ = mcp_mock_registry_clients
    return McpServerRegistryService(
        registry_id=REGISTRY_ID,
        control_client=ctl,
    )


@pytest.fixture
def passing_eval_gate(monkeypatch):
    """Every candidate build (digest, else tag) has a PASSING eval run, so promote tests written
    before the eval gate keep exercising what they pin; the promote also defaults to an override
    reason, so a test whose store is not this one still passes the gate. Opt in per module with
    ``pytestmark = pytest.mark.usefixtures("passing_eval_gate")`` (marks run before the module's own
    fixtures, so a ``ProjectService`` they build picks this store up as its default).
    ``tests/test_promote_eval_gate.py`` covers the gate itself against a real in-memory store."""
    from datetime import datetime, timezone

    from models.eval import EvalConfig, EvalRun, GateResult
    from services.eval_store import EvalStore

    class _PassingStore(EvalStore):
        def latest_run_for_build(self, repo_id, *, digest, tag):
            at = datetime(2026, 1, 1, tzinfo=timezone.utc)
            return EvalRun(
                run_id="run-pass",
                repo_id=repo_id,
                agent_id="a-1",
                project_id="proj-1",
                digest=digest,
                build_tag=tag,
                trigger="manual",
                started_by="seed",
                started_at=at,
                finished_at=at,
                status="passed",
                config=EvalConfig(),
                gate=GateResult(passed=True),
            )

    store = _PassingStore()
    monkeypatch.setattr("services.project_service.get_eval_store", lambda: store)

    from services.project_service import ProjectService

    real_promote = ProjectService.promote_repo

    def _promote(self, *args, override_reason=None, **kwargs):
        return real_promote(
            self,
            *args,
            override_reason="pre-E42 test" if override_reason is None else override_reason,
            **kwargs,
        )

    monkeypatch.setattr(ProjectService, "promote_repo", _promote)
    return store


@pytest.fixture
def build_svc():
    """The ``RuntimeBuildService`` mock the service promotes through; ``assert_not_called`` on it is
    how a gate test proves the refusal happened before the deploy."""
    svc = MagicMock()
    svc.start_runtime_build.return_value = "build-1"
    return svc


@pytest.fixture
def role_svc():
    """A ``ProjectRoleService`` MagicMock on the projects module global. Promote uses the strict gate,
    so a regression to the ungoverned-project fallback shows as a call on ``has_role_rows``."""
    import api.routes.projects as projects_module

    svc = MagicMock()
    svc.has_role_rows.return_value = False
    svc.list_all_strict.return_value = []
    svc.list_for_project.return_value = []
    projects_module._role_svc = svc
    return svc


@pytest.fixture
def project_svc(build_svc):
    """The real in-memory ``ProjectService`` on the projects module global, with only the single-repo
    read mocked. ``get_repo`` and ``_get_repo`` are the same mock, so one ``return_value`` reaches the
    route and the service alike."""
    from test_promote_repo import _real_service, _repo

    import api.routes.projects as projects_module

    svc = _real_service(build_svc)
    repo_read = MagicMock(return_value=_repo())
    svc.get_repo = repo_read
    svc._get_repo = repo_read
    projects_module._svc = svc
    return svc


@pytest.fixture
def client_factory(entra_settings, role_svc, project_svc):
    """Build a ``TestClient`` for a caller with a given platform role and project role."""
    from test_promote_repo import (
        _claims_for,
        _FakeProjectResolver,
        _FakeTenantResolver,
        _project_context,
        _tenant_context,
    )

    built = []

    def _make(*, platform_role="operator", project_role=None, roles=None, project_ctx=None, tenant_ctx=None):
        import api.routes.project_repos as project_repos_module
        import api.routes.projects as projects_module
        import api.routes.users as users_module
        from core.errors import install_error_handlers

        users_module._tenant_resolver = _FakeTenantResolver(
            tenant_ctx if tenant_ctx is not None else _tenant_context()
        )
        users_module._project_resolver = _FakeProjectResolver(
            project_ctx
            if project_ctx is not None
            else _project_context(is_global=platform_role == "admin", role=project_role, roles=roles)
        )

        app = FastAPI()
        install_error_handlers(app)
        app.include_router(projects_module.router, prefix="/api/v1")
        app.include_router(project_repos_module.router, prefix="/api/v1")
        client = TestClient(app, headers={"Authorization": "Bearer fake-token"})
        patcher = patch("core.security_entra.verify_entra_token", return_value=_claims_for(platform_role))
        patcher.start()
        client._entra_patcher = patcher
        built.append(client)
        return client

    yield _make
    for client in built:
        client._entra_patcher.stop()


FIXED = datetime(2026, 7, 2, tzinfo=timezone.utc)


VALID_AGENT_CONFIG = {
    "agent_name": "p1_agent",
    "framework": "strands",
    "model_id": "us.anthropic.claude-sonnet-4-6",
}


def _principal(oid="O1", email="e@x"):
    """A minimal principal — apply_creator_sponsor reads only .oid / .email."""
    return SimpleNamespace(oid=oid, email=email)


_SCAFFOLD_FILES = {
    "src/main.py": b"# the template's agent\n",
    ".github/workflows/build.yml": b"name: build\non:\n  push:\n    branches: [main]\n",
    "Dockerfile": b"FROM python:3.11-slim\n",
    "README.md": b"# agent\n",
}


TEMPLATE_NAME = "strands-agentcore"


@pytest.fixture(scope="session")
def scaffold_dir(tmp_path_factory):
    """A real on-disk template scaffold, written once per session."""
    root = tmp_path_factory.mktemp("agent-templates")
    for rel, content in _SCAFFOLD_FILES.items():
        path = root / TEMPLATE_NAME / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


@pytest.fixture
def project_service_fakes(scaffold_dir):
    """An in-memory service with every collaborator mocked.

    The provider mock is built with ``spec=`` so an attribute the real class does not have raises
    instead of silently passing an ``assert_*`` against a renamed method; ``spec`` checks names only,
    and ``test_repo_provider.py`` compares the signatures."""
    from services.github_repo_service import GitHubRepoService
    from services.project_service import ProjectService

    registry = MagicMock()
    registry.create.return_value = SimpleNamespace(id="agent-1", agent_arn=None, name="p1_agent")

    identity = MagicMock()
    identity.provision_identity.return_value = None

    conn = MagicMock()
    conn.get_connection.return_value = SimpleNamespace(org="acme", base_url=None)
    conn.get_bearer_token.return_value = "ghp_secret"

    github = MagicMock(spec=GitHubRepoService)
    github.create_repo.return_value = "https://github.com/acme/p1"

    ids = iter(["proj-1", "repo-1", "repo-2", "repo-3"])

    svc = ProjectService(
        table_name="",
        registry=registry,
        identity=identity,
        connection_service=conn,
        github_repo_service=github,
        agent_templates_dir=str(scaffold_dir),
        repo_vars={
            "AWS_REGION": "us-east-1",
            "AWS_ECR_PUSH_ROLE_ARN": "arn:aws:iam::123456789012:role/ecr-push",
            "ECR_REPOSITORY": "123456789012.dkr.ecr.us-east-1.amazonaws.com/agp",
            "AGP_API_URL": "https://agp.example/api/v1",
        },
        new_id=lambda: next(ids),
        now=lambda: FIXED,
    )
    return svc


def _make_project(svc: ProjectService, tenant_id: str = "default"):
    return svc.create_project(
        name="p1",
        connection_id="c1",
        tenant_id=tenant_id,
        description="the first project",
        created_by="op@x",
    )
