"""Contract tests for the CodeBuild buildspec.

The buildspec reaches CodeBuild from an S3 source, never inlined through `file()`: CodeBuild caps an
inline buildspec at 25600 characters, a cap that fails live at `UpdateProject` and nowhere offline,
and the file is already over it. So the first assertion is that delivery really is via S3.

A successful dev deploy persists the image tag it deployed (`last_dev_image_tag`) so the promote
route can resolve "the last good dev image" with no tag input from the user. A prod promotion must
never overwrite it.
"""

import re
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from services.langfuse_provisioning import LangfuseProvisioningService

MODULE = Path(__file__).resolve().parents[2] / "infrastructure/modules/codebuild"
BUILDSPEC = MODULE / "buildspec.yml"
_PY_DIR = Path(__file__).resolve().parents[1] / "venv/bin"
DEP_SCRIPT = "/tmp/dep.py"  # noqa: S108 — the buildspec's own path, asserted as text and never opened here; expires 2027-10-23; added E49A-50A/T18
FIXED_DEP_ITEM = "/tmp/dep.json"  # noqa: S108 — the buildspec's own path, asserted as text and never opened here; expires 2027-10-23; added E49A-50A/T18
AGENT_CLONE = "/tmp/agent"  # noqa: S108 — the buildspec's own path, asserted as text and never opened here; expires 2027-10-23; added E49A-50A/T18
IAM_ROLE_FLOOR = 6


def test_the_buildspec_is_delivered_from_s3_not_inlined():
    """The invariant that replaced the 25600-char size cap.

    An S3 source has no length limit, so the buildspec is free to grow, but only while it stays an
    S3 source. The file is already over the inline cap, so a regression to an inline
    `buildspec = file(...)` attribute would fail live at `UpdateProject` and nowhere else. Hence
    both halves are pinned: the S3 source is present, and no inline attribute is (the `file()` that
    remains belongs to the archive_file that packages the buildspec for S3)."""
    main_tf = (MODULE / "main.tf").read_text()

    assert 'type     = "S3"' in main_tf
    assert "aws_s3_object.buildspec.key" in main_tf
    assert not re.search(r"^\s*buildspec\s*=", main_tf, re.MULTILINE)


def _tag_lines():
    """Every line mentioning the `_tag` helper, split into (definition, call sites).
    Matches on `_tag` as a WHOLE word so `last_dev_image_tag` never counts as a hit, and skips
    shell comments so the buildspec's own prose about `_tag` is not read as a call site."""
    lines = [line.strip() for line in BUILDSPEC.read_text().splitlines() if not line.strip().startswith("#")]
    hits = [line for line in lines if re.search(r"(?<![\w])_tag\b", line)]
    return (
        [line for line in hits if line.startswith("_tag()")],
        [line for line in hits if not line.startswith("_tag()")],
    )


def test_buildspec_persists_dev_image_tag_on_success():
    text = BUILDSPEC.read_text()
    assert "last_dev_image_tag" in text
    definition, _ = _tag_lines()
    assert len(definition) == 1, definition
    assert "last_dev_image_tag" in definition[0]


def test_tag_is_called_on_the_success_path_only():
    """Pins the wiring, not just the definition: every `_tag` call site is a success path, immediately
    followed by `_st deployed` and `_dep succeeded` (the terminal Deployment row is written on exactly
    that path), so the assertion pins the prefix rather than the whole line. Dies if a call is deleted
    (leaving the definition as dead code) or moved onto a failure path.

    The count is one per deploy mechanism (`agentcore_runtime`, `agentcore_harness` and
    `agentcore_gateway`), each recording its serving tag for the same reason; the teeth do not depend
    on the number, since every call site must be the exact success triple. The per-branch test below
    derives the mechanisms from the real `case` labels, so a new mechanism that forgets its write-back
    fails there rather than silently shipping an un-rollbackable agent."""
    _, calls = _tag_lines()
    assert len(calls) == 3, calls
    for call in calls:
        assert call.startswith("_tag; _st deployed"), call
        assert "_dep succeeded" in call, call


def test_every_agent_deploying_branch_records_its_serving_tag_and_terminal_row():
    """The generalisation of the test above, pinned per branch.

    `_dep` is the ONLY writer of a terminal `Deployment` row, and the backend's `_has_succeeded`
    (`project_service.py`) scans exactly those rows to decide whether a tag may be ROLLED BACK and
    whether a promote may stamp `serving`. It fails CLOSED. So a deploy branch with no `_dep
    succeeded` produces agents that are permanently un-rollbackable and never recorded as serving —
    with no error anywhere, which is why this is pinned per-branch rather than in aggregate.

    Scoped to the branches that deploy an AGENT: the legacy `terraform` branch deploys foundation
    stacks and has no agent, no image tag and no repo row to write to. That scoping is DERIVED from
    the real case labels rather than hardcoded, so a fourth agent mechanism is covered the moment it
    is added instead of quietly sitting outside a literal list. Every failure exit in a branch
    records too, so the badge and the history agree."""
    text = BUILDSPEC.read_text()
    agent_branches = sorted(_iac_type_cases() - {"terraform"})
    assert agent_branches, "no agent-deploying branches found"
    for name in agent_branches:
        start = text.index(f"{name})")
        body = "\n".join(
            line
            for line in text[start : text.index(";;", start)].splitlines()
            if not line.strip().startswith("#")
        )
        assert "_dep succeeded" in body, f"{name} records no terminal succeeded row"
        assert "_st deployed" in body, f"{name} never marks the repo deployed"
        assert "_tag" in body, f"{name} never persists its serving tag"
        assert "_dep failed" in body, f"{name} has failure exits that record nothing"


def test_tag_persist_is_gated_on_the_stage_literal():
    """`_tag` never writes a field the stage did not earn — each half carries its own
    `$STAGE` comparison. (The prod half is pinned separately, from `yaml.safe_load`, in
    `test_tag_writes_the_stage_matched_field_for_dev_and_prod` below.)"""
    definition, _ = _tag_lines()
    assert 'STAGE" = "dev"' in definition[0]


def test_tag_persist_requires_a_non_empty_image_tag():
    """An empty IMAGE_TAG must write NOTHING — a persisted "" is a present-but-useless
    value the promote route would otherwise treat as a real tag."""
    definition, _ = _tag_lines()
    assert '[ -n "$IMAGE_TAG" ]' in definition[0]


def test_repository_model_carries_the_field():
    from models.repository import Repository

    assert "last_dev_image_tag" in Repository.model_fields


def _runtime_commands():
    """Every phase command AS CODEBUILD WILL RUN IT — the values `yaml.safe_load` yields.

    This is the only faithful view, and reading the raw file instead is what let a whole class of
    bug through twice. Inside a YAML block scalar each line keeps the block's own indentation at
    runtime, so a test that reads file lines and `.strip()`s them is asserting against a string
    that never executes. Anything about EXECUTION must come through here."""
    import yaml

    doc = yaml.safe_load(BUILDSPEC.read_text())
    out = []
    for phase, spec in doc["phases"].items():
        for cmd in spec.get("commands", []):
            if isinstance(cmd, str):
                out.append((phase, cmd))
    return out


def _dep_script_body():
    """The `/tmp/dep.py` heredoc body exactly as it lands on disk at runtime, plus its phase."""
    for phase, cmd in _runtime_commands():
        if "cat > /tmp/dep.py" in cmd:
            body = cmd.split("<< 'PYEOF'\n", 1)[1].split("\nPYEOF", 1)[0]
            return phase, body
    raise AssertionError("no /tmp/dep.py heredoc found in the buildspec")


def _dep_definition():
    """The single runtime line defining `_dep` (comments excluded)."""
    for _, cmd in _runtime_commands():
        for line in cmd.split("\n"):
            stripped = line.strip()
            if stripped.startswith("_dep()"):
                return stripped
    raise AssertionError("no _dep() definition found in the buildspec")


def _dep_lines():
    """(definitions, call sites) for `_dep`, from the RUNTIME command strings.

    Shell comments are skipped: the buildspec's own explanatory comments mention these helper
    names, and counting them as call sites made the wiring assertions see phantom entries (a
    prose edit would otherwise "break" them). The heredoc that creates `/tmp/dep.py` is not a call
    site either."""
    definition, calls = [], []
    for _, cmd in _runtime_commands():
        for line in cmd.split("\n"):
            stripped = line.strip()
            if stripped.startswith("#") or not re.search(r"(?<![\w])_dep\b", stripped):
                continue
            if stripped.startswith("_dep()"):
                definition.append(stripped)
            elif DEP_SCRIPT not in stripped:
                calls.append(stripped)
    return definition, calls


def test_dep_is_defined_and_wired_to_both_outcomes():
    """The buildspec is the only writer of a `succeeded`/`failed` Deployment row: the backend appends
    `started` when a build is requested, and only the build knows how it ended. A drift here does not
    raise, the row lands under a key `list_deployments` never queries and history reads empty, so the
    `_dep` tests pin the wiring (`test_promotion_history.py` pins the row's shape).

    One definition, and call sites on both the success and the failure paths: a `_dep` wired only to
    success would silently drop every failed build from the history, the state an operator most
    needs to see. Every call passes exactly one of the two enum wire values, never a third literal."""
    definition, calls = _dep_lines()
    assert len(definition) == 1, definition
    assert any(c.endswith("_dep succeeded") for c in calls), calls
    assert sum("_dep failed" in c for c in calls) >= 1, calls
    for call in calls:
        assert re.search(r"_dep (succeeded|failed)\b", call), call


def test_dep_covers_every_failure_exit_individually():
    """Per-exit coverage, not an aggregate count.

    An aggregate `sum(...) >= 1` is satisfied by any one of the three failure exits, so a missing
    `_dep failed` on the terraform-apply exit (the likeliest real failure) would pass. Each exit is
    therefore identified by its own distinguishing text and asserted to carry a `_dep failed`, so
    removing any single one fails this test by name. The three are: the cross-account promote with
    no dev image, the cross-account image copy/push, and the terraform apply (`RT_FAIL`). Every
    `_st failed` site has a `_dep failed` beside it too: the two must never diverge, or the badge
    says failed while the history shows nothing."""
    _, calls = _dep_lines()
    failure_exits = {
        "x-acct promote: no dev img": "no-dev-image refusal",
        "docker push": "cross-account image copy",
        "RT_FAIL": "terraform apply",
    }
    for marker, label in failure_exits.items():
        matching = [c for c in calls if marker in c]
        assert matching, f"no call site found for the {label} exit ({marker!r})"
        for call in matching:
            assert "_dep failed" in call, f"the {label} exit does not record a failed row: {call}"
    for call in calls:
        if "_st failed" in call:
            assert "_dep failed" in call, call


def test_dep_writes_the_pinned_partition_and_key_shape():
    """pk="deployment" and the sort key. The sk is built from `id[-4:]` (the 4-char collision
    breaker) and `started_at`, mirrored into `seq_key` for round-tripping; the id is `dep-<8 hex>`."""
    _, script = _dep_script_body()
    assert "'pk': 'deployment'" in script
    assert "i[-4:]" in script, "the sk suffix must be id[-4:], not the whole id"
    assert "'%s#%s#%s#%s' % (r, s, ts, i[-4:])" in script
    assert "'seq_key': k" in script and "'sk': k" in script
    assert "uuid.uuid4().hex[:8]" in script
    assert "'dep-'" in script


def test_dep_mints_the_item_with_python_not_shell_date_arithmetic():
    """Two shell pitfalls the item minting avoids: `${ID##*-}` expands to 8 hex chars (not
    `id[-4:]`), and `date -u +%...%6N` is not portable (it emits a literal "6N", an unparseable,
    unsortable timestamp). Both write a row nothing can read. python3 is already a hard dependency
    of this buildspec, so minting the item there is not a new one."""
    definition = _dep_definition()
    _, script = _dep_script_body()
    assert "python3 /tmp/dep.py" in definition
    assert "datetime.now(timezone.utc).isoformat()" in script
    assert "%6N" not in definition and "##*-" not in definition


def test_dep_invokes_a_script_FILE_never_python_dash_c():
    """The static half of the inline `python3 -c` guard (also executed further below).

    Inside a YAML block scalar every line of an inline `python3 -c` body carries the block's own
    indentation AT RUNTIME, and `python3 -c` rejects a leading indent with IndentationError on
    every interpreter before 3.14. The CodeBuild image
    (`amazonlinux2-aarch64-standard:3.0`, `main.tf`) is far below that, so the inline form failed
    100% of the time — silently, because `_dep` is `|| true` with stderr suppressed.

    A heredoc'd FILE is immune: the interpreter reads real bytes off disk. This mirrors
    `merge_outputs.py`, the idiom this buildspec already uses for every multi-line python.

    Both halves are asserted, because dropping either reinstates the bug."""
    definition = _dep_definition()
    assert "python3 /tmp/dep.py" in definition
    assert "python3 -c" not in definition, (
        "an inline -c body inherits the YAML block indent and dies on IndentationError "
        "below Python 3.14 — heredoc the script to a file instead"
    )


def test_the_dep_script_is_written_at_runtime_indent_zero():
    """The property that decides whether the script parses at all.

    Asserted against `yaml.safe_load`'s value, the bytes CodeBuild really writes, because a test
    that reads raw file lines and `.strip()`s them normalizes away this exact property and stays
    green through a 100%-failing helper."""
    import ast

    _, script = _dep_script_body()
    lines = [line for line in script.split("\n") if line.strip()]
    assert lines, "the heredoc body is empty"
    assert not lines[0].startswith((" ", "\t")), repr(lines[0])
    top_level = [line for line in lines if not line.startswith((" ", "\t"))]
    assert len(top_level) >= 5, f"only {len(top_level)} column-0 lines — the body looks indented"
    ast.parse(script)


def test_the_dep_script_is_written_before_any_path_that_can_call_dep():
    """`_dep` runs on the success path AND on three failure exits, the earliest inside the
    cross-account copy. The heredoc must therefore land in `pre_build`, not beside its first use —
    otherwise an early failure exit invokes a script that does not exist yet."""
    phase, _ = _dep_script_body()
    assert phase == "pre_build", f"the dep.py heredoc is in {phase!r}, not pre_build"


def test_dep_records_no_human_actor():
    """A build has no human actor. The platform keeps a GitHub login and an Entra oid as distinct
    currencies, so inventing an actor here would forge attribution; leaving both unset is the
    honest answer (the backend's own `started` row carries the proven requester)."""
    _, script = _dep_script_body()
    assert "actor" not in script


def test_dep_omits_source_sha_by_design():
    """`source_sha` is absent because the buildspec has NO commit sha in scope. It lives only on
    the backend-written `started` row (from the OIDC-proven token), so a reader collapsing the two
    rows for one `build_id` must take `source_sha` from the `started` row — never from this one.
    Pinned so a future edit does not invent one: a fabricated sha is worse than a missing one."""
    _, script = _dep_script_body()
    assert "source_sha" not in script
    assert "source_sha" in BUILDSPEC.read_text(), "the design note must stay in the buildspec"


def test_dep_is_best_effort_and_never_fails_a_good_deploy():
    """A history write must not turn a successful deploy into a failed build. Guarded for an
    unresolved REPO_SK the same way `_u` is — the query returns the literal string "None"."""
    definition = _dep_definition()
    assert '"$REPO_SK" != "None"' in definition
    assert "|| true" in definition
    assert '[ -n "$IMAGE_TAG" ]' in definition


def test_dep_uses_a_mktemp_item_path_not_a_fixed_one():
    """One build can call `_dep` more than once (a retry, or a failure exit after an earlier
    write), and a fixed `/tmp/dep.json` is shared mutable state across those calls. mktemp gives
    each call its own file, and it is removed afterwards."""
    definition = _dep_definition()
    assert "mktemp" in definition
    assert FIXED_DEP_ITEM not in definition
    assert 'rm -f "$D_ITEM"' in definition


def test_dep_error_hint_is_safe():
    """`error` is a safe short hint only, never a token, ARN or raw upstream body; a gateway build
    (MCP_SERVER_ID set) names the gateway, not the runtime."""
    _, script = _dep_script_body()
    assert "'the runtime build failed'" in script
    assert "'the gateway build failed' if m else 'the runtime build failed'" in script
    for leak in ("arn:", "SECRET", "TOKEN", "get-secret-value"):
        assert leak not in script


def _repo_sk_query_line():
    """The buildspec line that resolves `REPO_SK` from the `agent_id-index` GSI."""
    (line,) = [
        raw.strip() for raw in BUILDSPEC.read_text().splitlines() if "REPO_SK=$(aws dynamodb query" in raw
    ]
    return line


def test_the_repo_sk_query_filters_to_the_repository_partition():
    """`agent_id-index` indexes every row carrying an `agent_id`, and `models/deployment.py` gives
    `Deployment` one, so from the first append onward this GSI returns 2+ items for a single agent.

    GSI item order is not contractual, so without a filter `Items[0]` may be a DEPLOYMENT row and
    `REPO_SK` becomes a composite key (`repo-x#dev#2026-...#1027`). That value passes the
    `!= "None"` guard, so `_u` / `_st` / `_tag` / `_dep` all then write to a nonexistent key —
    breaking the PRE-EXISTING `cicd_status` write-back, nondeterministically.

    The index is KEYS_ONLY, which projects the table keys (`pk`, `sk`) alongside `agent_id`, so
    filtering on `pk` needs no index change; the agent key condition still selects the partition."""
    line = _repo_sk_query_line()
    assert "--filter-expression" in line, "the GSI query must exclude non-repository rows"
    assert "pk = :p" in line
    assert '\\":p\\":{\\"S\\":\\"repository\\"}' in line
    assert "agent_id = :a" in line


def test_a_deployment_row_sorting_first_still_resolves_the_repository_sk():
    """Exercises the resolution semantics of the GSI filter.

    This repo's convention is no moto (`conftest.py`), so rather than stand up
    DynamoDB this simulates the GSI in its WORST ordering — the deployment row first — and applies
    the buildspec's own filter to it. That is the case that broke: unfiltered, `Items[0].sk` is a
    deployment's composite key; filtered, only the repository row can match.

    The filter applied here is read OUT of the buildspec, so this cannot pass if the real query
    stops filtering."""
    import json

    line = _repo_sk_query_line()
    gsi_page = [
        {
            "pk": {"S": "deployment"},
            "sk": {"S": "r-1#dev#2026-07-31T12:20:17.180133+00:00#b62c"},
            "agent_id": {"S": "a-1"},
        },
        {"pk": {"S": "repository"}, "sk": {"S": "r-1"}, "agent_id": {"S": "a-1"}},
    ]

    assert gsi_page[0]["sk"]["S"] != "r-1"
    assert "#" in gsi_page[0]["sk"]["S"], "a composite key would be used as a repo sk"

    match = re.search(r'--expression-attribute-values "(.*?)" --query', line)
    assert match is not None
    values = json.loads(match.group(1).replace('\\"', '"'))
    wanted_pk = values[":p"]["S"]
    filtered = [i for i in gsi_page if i["pk"]["S"] == wanted_pk]

    assert len(filtered) == 1, filtered
    resolved = filtered[0]["sk"]["S"]
    assert resolved == "r-1", "REPO_SK must be the repository row's sk, not a deployment's"
    assert "#" not in resolved


def test_dep_passes_every_env_var_it_reads_into_the_python_process():
    """Asserted statically here and executed in the test below.

    `AGENT_ID` and `REPO_SK` are plain shell assignments (the file's only `export`s are the three AWS
    credential lines), so a bare `python3 -c` inherits neither: the body dies on `KeyError`, the `&&`
    chain short-circuits, `put-item` never runs, and `2>/dev/null || true` swallows the whole thing,
    no terminal row in any build, silently.

    Every name the python body reads must therefore appear as an inline `NAME="$NAME"` assignment
    on the `python3` invocation. The read-set comes from the script, the pass-through from the
    invocation (two strings now that the body lives in its own file), and the regex matches both
    access forms, the subscript `os.environ['X']` (which raises KeyError) and `os.environ.get('X')`;
    a parens-only pattern would find just the `.get()` call and pass while REPO_SK, AGENT_ID, STAGE
    and IMAGE_TAG went unchecked. `D_OUT` is supplied from the function's own `$1`, not an outer
    variable. Derived from the body itself, so a newly-read variable that nobody passes fails this
    test rather than shipping."""
    definition = _dep_definition()
    _, script = _dep_script_body()
    invocation = definition.split("python3 /tmp/dep.py")[0]
    read = set(re.findall(r"os\.environ(?:\.get)?[\[(]\s*['\"]([A-Z_]+)['\"]", script))
    assert read >= {"REPO_SK", "AGENT_ID", "STAGE", "IMAGE_TAG"}, (
        f"the extractor found only {sorted(read)} — it is not seeing the whole python body, "
        "so this test would pass vacuously"
    )
    for name in sorted(read):
        if name == "D_OUT":
            assert 'D_OUT="$1"' in invocation
            continue
        assert f'{name}="${name}"' in invocation or f'{name}="${{{name}:-}}"' in invocation, (
            f"{name} is read by the python body but never passed into its environment — "
            "it is a non-exported shell variable, so the row will never be written"
        )


