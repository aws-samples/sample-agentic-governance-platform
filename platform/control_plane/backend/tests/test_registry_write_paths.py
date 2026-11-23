"""Registry calls go through the one owner, and the buildspec's writes carry the re-approve shape.

The AWS Agent Registry resets an APPROVED record to DRAFT on every ``UpdateRegistryRecord``, and the
marketplace lists APPROVED only, so a platform write (an identity id, a runtime ARN, a grant, the publish
flag, the marketplace block) sent through the content path delists the card. ``services.registry_writer``
is the one module that calls the registry: ``RegistryWriter.write`` re-asserts APPROVED after a platform
write and ``RegistryWriter.write_content`` is the governed user edit. Text checks over the tree, no AWS:
every call under ``src/`` to one of the seven registry operations, directly or through a literal
``getattr``, or a ``get_paginator`` over one of them or over ``list_registries``, sits in the owner
module, which calls each of the eight (``core/registry_memory.py`` is the in-memory double and defines
them); neither registry service stores a bare or or-ed ``control_client`` or a ``_ctl`` on itself;
``write_content(`` is called only by the two services' ``update``, and the only callers of a registry
service's ``update()`` with an ``AgentUpdate`` / ``McpServerUpdate`` are the two user PUT routes in
``USER_CONTENT_UPDATE_ROUTES`` (the governed content edit, which stays DRAFT and is reported PROPOSED);
no registry write verb appears in a tracked shell, CI YAML or ``backend/scripts`` file other than the
buildspec, and inside the buildspec every write is preceded by ``_reg_status`` and followed by
``_reapprove_after``; the ``agent-registry-control`` literal, spelled out, bound to a module constant or
named ``REGISTRY_CONTROL``, reaches no ``client(`` call, bare or qualified, under ``src/`` outside
``core/aws_clients.py``, because the one constructor is ``core.aws_clients.registry_control_client``, and
that provider is named, by import, alias, attribute or bare name, by the owner alone. No predicate here
for a client constructed through a variable service name, an operation reached through a non-literal
``getattr`` or a CLI verb assembled from variables. The buildspec checks hold the shell writer.
"""

import ast
import os
import re
import shutil
import subprocess
from collections import namedtuple
from pathlib import Path

REPO_ROOT = (
    Path(os.environ["AGP_REPO_ROOT"])
    if "AGP_REPO_ROOT" in os.environ
    else Path(__file__).resolve().parents[4]
)
BACKEND = REPO_ROOT / "platform/control_plane/backend"
SRC = BACKEND / "src"
INFRA = REPO_ROOT / "platform/control_plane/infrastructure"
BUILDSPEC = INFRA / "modules/codebuild/buildspec.yml"
BUILDSPEC_REL = "platform/control_plane/infrastructure/modules/codebuild/buildspec.yml"
BACKEND_SCRIPTS_REL = "platform/control_plane/backend/scripts/"
GIT = shutil.which("git") or "git"

Finding = namedtuple("Finding", "key message")
_PARSED: dict[Path, ast.Module] = {}

REGISTRY_SERVICE = "agent-registry-control"
FACTORY_MODULE = "core/aws_clients.py"
OWNER_MODULE = "services/registry_writer.py"
OWNER = f"platform/control_plane/backend/src/{OWNER_MODULE}"
MEMORY_DOUBLE_MODULE = "core/registry_memory.py"
AGENT_SERVICE = "platform/control_plane/backend/src/services/agent_registry_service.py"
MCP_SERVICE = "platform/control_plane/backend/src/services/mcp_server_service.py"
REGISTRY_OPERATIONS = frozenset(
    {
        "list_registry_records",
        "get_registry_record",
        "create_registry_record",
        "update_registry_record",
        "delete_registry_record",
        "submit_registry_record_for_approval",
        "update_registry_record_status",
    }
)
PAGEABLE = REGISTRY_OPERATIONS | {"list_registries"}
OWNER_CALLS = REGISTRY_OPERATIONS | {"get_paginator(list_registries)"}
REGISTRY_VERBS = (
    "update-registry-record",
    "submit-registry-record-for-approval",
    "update-registry-record-status",
    "create-registry-record",
    "delete-registry-record",
)
CONTENT_UPDATE_MODELS = frozenset({"AgentUpdate", "McpServerUpdate"})
USER_CONTENT_UPDATE_ROUTES = frozenset(
    {
        ("platform/control_plane/backend/src/api/routes/agents.py", "update_agent"),
        ("platform/control_plane/backend/src/api/routes/mcp_servers.py", "update_mcp_server"),
    }
)
USER_CONTENT_WRITERS = frozenset(
    {(AGENT_SERVICE, "AgentRegistryService.update"), (MCP_SERVICE, "McpServerRegistryService.update")}
)
BUILDSPEC_WRITE_BLOCK_FLOOR = 2
CONTENT_UPDATE_CALLER_FLOOR = 2
WRITE_CONTENT_CALLER_FLOOR = 2
REGISTRY_CLIENT_OBTAINER_FLOOR = 1

