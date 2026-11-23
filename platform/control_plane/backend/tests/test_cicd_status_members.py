"""``CicdStatus`` is exactly the set its writers assign plus ``unknown``, the fold."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import NamedTuple, get_args

from models.repository import CicdStatus

BACKEND = Path(__file__).resolve().parents[1]
PROJECT_MODULES = sorted((BACKEND / "src" / "services").glob("project_*.py"))
BUILDSPEC = BACKEND.parent / "infrastructure" / "modules" / "codebuild" / "buildspec.yml"
BUILDSPEC_WRITE = re.compile(r"\b_st\s+([a-z]+)")
FOLD = "unknown"


class ServiceWrites(NamedTuple):
    literals: set[str]
    opaque_sites: list[str]


def service_writes() -> ServiceWrites:
    literals: set[str] = set()
    opaque_sites: list[str] = []
    for module in PROJECT_MODULES:
        for node in ast.walk(ast.parse(module.read_text())):
            if isinstance(node, ast.keyword) and node.arg == "cicd_status":
                written = node.value
            elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Attribute) and target.attr == "cicd_status" for target in node.targets
            ):
                written = node.value
            else:
                continue
            if isinstance(written, ast.Constant) and isinstance(written.value, str):
                literals.add(written.value)
            else:
                opaque_sites.append(f"{module.name}:{written.lineno}")
    return ServiceWrites(literals, opaque_sites)


def buildspec_writes() -> set[str]:
    text = BUILDSPEC.read_text()
    assert "_u cicd_status" in text, f"{BUILDSPEC} no longer defines the _st helper over cicd_status"
    return set(BUILDSPEC_WRITE.findall(text))


def test_every_service_write_is_a_literal():
    opaque = service_writes().opaque_sites
    assert opaque == [], f"cicd_status is assigned a non-literal at {opaque}; the union cannot see it"


def test_the_union_is_the_writers_set_plus_the_fold():
    assigned = service_writes().literals
    patched = buildspec_writes()
    assert assigned, "the project service assigns no cicd_status literal; the walk is broken"
    assert patched, "the buildspec writes no _st <value>; the regex is broken"

    assert set(get_args(CicdStatus)) == assigned | patched | {FOLD}, (
        f"service writes {sorted(assigned)}, buildspec writes {sorted(patched)}, fold {FOLD!r}"
    )


def test_the_fold_is_a_member_no_writer_assigns():
    assert FOLD in get_args(CicdStatus)
    assert FOLD not in service_writes().literals | buildspec_writes()
