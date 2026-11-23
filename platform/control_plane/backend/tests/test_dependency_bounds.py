"""Direct dependencies in every shipped manifest carry an upper bound, the templates and the
applications agree on each ceiling, and every pip-audit ignore in a template workflow says why and
until when.

The mistake this pins: an unbounded floor in a manifest that ships verbatim into customer
repositories or into the demo agents, so a new major breaks every agent built from it at its first
build. `0412cfcc` capped `mcp` in the Strands template after 2.x renamed an import and every
materialized repository's test collection errored; `dcde525e` capped it in the four applications
three days later, after every demo agent crashed at cold start while the template was already
capped and its four siblings floated; `f3c95107` capped an OpenTelemetry instrumentation and dated
the crewai pip-audit ignores. The first instance in this repo was a dev extra (`ruff>=0.6` floating
to 0.16 broke every materialized lint job at once), so the optional groups are in scope.

Text-level on purpose: the manifests are read as pip and setuptools read them (`requirements.txt`
lines with comments stripped and `-r`, `-c` and `--` lines skipped; `pyproject.toml` through
`tomllib`, `project.dependencies` and every `project.optional-dependencies` group), with a stdlib
regex over the operators, so the suite needs no resolver and no `packaging`. The manifests are
discovered, not listed: every file `git ls-files` names whose basename matches `*requirements*.txt`,
every `.txt` under a `requirements/` directory and every `pyproject.toml` declaring dependencies is
scanned, a `VENDORED_MANIFESTS` entry aside with the reason nothing shipped installs from it; the
templates are the `agent-templates/<name>/pyproject.toml` files among them and the applications the
`applications/<name>/requirements.txt` files, so a copied application is scanned and compared from
the moment it is tracked, and a tracked manifest missing from the checkout is an error, not a skip.
`[dependency-groups]`, `setup.py`, `setup.cfg` and `Pipfile` are not held. A specifier is bounded
when one clause uses `<`, `<=`, `==` or `~=`. A package bounded in one manifest and unbounded in
another is the bound check's finding, so the ceiling check compares only lines that carry a `<` or
`<=` bound, and only among the manifests that ship together: the templates and the applications.
Ladder rung 2: the bound check fails at `0412cfcc^`, `dcde525e^` and `f3c95107^`. Rung 1 is each
template's `constraints.txt`, read with the same requirements parser: every direct dependency pinned
`==` and no build tool pinned, so a new major reaches no image until the file is regenerated. No
offline predicate exists for an advisory inside an already bounded range (`52f11433`): the
templates' own `pip-audit` with network is the catch.
"""

import functools
import os
import re
import shutil
import subprocess
import tomllib
from collections import namedtuple
from fnmatch import fnmatch
from pathlib import Path

REPO_ROOT = (
    Path(os.environ["AGP_REPO_ROOT"])
    if "AGP_REPO_ROOT" in os.environ
    else Path(__file__).resolve().parents[4]
)

GIT = shutil.which("git") or "git"
TEMPLATE_HOME = Path("platform/control_plane/agent-templates")
APPLICATION_HOME = Path("applications")
VENDORED_MANIFESTS: dict[str, str] = {
    ".claude/skills/draw-io-minimized/scripts/requirements.txt": (
        "the draw-io skill's own tooling, installed by hand on a contributor's machine; nothing "
        "shipped installs from it"
    ),
}

Manifests = namedtuple("Manifests", "templates applications templates_and_applications shipped")


def _is_manifest(root: Path, path: str) -> bool:
    *directories, name = Path(path).parts
    if fnmatch(name, "*requirements*.txt") or (name.endswith(".txt") and "requirements" in directories):
        return True
    if name != "pyproject.toml":
        return False
    project = tomllib.loads((root / path).read_text()).get("project", {})
    return "dependencies" in project or "optional-dependencies" in project


def _git(root: Path, *arguments: str) -> str:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(
        [GIT, *arguments], capture_output=True, text=True, check=True, timeout=60, cwd=root, env=environment
    ).stdout


@functools.cache
def tracked_files(root: Path = REPO_ROOT) -> tuple[str, ...]:
    return tuple(_git(root, "ls-files").splitlines())