CALL_THE_REGISTRY_THROUGH_THE_OWNER = (
    "call the registry through services.registry_writer.RegistryWriter, and write it through "
    "persist_identity / set_published / persist_marketplace; a new direct write resets APPROVED to DRAFT "
    "and delists the card"
)
PLATFORM_FLAGS_ARE_NOT_CONTENT = "a platform flag goes through set_published / persist_identity, not update()"
VERBS_STAY_IN_THE_BUILDSPEC = (
    "a registry write from shell is a writer that can delist an approved card; record the fact on the "
    "repository row and let the backend adopt it, or call the backend"
)
WRAP_THE_WRITE = (
    "wrap the write: `_reg_status` on the code line before it and `_reapprove_after` on the code line "
    "after it, as the runtime and harness arms do; an unwrapped write resets APPROVED to DRAFT and "
    "delists the card"
)
OBTAIN_THROUGH_THE_PROVIDER = (
    "obtain the registry client from core.aws_clients.registry_control_client; "
    'a .client("agent-registry-control") call bypasses the seam'
)
CLIENTS_ARE_OBTAINED_BY_THE_OWNER = (
    "obtain the registry client in services.registry_writer alone and pass control_client= through; "
    "every new holder is a new writer that can reset APPROVED to DRAFT"
)
SERVICES_HOLD_THE_WRITER = "hold a RegistryWriter, not a registry client; the writer is the one caller"
USER_EDIT_IS_THE_UPDATE_ROUTE = (
    "write_content is the user's governed edit; a platform write goes through RegistryWriter.write"
)

WRITE_LINE = re.compile(r"agent-registry-control\s+update-registry-record(?![\w-])")


def _repo_relative(path: Path, root: Path) -> str:
    base = REPO_ROOT if path.is_relative_to(REPO_ROOT) else root
    return path.relative_to(base).as_posix()


def _modules(root: Path):
    for path in sorted(root.rglob("*.py")):
        if path not in _PARSED:
            _PARSED[path] = ast.parse(path.read_text(), filename=str(path))
        yield path, _PARSED[path]