def _interpreters_below_314():
    """Every available python3 interpreter older than 3.14, newest first.

    3.14 added dedenting for `python3 -c`; nothing earlier has it. So a test that runs only on 3.14
    cannot observe the indentation class of bug at all. The CodeBuild image is
    `amazonlinux2-aarch64-standard:3.0`, far below 3.14, so a sub-3.14 interpreter is the only
    faithful one here; the repo's own backend venv (3.12, `_PY_DIR`) is the first candidate after the
    running interpreter. De-duplicated by version, keeping the first found: three interpreters of
    one version add runtime, not coverage."""
    import subprocess
    import sys

    found = []
    candidates = [
        sys.executable,
        str(_PY_DIR / "python"),
        "/opt/homebrew/bin/python3.13",
        "/opt/homebrew/bin/python3.12",
        "/usr/bin/python3",
    ]
    for path in candidates:
        try:
            out = subprocess.run(
                [path, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode != 0:
            continue
        try:
            major, minor = (int(p) for p in out.stdout.strip().split("."))
        except ValueError:
            continue
        if (major, minor) < (3, 14):
            found.append((path, f"{major}.{minor}"))
    seen, unique = set(), []
    for path, ver in found:
        if ver not in seen:
            seen.add(ver)
            unique.append((path, ver))
    return unique


_SUB_314 = _interpreters_below_314()


@pytest.mark.skipif(
    not _SUB_314,
    reason=(
        "LOUD SKIP: no Python < 3.14 interpreter found, so the IndentationError class of bug in "
        "the buildspec's python invocation CANNOT be observed here. 3.14 dedents `python3 -c`; "
        "the CodeBuild image (amazonlinux2-aarch64-standard:3.0) does not. This test is only "
        "meaningful on a sub-3.14 interpreter — the repo's own backend venv is 3.12."
    ),
)
@pytest.mark.parametrize("interpreter,version", _SUB_314, ids=[v for _, v in _SUB_314])
def test_dep_actually_writes_a_row_with_the_vars_NON_exported(interpreter, version):
    """Executes the REAL helper the way CodeBuild does and proves a row is produced.

    Three properties of this harness matter, each closing a hole:

    1. The script comes from `yaml.safe_load`, not from stripped file lines: a `.strip()`ed variant
       is dedented, production never runs it, and it stays green while the real helper fails.
    2. The shell variables are not exported: the env-prefix form (`REPO_SK=r-1 python3 …`) does
       export, so it validates the script text against an environment production never provides.
    3. The interpreter is < 3.14 (parameterized): on 3.14 an indented `-c` body is dedented
       automatically and the bug is invisible.

    The heredoc command runs as CodeBuild runs it, comments stripped only because a bare `#` line is
    noise, code lines keeping their runtime indentation verbatim; only the paths are relocated into
    the sandbox, never the body. `python3` is pinned to the interpreter under test and `aws` is
    stubbed to capture the item the CLI would have sent, so nothing touches AWS; the emitted item is
    parsed back through the real `Deployment` model (if it cannot round-trip, `list_deployments`
    could never return it) and the key shape is re-derived here independently of the shell. The
    stage is a free-form value, not a dev/prod literal, to prove the row is written for any stage."""
    import json
    import subprocess
    import tempfile
    from pathlib import Path as _Path

    from models.deployment import Deployment

    heredoc_phase, _ = _dep_script_body()
    definition = _dep_definition()
    heredoc_cmd = next(
        "\n".join(line for line in cmd.split("\n") if not line.strip().startswith("#"))
        for phase, cmd in _runtime_commands()
        if phase == heredoc_phase and "cat > /tmp/dep.py" in cmd
    )

    with tempfile.TemporaryDirectory() as tmp:
        script_path = _Path(tmp) / "dep.py"
        item_path = _Path(tmp) / "item.json"
        heredoc_cmd = heredoc_cmd.replace(DEP_SCRIPT, str(script_path))
        body = definition.replace(DEP_SCRIPT, str(script_path))
        script = (
            f"{heredoc_cmd}\n"
            "REPO_SK=repo-abc123\n"
            "AGENT_ID=a-1\n"
            "STAGE=uat\n"
            "IMAGE_TAG=a-1-abc1234\n"
            "CODEBUILD_BUILD_ID=agp:1111-2222\n"
            "PROJECTS_TABLE_NAME=t\n"
            f'python3() {{ "{interpreter}" "$@"; }}\n'
            f'aws() {{ for a in "$@"; do case "$a" in file://*) cp "${{a#file://}}" '
            f'"{item_path}";; esac; done; return 0; }}\n'
            f"{body}\n"
            "_dep succeeded\n"
        )
        proc = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)
        assert script_path.exists(), f"the dep.py heredoc did not land — stderr={proc.stderr!r}"
        assert item_path.exists(), (
            f"no row was produced on python{version} — this is the IndentationError signature "
            f"if the body is inlined with -c. stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        raw = json.loads(item_path.read_text())

    flat = {k: (None if "NULL" in v else v["S"]) for k, v in raw.items()}
    flat.pop("pk")
    record = Deployment.model_validate(flat)
    assert record.repo_id == "repo-abc123"
    assert record.stage == "uat"
    assert record.agent_id == "a-1"
    assert record.image_tag == "a-1-abc1234"
    assert record.build_id == "agp:1111-2222"
    assert record.outcome.value == "succeeded"
    assert record.actor is None and record.actor_kind is None
    assert record.seq_key == f"repo-abc123#uat#{record.started_at}#{record.id[-4:]}"
    assert raw["pk"]["S"] == "deployment"
    assert raw["sk"]["S"] == record.seq_key


def _tag_definition():
    """The single runtime line defining `_tag`, from `yaml.safe_load` — NOT stripped file lines.

    Same discipline as `_dep_definition`: inside a YAML block scalar every line keeps the block's
    indentation at runtime, so a fragment built from `l.strip()` is a dedented variant production
    never executes. (`_tag_lines` above predates this rule; it only inspects text, never runs it.)"""
    for _, cmd in _runtime_commands():
        for line in cmd.split("\n"):
            stripped = line.strip()
            if stripped.startswith("_tag()"):
                return stripped
    raise AssertionError("no _tag() definition found in the buildspec")


def test_tag_writes_the_stage_matched_field_for_dev_and_prod():
    """`_tag` covers prod too. The backend's promote-time stamp of `last_promoted_image_tag` is
    conditional on an already `succeeded` prod deployment row, necessarily false on a genuine first
    promote, so with the backend as the only writer the field stayed empty and a deployed prod repo
    read "Never deployed" (`repoRowModel.ts`). The buildspec is the only producer that knows a prod
    `terraform apply` succeeded, and it already writes the symmetric dev field with the same helper.

    Each field is asserted together with the stage literal that must gate it: a `_tag` writing
    `last_promoted_image_tag` on dev (or the dev field on prod) would corrupt the drift comparison
    the frontend runs between the two scalars, which must never converge onto one stage. The two
    digest fields follow the same shape, so there are four writes, a (tag, digest) pair per stage.
    Each write is guarded on a non-empty value (a persisted "" is a present-but-useless value every
    reader would treat as a real tag or digest), two tag guards plus two digest guards, and all four
    are best-effort: a failed history write must never fail a good deploy."""
    definition = _tag_definition()
    for stage, field in (("dev", "last_dev_image_tag"), ("prod", "last_promoted_image_tag")):
        assert f'[ "$STAGE" = "{stage}" ]' in definition, definition
        assert f'_u {field} "$IMAGE_TAG"' in definition, definition
    for _stage, field in (("dev", "last_dev_digest"), ("prod", "last_promoted_digest")):
        assert f'_u {field} "$IMAGE_DIGEST"' in definition, definition
    assert definition.count('[ -n "$IMAGE_TAG" ]') == 2, definition
    assert definition.count('[ -n "${IMAGE_DIGEST:-}" ]') == 2, definition
    assert definition.count("|| true") == 4, definition


_DIGEST = "sha256:" + "ab" * 32


@pytest.mark.parametrize(
    "stage,image_tag,image_digest,expected",
    [
        ("dev", "a-1-abc1234", _DIGEST, ["last_dev_image_tag", "last_dev_digest"]),
        ("prod", "a-1-abc1234", _DIGEST, ["last_promoted_image_tag", "last_promoted_digest"]),
        ("dev", "a-1-abc1234", "", ["last_dev_image_tag"]),
        ("prod", "a-1-abc1234", "", ["last_promoted_image_tag"]),
        ("uat", "a-1-abc1234", _DIGEST, []),
        ("prod", "", "", []),
        ("prod", "", _DIGEST, ["last_promoted_digest"]),
    ],
    ids=["dev", "prod", "dev-no-digest", "prod-no-digest", "uat", "prod-empty-both", "prod-digest-only"],
)
def test_tag_actually_writes_only_the_stage_matched_field(stage, image_tag, image_digest, expected):
    """EXECUTES the real `_tag`. The `&&`/`||` chain is what routes per stage, and no amount of
    static text proves the precedence is right once there are two branches on one line.

    `STAGE`/`IMAGE_TAG` are assigned NON-exported, which is the production condition (`_tag` reads
    them as plain shell variables of its own block; this file's only `export`s are the three AWS
    credential lines). The env-prefix form `STAGE=x sh -c …` would export them and validate an
    environment production does not have. `_u` is stubbed to echo the field name, so nothing
    touches AWS and the assertion is on which field the helper would have written.

    A stage writes its own (tag, digest) pair and nothing else; no digest in scope writes no digest
    field rather than a "" a reader would treat as real; free-form stages earn no field; an empty tag
    writes nothing, yet does not suppress the digest write, or a partially populated build would
    silently record neither. `_tag` is called as `_tag; _st deployed`, so a non-zero status would not
    abort, but the helper is `|| true` by contract and must stay silent and clean."""
    import subprocess

    script = (
        f"{_tag_definition()}\n"
        '_u() { echo "$1"; }\n'
        f"STAGE={stage}\n"
        f"IMAGE_TAG={image_tag}\n"
        f"IMAGE_DIGEST={image_digest}\n"
        "_tag\n"
    )
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == expected, out.stdout


def test_repository_model_carries_the_promoted_tag_field():
    """The buildspec writes this attribute name straight into the repository row, so a rename on
    the model side without one here would strand the write on a field nothing reads."""
    from models.repository import Repository

    assert "last_promoted_image_tag" in Repository.model_fields


def _agentcore_branch():
    """The `agentcore_runtime)` case body AS CODEBUILD RUNS IT (from `yaml.safe_load`).

    Same discipline as `_dep_definition`/`_tag_definition`: never stripped file lines. Everything
    asserted about the runtime branch is read out of this string.

    Ends at the next case label (`agentcore_harness)`). Every `case` arm lives in one
    command string, so slicing only from the label ran the runtime branch's assertions over the
    harness branch too — and the "exactly one registry read" unpackings below then failed on a
    buildspec that was correct. Each branch is asserted by its own group."""
    for _, cmd in _runtime_commands():
        if "agentcore_runtime)" in cmd:
            body = cmd[cmd.index("agentcore_runtime)") :]
            nxt = body.find("agentcore_harness)")
            return body if nxt == -1 else body[:nxt]
    raise AssertionError("no agentcore_runtime) case body found in the buildspec")


def _tfvars_heredoc():
    """The `deploy.auto.tfvars` heredoc BODY exactly as it lands on disk at runtime.

    The delimiter is unquoted (`<<TFVARS`), which matters: the body is a list of
    `key="$VAR"` lines that only become real values because the shell expands them."""
    body = _agentcore_branch()
    assert "cat > deploy.auto.tfvars <<TFVARS\n" in body, (
        "the tfvars heredoc delimiter changed — if it is now QUOTED (<<'TFVARS') every $VAR "
        "lands in the file literally and the module receives no values at all"
    )
    return body.split("cat > deploy.auto.tfvars <<TFVARS\n", 1)[1].split("\nTFVARS", 1)[0]


def test_the_tfvars_pass_the_stage_to_the_runtime_module():
    """Stage-scoped resource names. With the terraform state key stage-scoped but the runtime
    module's resource names not, a prod promote dies on
    `creating IAM Role (platform_agent-agentcore-exec): EntityAlreadyExists`, prod's fresh state
    trying to create a name dev already owns account-globally. The buildspec is the only producer of
    the module's tfvars, so `stage` is threaded from here; the module derives both stage-scoped names
    from `var.stage`, which is required and has no default, precisely so a missing thread fails
    loudly at `terraform plan` instead of naming prod's resources `dev`. A tfvar passed to a module
    that does not declare it is a warning, not an error (silently ignored, resource names unchanged,
    a green build that fixed nothing), hence the drift guard pinned below.

    `$STAGE` is already in scope in this branch (`_tag`, `_dep`, the state key all read it), so
    this is a one-line thread, not new plumbing."""
    assert 'stage="$STAGE"' in _tfvars_heredoc().split("\n")


def test_every_tfvars_line_is_a_shell_expanded_assignment():
    """Non-vacuity guard for the test above: it asserts a LINE, so it would also pass if the
    heredoc had degenerated into prose. Every line here must be `key="…"`, and the `stage` one in
    particular must reference the shell variable rather than hardcoding a stage literal — a
    hardcoded `stage="dev"` is exactly the defect the stage-scoping removed."""
    lines = [raw for raw in _tfvars_heredoc().split("\n") if raw.strip()]
    assert len(lines) >= 12, lines
    for line in lines:
        assert re.match(r'^[a-z_]+="[^"]*"$', line), line
    (stage_line,) = [raw for raw in lines if raw.startswith("stage=")]
    assert "$STAGE" in stage_line, stage_line
    for literal in ('stage="dev"', 'stage="prod"'):
        assert literal not in lines, f"{literal} hardcodes a stage the build was not asked for"


def _arn_write_line():
    """The single runtime line patching the runtime ARN into the registry envelope."""
    body = _agentcore_branch()
    (line,) = [
        raw.strip()
        for raw in body.split("\n")
        if raw.strip().startswith("NEW_ENV=") and not raw.strip().startswith("#")
    ]
    return line


def test_the_arn_write_covers_the_per_stage_MAP_key():
    """Two runtimes co-exist, so one scalar cannot name both — the map keyed
    by stage is the only field that can. `jq` creates the object on assignment, so `.agent_arns[$s]`
    is safe against a legacy envelope that carries no map at all."""
    line = _arn_write_line()
    assert '--arg s "$STAGE"' in line, line
    assert ".agent_arns[$s]=$arn" in line, line


def test_the_arn_write_ALSO_KEEPS_the_scalar():
    """The dual write is MANDATORY, not a convenience — pinned separately from the map so a
    "simplification" that drops it fails by name.

    The reader (`models/agent.py::resolve_runtime_arns`) de-dupes the scalar against the map, so
    keeping it costs nothing; but `Agent.is_agentcore` is still `bool(self.agent_arn)`, which gates
    `POST /agents/{id}/reprovision` (409) and the provisioning gate. Dropping the scalar would turn
    every map-only agent non-agentcore. It is also what lets a rollback to code that predates the map
    still find a runtime. Both writes are in one jq program over one envelope, so they cannot
    diverge."""
    line = _arn_write_line()
    assert ".agent_arn=$arn" in line, line
    assert line.index(".agent_arns[$s]=$arn") < line.index('.identity_status="provisioned"'), line


def _registry_write_block():
    """The guarded registry-write sequence AS CODEBUILD RUNS IT — comments dropped.

    Spans the `$RUNTIME_ARN` emptiness guard through `_tag; _st deployed; _dep succeeded`, i.e.
    everything between `terraform output -raw` and the terminal report. Extracted from
    `_agentcore_branch()` (`yaml.safe_load`), never from stripped file lines."""
    lines = [
        line.strip()
        for line in _agentcore_branch().split("\n")
        if line.strip() and not line.strip().startswith("#")
    ]
    start = next(i for i, line in enumerate(lines) if line.startswith('if [ -z "$RUNTIME_ARN" ]'))
    end = next(i for i, line in enumerate(lines) if line.startswith("_tag; _st deployed"))
    assert start < end, lines[start : end + 1]
    return lines[start : end + 1]


def test_the_registry_write_is_GUARDED_on_every_step():
    """There is no `set -e` anywhere in this buildspec, so an unguarded failure walks straight into
    `_st deployed; _dep succeeded`.

    While the ARN was stable across applies a dropped write left a correct value; stage-scoped
    names force replacement of both, so the stored ARN is guaranteed stale at this point: a lost
    write leaves a live, invocable, billing runtime whose ARN the platform records nowhere, and
    `_delete_runtime` derives everything from `resolve_runtime_arns(agent)`, so the delete cascade
    can never reclaim it. Reported as a successful deploy. Each of the three failure paths records
    the failure the way the terraform-apply guard does rather than aborting silently or falling
    through; the jq that builds the envelope may not leave NEW_ENV unset by rc alone, and the write
    itself carries an `||` guard instead of sitting bare."""
    block = "\n".join(_registry_write_block())
    assert block.count("_st failed") == 3, block
    assert block.count("_dep failed") == 3, block
    assert block.count("exit 1") == 3, block
    (jq_line,) = [line for line in _registry_write_block() if line.startswith("NEW_ENV=")]
    assert jq_line.rstrip().endswith('|| NEW_ENV=""'), jq_line
    (write_line,) = [line for line in _registry_write_block() if "update-registry-record" in line]
    assert "||" in write_line and "_st failed" in write_line, write_line


def _registry_read_line():
    """The single runtime line that GETs the agent's registry record for this build."""
    (line,) = [
        raw.strip()
        for raw in _agentcore_branch().split("\n")
        if "get-registry-record" in raw and not raw.strip().startswith("#")
    ]
    return line


def _registry_write_call_line():
    """The single runtime line that PATCHes the runtime ARN back onto the registry record."""
    (line,) = [raw for raw in _registry_write_block() if "update-registry-record" in raw]
    return line


def test_the_registry_calls_name_the_AGENT_REGISTRY_namespace_not_the_retired_one():
    """The Registry moved off `bedrock-agentcore` onto `agent-registry`, and the old namespace shut
    down on 2026-09-17; brand-new AWS accounts cannot call it at all. So this is not a tidy-up: a
    buildspec left on the old name deploys a runtime and then fails the write-back on the very next
    line, which is the untracked-runtime data-loss path the guard below reports.

    Asserted over the WHOLE FILE, not just the two calls, because the failure this prevents is a
    partial migration — one call moved, its sibling missed. Comments are included deliberately: an
    old-namespace name in a comment is the seed of a future "fix" back onto a dead endpoint, and
    this buildspec makes no non-registry AgentCore API call for a mention to legitimately describe.
    Note the IAM verb is the signing name `agent-registry:*` (granted to the CodeBuild role), which
    differs from the CLI name asserted here."""
    assert "bedrock-agentcore" not in BUILDSPEC.read_text()
    for line in (_registry_read_line(), _registry_write_call_line()):
        assert "aws agent-registry-control " in line, line


def test_the_registry_read_uses_the_RENAMED_custom_descriptor_leaf():
    """The Registry move renamed the custom descriptor's blob `inlineContent` -> `data`, and `data`
    is now the only member of `custom`, asserted against the real botocore output shape here, so
    this test tracks the model rather than a reading of it.

    A stale `.inlineContent` does not fail: `jq -r` prints the string "null", every
    `.model_id // ""` below it comes back empty, and the build provisions a runtime with no model,
    no Entra app and no Langfuse key while reporting success. Silent mis-provision beats loud
    failure at being hard to find. No code line may still name the retired leaf; a comment
    explaining the rename may."""
    from botocore.model import StructureShape
    from botocore.session import get_session

    output = (
        get_session()
        .get_service_model("agent-registry-control")
        .operation_model("GetRegistryRecord")
        .output_shape
    )
    assert isinstance(output, StructureShape)
    fields = output.members
    assert isinstance(fields, dict)
    members = fields["descriptors"].members["custom"].members
    assert set(members) == {"data"}, list(members)

    (env_line,) = [line for line in _agentcore_branch().split("\n") if line.strip().startswith("ENV=$(")]
    assert ".descriptors.custom.data" in env_line, env_line
    code = [
        line for line in _agentcore_branch().split("\n") if line.strip() and not line.strip().startswith("#")
    ]
    assert [line for line in code if "inlineContent" in line] == []


def test_the_registry_write_sends_the_PATCH_SHAPE_THE_REAL_MODEL_ACCEPTS():
    """The half a namespace swap does not cover. `UpdateRegistryRecord` is a PATCH API and its
    nesting got DEEPER in the move: every leaf under an `Updated*Fields` struct is itself an
    `Updated*` wrapper, so the CUSTOM blob sits THREE `optionalValue` levels down
    (`descriptors -> custom -> data`) where the retired namespace took two. The retired shape fails
    botocore param validation client-side, so the write never leaves the box — same outcome as the
    dead namespace, and equally invisible offline.

    Runs the REAL shell fragment with `aws` stubbed to capture argv, then feeds the captured
    `--descriptors` string to botocore's own `ParamValidator`. That is what makes this a proof
    rather than a transcription: the shell's `\\"` escaping, `jq -Rs .`'s quoting and the model's
    shape are all exercised at once. Mirrors `RecordContent.update_params` in
    `services/registry_writer.py`, the in-repo precedent for this envelope. The call is
    reconstructed as boto3 would see it from the CLI flags actually passed, and non-vacuity is
    pinned: the blob really is the patched envelope, three levels down, and the ARN the whole
    guarded block exists to record actually reached it."""
    import json
    import subprocess
    import tempfile

    from botocore.session import get_session
    from botocore.validate import ParamValidator

    with tempfile.TemporaryDirectory() as tmp:
        cap = Path(tmp) / "argv"
        script = (
            "_st() { :; }\n_dep() { :; }\n_tag() { :; }\n"
            f'aws() {{ printf "%s\\0" "$@" > {cap}; }}\n'
            "AGENT_REGISTRY_ID=reg\nAGENT_ID=ag\nSTAGE=dev\n"
            "RUNTIME_ARN=arn:aws:bedrock-agentcore:eu-central-1:1:runtime/r-1\n"
            'ENV=\'{"model_id":"m","agent_arns":{}}\'\n' + "\n".join(_registry_write_block()) + "\n"
        )
        out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, f"stdout={out.stdout!r} stderr={out.stderr!r}"
        argv = [a for a in cap.read_text().split("\0") if a]

    flags = dict(pairwise(argv))
    params = {
        "registryId": flags["--registry-id"],
        "recordId": flags["--record-id"],
        "descriptors": json.loads(flags["--descriptors"]),
    }
    report = ParamValidator().validate(
        params,
        get_session()
        .get_service_model("agent-registry-control")
        .operation_model("UpdateRegistryRecord")
        .input_shape,
    )
    assert not report.has_errors(), report.generate_report()

    blob = params["descriptors"]["optionalValue"]["custom"]["optionalValue"]["data"]["optionalValue"]
    assert json.loads(blob)["agent_arns"]["dev"].endswith("runtime/r-1"), blob


@pytest.mark.parametrize(
    "env_json,runtime_arn,aws_rc,expect_exit,expect_aws_called",
    [
        ('{"model_id":"m","agent_arns":{}}', "arn:aws:x:new", 0, 0, True),
        ('{"model_id":"m"}', "", 0, 1, False),
        ('{"model_id":"m","agent_arns":"oops"}', "arn:aws:x:new", 0, 1, False),
        ("not json at all", "arn:aws:x:new", 0, 1, False),
        ('{"model_id":"m","agent_arns":{}}', "arn:aws:x:new", 254, 1, True),
    ],
    ids=["happy", "empty-arn", "legacy-string-map", "garbage-envelope", "write-fails"],
)
def test_the_registry_write_guards_ACTUALLY_fire(
    env_json, runtime_arn, aws_rc, expect_exit, expect_aws_called
):
    """EXECUTES the real guarded block. Static text cannot prove a `jq -e` rejects an empty string
    or that an `||` catches the rc — and the failure mode (a guard that never fires) is
    indistinguishable from a correct deploy.

    `aws` is stubbed as a shell function so no call leaves the machine. Assignments are
    NON-exported and the fragment runs under `sh -c`, matching production: this buildspec's
    variables are plain assignments, so a `VAR=x cmd` prefix would validate an environment
    production does not have.

    An empty ARN means `terraform output -raw` failed, and writing it would set agent_arn="" and
    flip Agent.is_agentcore to False while the deploy reports success. A legacy envelope whose
    agent_arns is a string makes jq exit 5 with empty stdout, which `jq -Rs .` would render as a
    quoted newline and erase the descriptor's `data` blob (model_id, entra_app_id,
    langfuse_key_secret_name and the whole ARN map). A truncated or non-JSON envelope is refused
    too, and the write itself can fail (throttle, expired creds, a transient control-plane 5xx).
    Whatever the cause, a failure must never report deployed or succeeded."""
    import subprocess

    script = (
        '_st() { echo "ST:$1"; }\n'
        '_dep() { echo "DEP:$1"; }\n'
        "_tag() { echo TAGGED; }\n"
        f"aws() {{ echo AWS_CALLED; return {aws_rc}; }}\n"
        "AGENT_REGISTRY_ID=reg\n"
        "AGENT_ID=ag\n"
        "STAGE=dev\n"
        f"RUNTIME_ARN='{runtime_arn}'\n"
        f"ENV='{env_json}'\n" + "\n".join(_registry_write_block()) + "\n"
    )
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == expect_exit, f"stdout={out.stdout!r} stderr={out.stderr!r}"
    assert ("AWS_CALLED" in out.stdout) is expect_aws_called, out.stdout
    if expect_exit:
        assert "ST:deployed" not in out.stdout, out.stdout
        assert "DEP:succeeded" not in out.stdout, out.stdout
        assert "ST:failed" in out.stdout and "DEP:failed" in out.stdout, out.stdout
        assert "ERROR:" in out.stdout, out.stdout
    else:
        assert "ST:deployed" in out.stdout and "DEP:succeeded" in out.stdout, out.stdout
        assert "TAGGED" in out.stdout, out.stdout


_CLI_SERVICE_UNKNOWN = "aws: error: argument command: Invalid choice, maybe you meant: agent-registry"
_CLI_SERVICE_KNOWN = "aws: error: the following arguments are required: operation"
_PROBE_RE = r'(!?)\s*AWS_PAGER="" aws\s+([a-z0-9-]+)\s+2>&1\s*\|\s*grep\s+-qi\s+"arguments are required"'


def _code_lines(cmd: str) -> list[str]:
    """A runtime command's EXECUTABLE lines, shell comments dropped.

    Same reason as `_agentcore_code_lines`: this probe carries ~60 lines of comment explaining
    itself, and every one of them contains the words this group asserts on ("arguments are
    required", "invalid choice", "agent-registry-control", "help"). Any assertion made over the raw
    command matches the PROSE first and passes no matter what the code does."""
    return [line.strip() for line in cmd.split("\n") if line.strip() and not line.strip().startswith("#")]


def _cli_capability_probe():
    """(flat command index, phase, command) of the AWS CLI capability probe.

    The registry tests pin the two Registry calls onto `agent-registry-control`; naming the service
    correctly is worthless if the CLI running them has never heard of it. The CodeBuild image ships
    whatever CLI it was baked with, and a real AWS CLI v2 (2.35.16) bundled the retired
    `bedrock-agentcore-control` and zero `agent-registry*` service models, so
    `aws agent-registry-control` exits 252 with `Invalid choice`. No botocore-based test can see it:
    the backend venv's botocore carries the service, while a self-contained AWS CLI v2 binary bundles
    its own, so a green suite says nothing about the build image. `Invalid choice` on the read aborts
    early and harmlessly; on the write it is the untracked-runtime data-loss path
    `test_the_registry_write_is_GUARDED_on_every_step` describes. Hence a capability probe in
    `install`, and this group, because that probe is a lone `if` in a phase full of setup noise.

    Identified by its discriminator (`_PROBE_RE`: an `aws <service>` invocation whose stdout+stderr
    is piped into a `grep -qi` for the healthy parse error), not by a comment marker, so the probe
    cannot be reduced to a comment and keep passing. The regex keys on the positive string, since
    `test_the_probe_PROVES_capability_and_does_NOT_infer_it_from_an_absent_failure_string` shows the
    negative form is a bug, and its `(!?)` group captures whether the branch is negated, which
    distinguishes the first probe (assert healthy) from the re-probe (fail if not healthy). The flat
    index is the position among all phase commands in the order CodeBuild runs them, which is what
    the ordering assertion below needs."""
    hits = [
        (i, phase, cmd)
        for i, (phase, cmd) in enumerate(_runtime_commands())
        if any(re.search(_PROBE_RE, line) for line in _code_lines(cmd))
    ]
    assert len(hits) == 1, f"expected exactly one AWS CLI capability probe, got {hits}"
    return hits[0]


def _probed_services() -> set[str]:
    """Every service name the probe actually tests the CLI for (read off the probe's own lines)."""
    _, _, cmd = _cli_capability_probe()
    return {m.group(2) for line in _code_lines(cmd) for m in [re.search(_PROBE_RE, line)] if m}


def test_the_cli_capability_probe_EXISTS_and_runs_BEFORE_the_first_registry_call():
    """Placement is the whole value. A probe next to the first call would fail AFTER the read, or —
    the state that matters — leave the WRITE as the first thing to discover the CLI cannot make it,
    which is the untracked-runtime path. In `install` nothing has been mutated and nothing can leak,
    so a missing capability is merely an inconvenience.

    Pinned as an ordering over the flattened runtime command list rather than "is in install",
    because the property is *before the first call*, and both halves are asserted: the probe sits in
    `install`, the calls it protects sit in `build`, and the probe's index is lower.

    A CALL is `aws agent-registry-control <operation>`. The operation is what makes it one: the probe
    invokes the same service with NO operation (`aws agent-registry-control 2>&1 | …`), which is the
    entire trick that keeps it credential-free and off the network — so a substring match on the
    service name finds the probe itself and reports it as its own first call."""
    probe_i, probe_phase, _ = _cli_capability_probe()
    calls = [
        (i, phase)
        for i, (phase, cmd) in enumerate(_runtime_commands())
        if any(re.search(r"aws\s+agent-registry-control\s+[a-z]", line) for line in _code_lines(cmd))
    ]
    assert calls, "no executable `aws agent-registry-control` call found — has the namespace moved?"
    assert probe_phase == "install", probe_phase
    first_call_i, first_call_phase = min(calls)
    assert first_call_phase == "build", first_call_phase
    assert probe_i < first_call_i, (
        f"the capability probe (command #{probe_i}, {probe_phase}) must run BEFORE the first "
        f"Registry call (command #{first_call_i}, {first_call_phase})"
    )


def test_the_probe_TESTS_THE_SERVICE_THE_BUILD_ACTUALLY_CALLS():
    """The drift this group is really here to catch. A probe and the calls it guards are edited
    independently, so the next namespace move can rename one and not the other — and BOTH directions
    fail silently. Probe moved, calls left behind: the build certifies a capability it never uses and
    then dies on the call. Calls moved, probe left behind: the probe passes on a CLI that cannot make
    the new call, which is exactly the unguarded state the probe was added for.

    So the service name is not written down twice: it is read off the probe and off the two call
    sites and required to be the same single name. The retired namespace is also banned by name,
    the one string that would make all of the above agree with each other and still be wrong (the
    file-wide `bedrock-agentcore` ban lives in the namespace test above; this pins the control-plane
    service specifically, so a partial revert fails by name here too)."""
    called: set[str] = set()
    for line in (_registry_read_line(), _registry_write_call_line()):
        match = re.search(r"aws\s+([a-z0-9-]+)\s", line)
        assert match is not None
        called.add(match.group(1))
    assert called == {"agent-registry-control"}, called
    assert _probed_services() == called, (_probed_services(), called)
    assert "bedrock-agentcore-control" not in BUILDSPEC.read_text()


def test_the_probe_FAILS_THE_BUILD_rather_than_WARNING_when_the_capability_is_absent():
    """A probe that only logged would be worse than none: the build would report the exact reason it
    was about to lose a runtime, in a passing log nobody reads, and then lose it.

    Every half is pinned off code. The `exit 1` exists; it is reached only from the RE-probe (the
    first probe's job is to trigger the upgrade, not to fail); it is accompanied by an `ERROR:` on
    stderr; and the fatal path carries no `|| true`-class swallow. `phases.install` also declares no
    `on-failure: CONTINUE`, without which CodeBuild would run the next phase regardless of the
    `exit 1`.

    There are two probe lines, the probe then the authoritative re-probe, and the re-probe is the
    gate on `exit 1`, not the initial one. Nothing on the fatal path may swallow it; the one
    tolerated failure is the download chain, whose `|| echo` is deliberate (the re-probe, not the
    download's rc, decides the verdict), and `hash -r || true` is the only `|| true`. `help` is
    rejected as the probe for a reason that is a build outage, not a nicety: AWS CLI v2 renders help
    through a man pager, so on an image without groff it exits non-zero for a service the CLI knows,
    firing the upgrade on a healthy image and then failing this very `exit 1`."""
    import yaml

    _, _, cmd = _cli_capability_probe()
    lines = _code_lines(cmd)

    (exit_i,) = [i for i, line in enumerate(lines) if line == "exit 1"]
    probe_is = [i for i, line in enumerate(lines) if re.search(_PROBE_RE, line)]
    assert len(probe_is) == 2, probe_is
    assert probe_is[1] < exit_i, (probe_is, exit_i)
    assert probe_is[0] < probe_is[1] < exit_i

    (error_i,) = [i for i, line in enumerate(lines) if line.startswith('echo "ERROR:')]
    assert error_i < exit_i, (error_i, exit_i)
    assert lines[error_i].rstrip().endswith(">&2"), lines[error_i]
    assert "Refusing to start" in lines[error_i]
    for i in (probe_is[1], error_i, exit_i):
        assert "|| true" not in lines[i], lines[i]
    assert sum("|| true" in line for line in lines) <= 1, lines

    assert not any(re.search(r"aws\s+[a-z0-9-]+\s+help", line) for line in lines), lines

    install = yaml.safe_load(BUILDSPEC.read_text())["phases"]["install"]
    assert install.get("on-failure", "ABORT") == "ABORT", install.get("on-failure")


def test_the_probe_PROVES_capability_and_does_NOT_infer_it_from_an_absent_failure_string():
    """The fail-open test. A probe written as

        if ! aws agent-registry-control 2>&1 | grep -qi "invalid choice"; then  # "healthy"

    asserts the absence of one known failure string, so every other outcome is silently
    classified as healthy: `aws` missing from `PATH`, an `ImportError` traceback from a half-installed
    CLI, empty output, or AWS simply rewording its unknown-service message. Each of those printed
    "aws CLI knows agent-registry-control" and exited 0, i.e. the guard became a no-op on precisely
    the image that CANNOT make the call, while the build log certified the capability. That is the
    unguarded state the probe was added to prevent, wearing a green log.

    So the contract is: the healthy branch must be entered by MATCHING the positive signal, never by
    failing to match a negative one. Both directions are pinned here — the presence of the positive
    form, and the absence of any capability decision resting on the unknown-service text — because a
    future edit that "restores" the old condition is the exact regression this file exists to stop.
    `test_the_probe_FAILS_CLOSED_on_every_unhealthy_CLI` proves the behavioural consequence by
    execution; this one pins the shape so the failure is legible when it breaks.

    The first probe takes the healthy branch on a match (not negated): capability proven. The
    re-probe is negated (`if ! <healthy>`), the same positive signal, so an `aws` that crashes,
    vanishes or prints nothing after the upgrade fails rather than reporting a successful heal. No
    capability decision anywhere may key on the unknown-service text: a `grep` for it inside an
    `if`/`while` is the old fail-open form, and it must not come back in either position."""
    _, _, cmd = _cli_capability_probe()
    lines = _code_lines(cmd)

    probes = [(i, m) for i, line in enumerate(lines) for m in [re.search(_PROBE_RE, line)] if m]
    assert len(probes) == 2, probes

    first_i, first_m = probes[0]
    assert first_m.group(1) == "", lines[first_i]
    second_i, second_m = probes[1]
    assert second_m.group(1) == "!", lines[second_i]

    for line in lines:
        assert not re.search(r"\bif\b.*grep[^|]*invalid choice", line, re.I), line
        assert not re.search(r"\bif\b.*grep[^|]*maybe you meant", line, re.I), line


@pytest.mark.parametrize(
    "first_probe,second_probe,expect_exit,expect_upgrade_attempted",
    [
        (_CLI_SERVICE_KNOWN, _CLI_SERVICE_KNOWN, 0, False),
        (_CLI_SERVICE_UNKNOWN, _CLI_SERVICE_UNKNOWN, 1, True),
        (_CLI_SERVICE_UNKNOWN, _CLI_SERVICE_KNOWN, 0, True),
    ],
    ids=["cli-knows-it", "absent-and-upgrade-fails", "absent-then-upgrade-heals"],
)
def test_the_probe_ACTUALLY_discriminates_the_two_parse_errors(
    tmp_path, first_probe, second_probe, expect_exit, expect_upgrade_attempted
):
    """EXECUTES the real probe. Static text cannot prove a `grep -qi` tells the two parse errors
    apart, and the failure mode, a probe that never fires, is indistinguishable from a healthy
    image right up to the moment a runtime goes missing. `_CLI_SERVICE_UNKNOWN` and
    `_CLI_SERVICE_KNOWN` are the two parse errors a credential-free `aws <service>` with no operation
    returns, both verified live against AWS CLI v2 2.35.16; both exit 252, so a probe that branched
    on the exit code would pass every static assertion above and be inert here.

    `aws` and `curl` are stubbed as shell functions, so nothing leaves the machine and no installer
    runs: the download chain is short-circuited at its first link, which also proves the documented
    property that the RE-PROBE is authoritative and the download's exit status is not — the
    `absent-then-upgrade-heals` case passes with a *failed* download.

    Run under `sh -c` with NON-exported assignments, matching production: this buildspec's only
    `export`s are its three AWS credential lines."""
    import subprocess

    _, _, cmd = _cli_capability_probe()
    state = tmp_path / "probed-once"
    script = (
        "aws() {\n"
        '  if [ "$1" = "--version" ]; then echo "aws-cli/2.0.0-stub"; return 0; fi\n'
        f'  if [ -f {state} ]; then echo "{second_probe}"; else : > {state}; echo "{first_probe}"; fi\n'
        "  return 252\n"
        "}\n"
        "curl() { echo UPGRADE_ATTEMPTED; return 1; }\n" + cmd + "\n"
    )
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)

    assert out.returncode == expect_exit, f"stdout={out.stdout!r} stderr={out.stderr!r}"
    assert ("UPGRADE_ATTEMPTED" in out.stdout) is expect_upgrade_attempted, out.stdout
    if expect_exit:
        assert "Refusing to start" in out.stderr, out.stderr
        assert "is available" not in out.stdout, out.stdout
    else:
        assert "Refusing to start" not in out.stderr, out.stderr
        assert "agent-registry-control" in out.stdout, out.stdout


_CERTIFIED = ("aws CLI knows agent-registry-control", "agent-registry-control is available")


def _assert_never_certified(stdout: str, ctx: str) -> None:
    """No success marker may appear on an unhealthy run.

    Both markers are also required to EXIST in the probe source, so a reword cannot make these
    assertions vacuously true — which is the failure mode a "string is absent" test invites: rename
    the success echo and the fail-open bug walks straight back in under a green suite."""
    _, _, cmd = _cli_capability_probe()
    for marker in _CERTIFIED:
        assert marker in cmd, f"success marker {marker!r} is no longer in the probe — update _CERTIFIED"
        assert marker not in stdout, f"{ctx} (certified via {marker!r})"


_UNHEALTHY_CLIS = {
    "unknown-service": (
        f'echo "{_CLI_SERVICE_UNKNOWN}" >&2; exit 252',
        "the service name is rejected — the case the probe was written for",
    ),
    "aws-missing-from-PATH": (
        None,
        "no binary at all: the shell writes `aws: not found` and returns 127",
    ),
    "crashing-cli": (
        'echo "Traceback (most recent call last):" >&2;'
        ' echo "ImportError: No module named awscli" >&2; exit 1',
        "a half-installed CLI: a traceback names no service at all",
    ),
    "silent-cli": (
        "exit 252",
        "non-zero with EMPTY output — matches no string, negative or positive",
    ),
    "reworded-unknown-service": (
        "echo \"aws: error: Unknown service 'agent-registry-control'\" >&2; exit 252",
        "AWS rewording its own message, which the negative match cannot survive",
    ),
}


@pytest.mark.parametrize("case", sorted(_UNHEALTHY_CLIS), ids=sorted(_UNHEALTHY_CLIS))
def test_the_probe_FAILS_CLOSED_on_every_unhealthy_CLI(tmp_path, case):
    """EXECUTES the probe against a CLI that cannot prove the capability, five ways, and requires it
    to refuse the build every time.

    `_UNHEALTHY_CLIS` holds every way a build image's `aws` can fail to prove it knows the service;
    `None` means the stub is not created at all, `aws` absent from `PATH`, which a negative-match
    probe scores as healthy because "command not found" contains no "invalid choice" either. The
    fail-open defect is invisible statically (a negative-match condition looks like a capability
    check and reads fine), and only an executed stub shows it printing "aws CLI knows
    agent-registry-control" and exiting 0 for four of these five inputs. The parametrisation is
    deliberately wider than the two strings the probe handles by name, because "the failures we
    enumerated" is exactly the assumption that breaks: the guard must fail closed on outputs nobody
    predicted, including AWS rewording its own error and the CLI producing no output at all.

    `curl` is stubbed to fail, so the upgrade cannot heal any case and the re-probe sees the same
    unhealthy `aws`, which makes `exit 1` the only correct outcome for all five. `PATH` is replaced
    (not prepended) so `aws-missing-from-PATH` is a genuine absence rather than a shadowed binary.
    An unhealthy CLI must never be reported as a working one, on either the initial probe or the
    post-upgrade re-probe."""
    import subprocess

    body, _why = _UNHEALTHY_CLIS[case]
    _, _, cmd = _cli_capability_probe()

    binder = tmp_path / "bin"
    binder.mkdir()
    (binder / "curl").write_text("#!/bin/sh\necho UPGRADE_ATTEMPTED\nexit 1\n")
    (binder / "curl").chmod(0o755)
    if body is not None:
        (binder / "aws").write_text(
            '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "aws-cli/2.0.0-stub"; exit 0; fi\n' + body + "\n"
        )
        (binder / "aws").chmod(0o755)

    out = subprocess.run(
        ["sh", "-c", cmd],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": f"{binder}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )

    ctx = f"case={case} rc={out.returncode} stdout={out.stdout!r} stderr={out.stderr!r}"
    assert out.returncode != 0, ctx
    assert "Refusing to start" in out.stderr, ctx
    _assert_never_certified(out.stdout, ctx)


def test_a_HEALTHY_cli_is_the_ONLY_input_that_certifies_the_capability(tmp_path):
    """The other half of fail-closed: proving the guard is not merely refusing everything.

    A probe that rejected every input would pass all five cases above and break every build. So the
    healthy CLI — the one printing `the following arguments are required: operation`, the real string
    AWS CLI v2 returns for a service it knows invoked with no operation — is run through the same
    harness and must certify the capability, exit 0, and never attempt the upgrade."""
    import subprocess

    _, _, cmd = _cli_capability_probe()
    binder = tmp_path / "bin"
    binder.mkdir()
    (binder / "curl").write_text("#!/bin/sh\necho UPGRADE_ATTEMPTED\nexit 1\n")
    (binder / "curl").chmod(0o755)
    (binder / "aws").write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "aws-cli/2.0.0-stub"; exit 0; fi\n'
        f'echo "{_CLI_SERVICE_KNOWN}" >&2\nexit 252\n'
    )
    (binder / "aws").chmod(0o755)

    out = subprocess.run(
        ["sh", "-c", cmd],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": f"{binder}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )

    ctx = f"rc={out.returncode} stdout={out.stdout!r} stderr={out.stderr!r}"
    assert out.returncode == 0, ctx
    assert "knows agent-registry-control" in out.stdout, ctx
    assert "Refusing to start" not in out.stderr, ctx
    assert "UPGRADE_ATTEMPTED" not in out.stdout, ctx