@functools.cache
def discover(root: Path = REPO_ROOT) -> Manifests:
    shipped = tuple(
        path for path in tracked_files(root) if path not in VENDORED_MANIFESTS and _is_manifest(root, path)
    )
    templates = tuple(
        str(Path(path).parent)
        for path in shipped
        if Path(path).name == "pyproject.toml" and Path(path).parent.parent == TEMPLATE_HOME
    )
    applications = tuple(
        path
        for path in shipped
        if Path(path).name == "requirements.txt" and Path(path).parent.parent == APPLICATION_HOME
    )
    together = (*(f"{template}/pyproject.toml" for template in templates), *applications)
    return Manifests(templates, applications, together, shipped)


TEMPLATES, APPLICATIONS, TEMPLATES_AND_APPLICATIONS, SHIPPED_MANIFESTS = discover(REPO_ROOT)
WORKFLOW = ".github/workflows/build.yml"
CONSTRAINTS = "constraints.txt"
COMPILE_COMMAND = (
    "uv pip compile pyproject.toml --universal --python-version 3.11 --all-extras "
    "--no-emit-package setuptools --no-emit-package pip --no-emit-package wheel -o constraints.txt"
)
BUILD_TOOLS = frozenset({"setuptools", "pip", "wheel"})

Requirement = namedtuple("Requirement", "manifest group name extras specifier raw")
Finding = namedtuple("Finding", "key message")

KNOWN_UNBOUNDED: dict[tuple[str, str, str], str] = {
    (
        "platform/control_plane/agent-templates/strands-agentcore/pyproject.toml",
        "dependencies",
        "boto3",
    ): "templates owner",
    (
        "platform/control_plane/agent-templates/strands-agentcore/pyproject.toml",
        "dev",
        "pytest",
    ): "templates owner",
    (
        "platform/control_plane/agent-templates/strands-agentcore/pyproject.toml",
        "dev",
        "pip-audit",
    ): "templates owner",
    (
        "platform/control_plane/agent-templates/langgraph-agentcore/pyproject.toml",
        "dependencies",
        "boto3",
    ): "templates owner",
    (
        "platform/control_plane/agent-templates/langgraph-agentcore/pyproject.toml",
        "dev",
        "pytest",
    ): "templates owner",
    (
        "platform/control_plane/agent-templates/langgraph-agentcore/pyproject.toml",
        "dev",
        "pip-audit",
    ): "templates owner",
    (
        "platform/control_plane/agent-templates/crewai-agentcore/pyproject.toml",
        "dependencies",
        "opentelemetry-instrumentation-crewai",
    ): "templates owner",
    (
        "platform/control_plane/agent-templates/crewai-agentcore/pyproject.toml",
        "dependencies",
        "boto3",
    ): "templates owner",
    (
        "platform/control_plane/agent-templates/crewai-agentcore/pyproject.toml",
        "dev",
        "pytest",
    ): "templates owner",
    (
        "platform/control_plane/agent-templates/crewai-agentcore/pyproject.toml",
        "dev",
        "pip-audit",
    ): "templates owner",
}

UNBOUNDED_FIX = (
    "a direct dependency needs an upper bound (<, <=, == or ~=); an unbounded floor let mcp 2.0 "
    "import-fail every agent built from this file; cap the next major you have not tested"
)
DRIFT_FIX = (
    "the templates and the applications ship the same agent stack, pin the same ceiling "
    "(dcde525e: capped in the template, floating in the applications for three days)"
)
IGNORE_FIX = (
    "put a comment on the step naming the advisory and why this agent never reaches it, "
    "and a `review by YYYY-MM-DD` line to re-run the audit without the ignore"
)
UNPINNED_FIX = (
    f"pip installs a name the constraints file does not pin unconstrained and without a warning; after "
    f"editing pyproject.toml run `{COMPILE_COMMAND}` in the template and commit both files"
)
BUILD_TOOL_FIX = (
    f"a pinned build tool fights the workflow's setuptools upgrade step; regenerate with `{COMPILE_COMMAND}`"
)
BOUNDING_OPERATORS = frozenset({"<", "<=", "==", "~="})
SKIPPED_LINE_PREFIXES = ("-r", "-c", "--")
SHIPPED_REQUIREMENT_FLOOR = 40
WORKFLOW_IGNORE_FLOOR = 1

