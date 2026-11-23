"""State writes carry a condition. Three rules over the DynamoDB calls under ``src/``, read from the
source, each held at zero: (1) a raw ``put_item`` / ``update_item`` / ``delete_item`` in any file other
than ``core/state_store.py`` is a write outside the store, whose finding says to write through
``core.state_store.StateStore``; ``raw_writes_outside_the_store`` lists every such write, conditional or
not, and ``unconditional`` the unconditional ones of the given methods. (2) Inside ``core/state_store.py``
a raw write without ``ConditionExpression=`` is a finding unless its enclosing method is named in
``core.state_store.BY_DESIGN_UNCONDITIONAL``, imported from the module. (3) In ``src/services/**`` and
``src/api/**`` every ``<receiver>.put(`` whose receiver matches ``STORE_RECEIVER_RE`` or ends in a name the
same module assigns from a ``StateStore(`` call is a whole-row write the store does not offer, keyed
``(file, qualname, "put")`` with ``Class.method``, ``function`` or ``<module>``, while a ``put`` reached
through an alias (``save = self._store.put``), an aliased local (``s = self._store; s.put(item)``), a
subscript (``stores[name].put``) or ``getattr(store, "put")`` is unseen. A write is conditional when the
call carries a ``ConditionExpression`` keyword or its spread dict literal carries that key as a string; a
condition built elsewhere and passed by name is unconditional to this reader.

Check-then-act on a shared row let two actors both pass: two eval starts both ran (``c908bbd9``), two
runtime builds both started (``1da71b26``), two admins could both approve one publish request or
subscription (``MarketplaceService._save_publish`` / ``_save``), and a monitor toggle could rewrite the
cursor the monitor loop advances (the ``MONITOR`` row of ``ContainmentStore``). Each fix was one
conditional write; nothing held that the next write carries one. Text-level because the behaviour tests
prove each past race on its parent (``test_two_starts_racing_past_the_running_check_cannot_both_run``
fails at ``c908bbd9^`` with ``DID NOT RAISE``) and cannot see the next unconditional write; rung 2
holding the ground for rung 1, ``core.state_store.StateStore``, whose write primitives carry their
condition (``put_new``, ``put_if_version``, ``transition``, ``acquire_lease`` / ``release_lease``,
``update_existing``, ``delete``) and whose one unconditional write, ``set_owned``, is named by design.

What has no predicate here: at ``c908bbd9^`` this check flags ``save_run`` and ``record_override`` as
raw writes outside the store, and cannot see the LOCK write that did not exist yet; a check-then-act
with no table write (``RuntimeBuildService._has_in_flight_build``, ``1da71b26``) and a poll's
completeness predicate (``SpanSource.session_spans``: ``b699c700``, ``4923034a``, ``45adc7d9``) are out
of a text check's reach, and the behaviour tests are the proof of those commits.
"""

import ast
import os
import re
from collections import namedtuple
from pathlib import Path

from core.state_store import BY_DESIGN_UNCONDITIONAL

REPO_ROOT = (
    Path(os.environ["AGP_REPO_ROOT"])
    if "AGP_REPO_ROOT" in os.environ
    else Path(__file__).resolve().parents[4]
)
BACKEND = REPO_ROOT / "platform/control_plane/backend"
SRC = BACKEND / "src"

Write = namedtuple("Write", "file qualname method conditional line")
Finding = namedtuple("Finding", "key message")
_PARSED: dict[Path, ast.Module] = {}

WRITE_METHODS = ("put_item", "update_item")
DELETE_METHODS = ("delete_item",)
CONDITION_KEY = "ConditionExpression"
STORE_FILE = "core/state_store.py"
STORE_CALLER_AREAS = ("api", "services")
STORE_RECEIVER_RE = re.compile(r"(^|[._])store$")
CONDITIONAL_WRITE_FLOOR = 4
DYNAMODB_WRITE_FLOOR = 5
RAW_WRITE_OUTSIDE_THE_STORE = (
    "write through core.state_store.StateStore; a raw table write outside the store has no condition "
    "anyone can see"
)
STORE_WRITE_WITHOUT_A_CONDITION = (
    "every StateStore write primitive carries its ConditionExpression, or its method is named in "
    "core.state_store.BY_DESIGN_UNCONDITIONAL"
)
STORE_PUT = (
    "StateStore has no whole-row put; write a create through put_new, a save through put_if_version, "
    "a move between states through transition, or the attributes the caller owns through set_owned"
)