def _drift_guard_line():
    """The runtime line that fails the build when the CLONED root does not declare `stage`."""
    body = _agentcore_branch()
    hits = [
        line.strip()
        for line in body.split("\n")
        if not line.strip().startswith("#") and "STALE RUNTIME MODULE" in line
    ]
    assert len(hits) == 1, f"expected exactly one drift guard line, got {hits}"
    return hits[0]


def test_a_stale_cloned_module_FAILS_the_build_instead_of_deploying_silently():
    """The structural guard.

    **The module in this repo is not the module that runs.** `runtime_build_service.py` sends
    `GIT_INFRA_ORG`/`GIT_INFRA_REPO`, the install phase git-clones the per-org `agp-runtime-infra`
    repo into `/tmp/workspace`, and this branch applies whatever `.tf` it finds there. The S3
    module zip is consumed only by a create-once rollout. So every terraform edit in this repo is
    INERT until the module is rolled out to the org.

    That is fine — but an unrolled module makes `stage="$STAGE"` a terraform WARNING ("Value for
    undeclared variable"), not an error: the value is discarded, the resource names stay
    account-global, and the build goes GREEN having fixed nothing. That is exactly how the langfuse
    vars once stayed silently dropped.

    The guard must therefore run BEFORE `terraform init`/`plan`, and it must exit non-zero, recorded
    as a failed deploy rather than a silent abort: the badge and the history must agree."""
    line = _drift_guard_line()
    assert "variable" in line and "stage" in line, line
    assert "exit 1" in line, line
    assert "_st failed" in line and "_dep failed" in line, line


def _agentcore_code_lines():
    """The runtime branch's EXECUTABLE lines only — shell comments dropped.

    The guard's own comment block explains that it "Runs BEFORE `terraform init`", so an ordering
    assertion over the raw body finds that prose first and passes no matter where the real command
    sits; ordering must be read off code."""
    return [
        line.strip()
        for line in _agentcore_branch().split("\n")
        if line.strip() and not line.strip().startswith("#")
    ]


def test_the_drift_guard_runs_BEFORE_terraform_touches_anything():
    """Ordering is the whole point: a guard after `terraform apply` would fire only once the
    stale module had already created (or refused to create) account-global resources. It must sit
    after `cd "$TF_DIR"` (it inspects the cloned root) and before `terraform init`. The tfvars the
    guard protects are written before it, so a root missing `stage` is caught rather than merely
    warned about."""
    lines = _agentcore_code_lines()

    def index_of(needle):
        hits = [i for i, line in enumerate(lines) if needle in line]
        assert hits, f"no executable line containing {needle!r}"
        return hits[0]

    assert index_of('cd "$TF_DIR"') < index_of("STALE RUNTIME MODULE"), (
        "the guard must come after the workspace is resolved"
    )
    assert index_of("STALE RUNTIME MODULE") < index_of("terraform init "), (
        "the guard must come before terraform init — otherwise a stale module has already run"
    )
    assert index_of("cat > deploy.auto.tfvars") < index_of("STALE RUNTIME MODULE")


def test_the_drift_guard_is_scoped_to_the_agentcore_runtime_branch_only():
    """`IAC_TYPE` selects between branches, and the other branches apply developer-owned
    terraform that has no `stage` variable and never should. A guard leaking outside this case
    body would fail every non-runtime deploy. The marker lives inside the `agentcore_runtime)` case
    body `_agentcore_branch()` returns, the body `IAC_TYPE=agentcore_runtime` selects."""
    text = BUILDSPEC.read_text()
    assert text.count("STALE RUNTIME MODULE") == 1, "the guard must exist in exactly one branch"
    assert "STALE RUNTIME MODULE" in _agentcore_branch()


@pytest.mark.parametrize(
    "root_tf,expect_exit",
    [
        ('variable "stage" {\n  type = string\n}\n', 0),
        ('variable  "stage"  {\n  type = string\n}\n', 0),
        ("", 1),
        ('variable "agent_name" {\n  type = string\n}\n', 1),
        ('# stage is threaded by the buildspec\nvariable "agent_name" {}\n', 1),
        ('locals {\n  stage = "dev"\n}\n', 1),
    ],
    ids=["declared", "declared-loose-spacing", "empty-root", "stale-module", "comment-only", "local-only"],
)
def test_the_drift_guard_actually_fires_on_a_stale_module(tmp_path, root_tf, expect_exit):
    """EXECUTES the real guard against a synthetic cloned root. Static text cannot prove a `grep`
    pattern matches what it claims — and the failure mode here (a guard that never fires) is
    indistinguishable from a correct build.

    The `comment-only` and `local-only` cases are the ones that make this non-vacuous: a naive
    `grep -q stage *.tf` passes on BOTH, which would disarm the guard against precisely the
    module a well-meaning maintainer half-updates. `stale-module` is `agp-runtime-infra`'s real
    content as verified live: it declares `agent_name` and neither `stage` nor the langfuse vars.
    `empty-root` is the rolled-out-nothing case, and `declared-loose-spacing` shows extra whitespace
    still declares the variable.

    The variables are assigned NON-exported and the fragment runs under `sh -c`, matching the
    production condition (this file's only `export`s are the three AWS credential lines)."""
    import subprocess

    (tmp_path / "main.tf").write_text(root_tf)
    script = (
        f"_st() {{ :; }}\n_dep() {{ :; }}\ncd {tmp_path}\n{_drift_guard_line()}\necho REACHED_TERRAFORM\n"
    )
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == expect_exit, f"stdout={out.stdout!r} stderr={out.stderr!r}"
    if expect_exit:
        assert "REACHED_TERRAFORM" not in out.stdout, "the guard did not stop the build"
        assert "STALE RUNTIME MODULE" in out.stdout, out.stdout
    else:
        assert "REACHED_TERRAFORM" in out.stdout, out.stdout


_RUNTIME_MODULE = Path(__file__).resolve().parents[2] / "infrastructure/modules/agentcore_runtime"


def test_the_runtime_module_DECLARES_the_stage_the_buildspec_passes():
    """The module side of the same contract, pinned across the file boundary: the buildspec and the
    module are edited independently and neither imports the other, so every agreement between them
    is a convention a single edit can break silently. These tests read the real module rather than
    restating its text.

    The other end of `stage="$STAGE"`. A tfvar with no matching `variable` block is a terraform
    warning, so this pair is exactly the silent-drift class the guard above exists for, and here it
    is caught offline. Comments are stripped first: the variable's own comment explains at length
    why it has no `default`, so a substring check over the raw block would match that prose."""
    body = (_RUNTIME_MODULE / "variables.tf").read_text()
    code = "\n".join(line for line in body.split("\n") if not line.strip().startswith("#"))
    assert re.search(r'variable\s+"stage"\s*\{', code), "the module must declare var.stage"
    block = code.split('variable "stage"', 1)[1]
    block = block[: block.index("\nvariable ")] if "\nvariable " in block else block
    assert not re.search(r"^\s*default\s*=", block, re.MULTILINE), (
        "var.stage must have NO default — a defaulted stage silently names prod's resources 'dev'"
    )


_LOCAL_TEMPLATES = {
    "runtime_name": r'"\$\{var\.agent_name\}_\$\{var\.stage\}"',
    "exec_role_name": r'"\$\{var\.agent_name\}-\$\{var\.stage\}-agentcore-exec"',
}


def _locals_stage_scoped(body):
    """Which of the two module locals actually ASSIGN the stage-scoped template.

    Structural on purpose: a bare `'<literal>' in body` substring over raw `main.tf` text is
    satisfied by a `#` comment carrying the literal, and `modules/agentcore_runtime/README.md`
    already carries both literals verbatim, so one copy-paste into main.tf's comment header would
    disarm the guard. Comments are stripped first and the pattern is anchored to a line-start
    assignment, so neither defence stands alone. A trailing `#` comment on the real assignment is
    tolerated (both locals carry one, naming their AWS ceiling)."""
    code = "\n".join(
        line
        for line in body.split("\n")
        if not (line.strip().startswith("#") or line.strip().startswith("//"))
    )
    return {
        name
        for name, template in _LOCAL_TEMPLATES.items()
        if re.search(rf"^\s*{name}\s*=\s*{template}\s*(#.*)?$", code, re.M)
    }


def test_the_stage_scoped_locals_check_cannot_be_satisfied_by_a_COMMENT():
    """Non-vacuity for the sibling below.

    Un-stage-scoping both locals in the real `main.tf` while leaving the correct templates in a `#`
    block immediately above is the exact shape a half-update produces, and a substring check passes
    it; that mutation is fed in as a fixture so it can never pass again. The real shapes are
    recognised too, so the emptiness is the comment being rejected rather than the pattern being
    broken outright."""
    defeated = (
        "# locals {\n"
        '#   runtime_name   = "${var.agent_name}_${var.stage}"\n'
        '#   exec_role_name = "${var.agent_name}-${var.stage}-agentcore-exec"\n'
        "# }\n"
        "locals {\n"
        "  runtime_name   = var.agent_name\n"
        '  exec_role_name = "${var.agent_name}-agentcore-exec"\n'
        "}\n"
    )
    assert _locals_stage_scoped(defeated) == set(), (
        "a COMMENT carrying the template must not satisfy the check"
    )
    assert _locals_stage_scoped(
        "locals {\n"
        '  runtime_name   = "${var.agent_name}_${var.stage}"   # AWS: [a-zA-Z][a-zA-Z0-9_]{0,47}\n'
        '  exec_role_name = "${var.agent_name}-${var.stage}-agentcore-exec" # IAM: <= 64\n'
        "}\n"
    ) == {"runtime_name", "exec_role_name"}


def test_both_account_global_names_are_stage_scoped_in_the_module():
    """The IAM role name collided first; `agent_runtime_name` is the same class, latent
    (`CreateAgentRuntime` declares `ConflictException`, which prod never reached because IAM failed
    first). Both are pinned so neither can regress alone, both must resolve through the module's
    single `locals` block rather than inlining `var.stage` at the resource, and the locals really
    carry the stage, read off the assignments."""
    body = (_RUNTIME_MODULE / "main.tf").read_text()
    (role_name,) = re.findall(r'resource\s+"aws_iam_role"\s+"exec"\s*\{\s*\n\s*name\s*=\s*([^\n]+)', body)
    (runtime_name,) = re.findall(r"agent_runtime_name\s*=\s*([^\n]+)", body)
    assert role_name.strip() == "local.exec_role_name", role_name
    assert runtime_name.strip() == "local.runtime_name", runtime_name
    assert _locals_stage_scoped(body) == {"runtime_name", "exec_role_name"}, body


_GOOD_DIGEST = "sha256:" + "ab" * 32


def _digest_shape_guard():
    """The runtime line that refuses a malformed `IMAGE_DIGEST`, from `yaml.safe_load`."""
    hits = [
        line
        for line in _agentcore_code_lines()
        if line.startswith('if [ -n "${IMAGE_DIGEST:-}" ]; then echo "$IMAGE_DIGEST"')
    ]
    assert len(hits) == 1, f"expected exactly one digest shape guard, got {hits}"
    return hits[0]


def _bad_digest_helper():
    """The `_bad_digest` helper definition — the recorded-failure path both guards share."""
    hits = [line for line in _agentcore_code_lines() if line.startswith("_bad_digest()")]
    assert len(hits) == 1, hits
    return hits[0]


def _image_ref_lines():
    """The two runtime lines computing `IMAGE_REF` (the default, then the digest override)."""
    lines = _agentcore_code_lines()
    base = [line for line in lines if line.startswith("IMAGE_REF=")]
    override = [line for line in lines if line.startswith('[ -n "${IMAGE_DIGEST:-}" ] && IMAGE_REF=')]
    assert len(base) == 1 and len(override) == 1, (base, override)
    return base[0], override[0]


def test_the_tfvars_deploy_the_resolved_reference_not_a_raw_tag():
    """Promotion deploys a digest, not a mutable tag.

    A `container_image_uri` of `$ECR_REPO:$IMAGE_TAG` names a mutable tag: the tenant ECR repo is
    mutable and this image build is not reproducible (the install pins every dependency through the
    template's `constraints.txt`, but the base image tag still floats), so the bytes behind one tag
    can differ between the moment an OWNER approves and the moment prod deploys. The digest names
    the bytes themselves. It is threaded in from the backend (`runtime_build_service.py` sets
    `IMAGE_DIGEST`) and deliberately not re-derived here from the tag: a
    `describe-images --image-ids imageTag=…` lookup would return whatever the tag points at now,
    which is precisely the window the digest closes. Every digest assertion reads the fragment from
    `yaml.safe_load` and, where behaviour is the claim, executes it under `sh -c` with non-exported
    variables, the production condition.

    Read off the parsed tfvars heredoc rather than by grepping the file, and the forbidden shape is
    reconstructed from its parts rather than written out as a literal, so it never appears in this
    file as a quotable string: this file's own prose discusses the old `<repo>:<tag>` form at length,
    and a substring check would be satisfied by that prose."""
    (line,) = [raw for raw in _tfvars_heredoc().split("\n") if raw.startswith("container_image_uri=")]
    assert line == 'container_image_uri="$IMAGE_REF"', line
    forbidden = '"$ECR_REPO' + ":" + '$IMAGE_TAG"'
    assert forbidden not in line, line


def test_the_image_reference_prefers_a_digest_and_falls_back_to_the_tag():
    """Both halves pinned: the `@digest` form when a digest exists, the `:tag` form when not.

    The fallback is not a courtesy — a rollback validates its target from tag-keyed `Deployment`
    rows that carry NO digest, and an agent repo on an older workflow POSTs no digest at all.
    Removing it would make both undeployable."""
    base, override = _image_ref_lines()
    assert base == 'IMAGE_REF="$ECR_REPO:$IMAGE_TAG"', base
    assert 'IMAGE_REF="$ECR_REPO@$IMAGE_DIGEST"' in override, override


def test_the_image_reference_is_computed_AFTER_the_cross_account_copy():
    """Ordering, and it matters rather than being stylistic.

    On a cross-account promote the digest that must be DEPLOYED is the one in the TARGET registry,
    and it does not exist until the copy has run. Computing `IMAGE_REF` before the copy would pin
    the SOURCE digest — which need not exist in the target at all, since a pull/tag/push through
    the docker daemon can re-serialize the manifest. That failure appears only on the
    cross-account path, the one with the least local evidence. The tfvars are written after the
    reference is resolved, or they would carry nothing."""
    lines = _agentcore_code_lines()

    def index_of(prefix):
        hits = [i for i, line in enumerate(lines) if line.startswith(prefix)]
        assert hits, f"no executable line starting {prefix!r}"
        return hits[0]

    assert index_of('if [ "${SOURCE_ECR_REQUIRED:-}"') < index_of("IMAGE_REF="), lines
    assert index_of("IMAGE_REF=") < index_of("cat > deploy.auto.tfvars"), lines


def test_the_digest_guard_is_recorded_as_a_failure_not_a_bare_exit():
    """A refusal must leave the badge and the history agreeing. A bare
    `exit 1` would abort the build with the record still reading whatever it read before."""
    helper = _bad_digest_helper()
    assert "_st failed" in helper and "_dep failed" in helper, helper
    assert "exit 1" in helper, helper


def test_the_digest_is_never_re_derived_from_the_tag():
    """The distinction the whole design rests on, pinned as an ABSENCE.

    `describe-images` is legitimate for reading back bytes we just pushed (the cross-account copy
    below). It is NOT legitimate as a way to resolve the digest to deploy: keyed on `imageTag` it
    returns whatever the tag points at NOW, so a tag overwritten between approval and deploy would
    silently ship unapproved bytes — the exact hole the digest exists to close.

    So the ONLY `describe-images` call in this branch must be the target-registry read-back inside
    the cross-account copy, and it must be positioned after the push it verifies. It resolves into
    TARGET_DIGEST, a verification of what we wrote, and its result is shape-checked before use
    rather than trusted."""
    lines = _agentcore_code_lines()
    calls = [i for i, line in enumerate(lines) if "describe-images" in line]
    assert len(calls) == 1, f"expected exactly one describe-images call, got {[lines[i] for i in calls]}"
    (call_idx,) = calls
    push_idx = next(i for i, line in enumerate(lines) if "docker push" in line)
    assert push_idx < call_idx, "the read-back must follow the push whose bytes it resolves"
    assert lines[call_idx].startswith("TARGET_DIGEST="), lines[call_idx]


def test_the_cross_account_copy_pulls_by_digest_when_one_exists():
    """A tag-based pull is a MUTABLE read on the promotion path: between the approval and this copy
    the source tag may point at different bytes, so it can copy an image the OWNER never approved.
    Both the digest form and the tag fallback are pinned, and the pull uses the resolved reference,
    never the raw tag again."""
    lines = _agentcore_code_lines()
    (base,) = [line for line in lines if line.startswith("SRC_REF=")]
    (override,) = [line for line in lines if 'SRC_REF="$SOURCE_ECR_REPO@$IMAGE_DIGEST"' in line]
    assert base == 'SRC_REF="$SOURCE_ECR_REPO:$IMAGE_TAG"', base
    assert override.startswith('[ -n "${IMAGE_DIGEST:-}" ]'), override
    (pull,) = [line for line in lines if line.startswith("docker pull ")]
    assert pull.startswith('docker pull "$SRC_REF"'), pull


@pytest.mark.parametrize(
    "digest,expect_exit",
    [
        (_GOOD_DIGEST, 0),
        ("sha256:" + "de" * 32, 0),
        ("", 0),
        ("sha256:" + "a" * 63, 1),
        ("sha256:" + "a" * 65, 1),
        ("sha256:" + "AB" * 32, 1),
        ("ab" * 32, 1),
        ("sha512:" + "a" * 64, 1),
        ("None", 1),
        (_GOOD_DIGEST + "x", 1),
        (_GOOD_DIGEST + "; echo PWNED", 1),
    ],
    ids=[
        "well-formed",
        "well-formed-but-wrong-image",
        "empty-legacy",
        "truncated",
        "too-long",
        "uppercase",
        "no-prefix",
        "wrong-algo",
        "literal-None",
        "trailing-junk",
        "metachar",
    ],
)
def test_the_digest_shape_guard_ACTUALLY_fires(digest, expect_exit):
    """EXECUTES the real guard. Static text cannot prove a `grep -E` pattern matches what it
    claims, and the failure mode here — a guard that never fires — is indistinguishable from a
    correct deploy.

    `IMAGE_DIGEST` is assigned NON-exported and the fragment runs under `sh -c`, matching
    production: this buildspec's only `export`s are the three AWS credential lines, so an
    env-prefix form would validate an environment production never provides.

    `_GOOD_DIGEST` is a well-formed digest; the other rows are the mutations that must all be
    refused, data-flow mutations and not just control-flow: a wrong-but-well-formed digest cannot be
    detected offline and must be accepted (proving the guard checks shape and does not pin one
    value), while every malformed shape fails the build loudly: one hex short or long, uppercase
    (registries emit lowercase), no algorithm prefix, the wrong algorithm, the literal `None` the
    AWS CLI prints for a missing image, trailing junk, and a shell metacharacter, which must never
    reach a shell that would run it. The empty digest is the legacy tag-only path. A refusal happens
    before any reference is built and is recorded as a failure."""
    import subprocess

    script = (
        "_st() { echo ST:$1; }\n"
        "_dep() { echo DEP:$1; }\n"
        f"{_bad_digest_helper()}\n"
        f"IMAGE_DIGEST={digest!r}\n"
        "ECR_REPO=registry.example/agp\n"
        "IMAGE_TAG=a-1-abc1234\n"
        f"{_digest_shape_guard()}\n"
        f"{_image_ref_lines()[0]}\n"
        f"{_image_ref_lines()[1]}\n"
        'echo "REF=$IMAGE_REF"\n'
    )
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == expect_exit, f"stdout={out.stdout!r} stderr={out.stderr!r}"
    assert "PWNED" not in out.stdout, out.stdout
    if expect_exit:
        assert "REF=" not in out.stdout, out.stdout
        assert "ST:failed" in out.stdout and "DEP:failed" in out.stdout, out.stdout
        assert "ERROR:" in out.stdout, out.stdout
    elif digest:
        assert f"REF=registry.example/agp@{digest}" in out.stdout, out.stdout
    else:
        assert "REF=registry.example/agp:a-1-abc1234" in out.stdout, out.stdout


def _cross_account_block():
    """The cross-account copy block AS CODEBUILD RUNS IT, up to (not including) `IMAGE_REF=`."""
    lines = _agentcore_code_lines()
    start = next(i for i, line in enumerate(lines) if line.startswith('if [ "${SOURCE_ECR_REQUIRED:-}"'))
    end = next(i for i, line in enumerate(lines) if line.startswith("IMAGE_REF="))
    assert start < end
    return "\n".join(lines[start:end])


