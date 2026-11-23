"""Contract test for `_app_modules_restored` in `tests/conftest.py`.

The planted pair pops the children under their kept packages before it evicts the whole namespace, because a
whole-namespace import builds new packages and leaves the restored ones pointing at their own children.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SRC = BACKEND / "src"
TESTS = BACKEND / "tests"
EVICTS = """
import importlib
import sys

NAMESPACE = ("api", "core", "models", "services", "main")


def test_pops_a_route_and_the_config_and_keeps_their_packages():
    importlib.import_module("core.security_entra")
    for name in ("api.routes.grants", "core.config"):
        sys.modules.pop(name)
        importlib.import_module(name)


def test_evicts_and_reimports_the_whole_namespace():
    evicted = sorted(name for name in sys.modules if name.partition(".")[0] in NAMESPACE)
    for name in evicted:
        del sys.modules[name]
    for name in evicted:
        importlib.import_module(name)
"""
READS = """
import sys

import api.routes.grants as grants_attr
import core
from core import config as bound


def test_the_collection_time_bindings_are_the_live_modules():
    from api.routes import grants

    assert sys.modules["core.config"] is bound
    assert core.config is bound
    assert grants is grants_attr is sys.modules["api.routes.grants"]


def test_a_module_the_evictor_imported_first_is_imported_afresh():
    from core import security_entra

    assert security_entra is sys.modules.get("core.security_entra")
"""


def test_a_module_that_evicts_the_app_leaves_the_next_module_its_collection_time_modules(tmp_path):
    (tmp_path / "test_a_evicts.py").write_text(EVICTS)
    (tmp_path / "test_b_reads.py").write_text(READS)
    child = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "conftest",
            "-c",
            os.devnull,
            "--rootdir",
            str(tmp_path),
            str(tmp_path),
        ],
        env={**os.environ, "PYTHONPATH": os.pathsep.join([str(SRC), str(TESTS)])},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert child.returncode == 0, child.stdout[-3000:]
    assert re.search(r"\b4 passed\b", child.stdout.strip().splitlines()[-1]), child.stdout[-3000:]