_COMMENT = re.compile(r"(^|\s)#.*$")
_NAME = re.compile(r"^([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(\[[^\]]*\])?\s*(.*?)\s*$")
_OPERATOR = re.compile(r"^(==|~=|!=|<=|>=|<|>)\s*(.*)$")
_PIN = re.compile(r"^==[^,*]+$")
_VERSION = re.compile(r"^\d+(?:\.\d+)*")
_IGNORE_VULN = re.compile(r"--ignore-vuln[= ]+(\S+)")
_STEP_START = re.compile(r"^\s*-\s*name:")
_REVIEW_BY = re.compile(r"review[- ]by\s+(\d{4}-\d{2}-\d{2})", re.IGNORECASE)


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirement_line(line: str, manifest: str, group: str) -> Requirement | None:
    raw = line.strip()
    match = _NAME.match(raw.split(";", 1)[0].strip())
    if not match:
        return None
    name, extras, specifier = match.groups()
    extras = extras[1:-1].replace(" ", "") if extras else ""
    return Requirement(manifest, group, normalize(name), extras, specifier.replace(" ", ""), raw)


def _logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    pending = ""
    for line in text.splitlines():
        if line.rstrip().endswith("\\") and not line.lstrip().startswith("#"):
            pending += line.rstrip()[:-1]
            continue
        lines.append(pending + line)
        pending = ""
    if pending:
        lines.append(pending)
    return lines


def parse_requirements(text: str, manifest: str) -> list[Requirement]:
    requirements: list[Requirement] = []
    for line in _logical_lines(text):
        body = _COMMENT.sub("", line).strip()
        if not body or body.startswith(SKIPPED_LINE_PREFIXES):
            continue
        parsed = parse_requirement_line(body, manifest, "requirements")
        if parsed is not None:
            requirements.append(parsed)
    return requirements


def parse_pyproject(text: str, manifest: str) -> list[Requirement]:
    project = tomllib.loads(text).get("project", {})
    groups = [("dependencies", project.get("dependencies", []))]
    groups.extend(project.get("optional-dependencies", {}).items())
    requirements: list[Requirement] = []
    for group, lines in groups:
        for line in lines:
            parsed = parse_requirement_line(line, manifest, group)
            if parsed is not None:
                requirements.append(parsed)
    return requirements


def _clauses(specifier: str) -> list[tuple[str, str]]:
    clauses: list[tuple[str, str]] = []
    for clause in specifier.split(","):
        match = _OPERATOR.match(clause.strip())
        if match:
            clauses.append((match.group(1), match.group(2)))
    return clauses


def is_bounded(specifier: str) -> bool:
    return any(operator in BOUNDING_OPERATORS for operator, _ in _clauses(specifier))


def ceiling(specifier: str) -> tuple[int, ...] | None:
    for operator, version in _clauses(specifier):
        match = _VERSION.match(version) if operator in ("<", "<=") else None
        if match:
            parts = [int(part) for part in match.group(0).split(".")]
            while len(parts) > 1 and parts[-1] == 0:
                parts.pop()
            return tuple(parts)
    return None


def _step_lines(lines: list[str], index: int) -> list[str]:
    start = index
    while start > 0 and not _STEP_START.match(lines[start]):
        start -= 1
    end = index + 1
    while end < len(lines) and not _STEP_START.match(lines[end]):
        end += 1
    return lines[start:end]


def _names(vuln_id: str, comment: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(vuln_id)}(?![\w-])", comment) is not None


def pip_audit_ignores(workflow_text: str) -> list[tuple[str, str | None, str | None]]:
    lines = workflow_text.splitlines()
    ignores: list[tuple[str, str | None, str | None]] = []
    for index, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        for vuln_id in _IGNORE_VULN.findall(line):
            comments = [c.strip()[1:].strip() for c in _step_lines(lines, index) if c.strip().startswith("#")]
            reasons = [c for c in comments if _names(vuln_id, c) and c.replace(vuln_id, "").strip(" -—:")]
            dates = [m.group(1) for c in comments if (m := _REVIEW_BY.search(c))]
            ignores.append((vuln_id, reasons[0] if reasons else None, dates[0] if dates else None))
    return ignores


def shipped_requirements(root: Path = REPO_ROOT) -> list[Requirement]:
    requirements: list[Requirement] = []
    for manifest in discover(root).shipped:
        path = root / manifest
        parser = parse_pyproject if path.suffix == ".toml" else parse_requirements
        requirements.extend(parser(path.read_text(), manifest))
    return requirements


def workflow_ignores(root: Path = REPO_ROOT) -> list[tuple[str, str, str | None, str | None]]:
    ignores: list[tuple[str, str, str | None, str | None]] = []
    for template in discover(root).templates:
        workflow = f"{template}/{WORKFLOW}"
        if (root / workflow).is_file():
            for vuln_id, reason, review_by in pip_audit_ignores((root / workflow).read_text()):
                ignores.append((workflow, vuln_id, reason, review_by))
    return ignores