@pytest.mark.parametrize(
    "digest,target_digest,pull_rc,expect_exit,expect_pull,expect_ref",
    [
        (
            _GOOD_DIGEST,
            "sha256:" + "cd" * 32,
            0,
            0,
            "src.example/agp@" + _GOOD_DIGEST,
            "tgt.example/agp@sha256:" + "cd" * 32,
        ),
        ("", "unused", 0, 0, "src.example/agp:a-1-abc1234", "tgt.example/agp:a-1-abc1234"),
        (_GOOD_DIGEST, "None", 0, 1, "src.example/agp@" + _GOOD_DIGEST, None),
        (_GOOD_DIGEST, "sha256:zz", 0, 1, "src.example/agp@" + _GOOD_DIGEST, None),
        (_GOOD_DIGEST, "sha256:" + "cd" * 32, 1, 1, "src.example/agp@" + _GOOD_DIGEST, None),
    ],
    ids=["digest-copy", "legacy-tag-copy", "target-missing", "target-malformed", "pull-fails"],
)
def test_the_cross_account_copy_ACTUALLY_resolves_the_target_digest(
    digest, target_digest, pull_rc, expect_exit, expect_pull, expect_ref
):
    """EXECUTES the real cross-account block with `docker`/`aws` stubbed, so nothing leaves the
    machine.

    The case that matters is `target-missing`: the source digest need NOT exist in the target
    registry, so a build that deployed the source digest here would fail to resolve at pull time —
    on the cross-account path ONLY, which is the path with no local evidence. This proves the
    read-back happens and that its result is shape-checked rather than trusted.

    NOT A SUBSTITUTE FOR A LIVE TEST: `docker` and `aws` are stubs, so this proves the SHELL LOGIC
    and the ordering. Whether a real ECR cross-account pull/push preserves the digest is an
    empirical question this cannot answer.

    Variables are NON-exported under `sh -c`, the production condition. `docker` records the pull
    reference, honours the injected rc and no-ops otherwise; `aws` emits three tab-separated creds
    for sts assume-role, the target digest under test for describe-images, and succeeds silently
    for everything else.

    The digest path pulls the approved bytes by digest and deploys the target registry's digest
    (which may legitimately differ, the daemon can re-serialize the manifest); the legacy tag-only
    path pulls by tag and deploys by tag; a target read-back that cannot resolve (the CLI prints
    "None") or is malformed would produce an unresolvable reference, so the build fails instead of
    reporting a deploy; and a failing pull is recorded rather than falling through to a deploy."""
    import subprocess

    script = (
        "_st() { echo ST:$1; }\n"
        "_dep() { echo DEP:$1; }\n"
        f"{_bad_digest_helper()}\n"
        f'docker() {{ case "$1" in pull) echo "PULLED:$2"; return {pull_rc};; '
        'push) echo "PUSHED:$2"; return 0;; *) return 0;; esac; }\n'
        'aws() { for a in "$@"; do case "$a" in '
        f'describe-images) printf "%s\\n" "{target_digest}"; return 0;; '
        'assume-role) printf "k\\ts\\tt\\n"; return 0;; '
        "get-login-password) echo pw; return 0;; esac; done; return 0; }\n"
        "SOURCE_ECR_REQUIRED=true\n"
        "SOURCE_ECR_REPO=src.example/agp\n"
        "ECR_REPO=tgt.example/agp\n"
        "SOURCE_DEPLOY_ROLE_ARN=arn:aws:iam::src:role/r\n"
        "TARGET_ROLE_ARN=arn:aws:iam::tgt:role/r\n"
        "SOURCE_REGION=eu-west-1\n"
        "AWS_TARGET_REGION=us-east-1\n"
        "IMAGE_TAG=a-1-abc1234\n"
        f"IMAGE_DIGEST={digest!r}\n"
        f"{_cross_account_block()}\n"
        f"{_image_ref_lines()[0]}\n"
        f"{_image_ref_lines()[1]}\n"
        'echo "REF=$IMAGE_REF"\n'
    )
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == expect_exit, f"stdout={out.stdout!r} stderr={out.stderr!r}"
    assert f"PULLED:{expect_pull}" in out.stdout, out.stdout
    if expect_exit:
        assert "REF=" not in out.stdout, out.stdout
        assert "ST:failed" in out.stdout and "DEP:failed" in out.stdout, out.stdout
    else:
        assert f"REF={expect_ref}" in out.stdout, out.stdout


def test_the_repository_model_carries_all_three_digest_fields():
    """The buildspec writes two of these attribute names straight into the repository row, and the
    promote path reads the third, so a rename on the model side without one here would strand the
    write on a field nothing reads."""
    from models.repository import Repository

    for field in ("last_dev_digest", "last_promoted_digest", "prod_candidate_digest"):
        assert field in Repository.model_fields, field


def test_the_digest_fields_are_ADDITIVE_to_the_tag_fields():
    """The digest fields sit alongside the tag fields, never replacing them.

    The tag is what every existing surface renders, what a rollback validates against, and what a
    rollback to code that predates the digest fields needs in order to deploy at all; a digest-only
    record would be undeployable by the previous release."""
    from models.repository import Repository

    for field in ("last_dev_image_tag", "last_promoted_image_tag", "prod_candidate_image_tag"):
        assert field in Repository.model_fields, field


def _describe_images_call():
    """The single runtime line resolving TARGET_DIGEST, from `yaml.safe_load`.

    The target read-back must run in the target account, and a read-back that queries the wrong
    account is statically determinable. This branch runs under the ambient CodeBuild
    (tooling-account) role: the pre_build assume-role is gated on `IAC_TYPE != "agentcore_runtime"`,
    so it never fires on this path, and `_lg`'s credentials are an inline env prefix, which applies
    to one command and is unset immediately after; `docker push` still succeeds only because
    `docker login` persists to ~/.docker/config.json, which is what hides the gap. Without
    `--registry-id` the CLI reads the caller's registry, and the tooling account owns a same-named
    repo (`modules/agent_ecr`, `<prefix>-agent-images`), so the usual outcome is RepositoryNotFound
    and a failed promote, and the worse one is a same-tag hit resolving a foreign digest, pinned
    onto $ECR_REPO, failing at ImagePull after a deploy was recorded. Both halves are required:
    `--registry-id` alone still needs the target account's credentials."""
    hits = [line for line in _agentcore_code_lines() if line.startswith("TARGET_DIGEST=")]
    assert len(hits) == 1, hits
    return hits[0]


def test_the_target_read_back_names_the_target_REGISTRY_ID():
    """The registry half. The account id is derived from `$ECR_REPO`'s own host, never hardcoded (a
    literal account id is forbidden project-wide), and the call is still scoped to the target region
    as well as the target account."""
    line = _describe_images_call()
    assert '--registry-id "${ECR_REPO%%.*}"' in line, line
    assert '--region "$AWS_TARGET_REGION"' in line, line


def test_the_target_read_back_runs_under_ASSUMED_TARGET_credentials():
    """The credential half, the one `--registry-id` cannot cover: a cross-account `describe-images`
    needs the target account's credentials regardless of the registry id. The helper runs in a
    subshell, so the exported credentials cannot leak into the terraform apply that follows;
    asserted on the body after the function header (`removeprefix`, not `lstrip`, which strips a
    character set and would eat into the body itself), and the command runs inside that subshell,
    forwarded whole rather than re-quoted."""
    line = _describe_images_call()
    assert "_with_target aws ecr describe-images" in line, line
    (helper,) = [raw for raw in _agentcore_code_lines() if raw.startswith("_with_target()")]
    assert '--role-arn "$TARGET_ROLE_ARN"' in helper, helper
    assert "export AWS_ACCESS_KEY_ID=" in helper, helper
    body = helper.removeprefix("_with_target() {").strip()
    assert body.startswith("("), helper
    assert '"$@"' in helper, helper


def test_the_credential_helper_does_not_leak_into_the_terraform_apply():
    """EXECUTED. If `_with_target` exported into the calling shell, every later `aws`/`terraform`
    call in this build would run as the TARGET account — silently redirecting the state-bucket and
    registry reads that must stay in the tooling account. The subshell is what prevents that, and
    only running it proves the boundary holds."""
    import subprocess

    (helper,) = [line for line in _agentcore_code_lines() if line.startswith("_with_target()")]
    script = (
        'aws() { printf "k\\ts\\tt\\n"; }\n'
        "TARGET_ROLE_ARN=arn:aws:iam::tgt:role/r\n"
        f"{helper}\n"
        "_with_target true\n"
        'echo "AFTER=${AWS_ACCESS_KEY_ID:-UNSET}"\n'
    )
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "AFTER=UNSET" in out.stdout, (
        f"credentials leaked out of the helper into the calling shell: {out.stdout!r}"
    )


@pytest.mark.parametrize(
    "creds_present,registry_id_correct,expect_exit",
    [
        (True, True, 0),
        (False, True, 1),
        (True, False, 1),
    ],
    ids=["target-creds-and-registry", "no-target-creds", "wrong-registry"],
)
def test_the_read_back_FAILS_if_it_queries_the_wrong_account(creds_present, registry_id_correct, expect_exit):
    """EXECUTES the block against a registry stub that answers only the target account.

    An `aws` stub that returns the target digest unconditionally cannot tell a correctly-scoped call
    from one aimed at the tooling account, a stub more generous than reality. Here the stub serves
    only the target account and only when addressed as such: `sts assume-role` hands out the target
    creds, `describe-images` demands them and the matching `--registry-id`, and otherwise behaves
    like ECR does, RepositoryNotFound, rc 254 with nothing on stdout. The two failure rows are each
    half in isolation, the call with no assumed credentials and the call with no `--registry-id`
    (so the caller's own registry is queried): both must fail the build rather than deploy an
    unnamed or foreign image."""
    import subprocess

    lines = _agentcore_code_lines()
    (helper,) = [line for line in lines if line.startswith("_with_target()")]
    call = _describe_images_call()
    if not creds_present:
        call = call.replace("_with_target aws ecr", "aws ecr")
    if not registry_id_correct:
        call = call.replace('--registry-id "${ECR_REPO%%.*}" ', "")

    target_digest = "sha256:" + "cd" * 32
    script = (
        "_st() { echo ST:$1; }\n"
        "_dep() { echo DEP:$1; }\n"
        f"{_bad_digest_helper()}\n"
        'aws() { case "$*" in\n'
        '  *assume-role*) printf "TGTKEY\\ts\\tt\\n"; return 0;;\n'
        "  *describe-images*)\n"
        '    case "$*" in *"--registry-id tgtacct"*) ;; *) return 254;; esac\n'
        '    [ "${AWS_ACCESS_KEY_ID:-}" = TGTKEY ] || return 254\n'
        f'    printf "%s\\n" "{target_digest}"; return 0;;\n'
        "  *) return 0;; esac; }\n"
        'docker() { case "$1" in pull) echo "PULLED:$2"; return 0;; *) return 0;; esac; }\n'
        "SOURCE_ECR_REQUIRED=true\n"
        "SOURCE_ECR_REPO=srcacct.dkr.ecr.eu-west-1.amazonaws.com/agp-agent-images\n"
        "ECR_REPO=tgtacct.dkr.ecr.us-east-1.amazonaws.com/agp-agent-images\n"
        "SOURCE_DEPLOY_ROLE_ARN=arn:aws:iam::src:role/r\n"
        "TARGET_ROLE_ARN=arn:aws:iam::tgt:role/r\n"
        "SOURCE_REGION=eu-west-1\n"
        "AWS_TARGET_REGION=us-east-1\n"
        "IMAGE_TAG=a-1-abc1234\n"
        f"IMAGE_DIGEST={_GOOD_DIGEST!r}\n"
        f"{helper}\n"
        f"{call}\n"
        f"echo \"$TARGET_DIGEST\" | grep -qE '^sha256:[0-9a-f]{{64}}$' || "
        '_bad_digest "the digest resolved in the target registry"\n'
        'echo "RESOLVED=$TARGET_DIGEST"\n'
    )
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == expect_exit, f"stdout={out.stdout!r} stderr={out.stderr!r}"
    if expect_exit:
        assert "RESOLVED=" not in out.stdout, out.stdout
        assert "ST:failed" in out.stdout and "DEP:failed" in out.stdout, out.stdout
    else:
        assert f"RESOLVED={target_digest}" in out.stdout, out.stdout


def _container_uri_condition():
    """The real `condition` expression from the module's own `variable` block, comments stripped.

    `var.container_image_uri`'s validation is the module-side backstop for "name a specific image".
    It is asserted by evaluating the real condition rather than by matching its text: a regex is
    exactly the kind of expression that reads correct and admits the wrong strings, and the table's
    rows include the strings that did. The block is cut at the next variable: without this the split
    runs to end-of-file and picks up every later variable's `condition` too, which is a
    `too many values to unpack` rather than a silent wrong answer, but only by luck."""
    body = (_RUNTIME_MODULE / "variables.tf").read_text()
    code = "\n".join(raw for raw in body.split("\n") if not raw.strip().startswith("#"))
    block = code.split('variable "container_image_uri"', 1)[1]
    if "\nvariable " in block:
        block = block[: block.index("\nvariable ")]
    (line,) = [raw.strip() for raw in block.split("\n") if raw.strip().startswith("condition")]
    return line.split("=", 1)[1].strip()


