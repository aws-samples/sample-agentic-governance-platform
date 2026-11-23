"""`core.aws_clients` is the one constructor for every boto3 client, resource and table.

What is pinned: the service is the first positional argument of `boto3.client` and the region a
keyword (two test files patch the global `boto3.client` and read `call_args.args`); `region=None`
reads `settings.AWS_REGION` at call time; `credentials` becomes the three `aws_*` keywords and
`config` reaches the constructor untouched; an empty table name is refused, because the memory
decision belongs to `core.state_store`; the registry provider asks for the registry service by the
module constant; one lock serialises construction; importing the module imports no SDK and
constructs nothing; `boto3` is imported when a client is built, so a test that swaps
`sys.modules["boto3"]` is honoured.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.config import Config

from core import aws_clients, registry_memory
from core.aws_clients import (
    REGISTRY_CONTROL,
    AssumedCredentials,
    client,
    dynamodb_table,
    registry_control_client,
)

_BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_client_passes_the_service_positionally_and_the_region_by_keyword():
    with patch("boto3.client") as constructor:
        built = client("s3", region="eu-west-1")
    assert built is constructor.return_value
    assert constructor.call_args.args == ("s3",)
    assert constructor.call_args.kwargs == {"region_name": "eu-west-1"}


def test_region_none_reads_the_setting(monkeypatch):
    monkeypatch.setattr(aws_clients.settings, "AWS_REGION", "ap-southeast-2")
    with patch("boto3.client") as constructor:
        client("sts")
    assert constructor.call_args.args == ("sts",)
    assert constructor.call_args.kwargs == {"region_name": "ap-southeast-2"}


def test_credentials_become_the_three_aws_keywords():
    credentials = AssumedCredentials("key-id", "key-secret", "session-token")
    with patch("boto3.client") as constructor:
        client("iam", region="us-east-1", credentials=credentials)
    assert constructor.call_args.args == ("iam",)
    assert constructor.call_args.kwargs == {
        "region_name": "us-east-1",
        "aws_access_key_id": "key-id",
        "aws_secret_access_key": "key-secret",
        "aws_session_token": "session-token",
    }


def test_config_is_forwarded_unchanged():
    config = Config(connect_timeout=2, read_timeout=20, retries={"total_max_attempts": 1})
    with patch("boto3.client") as constructor:
        client("bedrock-runtime", config=config)
    assert constructor.call_args.args == ("bedrock-runtime",)
    assert constructor.call_args.kwargs["config"] is config


def test_dynamodb_table_refuses_an_empty_name():
    with patch("boto3.resource") as constructor, pytest.raises(ValueError) as raised:
        dynamodb_table("")
    assert str(raised.value) == (
        "a DynamoDB table needs a name; an empty name means the in-memory store, "
        "which core.state_store selects"
    )
    constructor.assert_not_called()


def test_dynamodb_table_is_the_resource_table_for_the_name():
    with patch("boto3.resource") as constructor:
        table = dynamodb_table("agp-things", region="eu-central-1")
    assert constructor.call_args.args == ("dynamodb",)
    assert constructor.call_args.kwargs == {"region_name": "eu-central-1"}
    constructor.return_value.Table.assert_called_once_with("agp-things")
    assert table is constructor.return_value.Table.return_value


def test_registry_control_client_asks_for_the_registry_service():
    assert REGISTRY_CONTROL == "agent-registry-control"
    with patch("boto3.client") as constructor:
        registry_control_client("eu-west-1")
    assert constructor.call_args.args == (REGISTRY_CONTROL,)
    assert constructor.call_args.kwargs == {"region_name": "eu-west-1"}


def test_registry_control_client_with_no_region_builds_the_memory_registry_in_the_setting(monkeypatch):
    monkeypatch.setattr(aws_clients.settings, "REGISTRY_IN_MEMORY", True)
    monkeypatch.setattr(aws_clients.settings, "AWS_REGION", "ap-southeast-2")
    monkeypatch.setattr(registry_memory, "_INSTANCE", None)
    with patch("boto3.client", side_effect=AssertionError("no boto3 client under REGISTRY_IN_MEMORY")):
        registry = registry_control_client(None)
    page = next(iter(registry.get_paginator("list_registries").paginate()))
    arns = [r["registryArn"] for r in page["registries"]]
    assert arns
    assert all(arn.split(":")[3] == "ap-southeast-2" for arn in arns)
    assert not any("None" in arn for arn in arns)


def test_construction_holds_one_lock():
    spans: list[tuple[float, float]] = []

    def slow_constructor(*_args, **_kwargs):
        started = time.monotonic()
        time.sleep(0.05)
        spans.append((started, time.monotonic()))
        return MagicMock()

    with patch("boto3.client", side_effect=slow_constructor):
        threads = [threading.Thread(target=client, args=("s3",)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    first, second = sorted(spans)
    assert second[0] >= first[1], spans


def test_importing_the_module_constructs_nothing_and_leaves_boto3_unimported():
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import core.aws_clients\n"
            "leaked = [m for m in ('boto3', 'botocore') if m in sys.modules]\n"
            "assert not leaked, f'import pulled in {leaked}'\n"
            "print('OK')\n",
        ],
        cwd=_BACKEND_DIR,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(_BACKEND_DIR / "src")},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert proc.stdout.strip() == "OK"


def test_the_client_import_is_call_time(monkeypatch):
    fake = types.ModuleType("boto3")
    monkeypatch.setattr(fake, "client", MagicMock(name="boto3.client"), raising=False)
    monkeypatch.setitem(sys.modules, "boto3", fake)
    built = client("sts", region="us-east-1")
    fake.client.assert_called_once_with("sts", region_name="us-east-1")
    assert built is fake.client.return_value


def botocore_session_clients(root: Path) -> list[str]:
    findings = []
    for area in ("src", "tests", "scripts"):
        for path in sorted((root / area).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if rel == "src/core/aws_clients.py" or rel.startswith("tests/fixtures/iam/"):
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            findings.extend(
                f"{rel}:{node.lineno}"
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_client"
            )
    return findings


def test_no_client_is_built_from_a_botocore_session():
    findings = botocore_session_clients(_BACKEND_DIR)
    assert findings == [], "\n".join(
        [
            "construct clients through core.aws_clients.client; a botocore session's create_client"
            " bypasses the factory and the TID251 ban",
            *findings,
        ]
    )


def test_a_botocore_session_client_is_a_finding_and_a_factory_call_is_not(tmp_path):
    (tmp_path / "src" / "services").mkdir(parents=True)
    (tmp_path / "src" / "core").mkdir()
    (tmp_path / "tests" / "fixtures" / "iam").mkdir(parents=True)
    (tmp_path / "src" / "services" / "x.py").write_text(
        "import botocore.session\n"
        "from core import aws_clients\n"
        'escaped = botocore.session.get_session().create_client("ssm")\n'
        'paved = aws_clients.client("ssm")\n'
    )
    (tmp_path / "src" / "core" / "aws_clients.py").write_text(
        "def client(service):\n    return botocore.session.get_session().create_client(service)\n"
    )
    (tmp_path / "tests" / "fixtures" / "iam" / "binding_idioms.py").write_text(
        'ssm = botocore.session.get_session().create_client("ssm")\n'
    )
    assert botocore_session_clients(tmp_path) == ["src/services/x.py:3"]


KNOWN_BOTO_IMPORTS: tuple[tuple[str, str], ...] = (
    ("src/core/registry_memory.py", "botocore.session"),
    ("src/core/state_store.py", "boto3.dynamodb.conditions"),
    ("src/core/state_store.py", "boto3.dynamodb.types"),
)
"""Each file outside the factory, with a boto3 or botocore-session module its imports may reach."""

BOTO_MODULES = frozenset({"boto3", "boto3.session", "botocore.session"})
DYNAMIC_IMPORTS = frozenset(
    {"import_module", "__import__", "find_spec", "module_from_spec", "spec_from_file_location"}
)
IMPORT_MACHINERY = ("importlib", "importlib.util", "importlib.machinery", "builtins")
BOTO_NAMES = (
    BOTO_MODULES
    | DYNAMIC_IMPORTS
    | {"client", "resource", "Session", "DEFAULT_SESSION", "setup_default_session", "create_client"}
)


def _reaches_boto(module: str) -> bool:
    return module in ("boto3", "botocore.session") or module.startswith(("boto3.", "botocore.session."))


def _boto_import(node: ast.Import | ast.ImportFrom) -> str | None:
    """`import boto3.<sub>` binds `boto3` itself, so it reaches `boto3`."""
    if isinstance(node, ast.Import):
        names = ["boto3" if alias.name.startswith("boto3.") else alias.name for alias in node.names]
    elif node.module and not node.level:
        submodules = [f"botocore.{alias.name}" for alias in node.names if node.module == "botocore"]
        names = [node.module, *submodules]
    else:
        return None
    return next((name for name in names if _reaches_boto(name)), None)


def _module_strings(tree: ast.Module) -> dict[str, str]:
    strings: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign | ast.AnnAssign) and isinstance(node.value, ast.Constant):
            value = node.value.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if isinstance(value, str):
                strings.update((target.id, value) for target in targets if isinstance(target, ast.Name))
    return strings


def _dynamic_imports(tree: ast.Module) -> frozenset[str]:
    names = set(DYNAMIC_IMPORTS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in IMPORT_MACHINERY:
            names.update(alias.asname or alias.name for alias in node.names if alias.name in DYNAMIC_IMPORTS)
        elif isinstance(node, ast.Assign) and _last_name(node.value) in DYNAMIC_IMPORTS:
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return frozenset(names)


def _last_name(node: ast.AST) -> str | None:
    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None


def _string(node: ast.AST, strings: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return strings.get(node.id) if isinstance(node, ast.Name) else None


def _spellings(node: ast.AST, strings: dict[str, str], dynamic: frozenset[str]) -> list[str]:
    if isinstance(node, ast.Subscript):
        key = _string(node.slice, strings)
        return [f"{node.lineno} a subscript keyed {key!r}"] if key in BOTO_MODULES else []
    if not isinstance(node, ast.Call):
        return []
    func = node.func
    name = _last_name(func)
    is_dynamic = name in (dynamic if isinstance(func, ast.Name) else DYNAMIC_IMPORTS)
    if is_dynamic or name == "getattr":
        named = BOTO_NAMES
    elif isinstance(func, ast.Attribute) and _last_name(func.value) == "modules":
        named = BOTO_MODULES
    else:
        return []
    values = [_string(arg, strings) for arg in (*node.args, *(keyword.value for keyword in node.keywords))]
    found = [f"{node.lineno} a call to {name}"] if is_dynamic else []
    return found + [f"{node.lineno} {name}(...) names {value!r}" for value in values if value in named]


def boto3_census(root: Path) -> tuple[list[tuple[str, str, int]], list[str]]:
    imports, spellings = [], []
    for area in ("src", "scripts"):
        for path in sorted((root / area).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if rel == "src/core/aws_clients.py":
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            strings, dynamic = _module_strings(tree), _dynamic_imports(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import | ast.ImportFrom) and (module := _boto_import(node)):
                    imports.append((rel, module, node.lineno))
                spellings.extend(f"{rel}:{spelling}" for spelling in _spellings(node, strings, dynamic))
    return imports, spellings


def boto3_reaches(root: Path, known: tuple[tuple[str, str], ...] = KNOWN_BOTO_IMPORTS) -> list[str]:
    imports, spellings = boto3_census(root)
    unknown = [
        f"{rel}:{line} imports {module}" for rel, module, line in imports if (rel, module) not in known
    ]
    return sorted([*spellings, *unknown])


def test_nothing_outside_the_factory_reaches_boto3_by_any_spelling():
    findings = boto3_reaches(_BACKEND_DIR)
    assert findings == [], "\n".join(
        [
            "reach AWS through core.aws_clients; outside it only the imports in KNOWN_BOTO_IMPORTS may"
            " name boto3 or a botocore session",
            *findings,
        ]
    )


def test_every_known_boto_import_still_exists():
    imports, _spellings = boto3_census(_BACKEND_DIR)
    assert len(set(KNOWN_BOTO_IMPORTS)) == len(KNOWN_BOTO_IMPORTS), "list each KNOWN_BOTO_IMPORTS pair once"
    stale = set(KNOWN_BOTO_IMPORTS) - {(rel, module) for rel, module, _line in imports}
    assert not stale, f"remove the KNOWN_BOTO_IMPORTS entries no import carries: {sorted(stale)}"


def test_each_spelling_around_the_factory_is_a_finding_and_a_known_import_is_not(tmp_path):
    (tmp_path / "src" / "services").mkdir(parents=True)
    (tmp_path / "src" / "core").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "src" / "core" / "aws_clients.py").write_text("import boto3\nboto3.DEFAULT_SESSION\n")
    (tmp_path / "src" / "core" / "state_store.py").write_text(
        "from boto3.dynamodb.conditions import Attr\nimport boto3.dynamodb.types\n"
    )
    (tmp_path / "src" / "services" / "x.py").write_text(
        "import sys\n"
        "from importlib import import_module\n"
        "import boto3\n"
        "from boto3 import session\n"
        "boto3.setup_default_session()\n"
        'getattr(import_module("boto3"), "client")\n'
        'sys.modules["boto3"].client("s3")\n'
        'sys.modules.get("botocore.session")\n'
        'WIRE_LOGGERS = ("boto3", "botocore")\n'
        'BOTO = "boto3"\n'
        "sys.modules[BOTO]\n"
        "sys.modules.get(BOTO)\n"
        "from importlib import import_module as load\n"
        "load(name)\n"
        "imp = __import__\n"
        "imp(name)\n"
        'getattr(importlib, "import_module")("boto3")\n'
        'logging.getLogger("boto3")\n'
        'sys.modules.pop("boto3")\n'
        "from importlib.util import find_spec as fs\n"
        'fs("boto3")\n'
        "importlib.machinery.PathFinder.find_spec(name)\n"
        "iu.spec_from_file_location(name, path)\n"
        "module_from_spec(spec)\n"
    )
    (tmp_path / "scripts" / "y.py").write_text("from botocore import session\n")
    known = (
        ("src/core/state_store.py", "boto3.dynamodb.conditions"),
        ("src/core/state_store.py", "boto3.dynamodb.types"),
    )
    assert boto3_reaches(tmp_path, known) == [
        "scripts/y.py:1 imports botocore.session",
        "src/core/state_store.py:2 imports boto3",
        "src/services/x.py:11 a subscript keyed 'boto3'",
        "src/services/x.py:12 get(...) names 'boto3'",
        "src/services/x.py:14 a call to load",
        "src/services/x.py:16 a call to imp",
        "src/services/x.py:17 getattr(...) names 'import_module'",
        "src/services/x.py:19 pop(...) names 'boto3'",
        "src/services/x.py:21 a call to fs",
        "src/services/x.py:21 fs(...) names 'boto3'",
        "src/services/x.py:22 a call to find_spec",
        "src/services/x.py:23 a call to spec_from_file_location",
        "src/services/x.py:24 a call to module_from_spec",
        "src/services/x.py:3 imports boto3",
        "src/services/x.py:4 imports boto3",
        "src/services/x.py:6 a call to import_module",
        "src/services/x.py:6 getattr(...) names 'client'",
        "src/services/x.py:6 import_module(...) names 'boto3'",
        "src/services/x.py:7 a subscript keyed 'boto3'",
        "src/services/x.py:8 get(...) names 'botocore.session'",
    ]