def unbounded_findings(root: Path = REPO_ROOT) -> list[Finding]:
    return [
        Finding((r.manifest, r.group, r.name), f"{r.manifest} [{r.group}] {r.raw}: {UNBOUNDED_FIX}")
        for r in shipped_requirements(root)
        if not is_bounded(r.specifier)
    ]


def ceiling_findings(root: Path = REPO_ROOT) -> list[Finding]:
    by_package: dict[str, dict[tuple[int, ...], set[str]]] = {}
    compared = discover(root).templates_and_applications
    for r in shipped_requirements(root):
        bound = ceiling(r.specifier) if r.manifest in compared else None
        if bound is not None:
            by_package.setdefault(r.name, {}).setdefault(bound, set()).add(r.manifest)
    findings: list[Finding] = []
    for name, by_bound in sorted(by_package.items()):
        if len(by_bound) > 1:
            where = " and ".join(
                f"<{'.'.join(map(str, bound))} in {', '.join(sorted(manifests))}"
                for bound, manifests in sorted(by_bound.items())
            )
            findings.append(Finding(name, f"{name} is capped {where}; {DRIFT_FIX}"))
    return findings


def pip_audit_findings(root: Path = REPO_ROOT) -> list[Finding]:
    findings: list[Finding] = []
    for workflow, vuln_id, reason, review_by in workflow_ignores(root):
        missing = [
            what for what, value in (("reason line", reason), ("review-by date", review_by)) if value is None
        ]
        if missing:
            gap = " and no ".join(missing)
            findings.append(
                Finding(
                    (workflow, vuln_id),
                    f"{workflow}: pip-audit --ignore-vuln {vuln_id} carries no {gap}; {IGNORE_FIX}",
                )
            )
    return findings


def constraints_findings(template: str, root: Path = REPO_ROOT) -> list[Finding]:
    constraints = f"{template}/{CONSTRAINTS}"
    if not (root / constraints).is_file():
        return [
            Finding((constraints, None), f"{constraints} is missing; run `{COMPILE_COMMAND}` in the template")
        ]
    entries = parse_requirements((root / constraints).read_text(), constraints)
    pinned = {r.name for r in entries if _PIN.match(r.specifier)}
    manifest = f"{template}/pyproject.toml"
    findings = [
        Finding(
            (constraints, r.name),
            f"{constraints} pins no {r.name}== for {manifest} [{r.group}] {r.raw}; {UNPINNED_FIX}",
        )
        for r in parse_pyproject((root / manifest).read_text(), manifest)
        if r.name not in pinned
    ]
    findings.extend(
        Finding((constraints, r.name), f"{constraints}: {r.raw}; {BUILD_TOOL_FIX}")
        for r in entries
        if r.name in BUILD_TOOLS
    )
    return findings


def test_every_direct_dependency_is_bounded():
    fresh = [f for f in unbounded_findings() if f.key not in KNOWN_UNBOUNDED]
    assert fresh == [], "\n".join(f.message for f in fresh) + (
        "\nIf this is today's debt, add the key to KNOWN_UNBOUNDED with its remover, in a reviewed diff."
    )


def test_unbounded_known_list_only_shrinks():
    live = {f.key for f in unbounded_findings()}
    stale = sorted(k for k in KNOWN_UNBOUNDED if k not in live)
    assert stale == [], f"no longer a violation; delete it from KNOWN_UNBOUNDED: {stale}"


def test_same_package_same_ceiling_across_templates_and_applications():
    drift = ceiling_findings()
    assert drift == [], "\n".join(f.message for f in drift)


def test_pip_audit_ignores_carry_a_reason_and_a_review_by_date():
    bare = pip_audit_findings()
    assert bare == [], "\n".join(f.message for f in bare)


def test_each_template_constraints_file_pins_every_direct_dependency():
    findings = [f for template in TEMPLATES for f in constraints_findings(template)]
    assert findings == [], "\n".join(f.message for f in findings)


