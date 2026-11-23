"""Every YAML loader and every YAML parse under ``src`` is safe.

ruff's ``S506`` reads the loader's name in ``yaml.load(..., Loader=...)``, not its bases. Each
listed loader is a ``SafeLoader`` subclass and its constructor form refuses
``!!python/object/apply``, ``!!python/name`` and ``!!python/object``. The listed loaders are every
class under ``src`` built on a PyYAML loader or constructor, a base resolved through the module's
imports and assignments and followed through the classes under ``src`` that derive from one, so a
new loader is a finding until it is listed here. Every ``load``, ``load_all``, ``compose``,
``compose_all``, ``parse`` and ``scan`` call passes ``Loader=`` a listed loader, ``SafeLoader`` or
``CSafeLoader`` through a name its module binds once. A loader is instantiated, and the
``yaml_*`` tables of a stock loader, constructor or resolver are touched, only in a listed loader's
module. A listed loader's ``add_*`` methods and ``yaml_*`` tables are touched, and its attributes
written, only in its own module, and no module reassigns it. PyYAML's unsafe loaders, its unsafe
load functions, its ``add_*`` functions and methods and ``YAMLObject`` are named through ``yaml``
nowhere under ``src``. Not held: a loader or a call reached through ``type()``, ``importlib``,
``getattr``, ``setattr`` or a parameter, or a constructor its own module adds after import.
"""

import ast
import functools
import inspect
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml
from yaml.constructor import ConstructorError

from services import eval_config, mcp_identity_service

SRC = Path(__file__).resolve().parents[1] / "src"
LOADERS = (eval_config._Loader, mcp_identity_service._NoTimestampLoader)
LISTED_LOADERS = {
    f"{loader.__module__}.{loader.__qualname__}": (
        Path(inspect.getfile(loader)).resolve().relative_to(SRC).as_posix()
    )
    for loader in LOADERS
}
PYTHON_OBJECT_DOCUMENTS = (
    "!!python/object/apply:os.getcwd []",
    "!!python/name:os.system",
    "!!python/object:builtins.object {}",
)
SAFE_BASES = {"SafeLoader", "CSafeLoader"}
PARSE_FUNCTIONS = {f"yaml.{name}" for name in ("load", "load_all", "compose", "compose_all", "parse", "scan")}
REFUSED_NAMES = {
    "unsafe_load",
    "unsafe_load_all",
    "full_load",
    "full_load_all",
    "Loader",
    "UnsafeLoader",
    "FullLoader",
    "CLoader",
    "CUnsafeLoader",
    "CFullLoader",
    "Constructor",
    "UnsafeConstructor",
    "FullConstructor",
    "add_constructor",
    "add_multi_constructor",
    "add_implicit_resolver",
    "add_path_resolver",
    "YAMLObject",
    "YAMLObjectMetaclass",
}

Module = tuple[str, str, ast.Module, dict[str, str]]


def _module_name(path: Path) -> str:
    parts = path.relative_to(SRC).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _import_names(node: ast.Import | ast.ImportFrom) -> dict[str, str]:
    if isinstance(node, ast.Import):
        return {
            alias.asname or alias.name.split(".")[0]: alias.name if alias.asname else alias.name.split(".")[0]
            for alias in node.names
        }
    if not node.module:
        return {}
    return {alias.asname or alias.name: f"{node.module}.{alias.name}" for alias in node.names}


def _assigned(node: ast.Assign | ast.AnnAssign) -> Iterator[tuple[ast.expr, ast.expr | None]]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    for target in targets:
        values = node.value
        if isinstance(target, (ast.Tuple, ast.List)) and isinstance(values, (ast.Tuple, ast.List)):
            if len(target.elts) == len(values.elts):
                yield from zip(target.elts, values.elts, strict=True)
                continue
        yield target, values


def _bindings(tree: ast.Module, module: str) -> dict[str, str]:
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(_import_names(node))
    names.update(
        {node.name: f"{module}.{node.name}" for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    )
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target, value in _assigned(node):
                dotted = _dotted(value, names) if value is not None else None
                if dotted and isinstance(target, ast.Name):
                    names[target.id] = dotted
    return names


def _binders(node: ast.AST) -> dict[str, str]:
    where = f"{getattr(node, 'lineno', 0)}:{getattr(node, 'col_offset', 0)}"
    if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
        return {node.id: where}
    if isinstance(node, ast.arg):
        return {node.arg: where}
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name: where}
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return _import_names(node)
    if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
        return {node.name: where}
    return {}


def _bound(tree: ast.Module) -> dict[str, set[str]]:
    bound: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        for name, binder in _binders(node).items():
            bound.setdefault(name, set()).add(binder)
    return bound


def _dotted(node: ast.expr, names: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value, names)
        return f"{head}.{node.attr}" if head else None
    return None


def _is_pyyaml_loader(dotted: str) -> bool:
    return dotted.split(".")[0] == "yaml" and dotted.endswith(("Loader", "Constructor"))


def _is_safe_base(dotted: str) -> bool:
    return dotted.split(".")[0] == "yaml" and dotted.rsplit(".", 1)[-1] in SAFE_BASES


@functools.cache
def _modules() -> tuple[Module, ...]:
    assert (SRC / "main.py").is_file(), f"{SRC} is not the backend's src"
    modules = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        module = _module_name(path)
        modules.append((path.relative_to(SRC).as_posix(), module, tree, _bindings(tree, module)))
    return tuple(modules)


