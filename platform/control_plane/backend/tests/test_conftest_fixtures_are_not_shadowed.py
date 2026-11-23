"""Contract test: no test module redefines a fixture `tests/conftest.py` defines.

A copy in a test module shadows conftest's for that module alone, so its tests read a fixture other than
the one their parameter names, and a change to conftest's never reaches them. A fixture that differs from
conftest's carries a name that says how.

Two shapes are not seen: a decorator built with `functools.partial(pytest.fixture, ...)`, and a `conftest.py`
below `tests/`, since only `test_*.py` files are read.
"""

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent
PLANTED = {
    "test_plain.py": "import pytest\n\n\n@pytest.fixture\ndef entra_settings(): ...\n",
    "test_autouse.py": "import pytest\n\n\n@pytest.fixture(autouse=True)\ndef reset_modules(): ...\n",
    "test_named.py": 'import pytest\n\n\n@pytest.fixture(name="role_svc")\ndef _role(): ...\n',
    "nested/test_async.py": "from pytest import fixture\n\n\n@fixture\nasync def build_svc(): ...\n",
    "test_class.py": "import pytest\n\n\nclass TestX:\n    @pytest.fixture\n    def project_svc(self): ...\n",
    "test_assigned.py": "import pytest\n\nclient_factory = pytest.fixture(lambda: None)\n",
    "test_aliased.py": "from pytest import fixture as fx\n\n\n@fx\ndef scaffold_dir(): ...\n",
}


def _names_bound_to_fixture(tree: ast.Module) -> set[str]:
    return {"fixture"} | {
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name == "fixture" and alias.asname
    }


def _is_fixture(node: ast.expr, bound: set[str]) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    return (isinstance(target, ast.Attribute) and target.attr == "fixture") or (
        isinstance(target, ast.Name) and target.id in bound
    )


def _alias(node: ast.expr) -> str | None:
    for keyword in node.keywords if isinstance(node, ast.Call) else []:
        if (
            keyword.arg == "name"
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
        ):
            return keyword.value.value
    return None


def _fixture_names(source: str) -> set[str]:
    names: set[str] = set()
    tree = ast.parse(source)
    bound = _names_bound_to_fixture(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.update(
                _alias(decorator) or node.name
                for decorator in node.decorator_list
                if _is_fixture(decorator, bound)
            )
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call):
            call = node.value if _is_fixture(node.value, bound) else node.value.func
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if _is_fixture(call, bound):
                names.update(_alias(call) or target.id for target in targets if isinstance(target, ast.Name))
    return names


CONFTEST_FIXTURES = _fixture_names((TESTS / "conftest.py").read_text())


def _shadows(root: Path) -> list[str]:
    return sorted(
        f"{path.relative_to(root).as_posix()}::{name}"
        for path in root.rglob("test_*.py")
        for name in _fixture_names(path.read_text()) & CONFTEST_FIXTURES
    )


def test_no_test_module_redefines_a_conftest_fixture():
    shadows = _shadows(TESTS)
    assert shadows == [], f"{len(shadows)} fixtures shadow conftest's: {shadows}"


def test_every_planted_shadow_shape_is_seen(tmp_path):
    for relative, source in PLANTED.items():
        (tmp_path / relative).parent.mkdir(exist_ok=True)
        (tmp_path / relative).write_text(source)
    assert _shadows(tmp_path) == [
        "nested/test_async.py::build_svc",
        "test_aliased.py::scaffold_dir",
        "test_assigned.py::client_factory",
        "test_autouse.py::reset_modules",
        "test_class.py::project_svc",
        "test_named.py::role_svc",
        "test_plain.py::entra_settings",
    ]