def test_the_constraints_check_fails_on_a_missing_pin(tmp_path):
    source = REPO_ROOT / TEMPLATES[0]
    pyproject = (source / "pyproject.toml").read_text()
    assert pyproject.count("dependencies = [\n") == 1
    template = tmp_path / TEMPLATES[0]
    template.mkdir(parents=True)
    (template / "pyproject.toml").write_text(
        pyproject.replace("dependencies = [\n", 'dependencies = [\n    "unpinned_Extra>=1.0,<2.0",\n')
    )
    constraints = (source / CONSTRAINTS).read_text().rstrip("\n") + "\n"
    keyed = f"{TEMPLATES[0]}/{CONSTRAINTS}"

    for tail in ("", "# unpinned-extra==1.0\n", "unpinned-extra>=1.0,<2.0\n", "unpinned-extra==1.*\n"):
        (template / CONSTRAINTS).write_text(constraints + tail)
        findings = constraints_findings(TEMPLATES[0], tmp_path)
        assert [f.key for f in findings] == [(keyed, "unpinned-extra")], tail
        assert "unpinned_Extra>=1.0,<2.0" in findings[0].message and COMPILE_COMMAND in findings[0].message

    (template / CONSTRAINTS).write_text(constraints + "Unpinned.Extra==1.2.0\nsetuptools==80.9.0\n")
    assert [f.key for f in constraints_findings(TEMPLATES[0], tmp_path)] == [(keyed, "setuptools")]

    (template / CONSTRAINTS).unlink()
    assert [f.key for f in constraints_findings(TEMPLATES[0], tmp_path)] == [(keyed, None)]


COPIED_TEMPLATE = f"{TEMPLATE_HOME}/copied-agentcore"
COPIED_APPLICATION = f"{APPLICATION_HOME}/acme_copied_agent/requirements.txt"


