"""Registry operations are named by the owner alone. This module reads every place one is named under
``src/``: an attribute spelled as one of the seven operations (a bound method
``op = self._writer._ctl.update_registry_record``, a ``functools.partial`` over it, a monkeypatch) or a
string equal to one (``getattr``, ``operator.methodcaller``) is a finding outside
``services/registry_writer.py`` and the in-memory double ``core/registry_memory.py``, and so is any
attribute or string reaching the writer's client, ``._ctl`` or ``.control_client``, outside the owner. Each
test also requires the owner to carry what it names, so a reader that walked nothing fails. No predicate
here for a name assembled at run time.
"""

import ast
from collections import namedtuple

from test_registry_write_paths import (
    CALL_THE_REGISTRY_THROUGH_THE_OWNER,
    MEMORY_DOUBLE_MODULE,
    OWNER_MODULE,
    REGISTRY_OPERATIONS,
    SERVICES_HOLD_THE_WRITER,
    SRC,
    _modules,
    _repo_relative,
)

CLIENT_ATTRIBUTES = frozenset({"_ctl", "control_client"})

Named = namedtuple("Named", "module line name shape")


def _named(names: frozenset[str], *, strings: bool) -> list[Named]:
    found = []
    for path, tree in _modules(SRC):
        module = path.relative_to(SRC).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in names:
                found.append(Named(module, node.lineno, node.attr, "an attribute"))
            elif strings and isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in names:
                    found.append(Named(module, node.lineno, node.value, "a string"))
    return sorted(found)


def _message(named: Named, rule: str) -> str:
    rel = _repo_relative(SRC / named.module, SRC)
    return f"{rel}:{named.line} names {named.name} as {named.shape}; {rule}"


def test_no_registry_operation_is_named_outside_the_owner():
    named = _named(REGISTRY_OPERATIONS, strings=True)
    outside = [n for n in named if n.module not in (OWNER_MODULE, MEMORY_DOUBLE_MODULE)]
    assert outside == [], "\n".join(_message(n, CALL_THE_REGISTRY_THROUGH_THE_OWNER) for n in outside)
    in_owner = {n.name for n in named if n.module == OWNER_MODULE}
    assert in_owner == REGISTRY_OPERATIONS, f"{OWNER_MODULE} names {sorted(in_owner)}"


def test_no_module_but_the_owner_reads_the_writer_s_client():
    named = _named(CLIENT_ATTRIBUTES, strings=True)
    outside = [n for n in named if n.module != OWNER_MODULE]
    assert outside == [], "\n".join(_message(n, SERVICES_HOLD_THE_WRITER) for n in outside)
    assert any(n.module == OWNER_MODULE and n.name == "_ctl" for n in named), f"{OWNER_MODULE} reads no _ctl"
