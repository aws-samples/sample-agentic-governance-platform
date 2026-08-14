"""Offline tests for the E32 registry bootstrap (no AWS calls — injected client)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts.ensure_registry import ensure_registry, main

ARN = "arn:aws:agent-registry:us-east-1:123456789012:registry/REG123"


def _ctl(existing=None):
    ctl = MagicMock()
    ctl.get_paginator.return_value.paginate.return_value = [
        {"registries": existing or []}
    ]
    ctl.create_registry.return_value = {"registryArn": ARN}
    ctl.get_waiter.return_value = MagicMock()
    return ctl


def test_creates_when_absent_and_waits_ready():
    ctl = _ctl()
    assert ensure_registry("agp-agents", "us-east-1", ctl=ctl) == "REG123"
    kwargs = ctl.create_registry.call_args.kwargs
    assert kwargs["name"] == "agp-agents"
    assert kwargs["discoveryConfiguration"] == {"authorizerType": "AWS_IAM"}
    assert kwargs["approvalConfiguration"] == {"autoApprovalRules": []}
    assert len(kwargs["clientToken"]) >= 33
    ctl.get_waiter.assert_called_with("registry_ready")


def test_is_idempotent_when_present():
    ctl = _ctl([{"name": "agp-agents", "registryId": "EXISTING1", "registryArn": ARN}])
    assert ensure_registry("agp-agents", "us-east-1", ctl=ctl) == "EXISTING1"
    ctl.create_registry.assert_not_called()


def test_create_registry_params_pass_botocore_param_validation():
    """Guard the mock↔live seam on the NEW ``CreateRegistry`` call.

    Every test above injects a ``MagicMock``, which accepts ANY kwarg name — so a typo
    (``authorizerTyp``, ``autoApprovalRule``) would pass them all and first surface at
    Terraform's real bootstrap apply, the one path with no offline safety net. Same idiom as
    ``test_registry_update_param_shape.py::_assert_valid`` and
    ``test_create_policy_engine_client_token_passes_botocore_param_validation``: capture the
    EXACT kwargs our code passes, then feed them to the real ``ParamValidator`` for the
    operation's input shape. Offline — no AWS call, the model ships with botocore.
    """
    from botocore.session import get_session
    from botocore.validate import ParamValidator

    ctl = _ctl()
    ensure_registry("agp-agents", "us-east-1", ctl=ctl)

    captured = ctl.create_registry.call_args.kwargs
    shape = (
        get_session()
        .get_service_model("agent-registry-control")
        .operation_model("CreateRegistry")
        .input_shape
    )
    report = ParamValidator().validate(captured, shape)
    assert not report.has_errors(), report.generate_report()
    # The specific length floor the deterministic uuid5 token exists to clear (min 33).
    assert len(captured["clientToken"]) >= 33


def test_json_mode_emits_exactly_one_json_line(capsys, monkeypatch):
    monkeypatch.setattr("scripts.ensure_registry._client", lambda region: _ctl())
    assert main(["--name", "agp-agents", "--region", "us-east-1", "--json"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    assert json.loads(out[0]) == {
        "registry_id": "REG123",
        "registry_arn": ARN,
        "name": "agp-agents",
        "region": "us-east-1",
    }


# --- Terraform's stdout contract, proved in a real subprocess -------------------------
#
# The three tests above run in-process, where pytest's logging plugin installs its own
# root handler. That makes `logging.basicConfig(stream=sys.stderr, ...)` a no-op, so an
# in-process test can never actually prove that log records stay off stdout. Task 6's
# Terraform module parses stdout at the file-descriptor level, so the purity contract is
# verified the same way: run the real CLI in a subprocess with `_client` swapped for a
# MagicMock (still zero AWS calls) and inspect the real fds.

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_DRIVER = """
import sys
from unittest.mock import MagicMock

import scripts.ensure_registry as m

ARN = {arn!r}
FAIL = {fail!r}


def _fake_client(region):
    ctl = MagicMock()
    ctl.get_paginator.return_value.paginate.return_value = [{{"registries": {existing!r}}}]
    ctl.create_registry.return_value = {{"registryArn": ARN}}
    if FAIL:
        ctl.create_registry.side_effect = RuntimeError("boom")
    ctl.get_waiter.return_value = MagicMock()
    return ctl


m._client = _fake_client
sys.exit(m.main(sys.argv[1:]))
"""


def _run_cli(argv, existing=None, fail=False):
    """Run the CLI in a subprocess against an injected fake client. Returns CompletedProcess."""
    env = dict(os.environ)
    # The `:.` is what makes `scripts.ensure_registry` importable (scripts/ has no
    # __init__.py — it resolves as a namespace package from the backend dir).
    env["PYTHONPATH"] = f"{_BACKEND_DIR / 'src'}:{_BACKEND_DIR}"
    return subprocess.run(
        [sys.executable, "-c", _DRIVER.format(arn=ARN, existing=existing or [], fail=fail), *argv],
        cwd=_BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
    )


def test_json_mode_stdout_stays_pure_json_at_verbose():
    """`--json --verbose` together: DEBUG logging must not leak a byte onto stdout.

    The registry is found rather than created here so the per-item DEBUG lines inside
    `find_registry_by_name` also fire — logging at its loudest.
    """
    proc = _run_cli(
        ["--name", "agp-agents", "--region", "us-east-1", "--json", "--verbose"],
        existing=[{"name": "agp-agents", "registryId": "REG123", "registryArn": ARN}],
    )
    if proc.returncode != 0:
        pytest.fail(f"CLI exited {proc.returncode}; stderr:\n{proc.stderr}")

    lines = proc.stdout.strip().splitlines()
    assert len(lines) == 1, f"stdout was not exactly one line: {proc.stdout!r}"
    assert json.loads(lines[0]) == {
        "registry_id": "REG123",
        "registry_arn": ARN,
        "name": "agp-agents",
        "region": "us-east-1",
    }
    # Prove logging really was loud, and that all of it landed on stderr.
    assert "DEBUG" in proc.stderr
    assert "registry name=" in proc.stderr


def test_failure_exits_nonzero_with_one_stderr_line_and_empty_stdout():
    """A failure must be one actionable stderr line + non-zero exit, never a traceback."""
    proc = _run_cli(["--name", "agp-agents", "--region", "us-east-1", "--json"], fail=True)
    assert proc.returncode != 0
    assert proc.stdout == "", f"stdout must stay empty on failure: {proc.stdout!r}"
    # Progress INFO lines legitimately precede it; the *error* must be a single line and
    # must not be a raw traceback.
    errors = [ln for ln in proc.stderr.splitlines() if " ERROR " in ln]
    assert len(errors) == 1, f"expected one ERROR line, got {errors!r}"
    assert "boom" in errors[0]
    assert "Traceback" not in proc.stderr