def _index(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    _git(root, "init", "-q")
    _git(root, "add", "-f", "--", *files)


def test_parser_handles_markers_extras_continuations_and_optional_groups(tmp_path):
    pyproject = f"{COPIED_TEMPLATE}/pyproject.toml"
    optional_only = "scripts/tool/pyproject.toml"
    dev_requirements = "scripts/tool/requirements-dev.txt"
    base_requirements = "scripts/tool/requirements/base.txt"
    requirements = (
        "# the agent's own dependencies\n"
        "-r base.txt\n"
        "-c constraints.txt\n"
        "--no-binary :all:\n"
        "\n"
        "strands-agents[otel]>=1.38.0,<2.0  # 2.x renamed the client\n"
        'floored>=1.0; python_version < "3.12"\n'
        "tilde~=2.1\n"
        "Pinned_Name.Mixed==3.0\n"
        "spread>=1.0, \\\n"
        "    <2.0\n"
    )
    _index(
        tmp_path,
        {
            COPIED_APPLICATION: requirements,
            pyproject: (
                "[project]\n"
                'name = "fixture"\n'
                'dependencies = ["mcp>=1.0.0,<2.0", "boto3>=1.34.0"]\n'
                "[project.optional-dependencies]\n"
                'dev = ["pytest>=8.0", "ruff>=0.16,<0.17"]\n'
                "docs = [\"sphinx>=7; python_version < '3.12'\"]\n"
            ),
            optional_only: (
                '[project]\nname = "tool"\n[project.optional-dependencies]\nlint = ["ruff>=0.16,<0.17"]\n'
            ),
            dev_requirements: "pytest>=8.0,<9\n",
            base_requirements: "httpx>=0.27\n",
            "docs/notes.txt": "unread>=1.0\n",
            next(iter(VENDORED_MANIFESTS)): "vendored>=1.0\n",
            "platform/control_plane/backend/pyproject.toml": "[tool.ruff]\nline-length = 110\n",
        },
    )
    assert discover(tmp_path) == Manifests(
        templates=(COPIED_TEMPLATE,),
        applications=(COPIED_APPLICATION,),
        templates_and_applications=(pyproject, COPIED_APPLICATION),
        shipped=(COPIED_APPLICATION, pyproject, optional_only, dev_requirements, base_requirements),
    )

    found = shipped_requirements(tmp_path)
    by_key = {(r.group, r.name): r for r in found}
    assert set(by_key) == {
        ("requirements", "strands-agents"),
        ("requirements", "floored"),
        ("requirements", "tilde"),
        ("requirements", "pinned-name-mixed"),
        ("requirements", "spread"),
        ("dependencies", "mcp"),
        ("dependencies", "boto3"),
        ("dev", "pytest"),
        ("dev", "ruff"),
        ("docs", "sphinx"),
        ("lint", "ruff"),
        ("requirements", "pytest"),
        ("requirements", "httpx"),
    }
    assert by_key[("requirements", "strands-agents")].extras == "otel"
    assert by_key[("requirements", "strands-agents")].specifier == ">=1.38.0,<2.0"
    assert by_key[("requirements", "strands-agents")].raw == "strands-agents[otel]>=1.38.0,<2.0"
    assert by_key[("requirements", "floored")].specifier == ">=1.0"
    assert by_key[("requirements", "spread")].specifier == ">=1.0,<2.0"
    assert by_key[("requirements", "pinned-name-mixed")].specifier == "==3.0"
    assert by_key[("docs", "sphinx")].specifier == ">=7"
    assert by_key[("dependencies", "mcp")].manifest == pyproject

    assert is_bounded("~=2.1") and is_bounded("==3.0") and is_bounded(">=1.0,<2.0")
    assert not is_bounded(">=1.0") and not is_bounded("") and not is_bounded(">=1.0,!=1.5")
    assert ceiling(">=1.0,<2") == ceiling(">=1.0,<2.0") == ceiling("<=2.0.0") == (2,)
    assert ceiling("<0.17") == (0, 17)
    assert ceiling(">=1.0") is None and ceiling("==1.2") is None

    assert {f.key for f in unbounded_findings(tmp_path)} == {
        (COPIED_APPLICATION, "requirements", "floored"),
        (pyproject, "dependencies", "boto3"),
        (pyproject, "dev", "pytest"),
        (pyproject, "docs", "sphinx"),
        (base_requirements, "requirements", "httpx"),
    }


def test_ceiling_drift_between_a_template_and_an_application_is_reported(tmp_path):
    backend = "platform/control_plane/backend/requirements.txt"
    _index(
        tmp_path,
        {
            f"{COPIED_TEMPLATE}/pyproject.toml": (
                '[project]\ndependencies = ["mcp>=1.0.0,<2.0", "boto3>=1.34.0", "requests>=2.31,<3.0"]\n'
            ),
            COPIED_APPLICATION: "mcp>=1.9,<3\nboto3>=1.34,<2\nrequests>=2.31,<3\n",
            backend: "mcp>=1.0,<9\n",
        },
    )

    drift = ceiling_findings(tmp_path)
    assert [f.key for f in drift] == ["mcp"]
    assert "<2 in " in drift[0].message and "<3 in " in drift[0].message
    assert COPIED_APPLICATION in drift[0].message and backend not in drift[0].message


def test_pip_audit_ignore_parser_reads_the_step_comments(tmp_path):
    workflow = f"{COPIED_TEMPLATE}/{WORKFLOW}"
    steps = (
        "jobs:\n"
        "  quality:\n"
        "    steps:\n"
        "      - name: Dependency audit (pip-audit)\n"
        "        # the server-side path is never reached by this agent;\n"
        "        # review by 2026-12-31: re-run without the ignores.\n"
        "        #   PYSEC-2026-311 — code injection on the collections endpoint\n"
        "        run: |\n"
        "          pip-audit \\\n"
        "            --ignore-vuln PYSEC-2026-311\n"
        "      - name: Second audit\n"
        "        # pip-audit --ignore-vuln PYSEC-2026-999 would be wrong here\n"
        "        run: pip-audit --ignore-vuln=PYSEC-2026-3814 --ignore-vuln PYSEC-2026-3815\n"
        "      - name: Build\n"
        "        run: docker build .\n"
    )
    _index(tmp_path, {f"{COPIED_TEMPLATE}/pyproject.toml": "[project]\ndependencies = []\n", workflow: steps})
    ignores = pip_audit_ignores(steps)
    assert ignores == [
        ("PYSEC-2026-311", "PYSEC-2026-311 — code injection on the collections endpoint", "2026-12-31"),
        ("PYSEC-2026-3814", None, None),
        ("PYSEC-2026-3815", None, None),
    ]
    findings = pip_audit_findings(tmp_path)
    assert [f.key for f in findings] == [
        (workflow, "PYSEC-2026-3814"),
        (workflow, "PYSEC-2026-3815"),
    ]
    assert "no reason line and no review-by date" in findings[0].message


def test_the_scan_has_something_to_scan():
    assert TEMPLATES and APPLICATIONS and set(TEMPLATES_AND_APPLICATIONS) < set(SHIPPED_MANIFESTS)
    assert len(shipped_requirements()) >= SHIPPED_REQUIREMENT_FLOOR
    assert len(workflow_ignores()) >= WORKFLOW_IGNORE_FLOOR