@pytest.mark.parametrize(
    "uri,accepted",
    [
        ("acct.dkr.ecr.us-east-1.amazonaws.com/agp-agent-images@" + _GOOD_DIGEST, True),
        ("acct.dkr.ecr.us-east-1.amazonaws.com/agp-agent-images:a-1-abc1234", True),
        ("registry.example/agp:strands-latest", True),
        ("registry.example/agp", False),
        ("registry.example/agp@sha256:zz", False),
        ("registry.example/agp@sha256:" + "b" * 65, False),
        ("registry.example/agp@garbage", False),
        ("registry.example/agp:tag@sha256:zz", False),
        ("registry.example/agp:tag; echo PWNED", False),
        ("registry.example/agp:a$(id)", False),
        ("registry.example/agp:`id`", False),
        ("registry.example/agp:tag with space", False),
        ("registry.example/agp:-leading", False),
        ("registry.example/agp@evil@sha256:" + "b" * 64, False),
        ("@sha256:" + "b" * 64, False),
        ("registry.example/agp:tag@sha256:" + "b" * 64, False),
        ("junk stuff@sha256:" + "b" * 64, False),
    ],
    ids=[
        "digest",
        "tag",
        "tag-latest-style",
        "no-tag-no-digest",
        "malformed-digest-as-tag",
        "over-long-digest",
        "garbage-digest",
        "tag-plus-bad-digest",
        "semicolon",
        "dollar-subst",
        "backtick",
        "space",
        "leading-hyphen",
        "double-at",
        "empty-repo",
        "tag-and-digest",
        "space-in-repo",
    ],
)
def test_the_container_uri_validation_admits_only_a_named_image(tmp_path, uri, accepted):
    """EVALUATES the module's real condition through `terraform plan`.

    The condition is read OUT of `variables.tf` and dropped into a throwaway root, so this cannot
    pass if the real expression changes, and unlike a text match it fails when the expression is
    still *shaped* right but accepts the wrong strings, which is how a regex admits them.

    The digest form is what a promote deploys; the tag form serves a rollback and any pre-digest
    caller. `<repo>` alone names no image: it silently means `:latest`, which on a shared tenant
    registry is almost certainly some other agent's image. An unanchored tag alternative matches the
    tail of a malformed digest (`@sha256:zz` passing as though `:zz` were a tag); a permissive tag
    class admits shell metacharacters (inert today, since the value is only ever written into a
    quoted heredoc, but a validation whose job is "name a specific image" must not be what relies on
    that); ECR forbids a leading hyphen on a tag; and dropping the digest branch's `^` anchor
    survives every row but the ones naming something other than one image: two `@` (which host
    applies?), no repository at all, a tag and a digest together (ambiguous about which is
    deployed), and a repository part that is not a repository."""
    import shutil
    import subprocess

    if shutil.which("terraform") is None:
        pytest.skip(
            "LOUD SKIP: terraform is not installed, so the real HCL condition cannot be "
            "evaluated and this validation is unverified here."
        )

    (tmp_path / "main.tf").write_text(
        'variable "container_image_uri" {\n'
        "  type = string\n"
        "  validation {\n"
        f"    condition     = {_container_uri_condition()}\n"
        '    error_message = "must name a specific image"\n'
        "  }\n"
        "}\n"
    )
    subprocess.run(
        ["terraform", "init", "-backend=false", "-input=false"],
        cwd=tmp_path,
        capture_output=True,
        timeout=300,
        check=True,
    )
    out = subprocess.run(
        ["terraform", "plan", "-input=false", f"-var=container_image_uri={uri}"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert (out.returncode == 0) is accepted, (
        f"{uri!r}: expected {'accept' if accepted else 'refuse'}, "
        f"got rc={out.returncode}\n{out.stdout}\n{out.stderr}"
    )


_ECS_MODULE = Path(__file__).resolve().parents[2] / "infrastructure/modules/ecs"


def _iam_resource_patterns(body: str, sid: str) -> list[str]:
    """The `Resource` value(s) of the statement carrying `Sid = "<sid>"`, comments stripped.

    The IAM grants that make the Langfuse and reclaim paths real live in a file the code never
    imports, the same silent-drift class every guard above exists for, and it produces defects of
    exactly this shape: an `iam:DeleteRole` grant scoped to a name the reclaim can never target (so
    the reclaim is denied forever), or a runtime exec role with no Secrets Manager grant at all (so
    the container cannot read the key the platform provisioned). Neither raises anywhere. The IAM
    tests therefore match the real derived name against the real policy pattern, restating neither.

    Reads the real policy text rather than a restatement, and keys off the Sid because statement
    order in a jsonencode list is not a contract; the block is cut at the end of this statement
    object, the next `Sid` or the closing of the list."""
    code = "\n".join(line for line in body.split("\n") if not line.strip().startswith("#"))
    block = code.split(f'Sid    = "{sid}"', 1)[1]
    for stop in ("Sid    =", "\n  })"):
        if stop in block:
            block = block.split(stop, 1)[0]
    return re.findall(r'Resource\s*=\s*"([^"]+)"', block)


def _iam_actions(body: str, sid: str) -> set[str]:
    """The `Action` entries of the statement carrying `Sid = "<sid>"`."""
    code = "\n".join(line for line in body.split("\n") if not line.strip().startswith("#"))
    block = code.split(f'Sid    = "{sid}"', 1)[1]
    for stop in ("Sid    =", "\n  })"):
        if stop in block:
            block = block.split(stop, 1)[0]
    return set(re.findall(r'"(iam:[A-Za-z]+)"', block))


_ACCOUNT = "000000000000"
_REGION = "us-east-1"


def _resolved(pattern: str) -> str:
    """A policy `Resource` with the module's data-source interpolations substituted.

    The account and region reach these ARNs through `data.aws_caller_identity` and
    `data.aws_region`, never as literals, so the raw pattern is not a comparable ARN and a test
    matching against it unsubstituted fails for a reason that has nothing to do with the grant.
    `_ACCOUNT` is a stand-in: the real one is never written down (hard project rule)."""
    return (
        pattern.replace("${data.aws_caller_identity.current.account_id}", _ACCOUNT)
        .replace("${data.aws_region.current.region}", _REGION)
        .replace("${data.aws_region.current.id}", _REGION)
        .replace("${var.name_prefix}", "agp")
    )


def _matches_iam_pattern(pattern: str, arn: str) -> bool:
    """Does an IAM `Resource` pattern match a concrete ARN? `*` = any run, `?` = one char.

    IAM's own wildcard semantics, implemented rather than approximated by a substring check —
    a substring check is what would have let `role/{prefix}-ecr-push-*` look like it covered
    `{agent}-{stage}-agentcore-exec`."""
    resolved = _resolved(pattern)
    return re.fullmatch(re.escape(resolved).replace(r"\*", ".*").replace(r"\?", "."), arn) is not None


def test_the_task_role_grant_ACTUALLY_MATCHES_the_exec_role_name_it_reclaims():
    """The reclaim grant must match the exec-role name, asserted as the match.

    The backend's exec-role reclaim needs an `iam:DeleteRole` grant that reaches the role: a resource
    of `role/{prefix}-ecr-push-*` cannot match `{agent}-{stage}-agentcore-exec` under any inputs, so
    the live answer is always AccessDenied, the code swallows it as success, and account-global roles
    leak behind clean teardown reports.

    Both ends are REAL here — the name comes from the single Python producer, the pattern from the
    real module — so this fails if either drifts, which is the only way a name/grant pair can be
    kept honest across two files that never reference each other."""
    from services.project_runtime_teardown import agentcore_exec_role_name

    body = (_ECS_MODULE / "main.tf").read_text()
    patterns = _iam_resource_patterns(body, "ReclaimAgentcoreExecRoles")
    assert patterns, "the reclaim statement must exist and carry a Resource"

    for stage in ("dev", "prod", "uat"):
        role_arn = f"arn:aws:iam::{_ACCOUNT}:role/{agentcore_exec_role_name('my_agent', stage)}"
        assert any(_matches_iam_pattern(p, role_arn) for p in patterns), (stage, patterns)


def test_the_reclaim_grant_ALSO_covers_the_legacy_UN_STAGE_SCOPED_name():
    """The cascade always attempts the legacy un-stage-scoped name too (the migration case: every
    repo deployed before stage-scoping has a role there, and orphans exist in the account). A
    pattern covering only the stage-scoped shape would leave those denied forever, the name
    collision surviving its own fix in a third form."""
    from services.project_runtime_teardown import legacy_agentcore_exec_role_name

    body = (_ECS_MODULE / "main.tf").read_text()
    patterns = _iam_resource_patterns(body, "ReclaimAgentcoreExecRoles")
    legacy_arn = f"arn:aws:iam::{_ACCOUNT}:role/{legacy_agentcore_exec_role_name('my_agent')}"
    assert any(_matches_iam_pattern(p, legacy_arn) for p in patterns), patterns


def test_the_reclaim_grant_carries_BOTH_verbs_the_reclaim_uses_and_NO_create_verb():
    """`_reclaim_exec_role` makes TWO calls and needs both: `DeleteRolePolicy` then `DeleteRole`.
    Granting only the second does not merely leak the policy — IAM answers `DeleteConflict` to
    `DeleteRole` while an inline policy remains, so the whole role leaks and the symptom is
    identical to having no grant at all.

    And it must NOT carry a create/re-trust verb. Terraform is the only thing that creates these
    roles; a backend able to mint one or rewrite its trust policy would be a privilege-escalation
    path for no benefit."""
    body = (_ECS_MODULE / "main.tf").read_text()
    actions = _iam_actions(body, "ReclaimAgentcoreExecRoles")
    assert {"iam:DeleteRole", "iam:DeleteRolePolicy"} <= actions, actions
    forbidden = {"iam:CreateRole", "iam:PutRolePolicy", "iam:UpdateAssumeRolePolicy", "iam:PassRole"}
    assert not (forbidden & actions), forbidden & actions


def test_the_reclaim_grant_is_NOT_a_blanket_wildcard_over_every_role():
    """Scope fence.

    `Resource = "*"` satisfies every match assertion above while handing the ECS task role
    `iam:DeleteRole` over EVERY role in the account, including its own execution role and the
    Terraform deploy roles. "The reclaim now works" and "the reclaim is least-privilege" are
    different claims, and the match tests only prove the first. The `-agentcore-exec` suffix is the
    namespace, so the pattern must be anchored on it and must not match an unrelated role:
    concretely, the task's own role, the org access role and the deploy role stay out of reach."""
    body = (_ECS_MODULE / "main.tf").read_text()
    patterns = _iam_resource_patterns(body, "ReclaimAgentcoreExecRoles")
    assert patterns
    for pattern in patterns:
        assert pattern != "*"
        assert pattern.endswith("-agentcore-exec"), pattern
    for other in (
        f"arn:aws:iam::{_ACCOUNT}:role/agp-ecs-task",
        f"arn:aws:iam::{_ACCOUNT}:role/OrganizationAccountAccessRole",
        f"arn:aws:iam::{_ACCOUNT}:role/agp-terraform-deploy",
    ):
        assert not any(_matches_iam_pattern(p, other) for p in patterns), (other, patterns)


def test_the_ecr_push_grant_STILL_cannot_reach_an_agentcore_exec_role():
    """Non-vacuity for the test above, and a scope fence for the statement that was already there.

    The push-role statement is a separate, broader grant (it creates and re-trusts per-org push roles),
    and widening ITS resource to cover the exec roles would have been the lazy fix — handing the
    backend `CreateRole`/`UpdateAssumeRolePolicy` over every runtime exec role. It must stay unable
    to reach them. This also proves the reclaim statement is doing real work rather than
    duplicating a match that already existed."""
    from services.project_runtime_teardown import agentcore_exec_role_name

    body = (_ECS_MODULE / "main.tf").read_text()
    patterns = _iam_resource_patterns(body, "ManageEcrPushRoles")
    assert patterns
    exec_arn = f"arn:aws:iam::{_ACCOUNT}:role/{agentcore_exec_role_name('my_agent', 'dev')}"
    assert not any(_matches_iam_pattern(p, exec_arn) for p in patterns), patterns


def _provisioner_self() -> "LangfuseProvisioningService":
    return cast("LangfuseProvisioningService", SimpleNamespace(_agp_project_name="agp"))


def test_the_runtime_exec_role_CAN_READ_the_langfuse_secret_the_platform_writes():
    """The backend provisions a per-agent Langfuse key into Secrets Manager and passes only its name
    to the container, which resolves the value itself, so without a `secretsmanager:GetSecretValue`
    grant on that name the agent AccessDenies into exactly the silent zero the provisioning was
    added to fix.

    The expected name comes from the real producer (`LangfuseProvisioningService._agent_secret_name`),
    so this is a cross-file match and not two copies of a literal agreeing with each other. Secrets
    Manager appends its own 6-character random suffix to every secret ARN, so the concrete ARN the
    container asks for is never just the name."""
    from services.langfuse_provisioning import LangfuseProvisioningService

    secret_name = LangfuseProvisioningService._agent_secret_name(
        _provisioner_self(), SimpleNamespace(id="rec-abc123")
    )
    body = (_RUNTIME_MODULE / "main.tf").read_text()
    code = "\n".join(line for line in body.split("\n") if not line.strip().startswith("#"))
    (pattern,) = re.findall(
        r'Action\s*=\s*"secretsmanager:GetSecretValue"\s*\n\s*Resource\s*=\s*"([^"]+)"', code
    )
    secret_arn = f"arn:aws:secretsmanager:{_REGION}:{_ACCOUNT}:secret:{secret_name}-AbCdEf"
    assert _matches_iam_pattern(pattern, secret_arn), pattern


def test_the_runtime_exec_role_secret_grant_is_NOT_a_blanket_wildcard():
    """Scope fence. `Resource = "*"` would satisfy the test above while letting every agent runtime
    in the account read the platform's Graph client secret and every tenant credential. The grant
    has to be namespaced to the secrets the Langfuse provisioner writes, and it must not be able to
    read the platform's own Graph client secret."""
    body = (_RUNTIME_MODULE / "main.tf").read_text()
    code = "\n".join(line for line in body.split("\n") if not line.strip().startswith("#"))
    (pattern,) = re.findall(
        r'Action\s*=\s*"secretsmanager:GetSecretValue"\s*\n\s*Resource\s*=\s*"([^"]+)"', code
    )
    assert pattern != "*"
    assert "langfuse-agent-" in pattern, pattern
    other = f"arn:aws:secretsmanager:{_REGION}:{_ACCOUNT}:secret:entra-backend-client-secret-AbCdEf"
    assert not _matches_iam_pattern(pattern, other), pattern


def test_the_runtime_exec_role_CAN_READ_its_own_OBO_vault_secret():
    """GetResourceOauth2Token reads the provider's vault secret as the exec role, so a Strands agent
    without this grant loses every gateway tool. The provider name comes from the real config prefix;
    the grant must not reach other secrets."""
    from core.config import Settings

    prefix = Settings.model_fields["AGENT_CRED_PROVIDER_PREFIX"].default
    body = (_RUNTIME_MODULE / "main.tf").read_text()
    code = "\n".join(line for line in body.split("\n") if not line.strip().startswith("#"))
    (pattern,) = re.findall(
        r'Action\s*=\s*\["secretsmanager:GetSecretValue"\]\s*\n\s*Resource\s*=\s*\["([^"]+)"\]', code
    )
    pattern = pattern.replace("${data.aws_region.current.region}", _REGION).replace(
        "${data.aws_caller_identity.current.account_id}", _ACCOUNT
    )
    own = (
        f"arn:aws:secretsmanager:{_REGION}:{_ACCOUNT}:secret:"
        f"bedrock-agentcore-identity!default/oauth2/{prefix}JtP5DUSTUGL4-Xy12Ab"
    )
    assert _matches_iam_pattern(pattern, own), pattern
    for other in (
        "entra-backend-client-secret-AbCdEf",
        "bedrock-agentcore-identity!default/oauth2/github-AbCdEf",
    ):
        arn = f"arn:aws:secretsmanager:{_REGION}:{_ACCOUNT}:secret:{other}"
        assert not _matches_iam_pattern(pattern, arn), pattern


def test_the_runtime_exec_role_CARRIES_the_agentcore_identity_TOKEN_ACTIONS():
    """An agent that makes an outbound OBO call asks the workload identity for a token, and all three
    of these are needed: `GetWorkloadAccessToken` / `...ForJWT` for the workload identity itself and
    `GetResourceOauth2Token` for an OAuth2 credential provider in the token vault. A
    platform-provisioned role with no `bedrock-agentcore:*` action makes that call AccessDenied,
    invisibly to the deploy, which comes up green either way.

    Nothing in this repo can currently reach that denial: the scaffold template ships no OBO code and
    the acme agents run on roles hand-augmented outside Terraform (their own `deploy.sh` attaches
    exactly these three actions). This assertion IS the guard, so it also has to be un-fakeable:
    comments are stripped before matching, the same way the stage-scoping guards are, and the
    resources are matched as IAM patterns against concrete ARNs rather than as substrings.

    The statement is isolated and both lists come out of the same match, so the action set is the
    statement's own and not the file's: a whole-file read is fakeable by moving one action into a
    separate `{ Effect = "Allow", Action = "…", Resource = "*" }` one-liner (this module's dominant
    statement idiom), which leaves the whole-file set intact while the split-out grant is seen by
    neither the unpack nor the scope fence. That statement is also the only place the module may
    hand this role a `bedrock-agentcore` action in any form, so a second wildcard-scoped grant added
    elsewhere fails here instead of riding along unfenced; the opening `"` anchor keeps the four
    `arn:aws:bedrock-agentcore:…` resources of the same statement out of the count. Both the
    vault/directory itself and its children are matched, since some calls target the container and
    others a resource inside it, and a pattern ending at `default` matches only the former. Scope
    fence: `Resource = "*"` would satisfy every match while handing every agent runtime in the
    account every token vault and directory it will ever hold."""
    body = (_RUNTIME_MODULE / "main.tf").read_text()
    code = "\n".join(line for line in body.split("\n") if not line.strip().startswith("#"))
    ((action_list, block),) = re.findall(
        r'\{\s*Effect\s*=\s*"Allow"\s*\n\s*Action\s*=\s*\[([^\]]*bedrock-agentcore:[^\]]*)\]\s*\n\s*Resource\s*=\s*\[([^\]]*)\]',
        code,
    )
    actions = set(re.findall(r'"(bedrock-agentcore:[A-Za-z0-9]+)"', action_list))
    assert actions == {
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
        "bedrock-agentcore:GetResourceOauth2Token",
    }, actions
    assert len(re.findall(r'"bedrock-agentcore:', code)) == 3, re.findall(
        r'"bedrock-agentcore:[A-Za-z0-9]*', code
    )

    patterns = re.findall(r'"([^"]+)"', block)
    vault = f"arn:aws:bedrock-agentcore:{_REGION}:{_ACCOUNT}:token-vault/default"
    directory = f"arn:aws:bedrock-agentcore:{_REGION}:{_ACCOUNT}:workload-identity-directory/default"
    for arn in (
        vault,
        f"{vault}/oauth2credentialprovider/agp-obo",
        directory,
        f"{directory}/workload-identity/my_agent_dev",
    ):
        assert any(_matches_iam_pattern(p, arn) for p in patterns), (arn, patterns)
    assert "*" not in patterns, patterns
    for p in patterns:
        assert p.startswith(
            "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:"
            "${data.aws_caller_identity.current.account_id}:"
        ), p


def test_the_runtime_module_ARN_SCOPES_come_from_DATA_SOURCES_not_literals():
    """A hard project rule: no hardcoded AWS account id anywhere. It is also a correctness rule
    here — this module deploys into an account it does not choose (`deploy_role_arn`), so a literal
    would be wrong as well as banned. The data sources must exist and be what the ARN interpolates."""
    body = (_RUNTIME_MODULE / "main.tf").read_text()
    code = "\n".join(line for line in body.split("\n") if not line.strip().startswith("#"))
    assert 'data "aws_caller_identity" "current"' in code
    assert 'data "aws_region" "current"' in code
    assert "data.aws_caller_identity.current.account_id" in code
    assert not re.search(r"\b\d{12}\b", code), re.findall(r"\b\d{12}\b", code)


_INFRA = Path(__file__).resolve().parents[2] / "infrastructure"


def _tf_code(path: Path) -> str:
    """A .tf file's real code, comments stripped (a comment MENTIONING a resource is fine)."""
    return "\n".join(line for line in path.read_text().split("\n") if not line.strip().startswith("#"))


def test_terraform_declares_NO_github_oidc_provider_anywhere():
    """GitHub is a platform capability, not a Terraform deploy-time object: `terraform apply` ships
    zero GitHub artifacts, and the backend creates the account-global OIDC provider (plus the shared
    ECR-push role, whose trust names it) on the first GitHub connection. Two things make that
    decision enforceable rather than aspirational, both cross-file matches: Terraform must not
    reintroduce a GitHub resource or data source (a data source fails apply on a provider-less
    account just as surely as a resource does, while looking like a read), and the names the backend
    derives must match the IAM grants the ECS task role carries, the shared role's name matching
    what the retired Terraform resource produced, since that name is what makes an already-deployed
    account's live role get adopted instead of duplicated beside it. Terraform forgot that role in an
    earlier apply and now declares no resource that could name it; see
    `test_no_terraform_resource_can_produce_the_shared_push_role_AT_ALL`.

    The fresh-apply guarantee, asserted over the whole tree. Both a `resource` and a `data` block are
    forbidden: the data source is what the retired toggle used for its false branch, and it fails
    apply on an account that never onboarded GitHub Actions, precisely the customer this decision
    exists for."""
    offenders = [
        p.relative_to(_INFRA).as_posix()
        for p in _INFRA.rglob("*.tf")
        if "aws_iam_openid_connect_provider" in _tf_code(p)
    ]
    assert offenders == [], offenders


def test_terraform_has_NO_deploy_time_github_toggle_or_org_variable():
    """`create_github_oidc_provider` was the previous answer (create it at apply on a fresh
    account) and this change supersedes it — a customer should not have a GitHub question in their
    tfvars at all. `github_org` goes with it: the per-org roles are scoped to orgs an operator
    actually connected, so no org needs naming at deploy time."""
    for path in _INFRA.rglob("*.tf"):
        code = _tf_code(path)
        assert "create_github_oidc_provider" not in code, path
        assert "var.github_org" not in code, path


def test_the_shared_push_role_name_MATCHES_the_terraform_resource_it_adopts():
    """The adoption contract. `${var.name_prefix}-agent-ecr-push` is the name the retired
    `modules/agent_ecr` resource produced, `ECR_PUSH_ROLE_NAME_PREFIX` is that same `name_prefix`,
    and the root builds the ECS env var from the same literal. All three ends are real here, the
    Python producer, the root local and the ECS IAM grant, so this fails if any drifts: the root's
    deterministic ARN string resolves to that same name, and the ECS task role can actually manage
    it."""
    from services.ecr_push_role_service import EcrPushRoleService

    svc = EcrPushRoleService(
        role_name_prefix="agp", oidc_provider_arn="x", ecr_repository_arn="y", iam_client=object()
    )
    name = svc.shared_role_name()
    assert name == "agp-agent-ecr-push", name

    root = _tf_code(_INFRA / "main.tf")
    (arn_expr,) = re.findall(r'agent_ecr_push_role_arn\s*=\s*"([^"]+)"', root)
    assert arn_expr.replace("${local.name_prefix}", "agp").endswith(f":role/{name}"), arn_expr

    body = (_ECS_MODULE / "main.tf").read_text()
    patterns = _iam_resource_patterns(body, "ManageSharedEcrPushRole")
    assert patterns, "the shared-role statement must exist and carry a Resource"
    role_arn = f"arn:aws:iam::{_ACCOUNT}:role/{name}"
    assert any(_matches_iam_pattern(p, role_arn) for p in patterns), patterns


def test_the_shared_role_grant_carries_the_verbs_the_ensure_uses_and_NO_delete():
    """`ensure_shared_role` calls CreateRole / PutRolePolicy / GetRole (+ tags on create) and
    nothing else. It must NOT be able to DELETE: this role is the fallback stamped onto repos whose
    connection has no per-org role, so no single disconnect owns it and the backend never removes
    it. A delete grant it never issues would only be a way to lose a live role."""
    body = (_ECS_MODULE / "main.tf").read_text()
    actions = _iam_actions(body, "ManageSharedEcrPushRole")
    assert {"iam:CreateRole", "iam:PutRolePolicy", "iam:GetRole"} <= actions, actions
    assert not ({"iam:DeleteRole", "iam:DeleteRolePolicy"} & actions), actions


def test_the_shared_role_grant_is_an_EXACT_name_not_a_wildcard():
    """Scope fence. The name is a single exact string, so no wildcard is needed — and one would
    hand the task role CreateRole over a whole namespace for no benefit. `role/*` would satisfy the
    match test above while letting the backend mint any role in the account. Concretely, it must not
    reach the per-org roles' namespace or an unrelated role."""
    body = (_ECS_MODULE / "main.tf").read_text()
    for pattern in _iam_resource_patterns(body, "ManageSharedEcrPushRole"):
        assert "*" not in pattern.split(":role/")[1], pattern
    patterns = _iam_resource_patterns(body, "ManageSharedEcrPushRole")
    for other in (
        f"arn:aws:iam::{_ACCOUNT}:role/agp-ecr-push-acme",
        f"arn:aws:iam::{_ACCOUNT}:role/OrganizationAccountAccessRole",
    ):
        assert not any(_matches_iam_pattern(p, other) for p in patterns), other


def test_the_oidc_provider_grant_MATCHES_the_arn_the_backend_derives():
    """The provider bootstrap's grant, matched against the ARN the Python producer computes. The
    deterministic ARN is the part the whole design rests on (it is what lets Terraform pass a string
    with no resource behind it), so a drift here breaks the bootstrap silently: the backend would ask
    about one ARN while the grant covers another. The root passes that same derived ARN to ECS as a
    plain string, no resource reference."""
    from services.github_oidc_provider_service import github_oidc_provider_arn

    body = (_ECS_MODULE / "main.tf").read_text()
    patterns = _iam_resource_patterns(body, "ManageGithubOidcProvider")
    assert patterns, "the provider bootstrap statement must exist"
    assert any(_matches_iam_pattern(p, github_oidc_provider_arn(_ACCOUNT)) for p in patterns), patterns

    root = _tf_code(_INFRA / "main.tf")
    (arn_expr,) = re.findall(r'github_oidc_provider_arn\s*=\s*"(arn:aws:iam[^"]+)"', root)
    assert arn_expr.replace(
        "${data.aws_caller_identity.current.account_id}", _ACCOUNT
    ) == github_oidc_provider_arn(_ACCOUNT), arn_expr


def test_the_oidc_provider_grant_has_NO_delete_and_is_scoped_to_GITHUBs_issuer():
    """Two fences on an ACCOUNT-GLOBAL SINGLETON. No delete: anything else in the account that
    trusts GitHub Actions (other stacks, other teams, roles AGP never created) breaks the instant
    it is removed, and the backend has no delete path. And the resource must name GitHub's issuer
    specifically — `oidc-provider/*` would let the task role create a provider for ANY issuer,
    which is a federation-trust escalation."""
    body = (_ECS_MODULE / "main.tf").read_text()
    actions = _iam_actions(body, "ManageGithubOidcProvider")
    assert {"iam:GetOpenIDConnectProvider", "iam:CreateOpenIDConnectProvider"} <= actions, actions
    assert "iam:DeleteOpenIDConnectProvider" not in actions, actions
    for pattern in _iam_resource_patterns(body, "ManageGithubOidcProvider"):
        assert pattern.endswith("/token.actions.githubusercontent.com"), pattern


def _tf_name_patterns(kind: str, arg: str) -> list[tuple[str, str, str]]:
    """Every `resource "<kind>"` in the tree as `(file, address, regex)`.

    `regex` is the resource's `<arg>` expression turned into a pattern that matches any IAM
    name it could possibly produce: each `${…}` interpolation becomes `[^"]*`, since
    `name_prefix` is deploy-time input and no test may assume one deployment's value.

    Two indirections are followed rather than skipped, because each is a way the name could
    hide from a literal-only scan: `local.x` resolves from the same file's `locals`, and
    `aws_iam_role.<label>.{id,name}` (how every `aws_iam_role_policy` here names its role)
    resolves to THAT role's own `name` expression. An expression this cannot resolve RAISES —
    a silently-unresolved name is a hole in the guarantee below, not a pass."""
    out = []
    for path in sorted(_INFRA.rglob("*.tf")):
        code = _tf_code(path)
        for block in re.finditer(r'resource\s+"' + kind + r'"\s+"([^"]+)"\s*\{(.*?)\n\}', code, re.DOTALL):
            label, body = block.group(1), block.group(2)
            m = re.search(rf"^\s*{arg}\s*=\s*(.+?)\s*$", body, re.MULTILINE)
            assert m, f'{path}: {kind}."{label}" has no {arg} argument'
            expr = m.group(1)
            role_ref = re.fullmatch(r"aws_iam_role\.([A-Za-z0-9_]+)\.(?:id|name)", expr)
            if role_ref:
                target = re.search(
                    r'resource\s+"aws_iam_role"\s+"' + role_ref.group(1) + r'"\s*\{(.*?)\n\}',
                    code,
                    re.DOTALL,
                )
                assert target, f"{path}: {expr} names no aws_iam_role in this file"
                m = re.search(r"^\s*name\s*=\s*(.+?)\s*$", target.group(1), re.MULTILINE)
                assert m, f"{path}: aws_iam_role.{role_ref.group(1)} has no name"
                expr = m.group(1)
            if not expr.startswith('"'):
                ref = re.fullmatch(r"local\.([A-Za-z0-9_]+)", expr)
                assert ref, f"{path}: unresolvable {arg} expression {expr!r}"
                local = re.search(rf'^\s*{ref.group(1)}\s*=\s*("[^"]*")', code, re.MULTILINE)
                assert local, f"{path}: cannot resolve local.{ref.group(1)}"
                expr = local.group(1)
            literal = expr.strip('"')
            pattern = "".join(
                '[^"]*' if part.startswith("${") else re.escape(part)
                for part in re.split(r"(\$\{[^}]*\})", literal)
                if part
            )
            out.append((path.relative_to(_INFRA).as_posix(), f'{kind}."{label}"', pattern))
    return out


def test_no_terraform_resource_can_produce_the_shared_push_role_AT_ALL():
    """What keeps the live shared push role alive: Terraform declares no resource that could name it.

    `<prefix>-agent-ecr-push` is a live role that GitHub Actions workflows assume on every agent
    build, and no `terraform apply` may ever destroy it. A `removed { lifecycle { destroy = false } }`
    block would be strictly weaker: it only does anything while the address is still in state (an
    earlier apply already forgot this role, so such blocks are no-ops), and it is a one-shot
    instruction that has to be spelled correctly, kept in the config and eventually cleaned up. What
    holds instead is structural: Terraform declares NO resource that could ever be named
    `<prefix>-agent-ecr-push`, in any module, so the address cannot re-enter state and there is
    nothing to destroy. Proven over the whole tree by resolving every `aws_iam_role`'s name
    expression rather than trusting the one module the role used to live in. `aws_iam_role_policy`
    is covered too: it takes the role by name, so a policy resource pointed at the adopted role would
    let an apply delete the inline permissions `ensure_shared_role` writes; the role would survive
    and every push would start failing on authorization instead.

    The role's real owner is `services/ecr_push_role_service.ensure_shared_role()`, which
    create-or-adopts it by that exact name and holds no delete path at all
    (`test_the_shared_role_grant_carries_the_verbs_the_ensure_uses_and_NO_delete` fences the IAM
    grant, `test_the_shared_push_role_name_MATCHES_the_terraform_resource_it_adopts` fences the
    name). Terraform's only remaining involvement is handing the backend a deterministic ARN
    string, asserted below: a string has no resource behind it, which is what lets a fresh apply
    succeed on an account that has never connected GitHub. Non-vacuity, and proof the resolver
    resolves: the tree really does declare roles, and the retired resource's own expression,
    `"${var.name_prefix}-agent-ecr-push"`, is what this would catch if anyone reintroduced it."""
    from services.ecr_push_role_service import EcrPushRoleService

    shared = EcrPushRoleService(
        role_name_prefix="agp", oidc_provider_arn="x", ecr_repository_arn="y", iam_client=object()
    ).shared_role_name()

    roles = _tf_name_patterns("aws_iam_role", "name")
    policies = _tf_name_patterns("aws_iam_role_policy", "role")
    assert len(roles) >= IAM_ROLE_FLOOR, roles
    assert re.fullmatch('[^"]*' + re.escape("-agent-ecr-push"), shared), shared

    for where, addr, pattern in roles + policies:
        assert not re.fullmatch(pattern, shared), f"{where}: {addr} can produce {shared}"

    root = _tf_code(_INFRA / "main.tf")
    (arn_expr,) = re.findall(r'agent_ecr_push_role_arn\s*=\s*"([^"]+)"', root)
    assert arn_expr.endswith(f":role/${{local.name_prefix}}{shared[len('agp') :]}"), arn_expr
    (passed,) = re.findall(r"project_ecr_push_role_arn\s*=\s*(\S+)", root)
    assert passed == "local.agent_ecr_push_role_arn", passed


def _registry_id_guard_line():
    """The runtime line that fails the build when `AGENT_REGISTRY_ID` is empty.

    The registry id is not a Terraform-supplied value: AWS mints registry ids, there is no Terraform
    resource for the `agent-registry` namespace, and reading one back from a plan-time capture file
    is what makes a from-zero deploy need two applies. So `modules/codebuild` declares
    `AGENT_REGISTRY_ID` empty and the real id arrives per build as an `environmentVariablesOverride`
    from `RuntimeBuildService.start_runtime_build` (a `retry-build` replays the original overrides).
    That leaves one reachable gap: a hand-run `aws codebuild start-build` that supplies
    `IAC_TYPE=agentcore_runtime` plus the `GIT_*` wiring but forgets the id. Because this buildspec
    has no `set -e`, an empty id does not abort, it cascades: `get-registry-record --registry-id ""`
    fails non-fatally, every `jq` read of the empty result yields "" at rc 0, `terraform apply`
    runs, and an AgentCore runtime goes live with an empty model id. The `NEW_ENV` guard downstream
    then reports "LIVE but UNTRACKED", detecting the worst outcome this system can produce (a live,
    invocable, billing runtime whose ARN is recorded nowhere, which the delete cascade can never
    reclaim) without being able to prevent it. These tests pin the fail-fast guard that closes the
    gap and, the half that matters, its position ahead of the first registry call and any
    `terraform apply`; a guard that exists but sits below the apply is worth nothing.

    Read out of `_agentcore_branch()` (`yaml.safe_load`), never off stripped file lines, and
    comments dropped first: the guard's own comment block explains at length that it refuses
    "BEFORE anything is provisioned", so a substring search over the raw body finds that prose and
    passes with no guard present at all."""
    hits = [
        line.strip()
        for line in _agentcore_branch().split("\n")
        if not line.strip().startswith("#") and 'z "$AGENT_REGISTRY_ID"' in line
    ]
    assert len(hits) == 1, f"expected exactly one AGENT_REGISTRY_ID emptiness guard, got {hits}"
    return hits[0]


def test_an_empty_AGENT_REGISTRY_ID_FAILS_the_build():
    """The guard exists, tests emptiness, and exits non-zero.

    `exit 1` rather than `_st failed; _dep failed` is deliberate and asserted: those helpers are
    not defined until further down this branch, so calling them here would itself be a
    command-not-found — and there is nothing to record anyway, since no deployment was attempted.
    Contrast `test_a_stale_cloned_module_FAILS_the_build_instead_of_deploying_silently`, whose
    guard sits after the helpers and therefore must stamp the failure."""
    line = _registry_id_guard_line()
    assert "exit 1" in line, line
    assert "_st failed" not in line and "_dep failed" not in line, (
        "this guard runs before _st/_dep are defined — stamping here would be a "
        "command-not-found, and there is no deployment to record"
    )


def test_the_registry_id_guard_MESSAGE_names_the_override_that_supplies_it():
    """The message is the whole remedy, so it is pinned like one.

    An operator who trips this is, by construction, running a build by hand — the platform path
    cannot reach it. They need three things the message must carry: that the platform supplies the
    id as a per-build override (so the fix is usually "start it through the platform"), the exact
    flag to pass if they really must run it by hand, and how to obtain the id at all now that no
    file on disk holds one. Asserted on CONTENT, not on exception type, for the same reason
    `test_registry_name_resolution` asserts on its message: a message whose entire job is to be
    actionable is worth nothing if it degrades to "AGENT_REGISTRY_ID is empty". It must also say
    why it refuses, not merely that it did."""
    line = _registry_id_guard_line()
    assert "environmentVariablesOverride" in line, line
    assert "start_runtime_build" in line, line
    assert "--environment-variables-override" in line, line
    assert "list-registries" in line, line
    assert "BEFORE anything is provisioned" in line, line


def test_the_registry_id_guard_runs_BEFORE_the_first_registry_call_and_ANY_apply():
    """The half that matters. Position, off executable lines only.

    A guard below `get-registry-record` is pointless (the empty-id read has already happened and
    silently produced ""), and a guard below `terraform apply` is worse than pointless — the
    runtime is already live and unreclaimable, which is precisely the outcome the guard exists to
    make impossible. So it must be the FIRST executable line in the branch: everything above it
    cannot create a resource, everything below it can. Non-vacuity: the registry read and the apply
    really are in this branch, so the ordering is a real comparison."""
    lines = _agentcore_code_lines()
    guard = _registry_id_guard_line()

    assert lines[0] == "agentcore_runtime)", lines[0]
    assert lines[1] == guard, (
        "the emptiness guard must be the first executable statement in the branch, "
        f"but that slot holds: {lines[1]}"
    )

    def index_of(needle):
        hits = [i for i, line in enumerate(lines) if needle in line]
        assert hits, f"no executable line containing {needle!r}"
        return hits[0]

    guard_at = lines.index(guard)
    assert guard_at < index_of("agent-registry-control get-registry-record")
    assert guard_at < index_of("terraform apply")


def test_the_project_declares_the_registry_id_EMPTY_which_is_what_needs_the_guard():
    """Pins the other side of the contract across the file boundary.

    The guard only matters because the project-level value is `""`. If someone "helpfully"
    re-baked a real id into Terraform, they would reintroduce the plan-time capture file and the
    second apply, and the stale-id failure mode (write-back lands
    on the wrong registry, or 404s, leaving the same untracked runtime) on top. So the empty
    declaration is asserted, and so is the absence of any variable behind it."""
    main_tf = (MODULE / "main.tf").read_text()
    block = re.search(
        r'environment_variable\s*\{\s*name\s*=\s*"AGENT_REGISTRY_ID"\s*value\s*=\s*("[^"]*"|\S+)\s*\}',
        main_tf,
    )
    assert block, "the AGENT_REGISTRY_ID environment_variable declaration is gone"
    assert block.group(1) == '""', block.group(1)
    assert "var.agent_registry_id" not in main_tf, (
        "a Terraform-supplied registry id is back — that reinstates the plan-time capture file "
        "and the two-apply from-zero deploy this epic removed"
    )


def _scratch_purge_command():
    """The post_build purge command AS CODEBUILD RUNS IT (`yaml.safe_load`), plus its phase.

    `GIT_SECRET_ARN` is a StartBuild override holding a ready git bearer token (a verbatim PAT, or
    a ~1h GitHub App installation token); without a purge one Secrets Manager secret piles up per
    build, forever, billing monthly. `post_build` is the reclaim point for every build that started,
    running after a successful build and after a failed build phase alike. The complementary leg (a
    StartBuild that faults before any build exists) lives in the service and is pinned in
    `tests/test_runtime_build_service.py`."""
    hits = [(p, c) for p, c in _runtime_commands() if "delete-secret" in c]
    assert len(hits) == 1, f"expected exactly one delete-secret command, got {len(hits)}"
    return hits[0]


def test_the_scratch_clone_token_is_PURGED_in_post_build():
    """Force delete, by ARN, guarded on the variable being set.

    `--force-delete-without-recovery` is the flag that matters: a plain `delete-secret` schedules
    deletion 30 days out and the token stays READABLE (and billing) for that whole window, which is
    not a purge. The `-n` guard matters because only the git-clone source path defines the variable —
    a CodeCommit or S3-archive build has no scratch secret, and an unguarded delete would WARN on
    every one of those builds."""
    phase, cmd = _scratch_purge_command()

    assert phase == "post_build", f"the purge must run in post_build, not {phase}"
    assert 'aws secretsmanager delete-secret --secret-id "$GIT_SECRET_ARN"' in cmd
    assert "--force-delete-without-recovery" in cmd
    assert '[ -n "$GIT_SECRET_ARN" ]' in cmd


def test_the_purge_is_the_FIRST_post_build_command():
    """Position, and it matters rather than being cosmetic.

    A failing command aborts the remainder of its phase. `cat /tmp/outputs.json`, the command that
    would otherwise open post_build, fails whenever the build phase died before writing that file,
    exactly the failed-deploy path where the leaked token most needs reclaiming. Anywhere below it,
    the purge silently never runs for a failed build. Non-vacuity: the command it must precede
    really is in this phase and really can fail."""
    import yaml

    commands = yaml.safe_load(BUILDSPEC.read_text())["phases"]["post_build"]["commands"]
    _phase, cmd = _scratch_purge_command()

    assert commands[0] == cmd, (
        f"the scratch-token purge must be the first post_build command; that slot holds: {commands[0]!r}"
    )
    assert "cat /tmp/outputs.json" in commands


@pytest.mark.parametrize(
    "aws_body,git_secret_arn,expect_warn",
    [
        ("exit 254\n", "arn:aws:secretsmanager:us-east-1:111:secret:agp/rbt/x", True),
        ('echo \'{"ARN":"x"}\'\n', "arn:aws:secretsmanager:us-east-1:111:secret:agp/rbt/x", False),
        (None, "arn:aws:secretsmanager:us-east-1:111:secret:agp/rbt/x", True),
        ("exit 254\n", "", False),
    ],
    ids=["delete_fails", "delete_succeeds", "aws_missing_from_PATH", "no_scratch_secret"],
)
def test_the_purge_can_NEVER_fail_the_build(tmp_path, aws_body, git_secret_arn, expect_warn):
    """EXECUTES the real command and requires exit 0 for every outcome.

    A cleanup step that fails the phase would report a SUCCESSFUL deploy as a failed build — the
    worst possible trade for reclaiming a secret. This is an execution test rather than a
    `"|| " in cmd` string check because the exit status of an `if` block is the status of its last
    command: a guard that looks right can still propagate a non-zero rc. `PATH` is REPLACED so the
    `aws_missing_from_PATH` case is a genuine absence, and the WARN assertion keeps the failures
    visible in the build log instead of silently swallowed. The delete-secret response is noise in
    the build log, and the token itself must never be echoed: only the WARN (which carries the ARN,
    not the secret value) may reach stdout."""
    import subprocess

    _phase, cmd = _scratch_purge_command()

    binder = tmp_path / "bin"
    binder.mkdir()
    if aws_body is not None:
        (binder / "aws").write_text("#!/bin/sh\n" + aws_body)
        (binder / "aws").chmod(0o755)

    out = subprocess.run(
        ["sh", "-c", cmd],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": f"{binder}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "GIT_SECRET_ARN": git_secret_arn,
        },
    )

    ctx = f"rc={out.returncode} stdout={out.stdout!r} stderr={out.stderr!r}"
    assert out.returncode == 0, ctx
    assert ("WARN: could not purge the scratch clone token" in out.stdout) is expect_warn, ctx
    if not expect_warn:
        assert out.stdout.strip() == "", ctx


def _ambient_creds_gate() -> str:
    """The single pre_build line gating the ambient target-account assume-role, as CodeBuild runs
    it. Exactly one such gate must exist — two would be a second, unreviewed credential path."""
    for _, cmd in _runtime_commands():
        for raw in cmd.split("\n"):
            stripped = raw.strip()
            if stripped.startswith("if ") and "TARGET_ROLE_ARN" in stripped and "IAC_TYPE" in stripped:
                return stripped
    raise AssertionError("no ambient-creds gate found in the buildspec")


def test_the_ambient_target_creds_gate_is_a_POSITIVE_iac_type_allowlist():
    """The gate must name the iac_types that want ambient target creds.

    An exclusion (`!= "agentcore_runtime"`) is a gate that opens for every iac_type nobody thought
    about, `agentcore_harness` among them. Exporting target creds for the whole build breaks a
    config deploy in cross-account mode: the harness branch's calls are control-plane ones (the
    scratch secret, the agent registry record, and a `terraform init` against the control-plane
    STATE_BUCKET/LOCK_TABLE) and they would run in the tenant account, where none of those resources
    exist. It is also a double role hop, since the harness module's own provider already assumes
    `deploy_role_arn`. Single-account deploys mask all of it (same account either way), so a live
    test passes green; this assertion is the only thing that catches it.

    Positive form: no `!=` iac_type comparison anywhere in the gate. Neither agentcore branch may be
    in the allowlist, both drive their own provider-level assume_role and both make control-plane
    calls under the ambient CodeBuild role; the legacy `terraform` branch is the one that genuinely
    needs them (it has no provider assume_role of its own), so the gate must still fire for
    something."""
    gate = _ambient_creds_gate()
    assert '"$IAC_TYPE" != ' not in gate, gate
    assert '"$IAC_TYPE" = ' in gate, gate
    assert "agentcore_runtime" not in gate, gate
    assert "agentcore_harness" not in gate, gate
    assert "terraform" in gate, gate


def _iac_type_cases() -> set:
    """The `case "$IAC_TYPE" in` branch labels, INDENTATION-INSENSITIVE.

    Anchoring on a literal 10-space indent (`^          ([a-z_]+)\\)`) would be the wrong fence: a
    fourth branch added at any other indentation would simply not be seen, and every "we enumerated
    every case" assertion built on it would pass vacuously, the exact failure mode those assertions
    exist to prevent. Matched from the `case` statement to its `esac` so a `)` elsewhere
    in the file cannot be mistaken for a branch label."""
    text = BUILDSPEC.read_text()
    start = text.index('case "$IAC_TYPE" in')
    body = text[start : text.index("\n        esac", start)]
    return set(re.findall(r"^\s*([a-z_]+)\)", body, re.MULTILINE))


def test_every_iac_type_case_is_accounted_for_by_the_creds_gate():
    """Non-vacuity: the allowlist is asserted against the branches that actually EXIST, so adding
    a fourth `IAC_TYPE` case without deciding its credential mode fails here rather than silently
    inheriting whichever default the gate happens to express."""
    cases = _iac_type_cases()
    assert cases == {"terraform", "agentcore_runtime", "agentcore_harness", "agentcore_gateway"}, cases
    gate = _ambient_creds_gate()
    for case in cases:
        in_allowlist = f'"$IAC_TYPE" = "{case}"' in gate
        assert in_allowlist == (case == "terraform"), (case, gate)


def _harness_branch() -> str:
    """The `agentcore_harness)` case body, up to its `;;`, with shell comments STRIPPED.

    The config-build path's deploy step differs structurally from the `agentcore_runtime` branch in
    two ways the harness tests pin. It clones two repos: the harness module reads the agent
    definition with `yamldecode(file(var.harness_config_path))`, evaluated at plan time, so
    `harness.yaml` must exist on the apply host before terraform runs, and that file lives in the
    developer's agent repo while the module lives in the org's infra repo, so the pre_build clone of
    infra alone is not enough. And there is no image: no ECR login, no digest, no cross-account
    copy, the whole image leg of the runtime branch is absent rather than skipped.

    Scoped to the one branch so an assertion cannot pass by matching a string that lives in the
    `agentcore_runtime` branch instead. Comments are dropped for the reason `_tag_lines` above drops
    them: the branch's own prose explains what it deliberately does NOT do (derive an agent id from
    the tag, touch a registry), and an absence assertion read against prose would fire on the
    explanation of the very invariant it is checking."""
    text = BUILDSPEC.read_text()
    start = text.index("agentcore_harness)")
    end = text.index(";;", start)
    return "\n".join(line for line in text[start:end].splitlines() if not line.strip().startswith("#"))


def test_the_buildspec_has_an_agentcore_harness_case():
    """The branch key is the infra template's `iac_type`, which the backend now resolves from the
    catalog. Without a matching case the buildspec's `*)` default fires and every config build dies
    on "Unsupported IAC_TYPE", the deploy path failing at the last possible moment. The runtime case
    is still there: this is an addition, not a replacement."""
    text = BUILDSPEC.read_text()
    assert "agentcore_harness)" in text
    assert "agentcore_runtime)" in text


def test_the_harness_branch_clones_the_agent_repo_with_the_same_secret_mechanism():
    """The SECOND clone, and it must reuse the `GIT_SECRET_ARN` → `jq -r '.token'` mechanism.

    That mechanism exists because a GitHub App connection's own secret has no `.token` key at all;
    the backend stages a ready bearer token in a per-build scratch secret so one clone idiom works
    for PAT and App alike. A second clone inventing its own credential path would work for PAT
    connections and fail silently for App ones, the exact bug the mechanism closes. The base URL is
    honoured, so a GitHub Enterprise host is not silently replaced by github.com."""
    branch = _harness_branch()
    assert "$GIT_AGENT_REPO" in branch
    assert "GIT_SECRET_ARN" in branch
    assert "jq -r '.token" in branch
    assert "git clone" in branch
    assert "GIT_BASE_URL" in branch


def test_the_harness_branch_fails_loudly_when_the_agent_repo_is_not_named():
    """An empty `GIT_AGENT_REPO` must be a hard error, not a clone of `.../.git`.

    The backend sends this key on every config build, so an empty value means a bug upstream — and
    the failure has to happen HERE, before terraform, because the alternative is a `harness.yaml`
    that does not exist and a generic Terraform file-not-found much later."""
    branch = _harness_branch()
    assert re.search(r'if \[ -z "\$GIT_AGENT_REPO" \]', branch), branch[:400]
    assert "exit 1" in branch


def test_the_harness_branch_points_harness_config_path_into_the_agent_clone():
    """`harness_config_path` must resolve to the file in the AGENT clone, not the infra one.

    The infra repo holds the module; it has no `harness.yaml`. Pointing the tfvar at the infra
    workspace would make every config agent deploy the same (nonexistent) definition. The path is
    the agent clone's, and the agent clone is not `/tmp/workspace`, the infra one."""
    branch = _harness_branch()
    assert "harness_config_path" in branch
    assert "harness.yaml" in branch
    assert AGENT_CLONE in branch


def test_the_harness_branch_state_key_mirrors_the_runtime_idiom():
    """`agentcore-harness/{agent_id}/{stage}/terraform.tfstate` — the runtime key's shape with the
    prefix swapped, and byte-identical to the module's own documented `local.state_key`.

    The $STAGE segment is the half that matters: without it every stage shares one state file and
    one harness, so a prod apply mutates the dev harness. The module's own documented convention
    must agree, since the two are written independently."""
    branch = _harness_branch()
    assert "key=agentcore-harness/$AGENT_ID/$STAGE/terraform.tfstate" in branch
    module_main = (
        Path(__file__).resolve().parents[2] / "infrastructure/modules/agentcore_harness/main.tf"
    ).read_text()
    assert 'state_key = "agentcore-harness/${var.agent_id}/${var.stage}/terraform.tfstate"' in module_main


def _hoisted_agent_id_line() -> str:
    """The single shared-scope line that resolves `AGENT_ID` before the `case`."""
    (line,) = [
        raw.strip()
        for _, cmd in _runtime_commands()
        for raw in cmd.split("\n")
        if "AGENT_ID=" in raw and "%%-*" in raw and not raw.strip().startswith("#")
    ]
    return line


def test_the_tag_derivation_of_agent_id_is_SCOPED_TO_THE_CONTAINER_PATH():
    """The agent-id guard below must not be dead code.

    The shared `AGENT_ID` resolution is `${AGENT_ID:-${IMAGE_TAG%%-*}}`, and on the config path
    `IMAGE_TAG` is a validated 40-hex commit sha with no `-` in it. So an UNGATED default makes
    `AGENT_ID` non-empty for a config build even when the backend sent nothing — silently turning
    the harness branch's `[ -z "$AGENT_ID" ]` refusal into a no-op and keying Terraform state at
    `agentcore-harness/<the-whole-sha>/<stage>`. That is a fresh harness on every push with the
    predecessor orphaned: precisely the outcome the guard exists to prevent.

    The fallback fires only for the container mechanism, so "the backend forgot to send AGENT_ID"
    stays an empty string on the config path and the guard still refuses: the derivation is gated
    on the container iac_type, not applied unconditionally."""
    line = _hoisted_agent_id_line()
    assert "agentcore_runtime" in line, line
    assert "IAC_TYPE" in line, line


def test_a_config_build_with_no_AGENT_ID_in_the_env_REFUSES(tmp_path):
    """The property itself, EXECUTED rather than pattern-matched.

    A text assertion such as `"IMAGE_TAG%%-*" not in branch` goes vacuous the moment the derivation
    is hoisted out of the branch, true for the wrong reason while the real behaviour regresses. So
    this runs the two real lines in a shell: a config build whose env
    carries no `AGENT_ID` must end with an EMPTY `AGENT_ID`, which is what makes the guard fire."""
    import subprocess

    script = tmp_path / "probe.sh"
    script.write_text(
        "IAC_TYPE=agentcore_harness\n"
        "IMAGE_TAG=0123456789abcdef0123456789abcdef01234567\n"
        f"{_hoisted_agent_id_line()}\n"
        'printf "[%s]" "$AGENT_ID"\n'
    )
    for shell in ("bash", "sh"):
        out = subprocess.run([shell, str(script)], capture_output=True, text=True, check=True).stdout
        assert out == "[]", f"{shell}: AGENT_ID must stay empty on the config path, got {out}"


def test_the_container_path_still_derives_agent_id_from_its_tag_prefix(tmp_path):
    """The other half: gating the default must not break the container path, whose AGENT_ID has
    ALWAYS been derived from the `<agent_id>-` tag prefix and is never sent in the env.

    Note `%%-*` strips the LONGEST match from the left, i.e. everything from the FIRST hyphen — so
    this derivation only round-trips for a hyphen-free agent id (a registry `recordId`, as the
    existing executed probes in this file assume with `ag` / `a-1`). That is pre-existing container
    behaviour, preserved here byte-identically rather than changed; it is also a further reason the
    config path must not inherit the derivation, since a commit sha has no separator at all."""
    import subprocess

    script = tmp_path / "probe.sh"
    script.write_text(
        "IAC_TYPE=agentcore_runtime\n"
        "IMAGE_TAG=ag42-abc1234\n"
        f"{_hoisted_agent_id_line()}\n"
        'printf "[%s]" "$AGENT_ID"\n'
    )
    for shell in ("bash", "sh"):
        out = subprocess.run([shell, str(script)], capture_output=True, text=True, check=True).stdout
        assert out == "[ag42]", f"{shell}: expected [ag42], got {out}"


def test_an_explicit_AGENT_ID_always_wins_over_the_derivation(tmp_path):
    """Env-wins is the contract on BOTH paths: a sent value must never be overwritten by a
    derivation, or a config build's real agent id would be replaced by a sliced commit sha."""
    import subprocess

    for iac in ("agentcore_runtime", "agentcore_harness"):
        script = tmp_path / f"probe-{iac}.sh"
        script.write_text(
            f"IAC_TYPE={iac}\n"
            "IMAGE_TAG=0123456789abcdef0123456789abcdef01234567\n"
            "AGENT_ID=ag-real\n"
            f"{_hoisted_agent_id_line()}\n"
            'printf "[%s]" "$AGENT_ID"\n'
        )
        out = subprocess.run(["bash", str(script)], capture_output=True, text=True, check=True).stdout
        assert out == "[ag-real]", f"{iac}: expected [ag-real], got {out}"


def test_the_harness_branch_keeps_a_LIVE_agent_id_guard():
    """The guard must still be present, and must record before exiting (not a bare exit)."""
    branch = _harness_branch()
    (guard,) = [line for line in branch.splitlines() if '[ -z "$AGENT_ID" ]' in line]
    assert "_st failed" in guard and "_dep failed" in guard, guard
    assert "exit 1" in guard, guard


def test_the_harness_branch_has_no_image_leg():
    """No ECR, no digest, no cross-account image copy. There is no container on this path, so
    these are ABSENT rather than skipped — a blank-valued image variable would send the runtime
    branch's `${ECR_REPO%%.*}` account-id derivation down a path whose input does not exist."""
    branch = _harness_branch()
    for absent in ("ECR_REPO", "IMAGE_DIGEST", "SOURCE_ECR", "docker "):
        assert absent not in branch, absent


def test_the_harness_branch_applies_terraform_and_fails_the_build_on_error():
    """A failed apply must exit non-zero. The buildspec has no `set -e`, so every step is guarded
    explicitly — an unguarded apply would let the build go GREEN having deployed nothing."""
    branch = _harness_branch()
    assert "terraform init" in branch
    assert "terraform apply" in branch
    assert "exit 1" in branch


_HARNESS_MODULE = Path(__file__).resolve().parents[2] / "infrastructure/modules/agentcore_harness"


def test_every_harness_tfvar_the_buildspec_WRITES_is_DECLARED_by_the_module():
    """The silent-drop guard, in both directions.

    Terraform treats a tfvar with no matching `variable` block as a WARNING ("Value for undeclared
    variable") and DISCARDS it. That is exactly how `stage` and the langfuse vars were once lost, a
    build that goes green having ignored the value it was given, and a buildspec passing
    `aws_region=` to a module that declares only `region` is the same drift. So the heredoc's LHS
    names are parsed and each is required to be a real variable.

    Non-vacuity from the other side of the same seam: the vars with no default must all be supplied,
    or the apply prompts (and with `-input=false`, fails). "Has a default" is detected as the
    `default =` argument, never the substring `default`: the `region` and `stage` blocks both use the
    word in prose, so a substring test would classify them as optional and quietly shrink this half
    of the check. The required set is pinned as a count fence, so a prose edit or refactor that
    collapses it fails rather than checking almost nothing.
    """
    branch = _harness_branch()
    body = branch.split("<<TFVARS", 1)[1].split("TFVARS", 1)[0]
    written = {m.group(1) for m in re.finditer(r'^\s*([a-z_]+)\s*=\s*"', body, re.MULTILINE)}
    declared = set(
        re.findall(r'^variable\s+"([a-z_]+)"', (_HARNESS_MODULE / "variables.tf").read_text(), re.MULTILINE)
    )
    assert written, "no tfvars parsed out of the harness heredoc"
    assert written <= declared, f"undeclared tfvars would be SILENTLY DISCARDED: {written - declared}"
    variables_tf = (_HARNESS_MODULE / "variables.tf").read_text()
    required = {
        name
        for name in declared
        if not re.search(
            r"^\s*default\s*=",
            variables_tf.split(f'variable "{name}"', 1)[1].split("\nvariable ", 1)[0],
            re.MULTILINE,
        )
    }
    assert required == {
        "agent_id",
        "agent_name",
        "stage",
        "region",
        "harness_config_path",
        "tenant_id",
        "entra_app_audience",
    }, required
    assert required <= written, f"required module variables not passed: {required - written}"


def test_the_module_declares_no_dangling_aws_region_reference():
    """The other half of the same defect.

    A `variables.tf` documenting an `aws_region` companion variable that is never declared is a
    comment inviting callers to pass a var Terraform would discard. Neither the variable nor a
    reference to it may exist; the provider reads `var.region`. No `var.aws_region` dereference may
    exist anywhere in the module either (a hard "Reference to undeclared input variable" at validate
    time), checked across every .tf rather than variables.tf alone, since a dereference could live
    in main.tf or versions.tf. Deliberately not asserted on the bare string `aws_region`: the comment
    that replaced the dangling one names it, to record why it must not come back, and an assertion
    that fires on its own fix note would only teach the next maintainer to delete the explanation.
    The provider really does read var.region, so `region` is genuinely the only one."""
    variables_tf = (_HARNESS_MODULE / "variables.tf").read_text()
    assert 'variable "aws_region"' not in variables_tf
    for tf in sorted(_HARNESS_MODULE.glob("*.tf")):
        assert "var.aws_region" not in tf.read_text(), tf.name
    assert "region = var.region" in (_HARNESS_MODULE / "versions.tf").read_text()


def test_the_repo_sk_query_keeps_its_stderr_visible():
    """The hoisted query must not make a DDB/IAM failure silent.

    A permissions or table error prints into the build log, the only place an operator can see why
    `cicd_status` and the deployment history stopped updating; a `2>/dev/null` would suppress
    exactly that signal. Suppression is right for the write helpers (`_u`/`_dep` are best-effort, a
    history write must never fail a good deploy) but wrong for this read, whose failure silently
    disables all four."""
    line = _repo_sk_query_line()
    assert "2>/dev/null" not in line, line


def test_the_repo_sk_query_is_SCOPED_to_the_agent_paths():
    """The legacy `terraform` branch must not issue this query at all.

    That branch deploys foundation stacks: it has no agent, and it is the ONE path that runs under
    ambient TARGET-ACCOUNT credentials (see the creds-gate test). A query keyed on a derived
    AGENT_ID, against the CONTROL-PLANE table, from the tenant account, is a guaranteed
    access-denied on every legacy build, noise at best.

    Gating on a non-empty `AGENT_ID` is what scopes it, and that works only because the tag
    derivation is itself gated on the container path (see
    test_the_tag_derivation_of_agent_id_is_SCOPED_TO_THE_CONTAINER_PATH) — otherwise `terraform`
    builds would derive one from their own IMAGE_TAG and the guard would pass."""
    line = _repo_sk_query_line()
    assert '[ -n "$AGENT_ID" ]' in line, line


def test_the_legacy_terraform_path_resolves_no_agent_id(tmp_path):
    """EXECUTED proof of the scoping above: a legacy `terraform` build gets an empty AGENT_ID, so
    the guarded query never runs."""
    import subprocess

    script = tmp_path / "probe.sh"
    script.write_text(
        "IAC_TYPE=terraform\n"
        "IMAGE_TAG=some-foundation-stack-tag\n"
        f"{_hoisted_agent_id_line()}\n"
        'printf "[%s]" "$AGENT_ID"\n'
    )
    out = subprocess.run(["bash", str(script)], capture_output=True, text=True, check=True).stdout
    assert out == "[]", out


def test_the_write_back_helpers_are_defined_in_SHARED_scope_not_inside_a_case_branch():
    """The structural reason a config path can have no history at all.

    `_u`/`_st`/`_tag`/`_dep` (and the `REPO_SK` lookup that feeds them) defined inside the
    `agentcore_runtime` case body simply do not exist for any other branch, and pinning a single
    definition is not enough, since it can be single in the wrong scope. So this asserts the
    definitions sit before `case "$IAC_TYPE" in`, which is what makes them reachable from every
    branch and what stops the next mechanism from having to duplicate (and drift from) them. The
    REPO_SK resolution is shared too (the helpers all guard on it), and AGENT_ID is resolved once,
    env-wins-else-derive, so both tag shapes work."""
    text = BUILDSPEC.read_text()
    case_at = text.index('case "$IAC_TYPE" in')
    for helper in ("_u()", "_st()", "_tag()", "_dep()"):
        at = text.index(f"\n        {helper}")
        assert at < case_at, f"{helper} is defined inside a case branch, not shared scope"
    assert text.index("REPO_SK=$(aws dynamodb query") < case_at
    assert 'AGENT_ID="${AGENT_ID:-${IMAGE_TAG%%-*}}"' in text


def test_the_harness_branch_has_the_stale_module_drift_guard():
    """The agentcore_harness twin of the runtime branch's drift guard.

    The module that RUNS is the cloned per-org `agp-harness-infra`, not the one in this repo, and
    terraform DISCARDS a tfvar with no matching `variable` block with only a warning. Against a
    stale module `harness_config_path` would be dropped silently. `harness_config_path` is the
    sentinel because no pre-harness module version can declare it. The guard precedes
    `terraform init` (after `apply` it fires only once damage is done), and the refusal is recorded,
    not a bare exit."""
    branch = _harness_branch()
    assert 'variable[[:space:]]+"harness_config_path"' in branch, branch[-600:]
    assert "STALE HARNESS MODULE" in branch
    assert branch.index("STALE HARNESS MODULE") < branch.index("terraform init")
    guard_line = next(line for line in branch.splitlines() if "STALE HARNESS MODULE" in line)
    assert "_st failed" in guard_line and "_dep failed" in guard_line


def _harness_drift_guard_lines() -> list[str]:
    return [line.strip() for line in _harness_branch().splitlines() if "STALE HARNESS MODULE" in line]


def test_the_harness_branch_also_guards_on_the_tenant_id_sentinel():
    """An older harness module declares `harness_config_path` but not `tenant_id`, so the Entra
    tfvars would be discarded and the harness deployed with no authorizer. Second sentinel, same
    recorded-failure idiom, before `terraform init`."""
    branch = _harness_branch()
    (line,) = [raw for raw in _harness_drift_guard_lines() if '"tenant_id"' in raw]
    assert "_st failed" in line and "_dep failed" in line and "exit 1" in line, line
    assert "re-run the template rollout so agp-harness-infra carries the 30a module" in line.lower()
    assert branch.index(line) < branch.index("terraform init")


@pytest.mark.parametrize(
    "root_tf,expect_exit",
    [
        ('variable "harness_config_path" {}\nvariable "tenant_id" {}\n', 0),
        ('variable "harness_config_path" {}\n', 1),
        ('variable "harness_config_path" {}\n# tenant_id comes later\n', 1),
        ("", 1),
    ],
    ids=["both-declared", "no-tenant-id", "comment-only", "empty-root"],
)
def test_the_harness_drift_guards_actually_fire_on_a_stale_module(tmp_path, root_tf, expect_exit):
    """EXECUTES both harness sentinels against a synthetic cloned root (mirrors the runtime's): an
    older module declaring only `harness_config_path` fails, and a `#` mention of `tenant_id` is not
    a declaration."""
    import subprocess

    (tmp_path / "main.tf").write_text(root_tf)
    script = (
        '_st() { echo "ST:$1"; }\n'
        '_dep() { echo "DEP:$1"; }\n'
        f"cd {tmp_path}\n" + "\n".join(_harness_drift_guard_lines()) + "\necho REACHED_TERRAFORM\n"
    )
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == expect_exit, f"stdout={out.stdout!r} stderr={out.stderr!r}"
    if expect_exit:
        assert "REACHED_TERRAFORM" not in out.stdout, "the guard did not stop the build"
        assert "STALE HARNESS MODULE" in out.stdout and "ST:failed" in out.stdout, out.stdout
    else:
        assert "REACHED_TERRAFORM" in out.stdout, out.stdout


def test_the_harness_branch_fails_the_build_when_no_arn_comes_back():
    """Mirrors the runtime branch's `agent_runtime_arn` guard: no ARN must FAIL the build, and fail
    it recorded, rather than falling through to `_st deployed`.

    The ARN is written back to the agent registry record on this path too, so the operator-facing
    message must not claim it is "not recorded (backlog)": that wording would send whoever hits it
    looking in the wrong place."""
    branch = _harness_branch()
    assert "harness_arn" in branch
    (guard,) = [line for line in branch.splitlines() if "no harness_arn" in line]
    assert "_st failed" in guard and "_dep failed" in guard, guard
    assert "exit 1" in guard, guard
    assert "backlog" not in guard, guard


def test_the_harness_branch_records_its_outputs():
    """The outputs merge is what puts `harness_arn` where the platform can read it. Same helper the
    other branches use, so the outputs land in the same place from every mechanism."""
    branch = _harness_branch()
    assert "terraform output -json" in branch
    assert "merge_outputs.py" in branch


def _harness_revision_pin() -> str:
    """The shipped fetch+checkout lines, joined, following their `\\` continuations.

    The harness branch deploys the revision it names. The clone is `--depth 1` with no ref, so it
    lands on the agent repo's default-branch HEAD while `$IMAGE_TAG`, the commit sha, names the
    scratch secret, the DEPLOYMENT_ID, the `_tag` write-back and the `_dep` row. Recording one
    revision and applying another is a false governance attestation on the prod promote path, and
    it is silent: every other guard on this branch (harness.yaml exists, the drift guard, the
    harness_arn guard) tests presence, not identity, so all of them pass happily on the wrong tree.

    Read from `_harness_branch()` (i.e. from `yaml.safe_load`, comments stripped) so the probes below
    execute the real bytes CodeBuild runs rather than a re-typed approximation — the same reason
    `_dep_script_body` and `_hoisted_agent_id_line` read from the document instead of a literal."""
    lines = _harness_branch().splitlines()
    starts = [i for i, line in enumerate(lines) if "fetch --depth 1 origin" in line]
    assert len(starts) == 1, (
        "expected exactly one revision-pin fetch on the harness branch, got "
        f"{[lines[i].strip() for i in starts]}"
    )
    i = starts[0]
    frag = [lines[i]]
    while frag[-1].rstrip().endswith("\\"):
        i += 1
        frag.append(lines[i])
    return "\n".join(frag)


def test_the_harness_branch_CHECKS_OUT_the_commit_it_was_asked_to_deploy():
    """The revision pin, structural half.

    `$IMAGE_TAG` must SELECT the tree, not merely label the record. The ordering half matters as much
    as the presence half: `harness_config_path` is read by `yamldecode(file(...))` at PLAN time, so a
    checkout placed after terraform would be pure decoration. The pin operates on the agent clone,
    not the infra workspace (`/tmp/workspace`). A missing commit is a hard, recorded failure, never a
    fall-through to HEAD, which is the defect itself: the same `_st failed; _dep failed` idiom as
    every other failure on this branch, so the badge and the deploy history agree instead of the
    build vanishing as a bare exit."""
    branch = _harness_branch()
    frag = _harness_revision_pin()
    assert "$IMAGE_TAG" in frag, frag
    assert "git" in frag and "checkout" in frag, frag
    assert AGENT_CLONE in frag, frag
    first = frag.splitlines()[0]
    assert branch.index(first) < branch.index("HARNESS_CFG="), branch[:2000]
    assert branch.index(first) < branch.index("terraform init")
    assert "_st failed" in frag and "_dep failed" in frag, frag
    assert "exit 1" in frag, frag


def _probe_revision_pin(tmp_path, tag_of):
    """Run the SHIPPED pin lines against a real agent repo whose HEAD is NOT the named commit.

    Two commits (`v1` then `v2`), a `git clone --depth 1` with NO ref — byte-for-byte the shape of
    the buildspec's clone line, which is exactly why the clone lands on HEAD and the pin is needed.
    `uploadpack.allowAnySHA1InWant` is set because that is what the real remote does: GitHub serves a
    shallow fetch of a bare sha while a stock local remote refuses it, so without it the probe would
    fail for a reason the production path does not have. `/tmp/agent` → a tmp clone is the only
    substitution, and it is a path, not behaviour; `_st`/`_dep` are stubbed as echoing functions
    because the point is to observe WHICH recording calls the failure path makes."""
    import subprocess

    origin = tmp_path / "origin"
    origin.mkdir()
    git = ["git", "-C", str(origin), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "-C", str(origin), "config", "uploadpack.allowAnySHA1InWant", "true"], check=True)
    shas = []
    for body in ("prompt: v1\n", "prompt: v2\n"):
        (origin / "harness.yaml").write_text(body)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", body.strip()], check=True)
        shas.append(
            subprocess.run(
                ["git", "-C", str(origin), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )

    clone = tmp_path / "agent"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{origin}", str(clone)], check=True)
    script = tmp_path / "pin.sh"
    script.write_text(
        '_st() { echo "ST:$1"; }\n'
        '_dep() { echo "DEP:$1"; }\n'
        f'IMAGE_TAG="{tag_of(shas)}"\n'
        f"{_harness_revision_pin().replace(AGENT_CLONE, str(clone))}\n"
        f'printf "CFG:%s" "$(cat {clone}/harness.yaml)"\n'
    )
    return subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, timeout=120, cwd=str(tmp_path)
    )


def test_the_harness_revision_pin_ACTUALLY_deploys_the_named_commit(tmp_path):
    """The pin, EXECUTED: the tree `harness_config_path` points at IS the named commit.

    HEAD is `v2` and the build names `v1`, the wrong-revision case a pair of pushes inside one build
    duration produces for real, because `deploy.yml`'s concurrency group QUEUES build #1 behind #2
    rather than cancelling it. Without the pin this reads `v2` while the `Deployment` row,
    `last_dev_image_tag` and the console all say `v1`."""
    proc = _probe_revision_pin(tmp_path, lambda shas: shas[0])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "CFG:prompt: v1" in proc.stdout, proc.stdout + proc.stderr
    assert "prompt: v2" not in proc.stdout, "the pin fell through to default-branch HEAD"


def test_the_harness_revision_pin_FAILS_RECORDED_when_the_named_commit_is_absent(tmp_path):
    """The other half, and it is required behaviour rather than an accident.

    A config build whose named commit does not exist must NOT fall back to HEAD — that fallback IS
    the defect. So the pin fails the build, and it fails it RECORDED, which is the whole point of the
    shared helpers: the badge and the deploy history agree instead of the build vanishing. And it
    does not silently proceed to read a config off the wrong tree."""
    proc = _probe_revision_pin(tmp_path, lambda shas: "0" * 40)
    assert proc.returncode != 0, proc.stdout
    assert "ST:failed" in proc.stdout, proc.stdout + proc.stderr
    assert "DEP:failed" in proc.stdout, proc.stdout + proc.stderr
    assert "CFG:" not in proc.stdout, proc.stdout


def _harness_tfvars() -> list[str]:
    """The harness heredoc's tfvars lines.

    The harness branch wires the inbound Entra authorizer and writes its ARN back: a harness deployed
    with no authorizer and an ARN recorded nowhere can neither be invoked by AGP nor reclaimed by
    the delete cascade. So the harness module takes the same three Entra inputs as the runtime
    module, and the branch writes the ARN into the same envelope fields the runtime branch does:
    `.agent_arns[$STAGE]`, `.agent_arn`, `identity_status`."""
    body = _harness_branch().split("<<TFVARS", 1)[1].split("TFVARS", 1)[0]
    return [line.strip() for line in body.splitlines() if line.strip()]


def test_the_harness_branch_passes_the_three_entra_authorizer_tfvars():
    """Same three lines, same sources, as the runtime branch's heredoc. A missing `tenant_id`
    fails at plan (no default); a missing audience pair would leave every token refused."""
    lines = _harness_tfvars()
    for expected in (
        'tenant_id="$ENTRA_TENANT_ID"',
        'entra_app_audience="$APP_AUD"',
        'entra_app_id="$APP_ID"',
    ):
        assert expected in lines, (expected, lines)


def test_the_harness_branch_reads_the_entra_wiring_from_the_RENAMED_custom_leaf():
    """APP_AUD/APP_ID come from the one registry read the branch already does, through the
    `descriptors.custom.data` leaf; a stale leaf yields empty audiences silently. Still exactly one
    registry read on this path."""
    branch = _harness_branch()
    (env_line,) = [line.strip() for line in branch.splitlines() if line.strip().startswith("ENV=$(")]
    assert ".descriptors.custom.data" in env_line, env_line
    assert 'APP_AUD=$(echo "$ENV" | jq -r \'.entra_app_audience // ""\')' in branch
    assert 'APP_ID=$(echo "$ENV" | jq -r \'.entra_app_id // ""\')' in branch
    assert branch.count("get-registry-record") == 1, branch


def _harness_write_block() -> list[str]:
    """`if [ -z "$HARNESS_ARN" ]` through `_tag; _st deployed; _dep succeeded`, comments dropped."""
    lines = [line.strip() for line in _harness_branch().splitlines() if line.strip()]
    start = next(i for i, line in enumerate(lines) if line.startswith('if [ -z "$HARNESS_ARN" ]'))
    end = next(i for i, line in enumerate(lines) if line.startswith("_tag; _st deployed"))
    assert start < end, lines[start : end + 1]
    return lines[start : end + 1]


def test_the_harness_write_back_is_the_RUNTIME_BRANCHS_jq():
    """Exactly the runtime branch's trio, no new field."""
    (line,) = [raw for raw in _harness_write_block() if raw.startswith("NEW_ENV=")]
    assert '--arg arn "$HARNESS_ARN"' in line and '--arg s "$STAGE"' in line, line
    assert "'.agent_arns[$s]=$arn | .agent_arn=$arn | .identity_status=\"provisioned\"'" in line, line
    assert line.rstrip().endswith('|| NEW_ENV=""'), line


def test_the_harness_write_back_is_GUARDED_on_every_step():
    """Same three recorded-failure paths as the runtime branch (no `set -e` anywhere)."""
    block = "\n".join(_harness_write_block())
    assert block.count("_st failed") == 3, block
    assert block.count("_dep failed") == 3, block
    assert block.count("exit 1") == 3, block
    (write_line,) = [line for line in _harness_write_block() if "update-registry-record" in line]
    assert "aws agent-registry-control " in write_line, write_line
    assert "||" in write_line and "_st failed" in write_line, write_line


@pytest.mark.parametrize(
    "stage,env_json,expected_arns",
    [
        ("dev", '{"model_id":"m","entra_app_id":"g"}', {"dev": "NEW"}),
        (
            "prod",
            '{"model_id":"m","entra_app_id":"g","agent_arns":{"dev":"OLD"}}',
            {"dev": "OLD", "prod": "NEW"},
        ),
    ],
    ids=["dev", "prod-keeps-dev"],
)
def test_the_harness_write_back_sends_the_PATCH_SHAPE_THE_REAL_MODEL_ACCEPTS(stage, env_json, expected_arns):
    """EXECUTES the harness block with `aws` stubbed, then validates the captured `--descriptors`
    against botocore's own UpdateRegistryRecord input shape, the runtime test's proof reused. The
    rest of the envelope survives the full-replace write."""
    import json
    import subprocess
    import tempfile

    from botocore.session import get_session
    from botocore.validate import ParamValidator

    arn = "arn:aws:bedrock-agentcore:eu-central-1:123456789012:harness/h-1"
    with tempfile.TemporaryDirectory() as tmp:
        cap = Path(tmp) / "argv"
        script = (
            "_st() { :; }\n_dep() { :; }\n_tag() { :; }\n"
            f'aws() {{ printf "%s\\0" "$@" > {cap}; }}\n'
            f"AGENT_REGISTRY_ID=reg\nAGENT_ID=ag\nSTAGE={stage}\n"
            f"HARNESS_ARN={arn}\n"
            f"ENV='{env_json}'\n" + "\n".join(_harness_write_block()) + "\n"
        )
        out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, f"stdout={out.stdout!r} stderr={out.stderr!r}"
        argv = [a for a in cap.read_text().split("\0") if a]

    flags = dict(pairwise(argv))
    params = {
        "registryId": flags["--registry-id"],
        "recordId": flags["--record-id"],
        "descriptors": json.loads(flags["--descriptors"]),
    }
    report = ParamValidator().validate(
        params,
        get_session()
        .get_service_model("agent-registry-control")
        .operation_model("UpdateRegistryRecord")
        .input_shape,
    )
    assert not report.has_errors(), report.generate_report()
    blob = json.loads(
        params["descriptors"]["optionalValue"]["custom"]["optionalValue"]["data"]["optionalValue"]
    )
    assert blob["agent_arns"] == {k: arn if v == "NEW" else v for k, v in expected_arns.items()}, blob
    assert blob["agent_arn"] == arn, blob
    assert blob["identity_status"] == "provisioned", blob
    assert blob["model_id"] == "m" and blob["entra_app_id"] == "g", blob


@pytest.mark.parametrize(
    "env_json,harness_arn,aws_rc,expect_exit,expect_aws_called",
    [
        ('{"model_id":"m"}', "arn:aws:x:harness/h", 0, 0, True),
        ('{"model_id":"m"}', "", 0, 1, False),
        ('{"model_id":"m","agent_arns":"oops"}', "arn:aws:x:harness/h", 0, 1, False),
        ("not json at all", "arn:aws:x:harness/h", 0, 1, False),
        ('{"model_id":"m"}', "arn:aws:x:harness/h", 254, 1, True),
    ],
    ids=["happy", "empty-arn", "legacy-string-map", "garbage-envelope", "write-fails"],
)
def test_the_harness_write_back_guards_ACTUALLY_fire(
    env_json, harness_arn, aws_rc, expect_exit, expect_aws_called
):
    """The runtime guard matrix, run against the harness block."""
    import subprocess

    script = (
        '_st() { echo "ST:$1"; }\n'
        '_dep() { echo "DEP:$1"; }\n'
        "_tag() { echo TAGGED; }\n"
        f"aws() {{ echo AWS_CALLED; return {aws_rc}; }}\n"
        "AGENT_REGISTRY_ID=reg\nAGENT_ID=ag\nSTAGE=dev\n"
        f"HARNESS_ARN='{harness_arn}'\n"
        f"ENV='{env_json}'\n" + "\n".join(_harness_write_block()) + "\n"
    )
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == expect_exit, f"stdout={out.stdout!r} stderr={out.stderr!r}"
    assert ("AWS_CALLED" in out.stdout) is expect_aws_called, out.stdout
    if expect_exit:
        assert "ST:deployed" not in out.stdout and "DEP:succeeded" not in out.stdout, out.stdout
        assert "ST:failed" in out.stdout and "DEP:failed" in out.stdout, out.stdout
        assert "ERROR:" in out.stdout, out.stdout
    else:
        assert "ST:deployed" in out.stdout and "DEP:succeeded" in out.stdout, out.stdout
        assert "TAGGED" in out.stdout, out.stdout


_H3_ARN = "arn:aws:bedrock-agentcore:eu-central-1:123456789012:harness/h-1"
_H3_OTHER = "arn:aws:bedrock-agentcore:eu-central-1:123456789012:harness/h-0"


@pytest.mark.parametrize(
    "stage,env_json,expect_write",
    [
        (
            "dev",
            '{"model_id":"m","identity_status":"provisioned","agent_arns":{"dev":"%s"},"agent_arn":"%s"}'
            % (_H3_ARN, _H3_ARN),
            False,
        ),
        (
            "dev",
            '{"model_id":"m","identity_status":"provisioned","agent_arns":{"dev":"%s"},"agent_arn":"%s"}'
            % (_H3_OTHER, _H3_OTHER),
            True,
        ),
        ("dev", '{"model_id":"m","identity_status":"pending"}', True),
        (
            "prod",
            '{"model_id":"m","identity_status":"provisioned","agent_arns":{"dev":"%s","prod":"%s"},"agent_arn":"%s"}'
            % (_H3_OTHER, _H3_ARN, _H3_ARN),
            False,
        ),
        (
            "prod",
            '{"model_id":"m","identity_status":"provisioned","agent_arns":{"dev":"%s","prod":"%s"},"agent_arn":"%s"}'
            % (_H3_OTHER, _H3_ARN, _H3_OTHER),
            True,
        ),
        (
            "dev",
            '{"model_id":"m","identity_status":"pending","agent_arns":{"dev":"%s"},"agent_arn":"%s"}'
            % (_H3_ARN, _H3_ARN),
            True,
        ),
    ],
    ids=[
        "unchanged-skips",
        "changed-arn-writes",
        "first-build-writes",
        "prod-unchanged-skips",
        "prod-agent_arn-stale-writes",
        "status-not-provisioned-writes",
    ],
)
def test_the_harness_write_back_SKIPS_an_unchanged_record(stage, env_json, expect_write):
    """Any UpdateRegistryRecord resets APPROVED->DRAFT and delists the marketplace card, so a
    rebuild whose jq-patched envelope equals the current one must not call the API, and must still
    finish as a successful deploy. When prod's ARN is already current but `agent_arn` points at dev
    (dev deployed last), writing flips `agent_arn`, so that is a real change and must be written."""
    import subprocess

    script = (
        '_st() { echo "ST:$1"; }\n'
        '_dep() { echo "DEP:$1"; }\n'
        "_tag() { echo TAGGED; }\n"
        "aws() { echo AWS_CALLED; }\n"
        f"AGENT_REGISTRY_ID=reg\nAGENT_ID=ag\nSTAGE={stage}\n"
        f"HARNESS_ARN={_H3_ARN}\n"
        f"ENV='{env_json}'\n" + "\n".join(_harness_write_block()) + "\n"
    )
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"stdout={out.stdout!r} stderr={out.stderr!r}"
    assert ("AWS_CALLED" in out.stdout) is expect_write, out.stdout
    skipped = "registry record unchanged — skipping write (keeps approval status)" in out.stdout
    assert skipped is (not expect_write), out.stdout
    assert "ST:deployed" in out.stdout and "DEP:succeeded" in out.stdout, out.stdout
    assert "TAGGED" in out.stdout, out.stdout


def test_the_harness_module_sets_the_SAME_entra_authorizer_as_the_runtime_module():
    """Same discovery URL and audience list as agentcore_runtime/main.tf."""
    main_tf = (_HARNESS_MODULE / "main.tf").read_text()
    assert "authorizer_configuration {" in main_tf
    assert "custom_jwt_authorizer {" in main_tf
    assert (
        'discovery_url    = "https://login.microsoftonline.com/${var.tenant_id}/v2.0/'
        '.well-known/openid-configuration"'
    ) in main_tf
    assert "allowed_audience = compact([var.entra_app_audience, var.entra_app_id])" in main_tf


def _gateway_branch() -> str:
    """The `agentcore_gateway)` case body up to its `;;`, shell comments stripped (as `_harness_branch`).

    An MCP-server repo's config build. The harness arm's twin minus the registry: the gateway's ARN,
    id and url are written onto the repo row (the backend's reconcile adopts them into the MCP
    record), because any registry write resets an APPROVED record to DRAFT. There is no AGENT_ID on
    this path, so the repo row is keyed by an env `REPO_SK` instead of the `agent_id-index` GSI, and
    dep.py must tolerate the missing agent id."""
    text = BUILDSPEC.read_text()
    start = text.index("agentcore_gateway)")
    end = text.index(";;", start)
    return "\n".join(line for line in text[start:end].splitlines() if not line.strip().startswith("#"))


def _gateway_code_lines() -> list[str]:
    return [line.strip() for line in _gateway_branch().splitlines() if line.strip()]


def test_the_gateway_arm_exists_before_the_default_arm():
    """Without it the `*)` default fires and every MCP build dies on "Unsupported IAC_TYPE"."""
    text = BUILDSPEC.read_text()
    case_at = text.index('case "$IAC_TYPE" in')
    gw_at = text.index("agentcore_gateway)", case_at)
    assert gw_at < text.index("\n          *)", case_at)
    assert "agentcore_gateway" in _iac_type_cases()


def test_the_gateway_arm_never_touches_the_registry():
    """The write-back goes to the repo row only. A registry write would delist an approved MCP card,
    and the MCP envelope is not where the agent registry write puts things."""
    branch = _gateway_branch()
    assert "update-registry-record" not in branch
    assert "get-registry-record" not in branch
    assert "AGENT_REGISTRY_ID" not in branch


def _gateway_guard_line(var: str) -> str:
    (line,) = [
        raw
        for raw in _gateway_code_lines()
        if raw.startswith(f'[ -z "${var}" ]') or raw.startswith(f'if [ -z "${var}" ]')
    ]
    return line


@pytest.mark.parametrize(
    "var", ["MCP_SERVER_ID", "MCP_NAME", "GIT_AGENT_REPO", "ENTRA_APP_AUDIENCE", "REPO_SK"]
)
def test_the_gateway_arm_REFUSES_each_empty_required_var(tmp_path, var):
    """EXECUTED: each guard, with its var empty, records the failure and exits 1 before any clone, and
    a set value passes the guard."""
    import subprocess

    line = _gateway_guard_line(var)
    assert "_st failed" in line and "_dep failed" in line and "exit 1" in line, line
    script = f'_st() {{ echo "ST:$1"; }}\n_dep() {{ echo "DEP:$1"; }}\n{var}=""\n{line}\necho REACHED\n'
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 1, out
    assert "REACHED" not in out.stdout and "ST:failed" in out.stdout and "DEP:failed" in out.stdout
    ok = subprocess.run(
        ["bash", "-c", script.replace(f'{var}=""', f"{var}=x")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ok.returncode == 0 and "REACHED" in ok.stdout, ok


def test_the_gateway_arm_clones_the_repo_with_the_scratch_secret_and_pins_the_sha():
    branch = _gateway_branch()
    assert "GIT_SECRET_ARN" in branch and "jq -r '.token" in branch
    assert "GIT_BASE_URL" in branch and "$GIT_AGENT_REPO" in branch
    assert 'fetch --depth 1 origin "$IMAGE_TAG"' in branch
    assert "checkout --detach FETCH_HEAD" in branch


def test_the_gateway_arm_requires_gateway_yaml():
    lines = _gateway_code_lines()
    assert "GW_CFG=/tmp/agent/gateway.yaml" in lines
    (guard,) = [line for line in lines if line.startswith('[ -f "$GW_CFG" ]')]
    assert "_st failed" in guard and "_dep failed" in guard and "exit 1" in guard, guard


_REALPATH_SHIM = (
    'if ! realpath -e / >/dev/null 2>&1; then realpath() { [ "$1" = "-e" ] && shift; '
    "python3 -c 'import os,sys; p=os.path.realpath(sys.argv[1]); "
    'sys.exit(0 if os.path.exists(p) and print(p) is None else 1)\' "$1"; }; fi\n'
)


def _gateway_lines_between(first: str, last: str) -> list[str]:
    lines = _gateway_code_lines()
    start = next(i for i, line in enumerate(lines) if line.startswith(first))
    end = next(i for i, line in enumerate(lines) if line.startswith(last))
    return lines[start : end + 1]


def _gateway_cfg_lines() -> list[str]:
    """`GW_CFG=` through its resolved-path containment check (which defines `_in_clone`)."""
    return _gateway_lines_between("GW_CFG=/tmp/agent/gateway.yaml", "GW_CFG=$(_in_clone")


def _gateway_spec_lines() -> list[str]:
    """`SPEC_REL=` through the spec's resolved-path containment check."""
    return _gateway_lines_between("SPEC_REL=", "SPEC_PATH=$(_in_clone")


def _probe_clone(tmp_path):
    """A fake clone with a real spec, a nested spec, a `.git/config` holding a token, and a file
    OUTSIDE the clone — the symlink targets the containment check must refuse."""
    root = tmp_path.resolve()
    agent = root / "agent"
    (agent / "specs").mkdir(parents=True)
    (agent / ".git").mkdir()
    (agent / ".git/config").write_text("url = https://x-access-token:SECRET@host/o/r.git\n")
    (agent / "openapi.yaml").write_text("openapi: 3.0.0\n")
    (agent / "specs/api.json").write_text("{}")
    (root / "outside.yaml").write_text("openapi: 3.0.0\n")
    (agent / "to-git.yaml").symlink_to(agent / ".git/config")
    (agent / "to-outside.yaml").symlink_to(root / "outside.yaml")
    (agent / "to-inside.yaml").symlink_to(agent / "openapi.yaml")
    return agent


def _run_gateway_probe(agent, body: str, tail: str):
    """Run `body` then `tail` against the fake clone with `_st`/`_dep` echoing.

    `_REALPATH_SHIM` gives a host whose realpath lacks GNU's `-e` (macOS's BSD realpath) the same
    resolve-every-link, fail-on-missing semantics the CodeBuild image (Amazon Linux) runs; the
    buildspec lines themselves run verbatim."""
    import subprocess

    script = (
        '_st() { echo "ST:$1"; }\n_dep() { echo "DEP:$1"; }\n'
        + _REALPATH_SHIM
        + body.replace(AGENT_CLONE, str(agent))
        + "\n"
        + tail
        + "\n"
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize(
    "yaml_line,expect",
    [
        ("openapi_spec: openapi.yaml", "OK:openapi.yaml"),
        ('openapi_spec: "specs/api.json"   # the API', "OK:specs/api.json"),
        ("openapi_spec: to-inside.yaml", "OK:openapi.yaml"),
        ("openapi_spec: '/etc/passwd'", "REFUSED"),
        ("openapi_spec: ../other/openapi.yaml", "REFUSED"),
        ("openapi_spec: missing.yaml", "REFUSED"),
        ("openapi_spec: to-git.yaml", "REFUSED"),
        ("openapi_spec: to-outside.yaml", "REFUSED"),
        ("openapi_spec: .git/config", "REFUSED"),
        ("# no key", "REFUSED"),
    ],
    ids=[
        "plain",
        "quoted-commented",
        "symlink-inside",
        "absolute",
        "traversal",
        "file-missing",
        "symlink-into-git",
        "symlink-outside",
        "git-dir",
        "no-key",
    ],
)
def test_the_openapi_spec_path_resolves_inside_the_clone_ACTUALLY(tmp_path, yaml_line, expect):
    """EXECUTED: `openapi_spec` resolves relative to the clone root, and by its REAL path. An
    absolute or `..` spelling, a missing file, and a SYMLINK into `.git/` (the clone token lives in
    `.git/config`) or out of the clone are recorded refusals — the module inlines this file into a
    gateway target that every tool caller can read."""
    agent = _probe_clone(tmp_path)
    (agent / "gateway.yaml").write_text(f"description: x\n{yaml_line}\napi_key:\n  json_key: api_key\n")
    body = "\n".join(_gateway_cfg_lines() + _gateway_spec_lines())
    out = _run_gateway_probe(agent, body, f'echo "OK:${{SPEC_PATH#{agent}/}}"')
    if expect == "REFUSED":
        assert out.returncode == 1 and "ST:failed" in out.stdout and "DEP:failed" in out.stdout, out
        assert "OK:" not in out.stdout, out
    else:
        assert out.returncode == 0 and expect in out.stdout, out


@pytest.mark.parametrize(
    "link_to,expect_ok",
    [(None, True), ("to-inside", True), ("git", False), ("outside", False)],
    ids=["regular-file", "symlink-inside", "symlink-into-git", "symlink-outside"],
)
def test_gateway_yaml_itself_must_resolve_inside_the_clone_ACTUALLY(tmp_path, link_to, expect_ok):
    """EXECUTED: the same resolved-path containment for gateway.yaml, which the module reads too."""
    agent = _probe_clone(tmp_path)
    cfg = agent / "gateway.yaml"
    if link_to is None:
        cfg.write_text("openapi_spec: openapi.yaml\n")
    else:
        (agent / "real-gateway.yaml").write_text("openapi_spec: openapi.yaml\n")
        target = {
            "to-inside": agent / "real-gateway.yaml",
            "git": agent / ".git/config",
            "outside": agent.parent / "outside.yaml",
        }[link_to]
        cfg.symlink_to(target)
    out = _run_gateway_probe(agent, "\n".join(_gateway_cfg_lines()), 'echo "OK:$GW_CFG"')
    if expect_ok:
        assert out.returncode == 0 and f"OK:{agent}/" in out.stdout, out
        assert "/.git/" not in out.stdout, out
    else:
        assert out.returncode == 1 and "ST:failed" in out.stdout and "OK:" not in out.stdout, out


def test_the_containment_check_uses_realpath_e_under_the_clone_and_not_git():
    (definition,) = [line for line in _gateway_code_lines() if line.startswith("_in_clone()")]
    assert 'realpath -e "$1"' in definition, definition
    assert '"${R#/tmp/agent/}" != "$R"' in definition, definition
    assert '"${R#/tmp/agent/.git/}" = "$R"' in definition, definition


def _gateway_tfvars() -> list[str]:
    body = _gateway_branch().split("<<TFVARS", 1)[1].split("TFVARS", 1)[0]
    return [line.strip() for line in body.splitlines() if line.strip()]


def test_the_gateway_tfvars_are_exactly_the_module_variables():
    lines = _gateway_tfvars()
    assert {line.split("=", 1)[0] for line in lines} == {
        "mcp_server_id",
        "mcp_name",
        "stage",
        "region",
        "deploy_role_arn",
        "tenant_id",
        "entra_app_audience",
        "entra_app_id",
        "gateway_config_path",
        "openapi_spec_path",
    }, lines
    for expected in (
        'mcp_server_id="$MCP_SERVER_ID"',
        'mcp_name="$MCP_NAME"',
        'stage="$STAGE"',
        'region="$AWS_TARGET_REGION"',
        'deploy_role_arn="$TARGET_ROLE_ARN"',
        'tenant_id="$ENTRA_TENANT_ID"',
        'entra_app_audience="$ENTRA_APP_AUDIENCE"',
        'entra_app_id="$ENTRA_APP_ID"',
        'gateway_config_path="$GW_CFG"',
        'openapi_spec_path="$SPEC_PATH"',
    ):
        assert expected in lines, (expected, lines)


def test_the_gateway_state_key_is_stage_scoped_under_the_mcp_id():
    assert "key=agentcore-gateway/$MCP_SERVER_ID/$STAGE/terraform.tfstate" in _gateway_branch()


def _gateway_drift_guard_line() -> str:
    (line,) = [raw for raw in _gateway_code_lines() if "gateway_config_path" in raw and "grep -qE" in raw]
    return line


@pytest.mark.parametrize(
    "root_tf,expect_exit",
    [
        ('variable "gateway_config_path" {}\n', 0),
        ("# gateway_config_path comes later\n", 1),
        ('variable "harness_config_path" {}\n', 1),
        ("", 1),
    ],
    ids=["gateway-module", "comment-only", "wrong-module", "empty-root"],
)
def test_the_gateway_drift_guard_actually_fires(tmp_path, root_tf, expect_exit):
    """EXECUTED: a cloned infra repo without the gateway module's sentinel variable is refused
    (a tfvar with no `variable` block is only a WARNING to terraform, and silently dropped)."""
    import subprocess

    (tmp_path / "main.tf").write_text(root_tf)
    script = (
        '_st() { echo "ST:$1"; }\n_dep() { echo "DEP:$1"; }\n'
        f"cd {tmp_path}\n{_gateway_drift_guard_line()}\necho REACHED_TERRAFORM\n"
    )
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == expect_exit, out
    assert ("REACHED_TERRAFORM" in out.stdout) == (expect_exit == 0), out.stdout


def test_the_gateway_drift_guard_runs_before_terraform_init():
    lines = _gateway_code_lines()
    guard = lines.index(_gateway_drift_guard_line())
    init = next(i for i, line in enumerate(lines) if line.startswith("terraform init"))
    assert guard < init


def test_the_gateway_apply_is_guarded():
    branch = _gateway_branch()
    assert "terraform plan -input=false -out=tfplan && terraform apply" in branch
    assert 'if [ "${GW_FAIL:-0}" = "1" ]; then _st failed; _dep failed; exit 1; fi' in branch


def _gateway_write_line() -> str:
    (line,) = [raw for raw in _gateway_code_lines() if "SET gateway_arn" in raw]
    return line


def test_the_gateway_write_back_is_ONE_update_item_SETting_all_three():
    """The repo row is the ONLY record of the gateway, so the write is a single
    item write (no arn-without-url partial) and is NOT `_u` (best-effort would leave a live,
    untracked gateway behind a green build). Ordered after the non-empty guard, before success."""
    lines = _gateway_code_lines()
    for attr, var in (("gateway_arn", "GW_ARN"), ("gateway_id", "GW_ID"), ("gateway_url", "GW_URL")):
        assert f"{var}=$(terraform output -raw {attr} 2>/dev/null || true)" in lines, attr
        assert f"_u {attr}" not in _gateway_branch(), attr
    line = _gateway_write_line()
    assert line.startswith("aws dynamodb update-item"), line
    assert '--update-expression "SET gateway_arn = :a, gateway_id = :i, gateway_url = :u"' in line
    assert '\\"sk\\":{\\"S\\":\\"$REPO_SK\\"}' in line, line
    for var in ("$GW_ARN", "$GW_ID", "$GW_URL"):
        assert var in line, var
    assert "2>/dev/null" not in line, "a failed write-back must stay visible in the log"
    assert _gateway_branch().count("aws dynamodb update-item") == 1
    (guard,) = [raw for raw in lines if raw.startswith('if [ -z "$GW_ARN" ]')]
    assert '[ -z "$GW_ID" ]' in guard and '[ -z "$GW_URL" ]' in guard, guard
    assert "_st failed" in guard and "_dep failed" in guard and "exit 1" in guard, guard
    success = lines.index("_tag; _st deployed; _dep succeeded")
    assert lines.index(guard) < lines.index(line) < success
    assert "python3 /tmp/merge_outputs.py /tmp/tf_outputs.json" in lines


@pytest.mark.parametrize("aws_rc,expect_exit", [(0, 0), (1, 1)], ids=["write-ok", "write-fails"])
def test_the_gateway_write_back_FAILS_HARD_and_RECORDED_ACTUALLY(aws_rc, expect_exit):
    """EXECUTED with a stubbed `aws`: the write gets one well-formed item key + values, and a
    failed write records `_st failed; _dep failed` and exits 1 instead of reaching success."""
    import json
    import subprocess

    script = (
        '_st() { echo "ST:$1"; }\n_dep() { echo "DEP:$1"; }\n'
        f'aws() {{ printf "%s\\n" "$@" > "$ARGS_OUT"; return {aws_rc}; }}\n'
        "PROJECTS_TABLE_NAME=t\nREPO_SK=repo-1\n"
        "GW_ARN=arn:aws:bedrock-agentcore:eu-central-1:123456789012:gateway/gw-1\n"
        "GW_ID=gw-1\nGW_URL=https://gw-1.example.com/mcp\n"
        f"{_gateway_write_line()}\necho REACHED_SUCCESS\n"
    )
    import tempfile

    with tempfile.NamedTemporaryFile() as args_out:
        out = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            env={"PATH": "/usr/bin:/bin", "ARGS_OUT": args_out.name},
        )
        args = open(args_out.name).read().splitlines()
    assert out.returncode == expect_exit, out
    assert ("REACHED_SUCCESS" in out.stdout) == (expect_exit == 0), out.stdout
    if expect_exit:
        assert "ST:failed" in out.stdout and "DEP:failed" in out.stdout and "UNTRACKED" in out.stdout
    key = json.loads(args[args.index("--key") + 1])
    assert key == {"pk": {"S": "repository"}, "sk": {"S": "repo-1"}}
    values = json.loads(args[args.index("--expression-attribute-values") + 1])
    assert values == {
        ":a": {"S": "arn:aws:bedrock-agentcore:eu-central-1:123456789012:gateway/gw-1"},
        ":i": {"S": "gw-1"},
        ":u": {"S": "https://gw-1.example.com/mcp"},
    }


def test_the_gateway_branch_has_no_image_leg():
    branch = _gateway_branch()
    for absent in ("docker", "IMAGE_DIGEST", "AGENT_ID"):
        assert absent not in branch, absent


def _repo_sk_probe(tmp_path, env: str) -> str:
    """Run the real REPO_SK line with `aws` stubbed and return the resolved value; an env `REPO_SK`
    short-circuits the `agent_id-index` lookup. The env path also logs which source it used, so the
    probe returns the last line."""
    import subprocess

    script = tmp_path / "probe.sh"
    script.write_text(
        "aws() { echo QUERIED-sk; }\n"
        "PROJECTS_TABLE_NAME=t\n"
        f"{env}\n"
        f"{_repo_sk_query_line()}\n"
        'printf "[%s]" "$REPO_SK"\n'
    )
    out = subprocess.run(["bash", str(script)], capture_output=True, text=True, check=True).stdout
    return out.splitlines()[-1]


def test_an_env_REPO_SK_short_circuits_the_gsi_query(tmp_path):
    """Env REPO_SK wins even when an AGENT_ID is present."""
    assert _repo_sk_probe(tmp_path, "REPO_SK=repo-1\nAGENT_ID=") == "[repo-1]"
    assert _repo_sk_probe(tmp_path, "REPO_SK=repo-1\nAGENT_ID=ag") == "[repo-1]"


def test_the_agent_path_still_resolves_REPO_SK_from_the_gsi(tmp_path):
    """No agent and no env REPO_SK still resolves to empty (the legacy terraform path)."""
    assert _repo_sk_probe(tmp_path, "AGENT_ID=ag") == "[QUERIED-sk]"
    assert _repo_sk_probe(tmp_path, "AGENT_ID=") == "[]"


def test_the_repo_sk_line_is_valid_bash():
    import subprocess

    out = subprocess.run(["bash", "-n"], input=_repo_sk_query_line(), capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_dep_reads_both_artifact_ids_with_get():
    """dep.py tolerates a missing AGENT_ID: both artifact ids are read with `.get`."""
    _, script = _dep_script_body()
    assert "os.environ['AGENT_ID']" not in script
    assert "os.environ.get('AGENT_ID')" in script and "os.environ.get('MCP_SERVER_ID')" in script


@pytest.mark.parametrize(
    "env,present,absent",
    [
        ({"AGENT_ID": "", "MCP_SERVER_ID": "mcp-1"}, {"mcp_server_id": "mcp-1"}, "agent_id"),
        ({"AGENT_ID": "ag-1", "MCP_SERVER_ID": ""}, {"agent_id": "ag-1"}, "mcp_server_id"),
        ({"MCP_SERVER_ID": "mcp-1"}, {"mcp_server_id": "mcp-1"}, "agent_id"),
    ],
    ids=["gateway", "agent", "agent-id-unset"],
)
def test_dep_script_writes_the_artifact_id_it_has_and_omits_the_other(tmp_path, env, present, absent):
    """EXECUTED: the script body with no AGENT_ID writes a row instead of dying on KeyError."""
    import json
    import os
    import subprocess
    import sys

    _, body = _dep_script_body()
    (tmp_path / "dep.py").write_text(body)
    run_env = {
        "PATH": os.environ["PATH"],
        "REPO_SK": "r-1",
        "STAGE": "dev",
        "D_OUT": "succeeded",
        "IMAGE_TAG": "0123456789abcdef0123456789abcdef01234567",
        **env,
    }
    item_path = tmp_path / "item.json"
    subprocess.run([sys.executable, str(tmp_path / "dep.py"), str(item_path)], env=run_env, check=True)
    item = json.loads(item_path.read_text())
    for k, v in present.items():
        assert item[k] == {"S": v}, item
    assert absent not in item, item


_GW_ARN = f"arn:aws:bedrock-agentcore:{_REGION}:{_ACCOUNT}:gateway/agp-claims-dev-abc123"
_OBO_ARN = (
    f"arn:aws:bedrock-agentcore:{_REGION}:{_ACCOUNT}:token-vault/default/"
    "oauth2credentialprovider/agp-agent-obo-a1"
)


def _tool_auth_line() -> str:
    """The harness arm's `jq -n` line that writes `tool_auth.auto.tfvars.json`.

    A harness agent build receives `GATEWAY_TOOL_AUTH` (compact JSON {gateway_arn: scope}) and
    `OBO_PROVIDER_ARN` (the agent's MicrosoftOauth2 provider). The harness arm hands both to the
    module as a `*.auto.tfvars.json` written by jq (never interpolated into HCL), and the module
    gives every `tools` ARN found in the map an `outbound_auth.oauth` TOKEN_EXCHANGE block.
    Unmapped ARNs, or an empty provider ARN, render exactly as before."""
    (line,) = [
        raw.strip()
        for raw in _harness_branch().splitlines()
        if raw.strip().startswith("jq -n ") and "tool_auth.auto.tfvars.json" in raw
    ]
    return line


def test_the_harness_arm_writes_the_tool_auth_tfvars_as_JSON_with_jq():
    """JSON-safe by construction: the env values reach jq as `--arg` strings, never through the
    shell into HCL, and the refusal is RECORDED. Written before any drift guard and `terraform init`.
    The HCL heredoc does not carry them: an unquoted JSON blob there is the injection path."""
    line = _tool_auth_line()
    assert line.startswith("jq -n "), line
    assert '--arg m "${GATEWAY_TOOL_AUTH:-}"' in line and '--arg obo "${OBO_PROVIDER_ARN:-}"' in line, line
    assert "_st failed" in line and "_dep failed" in line and "exit 1" in line, line
    branch = _harness_branch()
    assert branch.index(line) < branch.index("terraform init")
    assert branch.index(line) < branch.index('variable[[:space:]]+"gateway_tool_auth"')
    assert not any("gateway_tool_auth" in raw or "obo_provider_arn" in raw for raw in _harness_tfvars())


_HARNESS_YAML_WITH_TOOLS = f"tools:\n  - {_GW_ARN}\n  - 'a\"$(touch PWNED)`id`'\n"


def _run_tool_auth(tmp_path, env: dict, harness_yaml: str = _HARNESS_YAML_WITH_TOOLS):
    """Run the tool-auth line in a scratch dir holding `harness_yaml`.

    The default, `_HARNESS_YAML_WITH_TOOLS`, is the cloned harness.yaml the line narrows the map
    against: it names every key the round-trip cases expect to survive, including the metacharacter
    one (data, never expanded)."""
    import os
    import subprocess

    (tmp_path / "harness.yaml").write_text(harness_yaml)
    script = (
        '_st() { echo "ST:$1"; }\n_dep() { echo "DEP:$1"; }\n'
        f"cd {tmp_path}\n" + _tool_auth_line() + "\necho REACHED_TERRAFORM\n"
    )
    run_env = {"PATH": os.environ["PATH"], "HARNESS_CFG": str(tmp_path / "harness.yaml"), **env}
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60, env=run_env)


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, {"gateway_tool_auth": {}, "obo_provider_arn": ""}),
        (
            {"GATEWAY_TOOL_AUTH": "", "OBO_PROVIDER_ARN": ""},
            {"gateway_tool_auth": {}, "obo_provider_arn": ""},
        ),
        (
            {"GATEWAY_TOOL_AUTH": "{}", "OBO_PROVIDER_ARN": _OBO_ARN},
            {"gateway_tool_auth": {}, "obo_provider_arn": _OBO_ARN},
        ),
        (
            {
                "GATEWAY_TOOL_AUTH": f'{{"{_GW_ARN}":"api://agp-mcp-m1/.default"}}',
                "OBO_PROVIDER_ARN": _OBO_ARN,
            },
            {"gateway_tool_auth": {_GW_ARN: "api://agp-mcp-m1/.default"}, "obo_provider_arn": _OBO_ARN},
        ),
        (
            {
                "GATEWAY_TOOL_AUTH": '{"a\\"$(touch PWNED)`id`":"x\'; rm -rf /"}',
                "OBO_PROVIDER_ARN": '$(touch PWNED)"',
            },
            {
                "gateway_tool_auth": {'a"$(touch PWNED)`id`': "x'; rm -rf /"},
                "obo_provider_arn": '$(touch PWNED)"',
            },
        ),
    ],
    ids=["unset", "empty", "empty-object", "mapped", "metacharacters"],
)
def test_the_tool_auth_tfvars_ACTUALLY_round_trip_the_env(tmp_path, env, expected):
    """Shell metacharacters stay data: nothing is expanded, nothing breaks the file."""
    import json

    out = _run_tool_auth(tmp_path, env)
    assert out.returncode == 0 and "REACHED_TERRAFORM" in out.stdout, (out.stdout, out.stderr)
    assert json.loads((tmp_path / "tool_auth.auto.tfvars.json").read_text()) == expected
    assert not (tmp_path / "PWNED").exists()