def _calls(node: ast.AST, scope: tuple[str, ...] = (), function: ast.AST | None = None):
    """Yield ``(qualname, enclosing function, call)`` for every call under ``node``.

    The qualname is the dotted path of enclosing classes and functions: ``Class.method``,
    ``function``, ``<module>`` at module level. A lambda adds nothing to it.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            inner = function if isinstance(child, ast.ClassDef) else child
            yield from _calls(child, (*scope, child.name), inner)
            continue
        if isinstance(child, ast.Call):
            yield ".".join(scope) or "<module>", function, child
        yield from _calls(child, scope, function)


def _attr(call: ast.Call) -> str | None:
    return call.func.attr if isinstance(call.func, ast.Attribute) else None


def _name(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _constructs_content_update(node: ast.AST | None) -> bool:
    """``AgentUpdate(...)``, ``models.AgentUpdate(...)`` or ``AgentUpdate.model_validate(...)``."""
    if not isinstance(node, ast.Call):
        return False
    if _name(node.func) in CONTENT_UPDATE_MODELS:
        return True
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "model_validate"
        and _name(func.value) in CONTENT_UPDATE_MODELS
    )


def _annotated_as_content_update(annotation: ast.AST | None) -> bool:
    """``AgentUpdate``, ``models.AgentUpdate``, ``AgentUpdate | None`` or ``Optional[AgentUpdate]``."""
    return annotation is not None and any(
        _name(node) in CONTENT_UPDATE_MODELS for node in ast.walk(annotation)
    )


def _content_update_names(function: ast.AST | None) -> set[str]:
    if function is None:
        return set()
    assert isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
    arguments = function.args
    names = {
        arg.arg
        for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
        if _annotated_as_content_update(arg.annotation)
    }
    for node in ast.walk(function):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if _annotated_as_content_update(node.annotation):
                names.add(node.target.id)
        elif isinstance(node, ast.Assign) and _constructs_content_update(node.value):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return names


def _carries_content_update(call: ast.Call, function: ast.AST | None) -> bool:
    names = _content_update_names(function)
    for argument in (*call.args, *(keyword.value for keyword in call.keywords)):
        if _constructs_content_update(argument):
            return True
        if isinstance(argument, ast.Name) and argument.id in names:
            return True
    return False


def _registry_operation(call: ast.Call) -> str | None:
    attr = _attr(call)
    if attr in REGISTRY_OPERATIONS:
        return attr
    if attr == "get_paginator" and call.args:
        page = call.args[0]
        if isinstance(page, ast.Constant) and page.value in PAGEABLE:
            return f"get_paginator({page.value})"
    if _name(call.func) == "getattr" and len(call.args) >= 2:
        operation = call.args[1]
        if isinstance(operation, ast.Constant) and operation.value in REGISTRY_OPERATIONS:
            return operation.value
    return None


def registry_operation_calls(src: Path = SRC) -> list[Finding]:
    findings = []
    for path, tree in _modules(src):
        if path.relative_to(src).as_posix() == MEMORY_DOUBLE_MODULE:
            continue
        rel = _repo_relative(path, src)
        for qualname, _, call in _calls(tree):
            operation = _registry_operation(call)
            if operation:
                message = f"{rel}: {qualname} calls {operation}; {CALL_THE_REGISTRY_THROUGH_THE_OWNER}"
                findings.append(Finding((rel, qualname, operation), message))
    return findings


def _stores_the_client_on_self(node: ast.AST) -> bool:
    if not isinstance(node, ast.Assign | ast.AnnAssign) or node.value is None:
        return False
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    values = node.value.values if isinstance(node.value, ast.BoolOp) else [node.value]
    on_self = any(
        isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self"
        for t in targets
    )
    return on_self and any(isinstance(v, ast.Name) and v.id == "control_client" for v in values)


def service_client_attributes(src: Path = SRC) -> list[str]:
    found = set()
    for rel in (AGENT_SERVICE, MCP_SERVICE):
        path = src / rel.removeprefix("platform/control_plane/backend/src/")
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            ctl = isinstance(node, ast.Attribute) and node.attr == "_ctl" and isinstance(node.ctx, ast.Store)
            if ctl or _stores_the_client_on_self(node):
                assert isinstance(node, ast.Attribute | ast.Assign | ast.AnnAssign)
                found.add(f"{rel}:{node.lineno}")
    return sorted(found)


def write_content_callers(src: Path = SRC) -> set[tuple[str, str]]:
    return {
        (_repo_relative(path, src), qualname)
        for path, tree in _modules(src)
        for qualname, _, call in _calls(tree)
        if _attr(call) == "write_content"
    }


def content_update_callers(src: Path = SRC) -> list[Finding]:
    findings = []
    for area in ("api", "services"):
        for path, tree in _modules(src / area):
            rel = _repo_relative(path, src)
            for qualname, function, call in _calls(tree):
                if _attr(call) == "update" and _carries_content_update(call, function):
                    message = (
                        f"{rel}: {qualname} calls update() with an AgentUpdate / McpServerUpdate;"
                        f" {PLATFORM_FLAGS_ARE_NOT_CONTENT}"
                    )
                    findings.append(Finding((rel, qualname), message))
    return findings


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run([GIT, "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return result.stdout


def _tracked_shell_and_ci_files(repo: Path) -> list[str]:
    files = []
    for rel in _git(repo, "ls-files", "-z").split("\0"):
        if not rel or rel == BUILDSPEC_REL or not (repo / rel).is_file():
            continue
        if rel.endswith((".sh", ".yml", ".yaml")) or rel.startswith(BACKEND_SCRIPTS_REL):
            files.append(rel)
    return files


def shell_registry_verbs(repo: Path = REPO_ROOT) -> list[Finding]:
    findings = []
    for rel in _tracked_shell_and_ci_files(repo):
        text = (repo / rel).read_text(errors="replace")
        for verb in REGISTRY_VERBS:
            if re.search(re.escape(verb) + r"(?![\w-])", text):
                findings.append(Finding((rel, verb), f"{rel}: carries {verb}; {VERBS_STAY_IN_THE_BUILDSPEC}"))
    return findings


def buildspec_write_blocks(text: str) -> list[tuple[int, bool, bool]]:
    code = [
        (number, line.strip())
        for number, line in enumerate(text.splitlines(), start=1)
        if line.strip() and not line.strip().startswith("#")
    ]
    blocks = []
    for index, (number, line) in enumerate(code):
        if not WRITE_LINE.search(line):
            continue
        before = code[index - 1][1].split()[0] if index else ""
        after = code[index + 1][1].split()[0] if index + 1 < len(code) else ""
        blocks.append((number, before == "_reg_status", after == "_reapprove_after"))
    return blocks


def _service_argument(call: ast.Call) -> ast.AST | None:
    if call.args:
        return call.args[0]
    return next(
        (keyword.value for keyword in call.keywords if keyword.arg in ("service", "service_name")), None
    )


def registry_client_files(src: Path = SRC) -> set[str]:
    files = set()
    for path, tree in _modules(src):
        if path.relative_to(src).as_posix() == FACTORY_MODULE:
            continue
        constants = {
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and node.value.value == REGISTRY_SERVICE
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for _, _, call in _calls(tree):
            if _name(call.func) != "client":
                continue
            service = _service_argument(call)
            literal = isinstance(service, ast.Constant) and service.value == REGISTRY_SERVICE
            constant = isinstance(service, ast.Name) and service.id in constants
            if literal or constant or _name(service) == "REGISTRY_CONTROL":
                files.add(_repo_relative(path, src))
    return files


def _names_the_provider(node: ast.AST) -> bool:
    if isinstance(node, ast.ImportFrom):
        return any(alias.name == "registry_control_client" for alias in node.names)
    return _name(node) == "registry_control_client"


def registry_client_obtainers(src: Path = SRC) -> set[str]:
    return {
        _repo_relative(path, src)
        for path, tree in _modules(src)
        if any(_names_the_provider(node) for node in ast.walk(tree))
    }


def test_every_registry_operation_call_sits_in_the_owner():
    calls = registry_operation_calls()
    outside = [f for f in calls if f.key[0] != OWNER]
    assert outside == [], "\n".join(f.message for f in outside)
    called = {f.key[2] for f in calls if f.key[0] == OWNER}
    assert called == OWNER_CALLS, (
        f"{OWNER} must call every registry operation; missing {sorted(OWNER_CALLS - called)}"
    )


def test_the_services_hold_no_client():
    found = service_client_attributes()
    assert found == [], f"{found}: {SERVICES_HOLD_THE_WRITER}"


def test_write_content_is_called_only_by_the_two_user_put_routes():
    callers = write_content_callers()
    assert callers == USER_CONTENT_WRITERS, (
        f"write_content callers {sorted(callers)}: {USER_EDIT_IS_THE_UPDATE_ROUTE}"
    )


def test_content_update_is_called_only_by_the_two_user_put_routes():
    callers = content_update_callers()
    fresh = [f for f in callers if f.key not in USER_CONTENT_UPDATE_ROUTES]
    census = ", ".join(f"{file}::{qualname}" for file, qualname in sorted(f.key for f in callers))
    assert fresh == [], "\n".join(f.message for f in fresh) + f"\nevery content update() caller: {census}"


def test_no_registry_write_verb_outside_the_buildspec():
    found = shell_registry_verbs()
    assert found == [], "\n".join(f.message for f in found)


def test_every_buildspec_registry_write_is_wrapped_in_the_reapprove_shape():
    blocks = buildspec_write_blocks(BUILDSPEC.read_text())
    bare = [
        f"{BUILDSPEC_REL}:{line}: update-registry-record"
        f"{'' if before else ' without _reg_status before it'}"
        f"{'' if after else ' without _reapprove_after after it'}"
        for line, before, after in blocks
        if not (before and after)
    ]
    assert bare == [], "\n".join(bare) + f"\n{WRAP_THE_WRITE}"


def test_the_registry_literal_appears_in_no_client_call():
    found = registry_client_files()
    assert found == set(), f"{sorted(found)}: {OBTAIN_THROUGH_THE_PROVIDER}"


def test_the_registry_client_is_obtained_only_by_the_owner():
    obtainers = registry_client_obtainers()
    assert obtainers == {OWNER}, (
        f"registry_control_client named in {sorted(obtainers)}: {CLIENTS_ARE_OBTAINED_BY_THE_OWNER}"
    )


def test_the_scan_has_something_to_scan():
    assert len(registry_operation_calls()) >= len(OWNER_CALLS)
    assert len(buildspec_write_blocks(BUILDSPEC.read_text())) >= BUILDSPEC_WRITE_BLOCK_FLOOR
    assert len(content_update_callers()) >= CONTENT_UPDATE_CALLER_FLOOR
    assert len(write_content_callers()) >= WRITE_CONTENT_CALLER_FLOOR
    assert len(registry_client_obtainers()) >= REGISTRY_CLIENT_OBTAINER_FLOOR


def test_content_update_callers_see_the_constructor_and_the_annotation_not_a_dict(tmp_path):
    routes = tmp_path / "api" / "routes"
    routes.mkdir(parents=True)
    (routes / "things.py").write_text(
        "class Thing:\n"
        "    def toggle(self, svc, thing_id, req):\n"
        "        return svc.update(thing_id, AgentUpdate(published=req.published))\n"
        "\n"
        "async def update_thing(thing_id: str, req: McpServerUpdate):\n"
        "    return get_service().update(thing_id, req)\n"
        "\n"
        "def assigned(svc, thing_id, data):\n"
        "    patch = AgentUpdate(**data)\n"
        "    return svc.update(thing_id, patch)\n"
        "\n"
        "def validated(svc, thing_id, data):\n"
        "    return svc.update(thing_id, McpServerUpdate.model_validate(data))\n"
        "\n"
        "def optional_union(svc, thing_id, req: AgentUpdate | None = None):\n"
        "    return svc.update(thing_id, req)\n"
        "\n"
        "def optional_typing(svc, thing_id, req: Optional[McpServerUpdate]):\n"
        "    return svc.update(thing_id, req)\n"
        "\n"
        "def rename(cache, name):\n"
        "    cache.update({name: 1})\n"
        "    settings = TenantUpdate()\n"
        "    cache.update(name, settings)\n"
        "    return cache.update(name, TenantUpdate())\n"
    )
    (tmp_path / "services").mkdir()
    keys = sorted(f.key for f in content_update_callers(tmp_path))
    assert keys == [
        ("api/routes/things.py", "Thing.toggle"),
        ("api/routes/things.py", "assigned"),
        ("api/routes/things.py", "optional_typing"),
        ("api/routes/things.py", "optional_union"),
        ("api/routes/things.py", "update_thing"),
        ("api/routes/things.py", "validated"),
    ]


def test_registry_operation_calls_report_the_qualname_and_skip_the_double(tmp_path):
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "writer.py").write_text(
        "ctl.update_registry_record(registryId=r)\n"
        "class Writer:\n"
        "    def save(self):\n"
        "        return retry(lambda: self._ctl.update_registry_record(registryId=r))\n"
        "def helper(ctl):\n"
        "    ctl.update_registry_record_status(status='APPROVED')\n"
        "def pages(ctl):\n"
        "    yield from ctl.get_paginator('list_registries').paginate()\n"
        "    yield from ctl.get_paginator('list_registry_records').paginate()\n"
        "def other(ctl, name):\n"
        "    ctl.get_paginator('list_agent_runtimes')\n"
        "    ctl.get_paginator(name)\n"
        "    ctl.update_gateway(name='g')\n"
        "def fetch(ctl, name):\n"
        "    getattr(ctl, 'get_registry_record')(registryId=r)\n"
        "    getattr(ctl, name)(registryId=r)\n"
    )
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "registry_memory.py").write_text(
        "class Double:\n"
        "    def list_registry_records(self, **kw):\n"
        "        return self.get_registry_record(**kw)\n"
    )
    keys = [f.key for f in registry_operation_calls(tmp_path)]
    assert keys == [
        ("services/writer.py", "<module>", "update_registry_record"),
        ("services/writer.py", "Writer.save", "update_registry_record"),
        ("services/writer.py", "helper", "update_registry_record_status"),
        ("services/writer.py", "pages", "get_paginator(list_registries)"),
        ("services/writer.py", "pages", "get_paginator(list_registry_records)"),
        ("services/writer.py", "fetch", "get_registry_record"),
    ]
    assert all(CALL_THE_REGISTRY_THROUGH_THE_OWNER in f.message for f in registry_operation_calls(tmp_path))


def test_service_client_attributes_see_an_assigned_ctl_not_a_read(tmp_path):
    services = tmp_path / "services"
    services.mkdir()
    (services / "agent_registry_service.py").write_text(
        "class AgentRegistryService:\n"
        "    def __init__(self, control_client=None):\n"
        "        self._ctl = control_client\n"
        "        self._client = control_client or make()\n"
    )
    (services / "mcp_server_service.py").write_text(
        "class McpServerRegistryService:\n    def get(self, writer):\n        return writer._ctl\n"
    )
    assert service_client_attributes(tmp_path) == [f"{AGENT_SERVICE}:3", f"{AGENT_SERVICE}:4"]


def test_buildspec_write_blocks_read_the_neighbouring_code_lines():
    text = (
        "  # `update-registry-record` writes the envelope back\n"
        "  _reg_status\n"
        '  aws agent-registry-control update-registry-record --record-id "$ID" || { echo failed; exit 1; }\n'
        "\n"
        "  # a blank and a comment between the write and the re-approve do not break the shape\n"
        '  _reapprove_after "$NEW_ENV"\n'
        '  aws agent-registry-control update-registry-record --record-id "$ID"\n'
        "  echo done\n"
        "  aws agent-registry-control update-registry-record-status --status APPROVED\n"
    )
    assert buildspec_write_blocks(text) == [(3, True, True), (7, False, False)]


def test_shell_registry_verbs_see_a_verb_in_a_tracked_script_and_skip_the_buildspec(tmp_path):
    repo = tmp_path / "repo"
    (repo / "platform/control_plane/infrastructure/modules/codebuild").mkdir(parents=True)
    (repo / "platform/control_plane/backend/scripts").mkdir(parents=True)
    (repo / BUILDSPEC_REL).write_text("aws agent-registry-control update-registry-record\n")
    (repo / "deploy.sh").write_text("aws agent-registry-control delete-registry-record --record-id x\n")
    (repo / "pipeline.yml").write_text("script: aws agent-registry-control update-registry-record-status\n")
    (repo / "platform/control_plane/backend/scripts/seed.py").write_text(
        'run(["aws", "agent-registry-control", "create-registry-record"])\n'
    )
    (repo / "notes.md").write_text("submit-registry-record-for-approval is a verb\n")
    (repo / "untracked.sh").write_text("aws agent-registry-control update-registry-record\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "deploy.sh", "pipeline.yml", "platform", "notes.md")
    keys = sorted(f.key for f in shell_registry_verbs(repo))
    assert keys == [
        ("deploy.sh", "delete-registry-record"),
        ("pipeline.yml", "update-registry-record-status"),
        ("platform/control_plane/backend/scripts/seed.py", "create-registry-record"),
    ]


def test_registry_client_files_see_the_literal_and_the_module_constant(tmp_path):
    core = tmp_path / "core"
    core.mkdir()
    (core / "resolver.py").write_text(
        'SERVICE_NAME = "agent-registry-control"\n'
        "def client(region):\n"
        "    return boto3.client(SERVICE_NAME, region_name=region)\n"
    )
    (core / "service.py").write_text(
        "class Svc:\n"
        "    def __init__(self, control_client=None):\n"
        '        self._ctl = control_client or boto3.client("agent-registry-control")\n'
    )
    (core / "other.py").write_text(
        'NAME = "agent-registry-control"\n'
        'ctl = boto3.client("bedrock-agentcore-control")\n'
        'bucket = boto3.resource("s3")\n'
    )
    assert registry_client_files(tmp_path) == {"core/resolver.py", "core/service.py"}


def test_registry_client_obtainers_see_the_qualified_and_the_bare_call(tmp_path):
    core = tmp_path / "core"
    core.mkdir()
    (core / "aws_clients.py").write_text(
        'REGISTRY_CONTROL = "agent-registry-control"\n'
        "def registry_control_client(region):\n"
        "    return client(REGISTRY_CONTROL, region=region)\n"
    )
    (core / "resolver.py").write_text(
        "def resolve(name, region, *, ctl=None):\n"
        "    from core.aws_clients import registry_control_client\n"
        "    return find(ctl if ctl is not None else registry_control_client(region), name)\n"
    )
    services = tmp_path / "services"
    services.mkdir()
    (services / "service.py").write_text(
        "from core import aws_clients\n"
        "class Svc:\n"
        "    def __init__(self, region, control_client=None):\n"
        "        self._ctl = control_client or aws_clients.registry_control_client(region)\n"
    )
    (services / "other.py").write_text('ctl = boto3.client("bedrock-agentcore-control")\n')
    assert registry_client_obtainers(tmp_path) == {"core/resolver.py", "services/service.py"}
    assert registry_client_files(tmp_path) == set()


def test_registry_client_obtainers_see_an_aliased_import_and_a_rebind_not_a_docstring(tmp_path):
    services = tmp_path / "services"
    services.mkdir()
    (services / "alias_import.py").write_text(
        'from core.aws_clients import registry_control_client as rcc\nctl = rcc("r")\n'
    )
    (services / "rebind.py").write_text('provider = registry_control_client\nctl = provider("r")\n')
    (services / "partial.py").write_text(
        'from core import aws_clients\nmake = functools.partial(aws_clients.registry_control_client, "r")\n'
    )
    (services / "other.py").write_text(
        '"""Holds no client; registry_control_client is only mentioned here."""\n'
        'ctl = boto3.client("bedrock-agentcore-control")\n'
    )
    assert registry_client_obtainers(tmp_path) == {
        "services/alias_import.py",
        "services/partial.py",
        "services/rebind.py",
    }


def test_registry_client_files_see_the_generic_constructor_and_skip_the_factory_module(tmp_path):
    core = tmp_path / "core"
    core.mkdir()
    (core / "aws_clients.py").write_text(
        'REGISTRY_CONTROL = "agent-registry-control"\n'
        "def client(service, *, region=None):\n"
        "    return boto3.client(service, region_name=region)\n"
        "def registry_control_client(region):\n"
        "    return client(REGISTRY_CONTROL, region=region)\n"
    )
    services = tmp_path / "services"
    services.mkdir()
    (services / "bare_constant.py").write_text(
        'from core.aws_clients import REGISTRY_CONTROL, client\nctl = client(REGISTRY_CONTROL, region="r")\n'
    )
    (services / "bare_literal.py").write_text(
        'from core.aws_clients import client\nctl = client("agent-registry-control", region="r")\n'
    )
    (services / "qualified.py").write_text(
        'from core import aws_clients\nctl = aws_clients.client(aws_clients.REGISTRY_CONTROL, region="r")\n'
    )
    (services / "keyword.py").write_text(
        "from core.aws_clients import REGISTRY_CONTROL, client\n"
        'ctl = client(service=REGISTRY_CONTROL, region="r")\n'
    )
    (services / "other.py").write_text(
        'from core.aws_clients import client\nctl = client("s3", region="r")\nbucket = client(service="s3")\n'
    )
    assert registry_client_files(tmp_path) == {
        "services/bare_constant.py",
        "services/bare_literal.py",
        "services/keyword.py",
        "services/qualified.py",
    }
