"""Contract tests for the configure-time scrub and the botocore guard in `tests/conftest.py`.

A developer's shell and every GitLab job export variables the suite must not see: an `AWS_PROFILE` or an
`AWS_ENDPOINT_URL` changes what botocore resolves when collection imports `boto3` and `moto`, a
`Settings` field such as `DEBUG` or a `.env` beside the backend changes the `core.config.settings`
snapshot collection builds, and an inherited `AGP_PYTHON` or `CI_COMMIT_BEFORE_SHA` moves what the lint
and CI scripts a test spawns compute. `pytest_configure` calls `_hermetic_shell_environment`, which
deletes every such name before any test module is imported, except the `AWS_PINNED` values it sets and
the `KEPT_SHELL_VARIABLES` names; a test that needs one sets it through `monkeypatch`, and a child
environment built from `os.environ` carries it. The same hook replaces botocore's send, so a request no
stub answered raises `BotocoreSendRefused`, `_no_botocore_send` errors a test that swallowed it, and
`_no_botocore_send_outside_a_test` errors the run's last test when one was swallowed outside every test.
`reset_modules` is pinned to its rule, every module under `api/routes` and the three `core` modules it
names: a route module it keeps, another `core` module it drops, or a named module gone from `src` fails.
Each module it drops leaves its package too, so `from core import config` and `importlib.import_module`
return the one module the next import builds.
"""

import importlib
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import conftest
import pytest
from moto.core.decorator import mock_aws

import core.config
from api.routes import projects as projects_at_collection
from core import aws_clients
from core.config import Settings

BACKEND = Path(__file__).resolve().parents[1]
SRC = BACKEND / "src"
TESTS = BACKEND / "tests"
RESET_CORE_MODULES = {"core.rbac", "core.security_entra", "core.config"}
PLANTED_SHELL = {
    "AWS_PROFILE": "bogus-profile",
    "AWS_ENDPOINT_URL": "http://127.0.0.1:9",
    "AWS_ENDPOINT_URL_STS": "http://127.0.0.1:9",
    "DEBUG": "true",
    "CONTAINMENT_TABLE_NAME": "planted-containment",
    "AGP_PYTHON": "/nonexistent/python",
    "CI_COMMIT_BEFORE_SHA": "0" * 40,
    "FORCE_COLOR": "1",
    "BUILD_SHA": "x",
    "NO_COLOR": "1",
    "CLICOLOR_FORCE": "1",
}
PINNED_AWS = {
    "AWS_EC2_METADATA_DISABLED": "true",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_SHARED_CREDENTIALS_FILE": "/nonexistent/aws/credentials",
    "AWS_CONFIG_FILE": "/nonexistent/aws/config",
    "BOTO_CONFIG": "/nonexistent/boto.cfg",
    "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "true",
}
UNSTUBBED_ENDPOINT = "http://127.0.0.1:9"
SWALLOWS_A_SEND = f'''
from moto.core.decorator import mock_aws

from core import aws_clients


def test_swallows_the_refusal(monkeypatch):
    monkeypatch.setenv("AWS_IGNORE_CONFIGURED_ENDPOINT_URLS", "false")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "{UNSTUBBED_ENDPOINT}")
    with mock_aws():
        try:
            aws_clients.client("secretsmanager", region="us-east-1").list_secrets()
        except Exception:
            pass
'''
SWALLOWS_A_SEND_OUTSIDE_A_TEST = f'''
import os

import pytest
from moto.core.decorator import mock_aws

from core import aws_clients


def swallow_one_send():
    os.environ.update(AWS_IGNORE_CONFIGURED_ENDPOINT_URLS="false", AWS_ENDPOINT_URL="{UNSTUBBED_ENDPOINT}")
    try:
        with mock_aws():
            aws_clients.client("secretsmanager", region="us-east-1").list_secrets()
    except Exception:
        pass
    finally:
        os.environ["AWS_IGNORE_CONFIGURED_ENDPOINT_URLS"] = "true"
        del os.environ["AWS_ENDPOINT_URL"]


swallow_one_send()


@pytest.fixture(scope="module")
def swallows_in_a_module_fixture():
    swallow_one_send()


def test_uses_the_module_fixture(swallows_in_a_module_fixture):
    pass


def test_sends_nothing():
    pass
'''
SHELL_APP_NAME = "LEAKED-FROM-SHELL"
READS_CONFIG_THROUGH_ITS_PACKAGE = f'''
from core import config


def test_the_settings_snapshot_is_built_after_the_scrub():
    assert config.settings.APP_NAME != "{SHELL_APP_NAME}"
'''


def _summary(stdout: str) -> str:
    return stdout.strip().splitlines()[-1]


AT_COLLECTION = conftest._scrubbed_names(os.environ, Settings.model_fields)
AWS_AT_COLLECTION = {k: v for k, v in os.environ.items() if k.startswith("AWS_") or k == "BOTO_CONFIG"}
READ_OUTSIDE_SETTINGS = ("BUILD_SHA", "CLICOLOR_FORCE", "FORCE_COLOR", "NO_COLOR")
OUTSIDE_SETTINGS_AT_COLLECTION = [name for name in READ_OUTSIDE_SETTINGS if name in os.environ]
SNAPSHOT_DEBUG = core.config.settings.DEBUG


def test_no_shell_value_reached_collection():
    assert AT_COLLECTION == {}
    assert SNAPSHOT_DEBUG is False


def test_no_settings_field_reaches_a_test():
    assert set(Settings.model_fields) & set(os.environ) == set()


