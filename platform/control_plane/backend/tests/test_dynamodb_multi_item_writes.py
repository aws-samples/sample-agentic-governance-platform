"""No multi-item or PartiQL DynamoDB write anywhere under ``src/``, ``core/state_store.py`` included.
A batch writer's put carries no condition at all. Every attribute spelled as one of
``MULTI_ITEM_WRITE_METHODS`` (a call, a bound method, a ``functools.partial``) and every string equal to
one (``getattr``) is a finding. No predicate here for a name assembled at run time.
"""

import ast
from collections import namedtuple

from test_state_writes_are_conditional import SRC, STORE_FILE, _module, _repo_relative

MULTI_ITEM_WRITE_METHODS = (
    "batch_write_item",
    "transact_write_items",
    "batch_writer",
    "execute_statement",
    "batch_execute_statement",
    "execute_transaction",
)
WRITE_ONE_ROW_THROUGH_THE_STORE = (
    "write one row at a time through core.state_store.StateStore, whose primitives carry their condition; "
    "a batch, a transaction or a PartiQL statement writes rows no condition check reads"
)

Named = namedtuple("Named", "file line name shape")


def multi_item_writes() -> list[Named]:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        rel = _repo_relative(path, SRC)
        for node in ast.walk(_module(path)):
            if isinstance(node, ast.Attribute) and node.attr in MULTI_ITEM_WRITE_METHODS:
                found.append(Named(rel, node.lineno, node.attr, "an attribute"))
            elif isinstance(node, ast.Constant) and node.value in MULTI_ITEM_WRITE_METHODS:
                found.append(Named(rel, node.lineno, node.value, "a string"))
    return sorted(found)


def test_no_multi_item_or_partiql_write_anywhere():
    assert (SRC / STORE_FILE).is_file(), f"{SRC} carries no {STORE_FILE}"
    found = multi_item_writes()
    assert found == [], "\n".join(
        f"{n.file}:{n.line} names {n.name} as {n.shape}; {WRITE_ONE_ROW_THROUGH_THE_STORE}" for n in found
    )