@pytest.mark.parametrize(
    "value",
    ["not json", "[1,2]", '{"arn":1}', '"a string"'],
    ids=["malformed", "array", "non-string-scope", "scalar"],
)
def test_a_malformed_GATEWAY_TOOL_AUTH_FAILS_RECORDED(tmp_path, value):
    out = _run_tool_auth(tmp_path, {"GATEWAY_TOOL_AUTH": value, "OBO_PROVIDER_ARN": _OBO_ARN})
    assert out.returncode == 1, (out.stdout, out.stderr)
    assert "REACHED_TERRAFORM" not in out.stdout and "ST:failed" in out.stdout and "DEP:failed" in out.stdout


_OTHER_GW_ARN = f"arn:aws:bedrock-agentcore:{_REGION}:{_ACCOUNT}:gateway/agp-other-dev-zzz999"


@pytest.mark.parametrize(
    "harness_yaml,expected_map",
    [
        ("model_id: m\ntools: []\n", {}),
        (f"tools:\n  - {_GW_ARN}\n", {_GW_ARN: "s1"}),
        (f"tools: [{_GW_ARN}, {_OTHER_GW_ARN}]\n", {_GW_ARN: "s1", _OTHER_GW_ARN: "s2"}),
    ],
    ids=["no-tools", "one-of-two", "both-flow-style"],
)
def test_the_tool_auth_map_is_narrowed_to_THIS_harness_yaml_tools(tmp_path, harness_yaml, expected_map):
    """GATEWAY_TOOL_AUTH covers every governed gateway in the tenant; only the ARNs this harness.yaml
    names reach the tfvars."""
    import json

    env = {
        "GATEWAY_TOOL_AUTH": json.dumps({_GW_ARN: "s1", _OTHER_GW_ARN: "s2"}),
        "OBO_PROVIDER_ARN": _OBO_ARN,
    }
    out = _run_tool_auth(tmp_path, env, harness_yaml)
    assert out.returncode == 0 and "REACHED_TERRAFORM" in out.stdout, (out.stdout, out.stderr)
    assert json.loads((tmp_path / "tool_auth.auto.tfvars.json").read_text()) == {
        "gateway_tool_auth": expected_map,
        "obo_provider_arn": _OBO_ARN,
    }


