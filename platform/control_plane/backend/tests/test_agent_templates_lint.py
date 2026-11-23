"""Each agent template passes its own `ruff check src tests` under its own `pyproject.toml`, the way
the `build.yml` it ships runs it.

The mistake this pins: `9df5f76c` landed `hashlib.sha1(full.encode())` in the Strands template's
`src/mcp_runtime.py`; the template's own `select` carries `S`, so `S324` failed the lint step of
every repository materialized from the template for a day, until `22cdf446`. The template files
ship verbatim into customer repositories, where that lint step is the first build.

Ruff runs from this venv (`sys.executable -m ruff`) with the template directory as the working
directory, so the template's `[tool.ruff]` applies exactly as in its workflow, and `--no-cache`
leaves no `.ruff_cache` behind. The venv's ruff is 0.17.1 against the templates' `>=0.16,<0.17`:
the explicit `select` bounds the drift and both carry `S324`; aligning the pin is the templates'
owner's call. Ladder rung 2: the test fails at `22cdf446^` and passes at `22cdf446`. Rung 1 would
run the templates' gate from the templates themselves, an edit under `agent-templates/`, outside
this lane. The three templates with Python sources are listed; the gateway and harness templates
carry no `src`.
"""

import functools
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = (
    Path(os.environ["AGP_REPO_ROOT"])
    if "AGP_REPO_ROOT" in os.environ
    else Path(__file__).resolve().parents[4]
)

TEMPLATES: tuple[str, ...] = (
    "platform/control_plane/agent-templates/strands-agentcore",
    "platform/control_plane/agent-templates/langgraph-agentcore",
    "platform/control_plane/agent-templates/crewai-agentcore",
)
RUFF = [sys.executable, "-m", "ruff"]
LINT_TARGETS = ("src", "tests")


@functools.cache
def ruff_available() -> bool:
    version = subprocess.run([*RUFF, "--version"], capture_output=True, text=True, timeout=60)
    return version.returncode == 0


def ruff_check(template_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*RUFF, "check", "--no-cache", *LINT_TARGETS],
        cwd=template_dir,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize("template", TEMPLATES, ids=[t.rsplit("/", 1)[-1] for t in TEMPLATES])
def test_each_template_passes_its_own_ruff(template):
    if not ruff_available():
        pytest.skip("ruff is not importable: pip install -r requirements-dev.txt")
    result = ruff_check(REPO_ROOT / template)
    assert result.returncode == 0, (
        f"{template}: its own 'ruff check src tests' fails; every repo materialized from this template "
        "fails its first build at lint. Fix the finding in the template under the rule set its own "
        "[tool.ruff.lint] select names.\n"
        f"{result.stdout}{result.stderr}"
    )


def test_the_scan_has_something_to_scan():
    for template in TEMPLATES:
        root = REPO_ROOT / template
        assert "[tool.ruff" in (root / "pyproject.toml").read_text(), template
        for target in LINT_TARGETS:
            assert list((root / target).glob("*.py")), (template, target)