def _repo_relative(path: Path, root: Path) -> str:
    base = REPO_ROOT if path.is_relative_to(REPO_ROOT) else root
    return path.relative_to(base).as_posix()


def _calls(node: ast.AST, scope: tuple[str, ...] = ()):
    """Yield ``(qualname, call)`` for every call under ``node``: ``Class.method``, ``function`` or
    ``<module>``, the dotted path of the enclosing classes and functions; a lambda adds nothing."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            yield from _calls(child, (*scope, child.name))
            continue
        if isinstance(child, ast.Call):
            yield ".".join(scope) or "<module>", child
        yield from _calls(child, scope)


def _is_conditional(call: ast.Call) -> bool:
    for keyword in call.keywords:
        if keyword.arg == CONDITION_KEY:
            return True
        if keyword.arg is None and isinstance(keyword.value, ast.Dict):
            if any(isinstance(k, ast.Constant) and k.value == CONDITION_KEY for k in keyword.value.keys):
                return True
    return False


def _module(path: Path) -> ast.Module:
    if path not in _PARSED:
        _PARSED[path] = ast.parse(path.read_text(), filename=str(path))
    return _PARSED[path]


def dynamodb_writes(src: Path = SRC) -> list[Write]:
    writes = []
    for path in sorted(src.rglob("*.py")):
        rel = _repo_relative(path, src)
        for qualname, call in _calls(_module(path)):
            method = call.func.attr if isinstance(call.func, ast.Attribute) else None
            if method in WRITE_METHODS or method in DELETE_METHODS:
                writes.append(Write(rel, qualname, method, _is_conditional(call), call.lineno))
    return writes


def _in_store(file: str) -> bool:
    return file == STORE_FILE or file.endswith("/" + STORE_FILE)


def _finding(write: Write, rule: str) -> Finding:
    carried = "with" if write.conditional else "without"
    return Finding(
        (write.file, write.qualname, write.method),
        f"{write.file}: {write.qualname} calls {write.method} {carried} a {CONDITION_KEY}; {rule}",
    )


def unconditional(methods: tuple[str, ...], src: Path = SRC) -> list[Finding]:
    return [
        _finding(write, RAW_WRITE_OUTSIDE_THE_STORE)
        for write in dynamodb_writes(src)
        if write.method in methods and not write.conditional and not _in_store(write.file)
    ]


def raw_writes_outside_the_store(src: Path = SRC) -> list[Finding]:
    return [
        _finding(write, RAW_WRITE_OUTSIDE_THE_STORE)
        for write in dynamodb_writes(src)
        if not _in_store(write.file)
    ]


def store_writes_without_a_condition(src: Path = SRC) -> list[Finding]:
    return [
        _finding(write, STORE_WRITE_WITHOUT_A_CONDITION)
        for write in dynamodb_writes(src)
        if _in_store(write.file)
        and not write.conditional
        and write.qualname.rsplit(".", 1)[-1] not in BY_DESIGN_UNCONDITIONAL
    ]


def _receiver(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _receiver(node.value)
        return f"{inner}.{node.attr}" if inner else None
    if isinstance(node, ast.Call):
        return _receiver(node.func)
    return None


def _store_names(module: ast.Module) -> set[str]:
    """Attribute and bare names the module assigns from a ``StateStore(`` call, at any depth."""
    names: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        called = _receiver(node.value.func) if isinstance(node.value, ast.Call) else None
        if called is None or called.rsplit(".", 1)[-1] != "StateStore":
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


def store_puts(src: Path = SRC) -> list[Finding]:
    """Every ``<receiver>.put(`` on a store receiver under the caller areas, keyed by qualname."""
    paths = sorted(
        p for area in STORE_CALLER_AREAS if (src / area).is_dir() for p in (src / area).rglob("*.py")
    )
    findings = []
    for path in paths:
        rel = _repo_relative(path, src)
        module = _module(path)
        bound = _store_names(module)
        for qualname, call in _calls(module):
            if not (isinstance(call.func, ast.Attribute) and call.func.attr == "put"):
                continue
            receiver = _receiver(call.func.value)
            if receiver is None:
                continue
            if STORE_RECEIVER_RE.search(receiver) or receiver.rsplit(".", 1)[-1] in bound:
                findings.append(
                    Finding((rel, qualname, "put"), f"{rel}: {qualname} calls put on {receiver}; {STORE_PUT}")
                )
    return findings


def test_every_store_write_carries_a_condition_unless_by_design():
    fresh = store_writes_without_a_condition()
    assert fresh == [], "\n".join(f.message for f in fresh)


def test_no_new_store_put():
    fresh = store_puts()
    assert fresh == [], "\n".join(f.message for f in fresh)


def test_no_new_unconditional_delete():
    fresh = unconditional(DELETE_METHODS)
    assert fresh == [], "\n".join(f.message for f in fresh)


def test_no_raw_table_write_outside_the_store():
    fresh = raw_writes_outside_the_store()
    assert fresh == [], "\n".join(f.message for f in fresh)


def test_the_scan_sees_conditional_writes():
    writes = dynamodb_writes()
    assert sum(1 for w in writes if w.conditional) >= CONDITIONAL_WRITE_FLOOR
    assert len(writes) >= DYNAMODB_WRITE_FLOOR


def test_dynamodb_writes_tell_a_conditional_put_from_an_unconditional_one(tmp_path):
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "store.py").write_text(
        "class Store:\n"
        "    def save(self, item):\n"
        "        self._table.put_item(Item=item)\n"
        "\n"
        "    def claim(self, item):\n"
        '        self._table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")\n'
        "\n"
        "    def forget(self, key):\n"
        "        self._table.delete_item(Key=key)\n"
        "\n"
        "def touch(table, key):\n"
        '    table.update_item(Key=key, UpdateExpression="SET a = :a", ConditionExpression=cond)\n'
    )
    writes = dynamodb_writes(tmp_path)
    assert [(w.qualname, w.method, w.conditional) for w in writes] == [
        ("Store.save", "put_item", False),
        ("Store.claim", "put_item", True),
        ("Store.forget", "delete_item", False),
        ("touch", "update_item", True),
    ]
    assert {w.file for w in writes} == {"services/store.py"}
    assert [w.line for w in writes] == [3, 6, 9, 12]


def test_a_condition_inside_a_spread_dict_literal_counts_and_one_passed_by_name_does_not(tmp_path):
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "store.py").write_text(
        "def spread(table, key):\n"
        '    table.update_item(**{"Key": key, "ConditionExpression": "attribute_exists(pk)"})\n'
        "\n"
        "def by_name(table, kwargs):\n"
        "    table.update_item(**kwargs)\n"
        "\n"
        "def built(table, key):\n"
        "    table.update_item(**_update_kwargs(key))\n"
    )
    writes = dynamodb_writes(tmp_path)
    assert [(w.qualname, w.conditional) for w in writes] == [
        ("spread", True),
        ("by_name", False),
        ("built", False),
    ]
    fresh = unconditional(WRITE_METHODS, tmp_path)
    assert [f.key for f in fresh] == [
        ("services/store.py", "by_name", "update_item"),
        ("services/store.py", "built", "update_item"),
    ]
    assert all(f.message.endswith(RAW_WRITE_OUTSIDE_THE_STORE) for f in fresh)


def test_a_raw_write_outside_the_store_is_a_finding(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "services").mkdir()
    (tmp_path / "core" / "state_store.py").write_text(
        "class StateStore:\n"
        "    def put_new(self, item):\n"
        '        self._table.put_item(Item=item, ConditionExpression=Attr("sk").not_exists())\n'
        "\n"
        "    def overwrite(self, item):\n"
        "        self._table.put_item(Item=item)\n"
    )
    (tmp_path / "services" / "legacy.py").write_text(
        "class Legacy:\n"
        "    def save(self, item):\n"
        "        self._table.put_item(Item=item)\n"
        "\n"
        "    def claim(self, item):\n"
        '        self._table.put_item(Item=item, ConditionExpression=Attr("sk").not_exists())\n'
        "\n"
        "    def forget(self, key):\n"
        "        self._table.delete_item(Key=key)\n"
    )
    findings = raw_writes_outside_the_store(tmp_path)
    assert [f.key for f in findings] == [
        ("services/legacy.py", "Legacy.save", "put_item"),
        ("services/legacy.py", "Legacy.claim", "put_item"),
        ("services/legacy.py", "Legacy.forget", "delete_item"),
    ]
    assert all(f.message.endswith(RAW_WRITE_OUTSIDE_THE_STORE) for f in findings)
    assert "Legacy.claim calls put_item with a ConditionExpression" in findings[1].message
    assert [f.key for f in unconditional(WRITE_METHODS, tmp_path)] == [
        ("services/legacy.py", "Legacy.save", "put_item")
    ]
    assert [f.key for f in unconditional(DELETE_METHODS, tmp_path)] == [
        ("services/legacy.py", "Legacy.forget", "delete_item")
    ]


def test_a_store_write_without_a_condition_is_a_finding_unless_by_design(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "state_store.py").write_text(
        "class StateStore:\n"
        "    def put_new(self, item):\n"
        '        self._table.put_item(Item=item, ConditionExpression=Attr("sk").not_exists())\n'
        "\n"
        "    def set_owned(self, key, attrs):\n"
        "        self._table.update_item(Key=key, **_set_expression(attrs))\n"
        "\n"
        "    def overwrite(self, item):\n"
        "        self._table.put_item(Item=item)\n"
        "\n"
        "    def forget(self, key):\n"
        "        self._table.delete_item(Key=key)\n"
    )
    findings = store_writes_without_a_condition(tmp_path)
    assert [f.key for f in findings] == [
        ("core/state_store.py", "StateStore.overwrite", "put_item"),
        ("core/state_store.py", "StateStore.forget", "delete_item"),
    ]
    assert all(f.message.endswith(STORE_WRITE_WITHOUT_A_CONDITION) for f in findings)
    assert raw_writes_outside_the_store(tmp_path) == []
    assert unconditional(WRITE_METHODS, tmp_path) == []


def test_a_store_put_caller_is_keyed_by_qualname(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "services").mkdir()
    (tmp_path / "api" / "routes").mkdir(parents=True)
    (tmp_path / "services" / "tenant_service.py").write_text(
        "class TenantService:\n"
        "    def _save(self, record):\n"
        "        self._store.put(_to_item(record))\n"
        "\n"
        "    def _create(self, record):\n"
        "        return self._store.put_new(_to_item(record))\n"
        "\n"
        "    def _restore(self, item):\n"
        "        self._restore.put(item)\n"
        "\n"
        "\n"
        "def seed(store, item):\n"
        "    store.put(item)\n"
        "\n"
        "\n"
        "def seeded(item):\n"
        "    get_eval_store().put(item)\n"
    )
    (tmp_path / "api" / "routes" / "containment.py").write_text(
        'def toggle(agent_id):\n    containment.store.put({"pk": agent_id})\n'
    )
    (tmp_path / "core" / "state_store.py").write_text(
        "class StateStore:\n"
        "    def overwrite(self, item):\n"
        "        self._table.put_item(Item=item)\n"
        "\n"
        "\n"
        "def helper(store, item):\n"
        "    store.put(item)\n"
    )
    findings = store_puts(tmp_path)
    assert [f.key for f in findings] == [
        ("api/routes/containment.py", "toggle", "put"),
        ("services/tenant_service.py", "TenantService._save", "put"),
        ("services/tenant_service.py", "seed", "put"),
        ("services/tenant_service.py", "seeded", "put"),
    ]
    assert all(f.message.endswith(STORE_PUT) for f in findings)
    assert "seeded calls put on get_eval_store" in findings[3].message


def test_a_store_held_under_another_name_is_a_put_caller_too(tmp_path):
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "repo_service.py").write_text(
        "from core import state_store\n"
        "from core.state_store import StateStore\n"
        "\n"
        "\n"
        "class RepoService:\n"
        "    def __init__(self, table_name):\n"
        "        self._db = StateStore(table_name)\n"
        "        self._queue = Queue()\n"
        "\n"
        "    def _save(self, item):\n"
        "        self._db.put(item)\n"
        "\n"
        "    def _enqueue(self, item):\n"
        "        self._queue.put(item)\n"
        "\n"
        "\n"
        'rows: StateStore = state_store.StateStore("")\n'
        "\n"
        "\n"
        "def seed(item):\n"
        "    rows.put(item)\n"
        "\n"
        "\n"
        "def elsewhere(cache, item):\n"
        "    cache.put(item)\n"
    )
    findings = store_puts(tmp_path)
    assert [f.key for f in findings] == [
        ("services/repo_service.py", "RepoService._save", "put"),
        ("services/repo_service.py", "seed", "put"),
    ]
    assert "RepoService._save calls put on self._db" in findings[0].message
    assert all(f.message.endswith(STORE_PUT) for f in findings)