def test_the_tool_auth_tfvars_name_only_DECLARED_module_variables():
    """The silent-drop guard for the JSON file: both keys must be real variables WITH defaults
    (an older backend that sends neither must still plan)."""
    variables_tf = (_HARNESS_MODULE / "variables.tf").read_text()
    for name, default in (("gateway_tool_auth", "{}"), ("obo_provider_arn", '""')):
        block = variables_tf.split(f'variable "{name}"', 1)[1].split("\nvariable ", 1)[0]
        assert re.search(rf"^\s*default\s*=\s*{re.escape(default)}\s*$", block, re.MULTILINE), block
    assert re.search(
        r"type\s*=\s*map\(string\)",
        variables_tf.split('variable "gateway_tool_auth"', 1)[1].split("\nvariable ", 1)[0],
    )


def _tool_auth_guard_line() -> str:
    (line,) = [raw for raw in _harness_drift_guard_lines() if '"gateway_tool_auth"' in raw]
    return line


@pytest.mark.parametrize(
    "root_tf,env,expect_exit",
    [
        (
            'variable "harness_config_path" {}\nvariable "tenant_id" {}\nvariable "gateway_tool_auth" {}\n',
            {"GATEWAY_TOOL_AUTH": f'{{"{_GW_ARN}":"s"}}', "OBO_PROVIDER_ARN": _OBO_ARN},
            0,
        ),
        (
            'variable "harness_config_path" {}\nvariable "tenant_id" {}\n',
            {"GATEWAY_TOOL_AUTH": f'{{"{_GW_ARN}":"s"}}', "OBO_PROVIDER_ARN": _OBO_ARN},
            1,
        ),
        (
            'variable "harness_config_path" {}\nvariable "tenant_id" {}\n# gateway_tool_auth\n',
            {"GATEWAY_TOOL_AUTH": f'{{"{_GW_ARN}":"s"}}', "OBO_PROVIDER_ARN": _OBO_ARN},
            1,
        ),
        (
            'variable "harness_config_path" {}\nvariable "tenant_id" {}\n',
            {"GATEWAY_TOOL_AUTH": "{}", "OBO_PROVIDER_ARN": _OBO_ARN},
            0,
        ),
        (
            'variable "harness_config_path" {}\nvariable "tenant_id" {}\n',
            {"GATEWAY_TOOL_AUTH": f'{{"{_GW_ARN}":"s"}}', "OBO_PROVIDER_ARN": ""},
            0,
        ),
        ('variable "harness_config_path" {}\nvariable "tenant_id" {}\n', {}, 0),
        (
            'variable "harness_config_path" {}\nvariable "tenant_id" {}\n',
            {"GATEWAY_TOOL_AUTH": f'{{"{_OTHER_GW_ARN}":"s"}}', "OBO_PROVIDER_ARN": _OBO_ARN},
            0,
        ),
    ],
    ids=[
        "declared",
        "undeclared-with-map",
        "comment-only",
        "undeclared-empty-map",
        "undeclared-no-provider",
        "undeclared-unset",
        "undeclared-unrelated-gateway",
    ],
)
def test_the_tool_auth_drift_guard_ACTUALLY_fires_only_when_values_would_be_discarded(
    tmp_path, root_tf, env, expect_exit
):
    """The guard fires only when a value would be discarded: with nothing to discard (an empty map, no
    provider, unset, or a governed gateway elsewhere in the tenant that this harness lists none of)
    an older module without `gateway_tool_auth` still builds."""
    import os
    import subprocess

    (tmp_path / "main.tf").write_text(root_tf)
    (tmp_path / "harness.yaml").write_text(f"tools:\n  - {_GW_ARN}\n")
    script = (
        '_st() { echo "ST:$1"; }\n_dep() { echo "DEP:$1"; }\n'
        f"cd {tmp_path}\n"
        + _tool_auth_line()
        + "\n"
        + "\n".join(_harness_drift_guard_lines())
        + "\necho REACHED_TERRAFORM\n"
    )
    run_env = {"PATH": os.environ["PATH"], "HARNESS_CFG": str(tmp_path / "harness.yaml"), **env}
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60, env=run_env)
    assert out.returncode == expect_exit, (out.stdout, out.stderr)
    if expect_exit:
        assert "STALE HARNESS MODULE" in out.stdout and "ST:failed" in out.stdout, out.stdout
        assert "REACHED_TERRAFORM" not in out.stdout
    else:
        assert "REACHED_TERRAFORM" in out.stdout