@functools.cache
def _loader_classes() -> tuple[tuple[str, int, str, list[str]], ...]:
    classes: list[tuple[str, int, str, list[str]]] = []
    for relative, module, tree, names in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = [_dotted(base, names) or ast.unparse(base) for base in node.bases]
                classes.append((relative, node.lineno, f"{module}.{node.name}", bases))
    loaders: dict[tuple[str, int], tuple[str, list[str]]] = {}
    grew = True
    while grew:
        grew = False
        known = {qualified for qualified, _ in loaders.values()}
        for relative, line, qualified, bases in classes:
            if (relative, line) not in loaders and any(_is_pyyaml_loader(b) or b in known for b in bases):
                loaders[(relative, line)] = (qualified, bases)
                grew = True
    return tuple(
        (relative, line, qualified, bases) for (relative, line), (qualified, bases) in sorted(loaders.items())
    )


def _names_a_safe_loader(node: ast.expr, names: dict[str, str], bound: dict[str, set[str]]) -> bool:
    root = node
    while isinstance(root, ast.Attribute):
        root = root.value
    dotted = _dotted(node, names) or ""
    return (
        isinstance(root, ast.Name)
        and len(bound.get(root.id, ())) == 1
        and (dotted in LISTED_LOADERS or _is_safe_base(dotted))
    )


def _parse_findings(relative: str, tree: ast.Module, names: dict[str, str]) -> Iterator[str]:
    loaders = {qualified for _, _, qualified, _ in _loader_classes()} | set(LISTED_LOADERS)
    bound = _bound(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _dotted(node.func, names) or ""
        if callee in PARSE_FUNCTIONS:
            loader = next((keyword.value for keyword in node.keywords if keyword.arg == "Loader"), None)
            safe = loader is not None and _names_a_safe_loader(loader, names, bound)
        elif callee in loaders or _is_pyyaml_loader(callee):
            safe = relative in LISTED_LOADERS.values() and _names_a_safe_loader(node.func, names, bound)
        else:
            continue
        if not safe:
            yield f"{relative}:{node.lineno}: {ast.unparse(node)}"


def _refused_findings(relative: str, tree: ast.Module, names: dict[str, str]) -> Iterator[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            referenced = list(_import_names(node).values())
        elif isinstance(node, ast.Attribute):
            referenced = [_dotted(node, names) or ""]
        else:
            continue
        for dotted in referenced:
            if dotted.split(".")[0] == "yaml" and dotted.rsplit(".", 1)[-1] in REFUSED_NAMES:
                yield f"{relative}:{node.lineno}: {dotted}"


def _mutation_findings(relative: str, tree: ast.Module, names: dict[str, str]) -> Iterator[str]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        receiver = _dotted(node.value, names) or ""
        writes = not isinstance(node.ctx, ast.Load)
        touches = writes or node.attr.startswith(("add_", "yaml_"))
        if (
            (receiver in LISTED_LOADERS and touches and relative != LISTED_LOADERS[receiver])
            or (
                receiver.split(".")[0] == "yaml"
                and receiver.endswith(("Loader", "Constructor", "Resolver"))
                and node.attr.startswith("yaml_")
                and relative not in LISTED_LOADERS.values()
            )
            or (f"{receiver}.{node.attr}" in LISTED_LOADERS and writes)
        ):
            yield f"{relative}:{node.lineno}: {ast.unparse(node)}"


def _each_module(findings: Callable[[str, ast.Module, dict[str, str]], Iterator[str]]) -> list[str]:
    return [finding for relative, _, tree, names in _modules() for finding in findings(relative, tree, names)]


@pytest.mark.parametrize("document", PYTHON_OBJECT_DOCUMENTS)
@pytest.mark.parametrize("loader", LOADERS, ids=lambda loader: loader.__name__)
def test_every_source_yaml_loader_refuses_python_object_tags(loader, document):
    where = LISTED_LOADERS[f"{loader.__module__}.{loader.__qualname__}"]
    assert issubclass(loader, yaml.SafeLoader), (
        f"src/{where}: {loader.__qualname__} derives from {[base.__name__ for base in loader.__bases__]}; a "
        "listed loader must derive from yaml.SafeLoader, whose constructor refuses the Python tags"
    )
    with pytest.raises(ConstructorError):
        loader(document).get_single_data()


def test_every_yaml_loader_subclass_in_src_is_listed_and_derives_from_safe_loader():
    census = _loader_classes()
    unsafe = [
        f"{relative}:{line}: {qualified}{bases}"
        for relative, line, qualified, bases in census
        if not all(_is_safe_base(b) for b in bases)
    ]
    assert unsafe == [], (
        "a YAML loader under src does not subclass yaml.SafeLoader or yaml.CSafeLoader directly"
    )
    counts = Counter(qualified for _, _, qualified, _ in census)
    unlisted = [
        f"{relative}:{line}: {qualified}"
        for relative, line, qualified, _ in census
        if LISTED_LOADERS.get(qualified) != relative or counts[qualified] > 1
    ]
    assert unlisted == [], "a YAML loader under src is not in LOADERS, or shares a listed loader's name"
    assert set(counts) == set(LISTED_LOADERS), "a listed loader is not a class statement under src"


def test_every_yaml_parse_in_src_passes_a_listed_loader_its_module_binds_once():
    assert _each_module(_parse_findings) == [], (
        "a YAML parse under src passes no listed loader as Loader=, or a name its module binds twice, "
        "or a loader is instantiated outside a listed loader's own module"
    )


def test_no_unsafe_pyyaml_name_is_referenced_in_src():
    assert _each_module(_refused_findings) == [], (
        "an unsafe PyYAML loader, load function or add_* is named in src"
    )


def test_only_a_listed_loaders_own_module_touches_its_constructors_and_resolvers():
    assert _each_module(_mutation_findings) == [], (
        "a YAML loader's constructors or resolvers are touched outside the listed loader's own module"
    )