def test_no_agp_or_ci_variable_but_the_kept_two_reaches_a_test():
    inherited = {key for key in os.environ if key.startswith(("AGP_", "CI_"))}
    assert inherited <= conftest.KEPT_SHELL_VARIABLES, sorted(inherited - conftest.KEPT_SHELL_VARIABLES)


def test_the_aws_names_at_collection_are_exactly_the_pinned_set():
    assert AWS_AT_COLLECTION == conftest.AWS_PINNED
    assert conftest.AWS_PINNED == PINNED_AWS


def test_no_name_read_outside_settings_reaches_collection_or_a_test():
    assert OUTSIDE_SETTINGS_AT_COLLECTION == []
    assert [name for name in READ_OUTSIDE_SETTINGS if name in os.environ] == []


def test_a_planted_shell_reaches_neither_collection_nor_a_test(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("APP_NAME=planted\nDEBUG=true\n")
    tests = (
        test_no_shell_value_reached_collection,
        test_no_settings_field_reaches_a_test,
        test_no_agp_or_ci_variable_but_the_kept_two_reaches_a_test,
        test_the_aws_names_at_collection_are_exactly_the_pinned_set,
        test_no_name_read_outside_settings_reaches_collection_or_a_test,
    )
    child = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            *(f"{Path(__file__).resolve()}::{test.__name__}" for test in tests),
        ],
        env={**os.environ, **PLANTED_SHELL, "ENV_FILE": str(env_file)},
        cwd=BACKEND,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert child.returncode == 0, child.stdout[-3000:]
    assert re.search(r"\b5 passed\b", _summary(child.stdout)), child.stdout[-3000:]


def test_botocore_refuses_to_send_past_every_stub(monkeypatch):
    monkeypatch.setenv("AWS_IGNORE_CONFIGURED_ENDPOINT_URLS", "false")
    monkeypatch.setenv("AWS_ENDPOINT_URL", UNSTUBBED_ENDPOINT)
    with mock_aws():
        secrets = aws_clients.client("secretsmanager", region="us-east-1")
        with pytest.raises(conftest.BotocoreSendRefused):
            secrets.list_secrets()
    assert conftest._REFUSED_SENDS.pop() == f"POST {UNSTUBBED_ENDPOINT}/"


def test_a_swallowed_send_still_errors_at_teardown(tmp_path):
    (tmp_path / "test_swallows.py").write_text(SWALLOWS_A_SEND)
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
    summary = _summary(child.stdout)
    assert re.search(r"\b1 passed\b", summary), child.stdout[-3000:]
    assert re.search(r"\b1 error\b", summary), child.stdout[-3000:]
    assert f"botocore sent past every stub: POST {UNSTUBBED_ENDPOINT}/" in child.stdout


def test_a_send_swallowed_outside_every_test_still_fails_the_run(tmp_path):
    (tmp_path / "test_swallows_outside.py").write_text(SWALLOWS_A_SEND_OUTSIDE_A_TEST)
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
    summary = _summary(child.stdout)
    assert child.returncode != 0, child.stdout[-3000:]
    assert re.search(r"\b2 passed\b", summary), child.stdout[-3000:]
    assert re.search(r"\b1 error\b", summary), child.stdout[-3000:]
    sends = f"POST {UNSTUBBED_ENDPOINT}/; POST {UNSTUBBED_ENDPOINT}/"
    assert f"botocore sent past every stub outside a test: {sends}" in child.stdout, child.stdout[-3000:]


def test_a_module_importing_config_from_its_package_reads_the_scrubbed_snapshot(tmp_path):
    (tmp_path / "test_reads_config.py").write_text(READS_CONFIG_THROUGH_ITS_PACKAGE)
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
        env={**os.environ, "APP_NAME": SHELL_APP_NAME, "PYTHONPATH": os.pathsep.join([str(SRC), str(TESTS)])},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert child.returncode == 0, child.stdout[-3000:]
    assert re.search(r"\b1 passed\b", _summary(child.stdout)), child.stdout[-3000:]


def test_the_kept_variables_are_exactly_the_two_with_reasons():
    assert conftest.KEPT_SHELL_VARIABLES == {"AGP_REPO_ROOT", "AGP_UPDATE_OPENAPI_SNAPSHOT"}
    docstring = conftest._hermetic_shell_environment.__doc__ or ""
    assert [name for name in sorted(conftest.KEPT_SHELL_VARIABLES) if name not in docstring] == []


def test_a_test_may_still_set_its_own_agp_variable(monkeypatch):
    monkeypatch.setenv("AGP_PYTHON", sys.executable)
    assert os.environ["AGP_PYTHON"] == sys.executable
    child = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ['AGP_PYTHON'])"],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert child.stdout == f"{sys.executable}\n"
    monkeypatch.undo()
    assert "AGP_PYTHON" not in os.environ


def test_reset_modules_pops_exactly_the_modules_it_names(request, monkeypatch):
    candidates = {
        ".".join(path.relative_to(SRC).with_suffix("").parts)
        for root in (SRC / "api/routes", SRC / "core")
        for path in root.rglob("*.py")
        if path.stem != "__init__"
    }
    for name in candidates:
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    monkeypatch.delitem(sys.modules, "api.routes", raising=False)
    monkeypatch.delitem(sys.modules, "core", raising=False)
    routes = {name for name in candidates if name.startswith("api.routes.")}
    request.getfixturevalue("reset_modules")
    assert routes
    assert {name for name in candidates if name not in sys.modules} == routes | RESET_CORE_MODULES


@pytest.mark.usefixtures("reset_modules")
def test_reset_modules_unbinds_each_popped_module_from_its_package():
    from core import config

    assert config is importlib.import_module("core.config")
    from api.routes import projects

    assert projects is importlib.import_module("api.routes.projects")
    assert projects is not projects_at_collection