def test_the_tool_auth_drift_guard_is_recorded_and_precedes_init():
    line = _tool_auth_guard_line()
    assert "_st failed" in line and "_dep failed" in line and "exit 1" in line, line
    assert _harness_branch().index(line) < _harness_branch().index("terraform init")


def _harness_main_code() -> str:
    return "\n".join(
        line
        for line in (_HARNESS_MODULE / "main.tf").read_text().splitlines()
        if not line.strip().startswith("#")
    )


def test_the_harness_module_renders_TOKEN_EXCHANGE_outbound_auth_for_mapped_tools_only():
    """The API/provider enum is lower-case (botocore HarnessToolType), upper-case fails at plan; and
    the empty-provider gate holds: no provider, no map, no outbound_auth anywhere."""
    code = _harness_main_code()
    tool_block = code.split('dynamic "tool" {', 1)[1].split("authorizer_configuration {", 1)[0]
    assert 'dynamic "outbound_auth" {' in tool_block, tool_block
    assert 'type = "agentcore_gateway"' in tool_block, tool_block
    auth = tool_block.split('dynamic "outbound_auth" {', 1)[1]
    assert "local.tool_auth_scopes" in auth.split("content", 1)[0], auth
    assert "provider_arn = var.obo_provider_arn" in auth
    assert "scopes       = [outbound_auth.value]" in auth
    assert 'grant_type   = "TOKEN_EXCHANGE"' in auth
    assert re.search(
        r'tool_auth_scopes\s*=\s*var\.obo_provider_arn\s*==\s*""\s*\?\s*\{\}\s*:\s*var\.gateway_tool_auth',
        code,
    ), code


def test_the_harness_exec_role_carries_the_SPIKED_token_vault_grants_scoped_not_wildcarded():
    """GetResourceOauth2Token on the four vault/directory ARNs and GetSecretValue on the provider's
    vault secret, and NOT GetWorkloadAccessToken (never needed). The provider ARN is passed verbatim
    (unquoted `var.obo_provider_arn`); the rest are patterns. Gated: a harness with no TOKEN_EXCHANGE
    tool gets none of it."""
    code = _harness_main_code()
    assert "GetWorkloadAccessToken" not in code
    (stmt,) = re.findall(
        r'Action\s*=\s*\["bedrock-agentcore:GetResourceOauth2Token"\]\s*\n\s*Resource\s*=\s*\[([^\]]*)\]',
        code,
    )
    (secret,) = re.findall(
        r'Action\s*=\s*\["secretsmanager:GetSecretValue"\]\s*\n\s*Resource\s*=\s*\["([^"]+)"\]', code
    )

    def sub(p: str) -> str:
        return (
            p.replace("${local.harness_name}", "my_agent_dev")
            .replace("${local.obo_provider_name}", "agp-agent-obo-a1")
            .replace("${var.obo_provider_arn}", _OBO_ARN)
        )

    patterns = [sub(p) for p in re.findall(r'"([^"]+)"', stmt)]
    if "var.obo_provider_arn" in stmt:
        patterns.append(_OBO_ARN)
    directory = f"arn:aws:bedrock-agentcore:{_REGION}:{_ACCOUNT}:workload-identity-directory/default"
    for arn in (
        f"arn:aws:bedrock-agentcore:{_REGION}:{_ACCOUNT}:token-vault/default",
        _OBO_ARN,
        directory,
        f"{directory}/workload-identity/harness_my_agent_dev-AbC123",
    ):
        assert any(_matches_iam_pattern(p, arn) for p in patterns), (arn, patterns)
    other = f"{directory}/workload-identity/other"
    assert not any(_matches_iam_pattern(p, other) for p in patterns)
    assert "*" not in patterns
    own_secret = (
        f"arn:aws:secretsmanager:{_REGION}:{_ACCOUNT}:secret:"
        "bedrock-agentcore-identity!default/oauth2/agp-agent-obo-a1-Xy12Ab"
    )
    assert _matches_iam_pattern(sub(secret), own_secret), secret
    assert re.search(r"length\(local\.oauth_tool_arns\)\s*>\s*0\s*\?", code)
    assert not re.search(r"\b\d{12}\b", code)


def _reapproval_helpers() -> str:
    """`_reg_get` / `_reg_status` / `_reapprove` as CodeBuild runs them (shared build scope).

    Any UpdateRegistryRecord resets APPROVED -> DRAFT and delists the marketplace card. AGP
    re-asserts APPROVED after its own writes (services/registry_writer.py); the build's ARN
    write-back is one of them. Guarded on the settled status read just before the write, the
    re-assert compares content (never updatedAt) and never fails the build."""
    for phase, cmd in _runtime_commands():
        if "_reapprove() {" in cmd:
            assert phase == "build", phase
            start = cmd.index("_reg_get() {")
            end = cmd.index("\n}\n", cmd.index("_reapprove() {")) + 3
            return cmd[start:end]
    raise AssertionError("no _reapprove helper in the buildspec")


def _reapprove_shape(block: list[str]) -> None:
    write_i = next(i for i, line in enumerate(block) if "update-registry-record " in line)
    prior = [i for i, line in enumerate(block) if line == "_reg_status"]
    after = [i for i, line in enumerate(block) if line == '_reapprove_after "$NEW_ENV"']
    assert len(prior) == 1 and prior[0] < write_i, block
    assert len(after) == 1 and write_i < after[0] < next(
        i for i, line in enumerate(block) if line.startswith("_tag; _st deployed")
    ), block


def test_both_write_backs_are_followed_by_a_re_approve_GUARDED_on_the_prior_APPROVED():
    _reapprove_shape(_registry_write_block())
    _reapprove_shape(_harness_write_block())


def test_the_re_approve_helpers_never_fail_the_build():
    """The runtime is live and its ARN recorded, so a failed re-approve is a WARNING, not a red build;
    the compare is on content only, never `updatedAt` (the live settle trap)."""
    helpers = _reapproval_helpers()
    assert "exit" not in helpers, helpers
    assert "_st failed" not in helpers and "_dep failed" not in helpers, helpers
    for line in [
        raw for raw in helpers.splitlines() if re.search(r"aws\s+agent-registry-control\s+[a-z]", raw)
    ]:
        assert "aws agent-registry-control " in line, line
    assert "updatedAt" not in helpers


_FAKE_REGISTRY = r"""
sleep() { :; }
_st() { echo "ST:$1"; }
_dep() { echo "DEP:$1"; }
_tag() { echo TAGGED; }
aws() {
  echo "CALL:$2" >> "$W/log"
  if [ "$2" = "get-registry-record" ]; then
    [ -n "$GET_FAILS" ] && return 254
    if [ -n "$ALWAYS_UPDATING" ] || [ -f "$W/settling" ]; then
      rm -f "$W/settling"; printf '{"status":"UPDATING"}'; return 0
    fi
    printf '{"status":"%s","statusReason":%s,"descriptors":{"custom":{"data":%s}}}' \
      "$(cat "$W/status")" "$(jq -Rs . < "$W/reason")" "$(jq -Rs . < "$W/data")"
  elif [ "$2" = "update-registry-record" ]; then
    while [ $# -gt 0 ]; do
      [ "$1" = "--descriptors" ] && printf "%s" "$2" \
        | jq -r '.optionalValue.custom.optionalValue.data.optionalValue' > "$W/data"
      shift
    done
    echo DRAFT > "$W/status"; touch "$W/settling"
    [ -n "$OTHER_WRITE" ] && echo '{"model_id":"edited-by-a-user"}' > "$W/data"
    [ -n "$OTHER_APPROVES" ] && echo '{"model_id":"edited-by-a-user"}' > "$W/data" \
      && echo APPROVED > "$W/status"
    return 0
  elif [ "$2" = "submit-registry-record-for-approval" ]; then
    [ -n "$SUBMIT_FAILS" ] && return 254
    echo PENDING_APPROVAL > "$W/status"; touch "$W/settling"
  elif [ "$2" = "update-registry-record-status" ]; then
    while [ $# -gt 0 ]; do [ "$1" = "--status-reason" ] && printf "%s" "$2" > "$W/sent_reason"; shift; done
    echo APPROVED > "$W/status"; touch "$W/settling"
  fi
}
"""


@pytest.mark.parametrize("arm", ["runtime", "harness"])
@pytest.mark.parametrize(
    "prior,env,expect_status,expect_warn,expect_reapprove_calls",
    [
        ("APPROVED", "", "APPROVED", False, True),
        ("DRAFT", "", "DRAFT", False, False),
        ("PENDING_APPROVAL", "", "DRAFT", False, False),
        ("APPROVED", "SUBMIT_FAILS=1", "DRAFT", True, True),
        ("APPROVED", "OTHER_WRITE=1", "DRAFT", True, False),
    ],
    ids=[
        "approved-is-re-approved",
        "draft-stays",
        "pending-not-touched",
        "submit-fails-warns",
        "other-write-left",
    ],
)
def test_the_write_back_re_approve_ACTUALLY_runs(
    tmp_path, arm, prior, env, expect_status, expect_warn, expect_reapprove_calls
):
    """EXECUTES both write-back blocks against `_FAKE_REGISTRY`, a stateful fake: GET returns
    {status, data}; a write resets to DRAFT; submit moves DRAFT -> PENDING_APPROVAL; update-status
    moves PENDING_APPROVAL -> APPROVED; the first GET after each mutation returns UPDATING (the
    async settle)."""
    import subprocess

    (tmp_path / "status").write_text(prior + "\n")
    (tmp_path / "data").write_text('{"model_id":"m"}')
    (tmp_path / "reason").write_text("")
    block = _registry_write_block() if arm == "runtime" else _harness_write_block()
    arn_var = "RUNTIME_ARN" if arm == "runtime" else "HARNESS_ARN"
    script = (
        f"W={tmp_path}\n{env}\n"
        + _FAKE_REGISTRY
        + _reapproval_helpers()
        + "\nAGENT_REGISTRY_ID=reg\nAGENT_ID=ag-1\nAGENT_NAME=my_agent\nSTAGE=dev\n"
        + f"{arn_var}=arn:aws:bedrock-agentcore:eu-central-1:1:runtime/r-1\n"
        + 'ENV=\'{"model_id":"m"}\'\n'
        + "\n".join(block)
        + "\n"
    )
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"stdout={out.stdout!r} stderr={out.stderr!r}"
    assert "ST:deployed" in out.stdout and "DEP:succeeded" in out.stdout, out.stdout
    assert (tmp_path / "status").read_text().strip() == expect_status, out.stdout
    log = (tmp_path / "log").read_text()
    assert "CALL:update-registry-record\n" in log, log
    assert ("CALL:submit-registry-record-for-approval" in log) is expect_reapprove_calls, log
    warned = [line for line in out.stdout.splitlines() if line.startswith("WARNING:")]
    if expect_warn:
        (w,) = warned
        assert "my_agent" in w and "ag-1" in w and "delisted" in w, w
    else:
        assert warned == [], out.stdout


def _run_write_back(tmp_path, arm, prior, env, reason=""):
    import subprocess

    (tmp_path / "status").write_text(prior + "\n")
    (tmp_path / "data").write_text('{"model_id":"m"}')
    (tmp_path / "reason").write_text(reason)
    block = _registry_write_block() if arm == "runtime" else _harness_write_block()
    arn_var = "RUNTIME_ARN" if arm == "runtime" else "HARNESS_ARN"
    script = (
        f"W={tmp_path}\n{env}\n"
        + _FAKE_REGISTRY
        + _reapproval_helpers()
        + "\nAGENT_REGISTRY_ID=reg\nAGENT_ID=ag-1\nAGENT_NAME=my_agent\nSTAGE=dev\n"
        + f"{arn_var}=arn:aws:bedrock-agentcore:eu-central-1:1:runtime/r-1\n"
        + 'ENV=\'{"model_id":"m"}\'\n'
        + "\n".join(block)
        + "\n"
    )
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"stdout={out.stdout!r} stderr={out.stderr!r}"
    assert "ST:deployed" in out.stdout and "DEP:succeeded" in out.stdout, out.stdout
    return out.stdout, (tmp_path / "log").read_text()


@pytest.mark.parametrize("arm", ["runtime", "harness"])
@pytest.mark.parametrize("env", ["GET_FAILS=1", "ALWAYS_UPDATING=1"], ids=["read-fails", "never-settles"])
def test_an_INCONCLUSIVE_prior_status_WARNS_instead_of_skipping_silently(tmp_path, arm, env):
    """An unreadable or never-settling pre-write status may hide an APPROVED record that the write is
    about to delist: say so, never skip silently."""
    stdout, log = _run_write_back(tmp_path, arm, "APPROVED", env)
    assert "CALL:update-registry-record\n" in log, log
    assert "CALL:submit-registry-record-for-approval" not in log, log
    (w,) = [line for line in stdout.splitlines() if line.startswith("WARNING:")]
    assert "my_agent" in w and "ag-1" in w and "unknown" in w and "delisted" in w, w


_SFX = " [re-approved by AGP after a platform write]"


@pytest.mark.parametrize(
    "prior_reason,expected",
    [
        ("Approved by Lars for Q4", "Approved by Lars for Q4" + _SFX),
        ("", "Re-approved by AGP after a platform write (build ARN write-back)"),
        ("Approved by Lars" + _SFX, "Approved by Lars" + _SFX),
        ("x" * 300, "x" * (255 - len(_SFX)) + _SFX),
    ],
    ids=["kept-plus-suffix", "empty-default", "already-suffixed", "truncated-to-255"],
)
def test_the_re_approve_KEEPS_the_human_approvers_reason(tmp_path, prior_reason, expected):
    """Mirrors registry_writer._reason: the approver's reason is audit evidence."""
    stdout, log = _run_write_back(tmp_path, "runtime", "APPROVED", "", reason=prior_reason)
    assert "CALL:update-registry-record-status" in log, log
    assert (tmp_path / "sent_reason").read_text() == expected
    assert (tmp_path / "status").read_text().strip() == "APPROVED", stdout


def test_APPROVED_by_another_write_is_NOT_reported_as_delisted(tmp_path):
    """The card IS listed, so the delisted WARNING would be false."""
    stdout, log = _run_write_back(tmp_path, "runtime", "APPROVED", "OTHER_APPROVES=1")
    assert "CALL:submit-registry-record-for-approval" not in log, log
    assert [line for line in stdout.splitlines() if line.startswith("WARNING:")] == [], stdout
    assert "APPROVED" in stdout and "another write" in stdout, stdout
