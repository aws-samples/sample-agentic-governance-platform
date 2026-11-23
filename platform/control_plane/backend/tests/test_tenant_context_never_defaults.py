"""A ``TenantContext`` parameter never has a default, so a scoped read cannot be called without its scope.

This census holds the requirement over ``src``: a parameter named ``ctx``, or annotated
``TenantContext`` (bare, ``Optional[...]``, ``... | None`` or quoted), whose default is anything but
``RBACDepends(get_tenant_ctx)`` or ``Depends(get_tenant_ctx)`` is a finding naming ``file:line
function``. The census itself is checked against the planted shapes in
``tests/fixtures/tenant_context/defaults_fixture.py``.

Not held, said so: a scope read from ``**kwargs`` through ``kwargs.get("ctx")``, an alias annotation
(``TC = TenantContext``), a dataclass or pydantic field default, and a lambda's defaults.
"""

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "tenant_context"

DEPENDENCY_FACTORIES = frozenset({"RBACDepends", "Depends"})
TENANT_SCOPE_PROVIDER = "get_tenant_ctx"
TENANT_CONTEXT_RE = re.compile(r"\bTenantContext\b")

FIXTURE_FINDINGS = {
    "kw_none",
    "global_default",
    "positional_none",
    "optional_none",
    "union_none",
    "quoted_none",
    "renamed_default",
    "qualified_default",
    "lambda_dependency",
    "other_provider",
    "bare_dependency",
    "wrapped_provider",
}
FIXTURE_DEPENDENCY_CONTROLS = {"scoped_route", "plain_route"}
FIXTURE_CONTROLS = {"required_kw"} | FIXTURE_DEPENDENCY_CONTROLS


def _names_tenant_context(annotation: ast.expr | None) -> bool:
    if annotation is None:
        return False
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name) and node.id == "TenantContext":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "TenantContext":
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if TENANT_CONTEXT_RE.search(node.value):
                return True
    return False


def _names_the_tenant_scope_provider(node: ast.expr) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr == TENANT_SCOPE_PROVIDER
    return isinstance(node, ast.Name) and node.id == TENANT_SCOPE_PROVIDER


def _is_tenant_scope_dependency(default: ast.expr) -> bool:
    if not isinstance(default, ast.Call) or len(default.args) != 1:
        return False
    func = default.func
    if isinstance(func, ast.Attribute):
        factory = func.attr in DEPENDENCY_FACTORIES
    else:
        factory = isinstance(func, ast.Name) and func.id in DEPENDENCY_FACTORIES
    return factory and _names_the_tenant_scope_provider(default.args[0])


def _defaulted_parameters(args: ast.arguments) -> list[tuple[ast.arg, ast.expr]]:
    positional = [*args.posonlyargs, *args.args]
    with_default = positional[len(positional) - len(args.defaults) :]
    pairs = list(zip(with_default, args.defaults, strict=True))
    pairs += [(arg, d) for arg, d in zip(args.kwonlyargs, args.kw_defaults, strict=True) if d is not None]
    return pairs


def tenant_context_defaults(root: Path) -> tuple[list[str], int]:
    """Every parameter under ``root`` named ``ctx`` or annotated ``TenantContext`` whose default is not
    the tenant-scope dependency, as ``file:line function``, and the count of those whose default is."""
    findings = []
    dependencies = 0
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for arg, default in _defaulted_parameters(fn.args):
                if not (arg.arg == "ctx" or _names_tenant_context(arg.annotation)):
                    continue
                if _is_tenant_scope_dependency(default):
                    dependencies += 1
                else:
                    findings.append(f"{path.relative_to(root)}:{fn.lineno} {fn.name}")
    return findings, dependencies


def test_no_tenant_context_parameter_has_a_default():
    assert SRC.is_dir(), f"the census root {SRC} is not a directory"
    findings, dependencies = tenant_context_defaults(SRC)
    assert dependencies > 0, f"the census saw no RBACDepends(get_tenant_ctx) parameter under {SRC}"
    assert findings == [], (
        "a TenantContext parameter has a default, so a caller that omits it reads unscoped; make it a "
        "required keyword (`*, ctx: TenantContext`) or an RBACDepends(get_tenant_ctx) dependency:\n"
        + "\n".join(findings)
    )


def test_the_default_census_sees_every_planted_shape():
    tree = ast.parse((FIXTURES / "defaults_fixture.py").read_text())
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
    assert {fn.name for fn in functions} == FIXTURE_FINDINGS | FIXTURE_CONTROLS
    planted = [fn for fn in functions if fn.name in FIXTURE_FINDINGS]
    expected = sorted(f"defaults_fixture.py:{fn.lineno} {fn.name}" for fn in planted)
    findings, dependencies = tenant_context_defaults(FIXTURES)
    assert sorted(findings) == expected
    assert dependencies == len(FIXTURE_DEPENDENCY_CONTROLS)
