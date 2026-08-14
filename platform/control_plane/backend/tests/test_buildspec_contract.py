"""Contract tests for the CodeBuild buildspec (E27/T7, E28/T1+T2).

The buildspec used to be INLINED into the CodeBuild project via `file()`, where CodeBuild
hard-caps it at 25600 characters — a cap that fails live at `UpdateProject`, invisible to
every offline test, and which this file used to guard with a size assertion. E28/T1 moved
delivery to an S3 source, so the cap is gone and the size assertion with it; what replaced
it is the assertion that delivery really IS via S3, since silently regressing to `buildspec
= file(...)` would reinstate the cap on a file that now exceeds it.

T7 makes a successful DEV deploy persist the image tag it deployed
(`last_dev_image_tag`) so the promote route can resolve "the last good dev image"
with no tag input from the user. A PROD promotion must never overwrite it.
"""

import re
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[2] / "infrastructure/modules/codebuild"
BUILDSPEC = MODULE / "buildspec.yml"
# The repo's own backend venv (Python 3.12) — the faithful interpreter for the buildspec's
# python invocation, since the CodeBuild image is far below 3.14. See _interpreters_below_314.
_PY_DIR = Path(__file__).resolve().parents[1] / "venv/bin"


def test_the_buildspec_is_delivered_from_s3_not_inlined():
    """The invariant that replaced the 25600-char size cap (E28/T1).

    An S3 source has no length limit, so the buildspec is free to grow — but ONLY while it
    stays an S3 source. The file is already over the inline cap, so a regression to an inline
    `buildspec = file(...)` attribute would fail live at `UpdateProject` and nowhere else.
    Hence both halves are pinned: the S3 source is present, and no inline attribute is."""
    main_tf = (MODULE / "main.tf").read_text()

    assert 'type     = "S3"' in main_tf
    assert 'aws_s3_object.buildspec.key' in main_tf
    # No inline `buildspec = ...` attribute on the project (the `file()` that remains belongs
    # to the archive_file that PACKAGES the buildspec for S3, which is a different thing).
    assert not re.search(r"^\s*buildspec\s*=", main_tf, re.MULTILINE)


def _tag_lines():
    """Every line mentioning the `_tag` helper, split into (definition, call sites).
    Matches on `_tag` as a WHOLE word so `last_dev_image_tag` never counts as a hit, and skips
    shell comments so the buildspec's own prose about `_tag` is not read as a call site."""
    lines = [
        l.strip() for l in BUILDSPEC.read_text().splitlines() if not l.strip().startswith("#")
    ]
    hits = [l for l in lines if re.search(r"(?<![\w])_tag\b", l)]
    return (
        [l for l in hits if l.startswith("_tag()")],
        [l for l in hits if not l.startswith("_tag()")],
    )


def test_buildspec_persists_dev_image_tag_on_success():
    text = BUILDSPEC.read_text()
    assert "last_dev_image_tag" in text
    # Non-vacuous: `_tag` as a whole word, not the `..._tag` suffix of the field name.
    definition, _ = _tag_lines()
    assert len(definition) == 1, definition
    assert "last_dev_image_tag" in definition[0]


def test_tag_is_called_on_the_success_path_only():
    """Pins the WIRING, not just the definition: the sole `_tag` call site is the dev
    success path, immediately before `_st deployed`. Dies if the call is deleted (leaving
    the definition as dead code) or moved onto a failure path.

    E28/T4b appended `; _dep succeeded` to this same line (the terminal Deployment row is
    written on exactly the success path `_st deployed` marks), so the assertion pins the
    PREFIX rather than the whole line. It keeps its teeth: a single call site, still
    beginning with `_tag`, still immediately followed by `_st deployed`."""
    _, calls = _tag_lines()
    assert len(calls) == 1, calls
    assert calls[0].startswith("_tag; _st deployed"), calls


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


# --------------------------------------------------------------------------- #
# E28/T4b — `_dep`, the terminal Deployment row (contract C1)
# --------------------------------------------------------------------------- #
#
# The buildspec is the ONLY writer of a `succeeded`/`failed` Deployment row: the backend appends
# `started` when a build is REQUESTED, and only the build knows how it ended. A drift here does
# not raise — the row lands under a key `list_deployments` never queries, and history reads empty.
# `tests/test_promotion_history.py` pins the row's SHAPE round-tripping; this pins the WIRING.


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
    prose edit would otherwise "break" them)."""
    definition, calls = [], []
    for _, cmd in _runtime_commands():
        for line in cmd.split("\n"):
            stripped = line.strip()
            if stripped.startswith("#") or not re.search(r"(?<![\w])_dep\b", stripped):
                continue
            if stripped.startswith("_dep()"):
                definition.append(stripped)
            elif "/tmp/dep.py" not in stripped:  # the heredoc that CREATES the script
                calls.append(stripped)
    return definition, calls


def test_dep_is_defined_and_wired_to_both_outcomes():
    """One definition, and call sites on BOTH the success and the failure paths. A `_dep` wired
    only to success would silently drop every failed build from the history — the state an
    operator most needs to see."""
    definition, calls = _dep_lines()
    assert len(definition) == 1, definition
    assert any(c.endswith("_dep succeeded") for c in calls), calls
    assert sum("_dep failed" in c for c in calls) >= 1, calls
    # Every call passes exactly one of the two enum wire values — never a third literal.
    for call in calls:
        assert re.search(r"_dep (succeeded|failed)\b", call), call


def test_dep_covers_every_failure_exit_individually():
    """PER-EXIT coverage, not an aggregate count.

    `sum(...) >= 1` was satisfied by any ONE of the three failure exits: a reviewer deleted
    `_dep failed` from the terraform-apply exit — the LIKELIEST real failure — and the whole file
    still passed. Each exit is therefore identified by its own distinguishing text and asserted to
    carry a `_dep failed`, so removing any single one fails this test by name.

    The three are: the cross-account promote with no dev image, the cross-account image
    copy/push, and the terraform apply (`RT_FAIL`)."""
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
    # …and every `_st failed` site has a `_dep failed` beside it: the two must never diverge, or
    # the badge says failed while the history shows nothing.
    for call in calls:
        if "_st failed" in call:
            assert "_dep failed" in call, call


def test_dep_writes_the_pinned_partition_and_key_shape():
    """pk="deployment" and the C1 sort key. The sk is built from `id[-4:]` (the 4-char
    collision breaker) and `started_at`, mirrored into `seq_key` for round-tripping."""
    _, script = _dep_script_body()
    assert "'pk': 'deployment'" in script
    assert "i[-4:]" in script, "the sk suffix must be id[-4:], not the whole id"
    assert "'%s#%s#%s#%s' % (r, s, ts, i[-4:])" in script
    assert "'seq_key': k" in script and "'sk': k" in script
    assert "uuid.uuid4().hex[:8]" in script  # the `dep-<8 hex>` id shape
    assert "'dep-'" in script


def test_dep_mints_the_item_with_python_not_shell_date_arithmetic():
    """Regression guard for two bugs the first shell draft shipped: `${ID##*-}` expands to 8 hex
    chars (not `id[-4:]`), and `date -u +%...%6N` is not portable — it emitted a LITERAL "6N",
    producing an unparseable, unsortable timestamp. Both write a row nothing can read. python3
    is already a hard dependency of this buildspec, so minting the item there is not a new one."""
    definition = _dep_definition()
    _, script = _dep_script_body()
    assert "python3 /tmp/dep.py" in definition
    assert "datetime.now(timezone.utc).isoformat()" in script
    assert "%6N" not in definition and "##*-" not in definition


def test_dep_invokes_a_script_FILE_never_python_dash_c():
    """THE round-3 Critical guard, in static form (also EXECUTED further below).

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

    Asserted against `yaml.safe_load`'s value — the bytes CodeBuild really writes — because the
    earlier tests read raw file lines and `.strip()`ed them, which normalizes away this exact
    property and is why they stayed green through a 100%-failing helper."""
    import ast

    _, script = _dep_script_body()
    lines = [l for l in script.split("\n") if l.strip()]
    assert lines, "the heredoc body is empty"
    assert not lines[0].startswith((" ", "\t")), repr(lines[0])
    top_level = [l for l in lines if not l.startswith((" ", "\t"))]
    assert len(top_level) >= 5, f"only {len(top_level)} column-0 lines — the body looks indented"
    ast.parse(script)  # genuinely parseable as a module


def test_the_dep_script_is_written_before_any_path_that_can_call_dep():
    """`_dep` runs on the success path AND on three failure exits, the earliest inside the
    cross-account copy. The heredoc must therefore land in `pre_build`, not beside its first use —
    otherwise an early failure exit invokes a script that does not exist yet."""
    phase, _ = _dep_script_body()
    assert phase == "pre_build", f"the dep.py heredoc is in {phase!r}, not pre_build"


def test_dep_records_no_human_actor():
    """A build has no human actor. C1 keeps a GitHub login and an Entra oid as distinct
    currencies, so inventing an actor here would FORGE attribution — leaving both unset is the
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
    # …and the reasoning is recorded where a maintainer will actually see it.
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
    assert "/tmp/dep.json" not in definition
    assert 'rm -f "$D_ITEM"' in definition


def test_dep_error_hint_is_safe():
    """`error` is a SAFE short hint only — never a token, ARN or raw upstream body (C1)."""
    _, script = _dep_script_body()
    assert "'the runtime build failed'" in script
    for leak in ("arn:", "SECRET", "TOKEN", "get-secret-value"):
        assert leak not in script


def _repo_sk_query_line():
    """The buildspec line that resolves `REPO_SK` from the `agent_id-index` GSI."""
    (line,) = [
        l.strip()
        for l in BUILDSPEC.read_text().splitlines()
        if "REPO_SK=$(aws dynamodb query" in l
    ]
    return line


def test_the_repo_sk_query_filters_to_the_repository_partition():
    """Critical 2: `agent_id-index` indexes EVERY row carrying an `agent_id`, and
    `models/deployment.py` gives `Deployment` one — so from the first append onward this GSI
    returns 2+ items for a single agent.

    GSI item order is not contractual, so without a filter `Items[0]` may be a DEPLOYMENT row and
    `REPO_SK` becomes a composite key (`repo-x#dev#2026-...#1027`). That value passes the
    `!= "None"` guard, so `_u` / `_st` / `_tag` / `_dep` all then write to a nonexistent key —
    breaking the PRE-EXISTING `cicd_status` write-back, nondeterministically.

    The index is KEYS_ONLY, which projects the table keys (`pk`, `sk`) alongside `agent_id`, so
    filtering on `pk` needs no index change."""
    line = _repo_sk_query_line()
    assert "--filter-expression" in line, "the GSI query must exclude non-repository rows"
    assert 'pk = :p' in line
    assert '\\":p\\":{\\"S\\":\\"repository\\"}' in line
    # …and the agent key condition is still what selects the partition.
    assert "agent_id = :a" in line


def test_a_deployment_row_sorting_first_still_resolves_the_repository_sk():
    """The test that would have caught Critical 2, exercising the RESOLUTION SEMANTICS.

    This repo's convention is no moto (`conftest.py:14`, research §10), so rather than stand up
    DynamoDB this simulates the GSI in its WORST ordering — the deployment row first — and applies
    the buildspec's own filter to it. That is the case that broke: unfiltered, `Items[0].sk` is a
    deployment's composite key; filtered, only the repository row can match.

    The filter applied here is read OUT of the buildspec, so this cannot pass if the real query
    stops filtering."""
    import json

    line = _repo_sk_query_line()
    # A GSI page for one agent, deployment row deliberately FIRST (KEYS_ONLY ⇒ pk/sk/agent_id).
    gsi_page = [
        {"pk": {"S": "deployment"}, "sk": {"S": "r-1#dev#2026-07-31T12:20:17.180133+00:00#b62c"},
         "agent_id": {"S": "a-1"}},
        {"pk": {"S": "repository"}, "sk": {"S": "r-1"}, "agent_id": {"S": "a-1"}},
    ]

    # Unfiltered — the pre-fix behaviour, asserted so the test states what it is preventing.
    assert gsi_page[0]["sk"]["S"] != "r-1"
    assert "#" in gsi_page[0]["sk"]["S"], "a composite key would be used as a repo sk"

    # Now apply the buildspec's OWN filter, parsed from the file rather than restated.
    values = json.loads(
        re.search(r'--expression-attribute-values "(.*?)" --query', line).group(1).replace('\\"', '"')
    )
    wanted_pk = values[":p"]["S"]
    filtered = [i for i in gsi_page if i["pk"]["S"] == wanted_pk]

    assert len(filtered) == 1, filtered
    resolved = filtered[0]["sk"]["S"]  # the CLI's `Items[0].sk.S`
    assert resolved == "r-1", "REPO_SK must be the repository row's sk, not a deployment's"
    assert "#" not in resolved


def test_dep_passes_every_env_var_it_reads_into_the_python_process():
    """THE Critical-1 regression guard, asserted statically here and EXECUTED in the test below.

    `AGENT_ID` (:258) and `REPO_SK` (:274) are plain shell assignments — the file's only `export`s
    are the three AWS credential lines (:64-66) — so a bare `python3 -c` inherits NEITHER. The
    body died on `KeyError: 'REPO_SK'`, the `&&` chain short-circuited, `put-item` never ran, and
    `2>/dev/null || true` swallowed the whole thing: NO terminal row in ANY build, silently.

    Every name the python body reads must therefore appear as an inline `NAME="$NAME"` assignment
    on the `python3` invocation. Derived from the body itself, so a newly-read variable that nobody
    passes fails this test rather than shipping."""
    definition = _dep_definition()
    _, script = _dep_script_body()
    invocation = definition.split("python3 /tmp/dep.py")[0]
    # The read-set comes from the SCRIPT, the pass-through from the INVOCATION — two different
    # strings now that the body lives in its own file, and the union is what must agree.
    # `[\[(]` matches BOTH access forms: the subscript `os.environ['X']` (which raises KeyError —
    # the Critical-1 form) and `os.environ.get('X')`. A parens-only pattern found just the single
    # `.get()` call and so passed while REPO_SK/AGENT_ID/STAGE/IMAGE_TAG went unchecked.
    read = set(re.findall(r"os\.environ(?:\.get)?[\[(]\s*['\"]([A-Z_]+)['\"]", script))
    assert read >= {"REPO_SK", "AGENT_ID", "STAGE", "IMAGE_TAG"}, (
        f"the extractor found only {sorted(read)} — it is not seeing the whole python body, "
        "so this test would pass vacuously"
    )
    for name in sorted(read):
        if name == "D_OUT":  # supplied from the function's own "$1", not an outer variable
            assert 'D_OUT="$1"' in invocation
            continue
        assert f'{name}="${name}"' in invocation or f'{name}="${{{name}:-}}"' in invocation, (
            f"{name} is read by the python body but never passed into its environment — "
            "it is a non-exported shell variable, so the row will never be written"
        )


def _interpreters_below_314():
    """Every available python3 interpreter older than 3.14, newest first.

    3.14 added dedenting for `python3 -c`; nothing earlier has it. So a test that runs ONLY on
    3.14 cannot observe the indentation class of bug at all — which is exactly how it slipped
    through twice (both the author's and the reviewer's default `python3` was 3.14.5). The
    CodeBuild image is `amazonlinux2-aarch64-standard:3.0`, far below 3.14, so a sub-3.14
    interpreter is the ONLY faithful one here."""
    import subprocess

    found = []
    candidates = [
        str(_PY_DIR / "python"),  # the repo's own backend venv (3.12.x)
        "/opt/homebrew/bin/python3.13",
        "/opt/homebrew/bin/python3.12",
        "/usr/bin/python3",
    ]
    for path in candidates:
        try:
            out = subprocess.run(
                [path, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                capture_output=True, text=True, timeout=30,
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
    # De-duplicate by resolved version, keeping the first (the venv) — running three interpreters
    # of the same version adds runtime, not coverage.
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

    Three properties of this harness are load-bearing, each one a bug that got through before:

    1. **The script comes from `yaml.safe_load`**, not from stripped file lines. The previous
       version `.strip()`ed every line, so it executed a DEDENTED variant that production never
       runs — and stayed green while the real helper failed 100% of the time.
    2. **The shell variables are NOT exported.** The original capture used the env-prefix form
       (`REPO_SK=r-1 python3 …`), which DOES export, so it validated the script text against an
       environment production never provides.
    3. **The interpreter is < 3.14** (parameterized). On 3.14 an indented `-c` body is dedented
       automatically and the bug is invisible.

    `aws` is stubbed, so nothing touches AWS; the emitted item is parsed back through the real
    `Deployment` model — if it cannot round-trip, `list_deployments` could never return it."""
    import json
    import subprocess
    import tempfile
    from pathlib import Path as _Path

    from models.deployment import Deployment

    heredoc_phase, _ = _dep_script_body()
    definition = _dep_definition()
    # The heredoc command AS CODEBUILD RUNS IT (comments stripped only because `set -e` + a bare
    # `#` line is noise; the code lines keep their runtime indentation verbatim).
    heredoc_cmd = next(
        "\n".join(l for l in cmd.split("\n") if not l.strip().startswith("#"))
        for phase, cmd in _runtime_commands()
        if phase == heredoc_phase and "cat > /tmp/dep.py" in cmd
    )

    with tempfile.TemporaryDirectory() as tmp:
        script_path = _Path(tmp) / "dep.py"
        item_path = _Path(tmp) / "item.json"
        # Relocate only the PATHS into the sandbox — never reformat the body.
        heredoc_cmd = heredoc_cmd.replace("/tmp/dep.py", str(script_path))
        body = definition.replace("/tmp/dep.py", str(script_path))
        script = (
            f"{heredoc_cmd}\n"
            # Deliberately NOT exported — this is the production condition.
            "REPO_SK=repo-abc123\n"
            "AGENT_ID=a-1\n"
            "STAGE=uat\n"
            "IMAGE_TAG=a-1-abc1234\n"
            "CODEBUILD_BUILD_ID=agp:1111-2222\n"
            "PROJECTS_TABLE_NAME=t\n"
            # Pin the interpreter under test, and capture the item the CLI would have sent.
            f'python3() {{ "{interpreter}" "$@"; }}\n'
            f'aws() {{ for a in "$@"; do case "$a" in file://*) cp "${{a#file://}}" '
            f'"{item_path}";; esac; done; return 0; }}\n'
            f"{body}\n"
            "_dep succeeded\n"
        )
        proc = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)
        assert script_path.exists(), (
            f"the dep.py heredoc did not land — stderr={proc.stderr!r}"
        )
        assert item_path.exists(), (
            f"no row was produced on python{version} — this is the IndentationError signature "
            f"if the body is inlined with -c. stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        raw = json.loads(item_path.read_text())

    # Unwrap the low-level CLI shape and validate through the REAL model: if the shell-built item
    # cannot become a Deployment, `list_deployments` could never return it.
    flat = {k: (None if "NULL" in v else v["S"]) for k, v in raw.items()}
    flat.pop("pk")
    record = Deployment.model_validate(flat)
    assert record.repo_id == "repo-abc123"
    assert record.stage == "uat"  # free-form (D8) — not a dev/prod literal
    assert record.agent_id == "a-1"
    assert record.image_tag == "a-1-abc1234"
    assert record.build_id == "agp:1111-2222"
    assert record.outcome.value == "succeeded"
    assert record.actor is None and record.actor_kind is None  # a build has no human actor
    # The contract's key shape, computed by the SHELL and re-derived here independently.
    assert record.seq_key == f"repo-abc123#uat#{record.started_at}#{record.id[-4:]}"
    assert raw["pk"]["S"] == "deployment"
    assert raw["sk"]["S"] == record.seq_key


# --------------------------------------------------------------------------- #
# E28A/T6 (fix) — `_tag` covers PROD too: `last_promoted_image_tag`
# --------------------------------------------------------------------------- #
#
# T6 made the backend's promote-time stamp of `last_promoted_image_tag` conditional on an already
# `succeeded` prod deployment row, which on a genuine FIRST promote is necessarily false — so with
# the backend as the only writer the field stayed empty forever and a truly deployed prod repo read
# "Never deployed" (`repoRowModel.ts`), with the dev↔prod drift comparison unable to fire. The
# buildspec is the only producer that knows a prod `terraform apply` actually succeeded, and it
# already writes the symmetric dev field with the same helper, so the prod tag belongs here too.
#
# Both halves are pinned below, so a future edit cannot silently drop either one.


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
    """Each field asserted together with the stage literal that must gate it. A `_tag` writing
    `last_promoted_image_tag` on dev (or the dev field on prod) would corrupt the drift comparison
    the frontend runs BETWEEN the two scalars — they must never converge onto one stage.

    E28B/T4 added the two DIGEST fields on the same shape, so there are now four writes: a
    (tag, digest) pair per stage."""
    definition = _tag_definition()
    for stage, field in (("dev", "last_dev_image_tag"), ("prod", "last_promoted_image_tag")):
        assert f'[ "$STAGE" = "{stage}" ]' in definition, definition
        assert f'_u {field} "$IMAGE_TAG"' in definition, definition
    for stage, field in (("dev", "last_dev_digest"), ("prod", "last_promoted_digest")):
        assert f'_u {field} "$IMAGE_DIGEST"' in definition, definition
    # Each write guarded on a non-empty value: a persisted "" is a present-but-useless value that
    # every reader would treat as a real tag/digest. Two tag guards + two digest guards.
    assert definition.count('[ -n "$IMAGE_TAG" ]') == 2, definition
    assert definition.count('[ -n "${IMAGE_DIGEST:-}" ]') == 2, definition
    # Best-effort, all four: a failed history write must never fail a good deploy.
    assert definition.count("|| true") == 4, definition


_DIGEST = "sha256:" + "ab" * 32


@pytest.mark.parametrize(
    "stage,image_tag,image_digest,expected",
    [
        # E28B/T4 — the DIGEST cases. A stage writes its own (tag, digest) pair and nothing else.
        ("dev", "a-1-abc1234", _DIGEST, ["last_dev_image_tag", "last_dev_digest"]),
        ("prod", "a-1-abc1234", _DIGEST, ["last_promoted_image_tag", "last_promoted_digest"]),
        # The legacy tag-only path: no digest in scope writes NO digest field, rather than
        # persisting a "" that a reader would treat as a real digest.
        ("dev", "a-1-abc1234", "", ["last_dev_image_tag"]),
        ("prod", "a-1-abc1234", "", ["last_promoted_image_tag"]),
        ("uat", "a-1-abc1234", _DIGEST, []),  # free-form stages (D8) earn no field
        ("prod", "", "", []),  # an empty tag writes nothing
        # The two halves are INDEPENDENT: an empty tag must not suppress the digest write, or a
        # partially-populated build would silently record neither.
        ("prod", "", _DIGEST, ["last_promoted_digest"]),
    ],
    ids=["dev", "prod", "dev-no-digest", "prod-no-digest", "uat", "prod-empty-both",
         "prod-digest-only"],
)
def test_tag_actually_writes_only_the_stage_matched_field(stage, image_tag, image_digest, expected):
    """EXECUTES the real `_tag`. The `&&`/`||` chain is what routes per stage, and no amount of
    static text proves the precedence is right once there are two branches on one line.

    `STAGE`/`IMAGE_TAG` are assigned NON-exported, which is the production condition (`_tag` reads
    them as plain shell variables of its own block; this file's only `export`s are the three AWS
    credential lines). The env-prefix form `STAGE=x sh -c …` would export them and validate an
    environment production does not have. `_u` is stubbed to echo the field name, so nothing
    touches AWS and the assertion is on WHICH field the helper would have written."""
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
    # `_tag` is called as `_tag; _st deployed` — a non-zero status would not abort, but the helper
    # is `|| true` by contract and must stay silent-and-clean.
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == expected, out.stdout


def test_repository_model_carries_the_promoted_tag_field():
    """The buildspec writes this attribute name straight into the repository row, so a rename on
    the model side without one here would strand the write on a field nothing reads."""
    from models.repository import Repository

    assert "last_promoted_image_tag" in Repository.model_fields


# --------------------------------------------------------------------------- #
# E28A/T1b — stage-scoped resource names + per-stage ARNs (C-A1, C-A2)
# --------------------------------------------------------------------------- #
#
# Finding #9, observed live: a prod promote died on
#   `creating IAM Role (platform_agent-agentcore-exec): EntityAlreadyExists`.
# E28/T2 stage-scoped the terraform STATE KEY but the runtime module never stage-scoped its
# RESOURCE NAMES, so prod's fresh state tried to create a name dev already owns account-globally.
#
# The buildspec is the only producer of the module's tfvars, so `stage` has to be threaded from
# here or the module's new required variable is simply never supplied. And #11's lesson is that a
# tfvar passed to a module that does not DECLARE it is a WARNING, not an error — silently ignored,
# resource names unchanged, a green build that fixed nothing. Hence the drift guard, pinned below.


def _agentcore_branch():
    """The `agentcore_runtime)` case body AS CODEBUILD RUNS IT (from `yaml.safe_load`).

    Same discipline as `_dep_definition`/`_tag_definition`: never stripped file lines. Everything
    asserted about the runtime branch is read out of this string."""
    for _, cmd in _runtime_commands():
        if "agentcore_runtime)" in cmd:
            return cmd[cmd.index("agentcore_runtime)") :]
    raise AssertionError("no agentcore_runtime) case body found in the buildspec")


def _tfvars_heredoc():
    """The `deploy.auto.tfvars` heredoc BODY exactly as it lands on disk at runtime.

    The delimiter is unquoted (`<<TFVARS`), which is load-bearing: the body is a list of
    `key="$VAR"` lines that only become real values because the shell expands them."""
    body = _agentcore_branch()
    assert "cat > deploy.auto.tfvars <<TFVARS\n" in body, (
        "the tfvars heredoc delimiter changed — if it is now QUOTED (<<'TFVARS') every $VAR "
        "lands in the file literally and the module receives no values at all"
    )
    return body.split("cat > deploy.auto.tfvars <<TFVARS\n", 1)[1].split("\nTFVARS", 1)[0]


def test_the_tfvars_pass_the_stage_to_the_runtime_module():
    """C-A1's other half. The module derives BOTH stage-scoped names from `var.stage`, which is
    REQUIRED and has NO default — precisely so a missing thread fails loudly at `terraform plan`
    instead of naming prod's resources `dev`. If this line is dropped the module cannot even plan.

    `$STAGE` is already in scope in this branch (`_tag`, `_dep`, the state key all read it), so
    this is a one-line thread, not new plumbing."""
    assert 'stage="$STAGE"' in _tfvars_heredoc().split("\n")


def test_every_tfvars_line_is_a_shell_expanded_assignment():
    """Non-vacuity guard for the test above: it asserts a LINE, so it would also pass if the
    heredoc had degenerated into prose. Every line here must be `key="…"`, and the `stage` one in
    particular must reference the shell variable rather than hardcoding a stage literal — a
    hardcoded `stage="dev"` is exactly the defect D-A5/D8 spent this epic removing."""
    lines = [l for l in _tfvars_heredoc().split("\n") if l.strip()]
    assert len(lines) >= 12, lines
    for line in lines:
        assert re.match(r'^[a-z_]+="[^"]*"$', line), line
    (stage_line,) = [l for l in lines if l.startswith("stage=")]
    assert "$STAGE" in stage_line, stage_line
    for literal in ('stage="dev"', 'stage="prod"'):
        assert literal not in lines, f"{literal} hardcodes a stage the build was not asked for"


def _arn_write_line():
    """The single runtime line patching the runtime ARN into the registry envelope."""
    body = _agentcore_branch()
    (line,) = [
        l.strip()
        for l in body.split("\n")
        if l.strip().startswith("NEW_ENV=") and not l.strip().startswith("#")
    ]
    return line


def test_the_arn_write_covers_the_per_stage_MAP_key():
    """C-A2's new half. Two runtimes now co-exist, so one scalar cannot name both — the map keyed
    by stage is the only field that can. `jq` creates the object on assignment, so `.agent_arns[$s]`
    is safe against a legacy envelope that carries no map at all."""
    line = _arn_write_line()
    assert '--arg s "$STAGE"' in line, line
    assert ".agent_arns[$s]=$arn" in line, line


def test_the_arn_write_ALSO_KEEPS_the_scalar():
    """The dual write is MANDATORY, not a convenience — pinned separately from the map so a
    "simplification" that drops it fails by name.

    T1a's reader (`models/agent.py::resolve_runtime_arns`) de-dupes the scalar against the map, so
    keeping it costs nothing; but `Agent.is_agentcore` is still `bool(self.agent_arn)`, which gates
    `POST /agents/{id}/reprovision` (409) and the E6 provisioning gate. Dropping the scalar would
    turn every map-only agent non-agentcore. It is also what lets a rollback to pre-E28A code still
    find a runtime."""
    line = _arn_write_line()
    assert ".agent_arn=$arn" in line, line
    # …and both writes are in ONE jq program over one envelope, so they cannot diverge.
    assert line.index(".agent_arns[$s]=$arn") < line.index('.identity_status="provisioned"'), line


def _registry_write_block():
    """The guarded registry-write sequence AS CODEBUILD RUNS IT — comments dropped.

    Spans the `$RUNTIME_ARN` emptiness guard through `_tag; _st deployed; _dep succeeded`, i.e.
    everything between `terraform output -raw` and the terminal report. Extracted from
    `_agentcore_branch()` (`yaml.safe_load`), never from stripped file lines."""
    lines = [
        l.strip()
        for l in _agentcore_branch().split("\n")
        if l.strip() and not l.strip().startswith("#")
    ]
    start = next(i for i, l in enumerate(lines) if l.startswith('if [ -z "$RUNTIME_ARN" ]'))
    end = next(i for i, l in enumerate(lines) if l.startswith("_tag; _st deployed"))
    assert start < end, lines[start : end + 1]
    return lines[start : end + 1]


def test_the_registry_write_is_GUARDED_on_every_step():
    """E28A/T1b FIX 2 (review C1). There is no `set -e` anywhere in this buildspec (`grep -c
    'set -e'` = 0), so an unguarded failure walks straight into `_st deployed; _dep succeeded`.

    Before T1b that was tolerable: the ARN was stable across applies, so a dropped write left a
    CORRECT value. T1b forces replacement of both names, so the stored ARN is guaranteed stale at
    this point — a lost write leaves a live, invocable, billing runtime whose ARN the platform
    records nowhere, and `_delete_runtime` derives everything from `resolve_runtime_arns(agent)`,
    so the E23 cascade can never reclaim it. Reported as a successful deploy."""
    block = "\n".join(_registry_write_block())
    # Each of the three failure paths records the failure the way the terraform-apply guard does,
    # rather than aborting silently or falling through.
    assert block.count("_st failed") == 3, block
    assert block.count("_dep failed") == 3, block
    assert block.count("exit 1") == 3, block
    # The jq that builds the envelope must not be allowed to leave NEW_ENV unset by rc alone…
    (jq_line,) = [l for l in _registry_write_block() if l.startswith("NEW_ENV=")]
    assert jq_line.rstrip().endswith('|| NEW_ENV=""'), jq_line
    # …and the write itself must carry an `||` guard, not sit bare.
    (write_line,) = [l for l in _registry_write_block() if "update-registry-record" in l]
    assert "||" in write_line and "_st failed" in write_line, write_line


def _registry_read_line():
    """The single runtime line that GETs the agent's registry record for this build."""
    (line,) = [
        l.strip()
        for l in _agentcore_branch().split("\n")
        if "get-registry-record" in l and not l.strip().startswith("#")
    ]
    return line


def _registry_write_call_line():
    """The single runtime line that PATCHes the runtime ARN back onto the registry record."""
    (line,) = [l for l in _registry_write_block() if "update-registry-record" in l]
    return line


def test_the_registry_calls_name_the_AGENT_REGISTRY_namespace_not_the_retired_one():
    """E32. The Registry moved off `bedrock-agentcore` onto `agent-registry`, and the old namespace
    SHUTS DOWN 2026-09-17 — brand-new AWS accounts cannot call it at all, today. So this is not a
    tidy-up: a buildspec left on the old name deploys a runtime and then fails the write-back on
    the very next line, which is the untracked-runtime data-loss path the guard below reports.

    Asserted over the WHOLE FILE, not just the two calls, because the failure this prevents is a
    partial migration — one call moved, its sibling missed. Comments are included deliberately: an
    old-namespace name in a comment is the seed of a future "fix" back onto a dead endpoint, and
    this buildspec makes no non-registry AgentCore API call for a mention to legitimately describe.
    Note the IAM verb is the SIGNING name `agent-registry:*` (E32/T7 granted it to the CodeBuild
    role), which differs from the CLI name asserted here."""
    assert "bedrock-agentcore" not in BUILDSPEC.read_text()
    for line in (_registry_read_line(), _registry_write_call_line()):
        assert "aws agent-registry-control " in line, line


def test_the_registry_read_uses_the_RENAMED_custom_descriptor_leaf():
    """E32 renamed the CUSTOM descriptor's blob `inlineContent` -> `data`, and `data` is now the
    ONLY member of `custom` — asserted against the REAL botocore output shape here, so this test
    tracks the model rather than my reading of it.

    A stale `.inlineContent` does not fail: `jq -r` prints the string "null", every
    `.model_id // ""` below it comes back empty, and the build provisions a runtime with no model,
    no Entra app and no Langfuse key while reporting success. Silent mis-provision beats loud
    failure at being hard to find."""
    from botocore.session import get_session

    members = (
        get_session()
        .get_service_model("agent-registry-control")
        .operation_model("GetRegistryRecord")
        .output_shape.members["descriptors"]
        .members["custom"]
        .members
    )
    assert set(members) == {"data"}, list(members)

    (env_line,) = [l for l in _agentcore_branch().split("\n") if l.strip().startswith("ENV=$(")]
    assert ".descriptors.custom.data" in env_line, env_line
    # No CODE line may still name the retired leaf (a comment explaining the rename may).
    code = [
        l for l in _agentcore_branch().split("\n") if l.strip() and not l.strip().startswith("#")
    ]
    assert [l for l in code if "inlineContent" in l] == []


def test_the_registry_write_sends_the_PATCH_SHAPE_THE_REAL_MODEL_ACCEPTS():
    """The half a namespace swap does not cover. `UpdateRegistryRecord` is a PATCH API and its
    nesting got DEEPER in the move: every leaf under an `Updated*Fields` struct is itself an
    `Updated*` wrapper, so the CUSTOM blob sits THREE `optionalValue` levels down
    (`descriptors -> custom -> data`) where the retired namespace took two. The pre-E32 shape fails
    botocore param validation client-side, so the write never leaves the box — same outcome as the
    dead namespace, and equally invisible offline.

    Runs the REAL shell fragment with `aws` stubbed to capture argv, then feeds the captured
    `--descriptors` string to botocore's own `ParamValidator`. That is what makes this a proof
    rather than a transcription: the shell's `\\"` escaping, `jq -Rs .`'s quoting and the model's
    shape are all exercised at once. Mirrors `_wrap_update_descriptors_custom` in
    `services/agent_registry_service.py` — the in-repo precedent for this envelope."""
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
            "ENV='{\"model_id\":\"m\",\"agent_arns\":{}}'\n"
            + "\n".join(_registry_write_block())
            + "\n"
        )
        out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, f"stdout={out.stdout!r} stderr={out.stderr!r}"
        argv = [a for a in cap.read_text().split("\0") if a]

    # Reconstruct the call as boto3 would see it, from the CLI flags actually passed.
    flags = dict(zip(argv, argv[1:]))
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

    # Non-vacuity: the blob really is the patched envelope, three levels down, and the ARN the
    # whole guarded block exists to record actually reached it.
    blob = params["descriptors"]["optionalValue"]["custom"]["optionalValue"]["data"]["optionalValue"]
    assert json.loads(blob)["agent_arns"]["dev"].endswith("runtime/r-1"), blob


@pytest.mark.parametrize(
    "env_json,runtime_arn,aws_rc,expect_exit,expect_aws_called",
    [
        ('{"model_id":"m","agent_arns":{}}', "arn:aws:x:new", 0, 0, True),
        # An empty ARN means `terraform output -raw` failed. Writing it would set agent_arn="",
        # which flips Agent.is_agentcore to False while the deploy reports success.
        ('{"model_id":"m"}', "", 0, 1, False),
        # A LEGACY envelope whose agent_arns is a STRING: jq exits 5 with EMPTY stdout. Reproduced
        # live by the reviewer. `jq -Rs .` would render that as "\\n" and ERASE the descriptor's
        # `data` blob — model_id, entra_app_id, langfuse_key_secret_name and the whole ARN map.
        ('{"model_id":"m","agent_arns":"oops"}', "arn:aws:x:new", 0, 1, False),
        # A truncated / non-JSON envelope reaching this point must also be refused.
        ("not json at all", "arn:aws:x:new", 0, 1, False),
        # The write itself failing (throttle, expired creds, a transient control-plane 5xx).
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
    production does not have."""
    import subprocess

    script = (
        "_st() { echo \"ST:$1\"; }\n"
        "_dep() { echo \"DEP:$1\"; }\n"
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
        # The whole point: a failure must NEVER report deployed/succeeded.
        assert "ST:deployed" not in out.stdout, out.stdout
        assert "DEP:succeeded" not in out.stdout, out.stdout
        assert "ST:failed" in out.stdout and "DEP:failed" in out.stdout, out.stdout
        assert "ERROR:" in out.stdout, out.stdout
    else:
        assert "ST:deployed" in out.stdout and "DEP:succeeded" in out.stdout, out.stdout
        assert "TAGGED" in out.stdout, out.stdout


# --------------------------------------------------------------------------- #
# E32/T9 — the build image's AWS CLI must KNOW the namespace those calls name
# --------------------------------------------------------------------------- #
#
# The group above pins the two Registry calls onto `agent-registry-control`. Naming the service
# correctly is worthless if the CLI running them has never heard of it, and that was the state of
# this image: AWS shipped the namespace on 2026-08-06, `aws/codebuild/amazonlinux2-aarch64-standard:3.0`
# ships whatever CLI it was baked with, and a real AWS CLI v2 (2.35.16) was verified to bundle the
# RETIRED `bedrock-agentcore-control` and **zero** `agent-registry*` service models — `aws
# agent-registry-control` exits 252 with `Invalid choice`.
#
# Why no existing test could see it: `test_the_registry_read_uses_the_RENAMED_custom_descriptor_leaf`
# and its siblings read the BACKEND VENV's botocore, which does carry the service. A self-contained
# AWS CLI v2 binary in a CodeBuild container bundles its OWN botocore. The two are unrelated
# artefacts, so a green suite says nothing about the build image.
#
# What the miss costs is not a red build. `Invalid choice` on the READ aborts early and harmlessly;
# on the WRITE it is the untracked-runtime data-loss path `test_the_registry_write_is_GUARDED_on_
# every_step` describes — a live, billing, invocable runtime whose ARN is recorded nowhere and which
# the E23 cascade can never reclaim. T9 answered that with a capability probe in `install`. These
# tests exist because that probe is a lone `if` in a phase full of setup noise: nothing else in the
# repo requires it to be there, name the service the build actually calls, or fail rather than warn.

# The two parse errors a credential-free `aws <service>` with no operation can return. Both exit 252
# — verified live against AWS CLI v2 2.35.16 — which is precisely why the probe discriminates on the
# TEXT and not the exit status, and why these are the fixtures the execution test drives.
_CLI_SERVICE_UNKNOWN = "aws: error: argument command: Invalid choice, maybe you meant: agent-registry"
_CLI_SERVICE_KNOWN = "aws: error: the following arguments are required: operation"

# The regex that finds a probe: an `aws <service>` invocation whose stdout+stderr is piped into a
# `grep -qi` for the HEALTHY signal. Keyed on the positive string on purpose — see
# `test_the_probe_PROVES_capability_and_does_NOT_infer_it_from_an_absent_failure_string` for why the
# negative form is a bug and not a stylistic choice. `(!?)` captures whether the branch is negated,
# which is what distinguishes the first probe (assert healthy) from the re-probe (fail if not healthy).
_PROBE_RE = r'(!?)\s*AWS_PAGER="" aws\s+([a-z0-9-]+)\s+2>&1\s*\|\s*grep\s+-qi\s+"arguments are required"'


def _code_lines(cmd: str) -> list[str]:
    """A runtime command's EXECUTABLE lines, shell comments dropped.

    Same reason as `_agentcore_code_lines`: this probe carries ~60 lines of comment explaining
    itself, and every one of them contains the words this group asserts on ("arguments are
    required", "invalid choice", "agent-registry-control", "help"). Any assertion made over the raw
    command matches the PROSE first and passes no matter what the code does."""
    return [l.strip() for l in cmd.split("\n") if l.strip() and not l.strip().startswith("#")]


def _cli_capability_probe():
    """(flat command index, phase, command) of the AWS CLI capability probe.

    Identified by its DISCRIMINATOR — a `grep -qi` for the healthy parse error over an
    `aws <service>` call — not by a comment marker, so the probe cannot be reduced to a comment and
    keep passing. The flat index is the position among all phase commands in the order CodeBuild
    runs them, which is what the ordering assertion below needs."""
    hits = [
        (i, phase, cmd)
        for i, (phase, cmd) in enumerate(_runtime_commands())
        if any(re.search(_PROBE_RE, l) for l in _code_lines(cmd))
    ]
    assert len(hits) == 1, f"expected exactly one AWS CLI capability probe, got {hits}"
    return hits[0]


def _probed_services() -> set[str]:
    """Every service name the probe actually tests the CLI for (read off the probe's own lines)."""
    _, _, cmd = _cli_capability_probe()
    return {
        m.group(2)
        for l in _code_lines(cmd)
        for m in [re.search(_PROBE_RE, l)]
        if m
    }


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
        if any(re.search(r"aws\s+agent-registry-control\s+[a-z]", l) for l in _code_lines(cmd))
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
    the new call, which is exactly the unguarded state T9 found.

    So the service name is not written down twice — it is read off the probe and off the two call
    sites and required to be the same single name."""
    called = {
        re.search(r"aws\s+([a-z0-9-]+)\s", line).group(1)
        for line in (_registry_read_line(), _registry_write_call_line())
    }
    assert called == {"agent-registry-control"}, called
    assert _probed_services() == called, (_probed_services(), called)
    # The retired namespace, by name: it is the one string that would make all of the above agree
    # with each other and still be wrong. (`test_the_registry_calls_name_the_AGENT_REGISTRY_
    # namespace_not_the_retired_one` bans the whole `bedrock-agentcore` prefix file-wide; this pins
    # the control-plane service specifically, so a partial revert fails by name here too.)
    assert "bedrock-agentcore-control" not in BUILDSPEC.read_text()


def test_the_probe_FAILS_THE_BUILD_rather_than_WARNING_when_the_capability_is_absent():
    """A probe that only logged would be worse than none: the build would report the exact reason it
    was about to lose a runtime, in a passing log nobody reads, and then lose it.

    Every half is pinned off code. The `exit 1` exists; it is reached only from the RE-probe (the
    first probe's job is to trigger the upgrade, not to fail); it is accompanied by an `ERROR:` on
    stderr; and the fatal path carries no `|| true`-class swallow. `phases.install` also declares no
    `on-failure: CONTINUE`, without which CodeBuild would run the next phase regardless of the
    `exit 1`."""
    import yaml

    _, _, cmd = _cli_capability_probe()
    lines = _code_lines(cmd)

    (exit_i,) = [i for i, l in enumerate(lines) if l == "exit 1"]
    probe_is = [i for i, l in enumerate(lines) if re.search(_PROBE_RE, l)]
    assert len(probe_is) == 2, probe_is  # the probe, then the authoritative re-probe
    assert probe_is[1] < exit_i, (probe_is, exit_i)
    # The re-probe is the gate on `exit 1`, not the initial one.
    assert probe_is[0] < probe_is[1] < exit_i

    (error_i,) = [i for i, l in enumerate(lines) if l.startswith("echo \"ERROR:")]
    assert error_i < exit_i, (error_i, exit_i)
    assert lines[error_i].rstrip().endswith(">&2"), lines[error_i]
    assert "Refusing to start" in lines[error_i]
    # Nothing on the fatal path may swallow it. The ONE tolerated failure is the download chain,
    # whose `|| echo` is deliberate — the re-probe, not the download's rc, decides the verdict.
    for i in (probe_is[1], error_i, exit_i):
        assert "|| true" not in lines[i], lines[i]
    assert sum("|| true" in l for l in lines) <= 1, lines  # `hash -r || true` only

    # `help` was rejected as the probe for a reason that is a build outage, not a nicety: AWS CLI v2
    # renders help through a man pager, so on an image without groff it exits non-zero for a service
    # the CLI knows — firing the upgrade on a healthy image and then failing this very `exit 1`.
    assert not any(re.search(r"aws\s+[a-z0-9-]+\s+help", l) for l in lines), lines

    install = yaml.safe_load(BUILDSPEC.read_text())["phases"]["install"]
    assert install.get("on-failure", "ABORT") == "ABORT", install.get("on-failure")


def test_the_probe_PROVES_capability_and_does_NOT_infer_it_from_an_absent_failure_string():
    """THE fail-open test. The first implementation of this probe read

        if ! aws agent-registry-control 2>&1 | grep -qi "invalid choice"; then  # "healthy"

    which asserts the ABSENCE of one known failure string — so every OTHER outcome was silently
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
    execution; this one pins the shape so the failure is legible when it breaks."""
    _, _, cmd = _cli_capability_probe()
    lines = _code_lines(cmd)

    probes = [(i, m) for i, l in enumerate(lines) for m in [re.search(_PROBE_RE, l)] if m]
    assert len(probes) == 2, probes  # the probe, then the authoritative re-probe

    # The FIRST probe takes the healthy branch on a MATCH (not negated): capability proven.
    first_i, first_m = probes[0]
    assert first_m.group(1) == "", lines[first_i]
    # The RE-probe is negated: `if ! <healthy>` → fail. Same positive signal, so an `aws` that now
    # crashes/vanishes/prints nothing after the upgrade fails rather than reporting a successful heal.
    second_i, second_m = probes[1]
    assert second_m.group(1) == "!", lines[second_i]

    # No capability decision anywhere may key on the unknown-service text. A `grep` for it inside an
    # `if`/`while` is the old fail-open form; it must not come back in either position.
    for l in lines:
        assert not re.search(r"\bif\b.*grep[^|]*invalid choice", l, re.I), l
        assert not re.search(r"\bif\b.*grep[^|]*maybe you meant", l, re.I), l


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
    apart, and the failure mode — a probe that never fires — is indistinguishable from a healthy
    image right up to the moment a runtime goes missing. Both cases exit 252, so a probe that
    branched on the exit code would pass every static assertion above and be inert here.

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


# Every way a build image's `aws` can fail to prove it knows the service. `None` means the stub is
# not created at all, i.e. `aws` is absent from `PATH` — which the old negative-match probe scored as
# HEALTHY, because "command not found" contains no "invalid choice" either.
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

    This is the regression test for the fail-open bug, and the reason it is an execution test rather
    than a string assertion: the defect was invisible statically — the old condition *looked* like a
    capability check and read fine — and only an executed stub showed it printing "aws CLI knows
    agent-registry-control" and exiting 0 for four of these five inputs. The parametrisation is
    deliberately wider than the two strings the probe handles by name, because "the failures we
    enumerated" is exactly the assumption that broke: the guard must fail closed on outputs nobody
    predicted, including AWS rewording its own error and the CLI producing no output at all.

    `curl` is stubbed to fail, so the upgrade cannot heal any case and the re-probe sees the same
    unhealthy `aws` — which makes `exit 1` the only correct outcome for all five. `PATH` is replaced
    (not prepended) so `aws-missing-from-PATH` is a genuine absence rather than a shadowed binary."""
    import subprocess

    body, _why = _UNHEALTHY_CLIS[case]
    _, _, cmd = _cli_capability_probe()

    binder = tmp_path / "bin"
    binder.mkdir()
    (binder / "curl").write_text("#!/bin/sh\necho UPGRADE_ATTEMPTED\nexit 1\n")
    (binder / "curl").chmod(0o755)
    if body is not None:
        (binder / "aws").write_text(
            '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "aws-cli/2.0.0-stub"; exit 0; fi\n'
            + body
            + "\n"
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
    # The precise fail-open symptom, asserted as its own claim: an unhealthy CLI must never be
    # reported as a working one, on either the initial probe or the post-upgrade re-probe.
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
        l.strip()
        for l in body.split("\n")
        if not l.strip().startswith("#") and "STALE RUNTIME MODULE" in l
    ]
    assert len(hits) == 1, f"expected exactly one drift guard line, got {hits}"
    return hits[0]


def test_a_stale_cloned_module_FAILS_the_build_instead_of_deploying_silently():
    """THE structural guard, and the most valuable line in T1b after the rename itself.

    **The module in this repo is not the module that runs.** `runtime_build_service.py` sends
    `GIT_INFRA_ORG`/`GIT_INFRA_REPO`, the install phase git-clones the per-org `agp-runtime-infra`
    repo into `/tmp/workspace`, and this branch applies whatever `.tf` it finds there. The S3
    module zip is consumed only by a create-once rollout. So every terraform edit in this repo is
    INERT until the module is rolled out to the org.

    That is fine — but an unrolled module makes `stage="$STAGE"` a terraform WARNING ("Value for
    undeclared variable"), not an error: the value is discarded, the resource names stay
    account-global, and the build goes GREEN having fixed nothing. That is exactly how finding #11
    (the langfuse vars, silently dropped for a whole epic) stayed hidden.

    The guard must therefore run BEFORE `terraform init`/`plan`, and it must exit non-zero."""
    line = _drift_guard_line()
    assert "variable" in line and "stage" in line, line
    assert "exit 1" in line, line
    # Recorded as a FAILED deploy, not a silent abort — the badge and the history must agree.
    assert "_st failed" in line and "_dep failed" in line, line


def _agentcore_code_lines():
    """The runtime branch's EXECUTABLE lines only — shell comments dropped.

    Load-bearing, and it cost this test a red round to learn: the guard's own comment block
    explains that it "Runs BEFORE `terraform init`", so an ordering assertion over the raw body
    finds that PROSE first and passes no matter where the real command sits. Five guards in this
    epic were defeated by their own comments; ordering must be read off code."""
    return [
        l.strip()
        for l in _agentcore_branch().split("\n")
        if l.strip() and not l.strip().startswith("#")
    ]


def test_the_drift_guard_runs_BEFORE_terraform_touches_anything():
    """Ordering is the whole point: a guard after `terraform apply` would fire only once the
    stale module had already created (or refused to create) account-global resources. It must sit
    after `cd "$TF_DIR"` — it inspects the cloned root — and before `terraform init`."""
    lines = _agentcore_code_lines()

    def index_of(needle):
        hits = [i for i, l in enumerate(lines) if needle in l]
        assert hits, f"no executable line containing {needle!r}"
        return hits[0]

    assert index_of('cd "$TF_DIR"') < index_of("STALE RUNTIME MODULE"), (
        "the guard must come after the workspace is resolved"
    )
    assert index_of("STALE RUNTIME MODULE") < index_of("terraform init "), (
        "the guard must come before terraform init — otherwise a stale module has already run"
    )
    # …and the tfvars the guard protects are written before it, so a root missing `stage` is
    # caught rather than merely warned about.
    assert index_of("cat > deploy.auto.tfvars") < index_of("STALE RUNTIME MODULE")


def test_the_drift_guard_is_scoped_to_the_agentcore_runtime_branch_only():
    """`IAC_TYPE` selects between branches, and the other branches apply developer-owned
    terraform that has no `stage` variable and never should. A guard leaking outside this case
    body would fail every non-runtime deploy."""
    text = BUILDSPEC.read_text()
    assert text.count("STALE RUNTIME MODULE") == 1, "the guard must exist in exactly one branch"
    # The marker lives inside the `agentcore_runtime)` case body, which _agentcore_branch()
    # returns — and that body is what `IAC_TYPE=agentcore_runtime` selects.
    assert "STALE RUNTIME MODULE" in _agentcore_branch()


@pytest.mark.parametrize(
    "root_tf,expect_exit",
    [
        ('variable "stage" {\n  type = string\n}\n', 0),
        ('variable  "stage"  {\n  type = string\n}\n', 0),  # extra whitespace still declares it
        ("", 1),  # the ROLLED-OUT-NOTHING case
        ('variable "agent_name" {\n  type = string\n}\n', 1),  # the STALE module, verbatim
        ('# stage is threaded by the buildspec\nvariable "agent_name" {}\n', 1),  # a COMMENT is not
        ('locals {\n  stage = "dev"\n}\n', 1),  # a local named stage is not a variable
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

    The variables are assigned NON-exported and the fragment runs under `sh -c`, matching the
    production condition (this file's only `export`s are the three AWS credential lines)."""
    import subprocess

    (tmp_path / "main.tf").write_text(root_tf)
    script = (
        "_st() { :; }\n"
        "_dep() { :; }\n"
        f"cd {tmp_path}\n"
        f"{_drift_guard_line()}\n"
        "echo REACHED_TERRAFORM\n"
    )
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == expect_exit, f"stdout={out.stdout!r} stderr={out.stderr!r}"
    if expect_exit:
        assert "REACHED_TERRAFORM" not in out.stdout, "the guard did not stop the build"
        assert "STALE RUNTIME MODULE" in out.stdout, out.stdout
    else:
        assert "REACHED_TERRAFORM" in out.stdout, out.stdout


# --- the module side of the same contract, pinned across the file boundary ------------------- #
#
# The buildspec and the module are edited independently and neither imports the other, so every
# agreement between them is a convention a single edit can break silently. These read the REAL
# module rather than restating its text.

_RUNTIME_MODULE = Path(__file__).resolve().parents[2] / "infrastructure/modules/agentcore_runtime"


def test_the_runtime_module_DECLARES_the_stage_the_buildspec_passes():
    """The other end of `stage="$STAGE"`. A tfvar with no matching `variable` block is a terraform
    WARNING, so this pair is exactly the silent-drift class the guard above exists for — and here
    it is caught offline."""
    body = (_RUNTIME_MODULE / "variables.tf").read_text()
    # Comments stripped FIRST. The variable's own comment explains at length why it has no
    # `default`, so a substring check over the raw block matched that PROSE and failed — the same
    # comment-defeats-its-own-guard class that bit five guards in this epic.
    code = "\n".join(l for l in body.split("\n") if not l.strip().startswith("#"))
    assert re.search(r'variable\s+"stage"\s*\{', code), "the module must declare var.stage"
    block = code.split('variable "stage"', 1)[1]
    block = block[: block.index("\nvariable ")] if "\nvariable " in block else block
    assert not re.search(r"^\s*default\s*=", block, re.MULTILINE), (
        "var.stage must have NO default — a defaulted stage silently names prod's resources 'dev'"
    )


_LOCAL_TEMPLATES = {
    # C-A1's exact shapes, as regexes over the ASSIGNMENT rather than substrings of the file.
    "runtime_name": r'"\$\{var\.agent_name\}_\$\{var\.stage\}"',
    "exec_role_name": r'"\$\{var\.agent_name\}-\$\{var\.stage\}-agentcore-exec"',
}


def _locals_stage_scoped(body):
    """Which of the two module locals actually ASSIGN the stage-scoped template.

    Structural on purpose (E28A/T1b FIX 2, review C2). The previous version of this check was two
    bare `'<literal>' in body` substrings over raw `main.tf` text, which a `#` comment carrying the
    literal satisfies — and `modules/agentcore_runtime/README.md` already carries both literals
    verbatim, so one copy-paste into main.tf's comment header disarmed the guard on the epic's
    CRITICAL finding permanently. Comments are stripped first AND the pattern is anchored to a
    line-start assignment, so neither defence alone is load-bearing. A trailing `#` comment on the
    real assignment is tolerated (both locals carry one, naming their AWS ceiling)."""
    code = "\n".join(
        l
        for l in body.split("\n")
        if not (l.strip().startswith("#") or l.strip().startswith("//"))
    )
    return {
        name
        for name, template in _LOCAL_TEMPLATES.items()
        if re.search(rf"^\s*{name}\s*=\s*{template}\s*(#.*)?$", code, re.M)
    }


def test_the_stage_scoped_locals_check_cannot_be_satisfied_by_a_COMMENT():
    """Non-vacuity for the sibling below, and the mutation the quality review ran by hand.

    The reviewer un-stage-scoped BOTH locals in the real `main.tf` — literally reintroducing
    finding #9 — while leaving the correct templates in a `#` block immediately above, the exact
    shape a half-update produces. Every assertion stayed green. This feeds that mutation in as a
    fixture so the defeat cannot come back."""
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
    # …and the real shapes ARE recognised, so the emptiness above is the comment being rejected
    # rather than the pattern being broken outright.
    assert _locals_stage_scoped(
        "locals {\n"
        '  runtime_name   = "${var.agent_name}_${var.stage}"   # AWS: [a-zA-Z][a-zA-Z0-9_]{0,47}\n'
        '  exec_role_name = "${var.agent_name}-${var.stage}-agentcore-exec" # IAM: <= 64\n'
        "}\n"
    ) == {"runtime_name", "exec_role_name"}


def test_both_account_global_names_are_stage_scoped_in_the_module():
    """Finding #9 was the IAM role; `agent_runtime_name` is the same class, latent
    (`CreateAgentRuntime` declares `ConflictException` — prod never reached it because IAM failed
    first). Both are pinned so neither can regress alone, and both must resolve through the
    module's single `locals` block rather than inlining `var.stage` at the resource."""
    body = (_RUNTIME_MODULE / "main.tf").read_text()
    (role_name,) = re.findall(
        r'resource\s+"aws_iam_role"\s+"exec"\s*\{\s*\n\s*name\s*=\s*([^\n]+)', body
    )
    (runtime_name,) = re.findall(r"agent_runtime_name\s*=\s*([^\n]+)", body)
    assert role_name.strip() == "local.exec_role_name", role_name
    assert runtime_name.strip() == "local.runtime_name", runtime_name
    # …and the locals really carry the stage (C-A1's exact shapes), read off the ASSIGNMENTS.
    assert _locals_stage_scoped(body) == {"runtime_name", "exec_role_name"}, body


# --------------------------------------------------------------------------- #
# E28B/T4 (D-B3) — promotion deploys a DIGEST, not a mutable tag
# --------------------------------------------------------------------------- #
#
# `container_image_uri` was `$ECR_REPO:$IMAGE_TAG` — a MUTABLE tag. The tenant ECR repo is mutable
# and this image build is not reproducible (a floating base image, ranged deps, no lockfile), so
# the bytes behind one tag can differ between the moment an OWNER approves and the moment prod
# deploys. The digest names the bytes themselves.
#
# The digest is threaded in from the backend (`runtime_build_service.py` → `IMAGE_DIGEST`) and is
# deliberately NOT re-derived here from the tag: a `describe-images --image-ids imageTag=…` lookup
# would return whatever the tag points at NOW, which is precisely the window the digest closes.
# Every assertion below reads the fragment from `yaml.safe_load` and, where behaviour is the claim,
# EXECUTES it under `sh -c` with NON-EXPORTED variables — the production condition.

# A well-formed digest, and the mutations that must all be refused. Data-flow mutations, not just
# control-flow: a wrong-but-well-formed digest is accepted (nothing offline can know it is wrong),
# while every MALFORMED shape must fail the build loudly.
_GOOD_DIGEST = "sha256:" + "ab" * 32


def _digest_shape_guard():
    """The runtime line that refuses a malformed `IMAGE_DIGEST`, from `yaml.safe_load`."""
    hits = [
        l for l in _agentcore_code_lines()
        if l.startswith('if [ -n "${IMAGE_DIGEST:-}" ]; then echo "$IMAGE_DIGEST"')
    ]
    assert len(hits) == 1, f"expected exactly one digest shape guard, got {hits}"
    return hits[0]


def _bad_digest_helper():
    """The `_bad_digest` helper definition — the recorded-failure path both guards share."""
    hits = [l for l in _agentcore_code_lines() if l.startswith("_bad_digest()")]
    assert len(hits) == 1, hits
    return hits[0]


def _image_ref_lines():
    """The two runtime lines computing `IMAGE_REF` (the default, then the digest override)."""
    lines = _agentcore_code_lines()
    base = [l for l in lines if l.startswith("IMAGE_REF=")]
    override = [l for l in lines if l.startswith('[ -n "${IMAGE_DIGEST:-}" ] && IMAGE_REF=')]
    assert len(base) == 1 and len(override) == 1, (base, override)
    return base[0], override[0]


def test_the_tfvars_deploy_the_resolved_reference_not_a_raw_tag():
    """THE headline assertion of D-B3, made STRUCTURALLY.

    Read off the parsed tfvars heredoc rather than by grepping the file, and the forbidden shape is
    reconstructed from its parts rather than written out as a literal — this file's own prose
    discusses the old `<repo>:<tag>` form at length, and a substring check would be satisfied by
    that prose. (A guard defeated by its own comment has happened repeatedly in this project.)"""
    (line,) = [l for l in _tfvars_heredoc().split("\n") if l.startswith("container_image_uri=")]
    assert line == 'container_image_uri="$IMAGE_REF"', line
    # The tag-based reference must no longer be what is deployed. Built from parts so the literal
    # never appears in this file as a quotable string.
    forbidden = '"$ECR_REPO' + ":" + '$IMAGE_TAG"'
    assert forbidden not in line, line


def test_the_image_reference_prefers_a_digest_and_falls_back_to_the_tag():
    """Both halves pinned: the `@digest` form when a digest exists, the `:tag` form when not.

    The fallback is not a courtesy — a rollback validates its target from tag-keyed `Deployment`
    rows that carry NO digest, and a pre-E28B agent repo POSTs no digest at all. Removing it would
    make both undeployable."""
    base, override = _image_ref_lines()
    assert base == 'IMAGE_REF="$ECR_REPO:$IMAGE_TAG"', base
    assert 'IMAGE_REF="$ECR_REPO@$IMAGE_DIGEST"' in override, override


def test_the_image_reference_is_computed_AFTER_the_cross_account_copy():
    """Ordering, and it is load-bearing rather than stylistic.

    On a cross-account promote the digest that must be DEPLOYED is the one in the TARGET registry,
    and it does not exist until the copy has run. Computing `IMAGE_REF` before the copy would pin
    the SOURCE digest — which need not exist in the target at all, since a pull/tag/push through
    the docker daemon can re-serialize the manifest. That failure appears only on the
    cross-account path, the one with the least local evidence."""
    lines = _agentcore_code_lines()

    def index_of(prefix):
        hits = [i for i, l in enumerate(lines) if l.startswith(prefix)]
        assert hits, f"no executable line starting {prefix!r}"
        return hits[0]

    assert index_of('if [ "${SOURCE_ECR_REQUIRED:-}"') < index_of("IMAGE_REF="), lines
    # …and the tfvars are written after the reference is resolved, or they would carry nothing.
    assert index_of("IMAGE_REF=") < index_of("cat > deploy.auto.tfvars"), lines


def test_the_digest_guard_is_recorded_as_a_failure_not_a_bare_exit():
    """A refusal must leave the badge and the history agreeing (the E28A/T1b idiom). A bare
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
    the cross-account copy, and it must be positioned after the push it verifies."""
    lines = _agentcore_code_lines()
    calls = [i for i, l in enumerate(lines) if "describe-images" in l]
    assert len(calls) == 1, f"expected exactly one describe-images call, got {[lines[i] for i in calls]}"
    (call_idx,) = calls
    push_idx = next(i for i, l in enumerate(lines) if "docker push" in l)
    assert push_idx < call_idx, "the read-back must follow the push whose bytes it resolves"
    # It resolves into TARGET_DIGEST — i.e. it is a verification of what we wrote, and its result
    # is shape-checked before use rather than trusted.
    assert lines[call_idx].startswith("TARGET_DIGEST="), lines[call_idx]


def test_the_cross_account_copy_pulls_by_digest_when_one_exists():
    """A tag-based pull is a MUTABLE read on the promotion path: between the approval and this copy
    the source tag may point at different bytes, so it can copy an image the OWNER never approved.
    Both the digest form and the tag fallback are pinned."""
    lines = _agentcore_code_lines()
    (base,) = [l for l in lines if l.startswith("SRC_REF=")]
    (override,) = [l for l in lines if 'SRC_REF="$SOURCE_ECR_REPO@$IMAGE_DIGEST"' in l]
    assert base == 'SRC_REF="$SOURCE_ECR_REPO:$IMAGE_TAG"', base
    assert override.startswith('[ -n "${IMAGE_DIGEST:-}" ]'), override
    # …and the pull uses the resolved reference, never the raw tag again.
    (pull,) = [l for l in lines if l.startswith("docker pull ")]
    assert pull.startswith('docker pull "$SRC_REF"'), pull


@pytest.mark.parametrize(
    "digest,expect_exit",
    [
        (_GOOD_DIGEST, 0),
        # A wrong-but-WELL-FORMED digest cannot be detected offline and MUST be accepted — this
        # case exists to prove the guard checks SHAPE and does not accidentally pin one value.
        ("sha256:" + "de" * 32, 0),
        ("", 0),                                  # the legacy tag-only path
        ("sha256:" + "a" * 63, 1),                # TRUNCATED — one hex short
        ("sha256:" + "a" * 65, 1),                # one hex long
        ("sha256:" + "AB" * 32, 1),               # uppercase: registries emit lowercase
        ("ab" * 32, 1),                           # no algorithm prefix
        ("sha512:" + "a" * 64, 1),                # wrong algorithm
        ("None", 1),                              # what the AWS CLI prints for a missing image
        (_GOOD_DIGEST + "x", 1),                  # trailing junk
        (_GOOD_DIGEST + "; echo PWNED", 1),       # a shell metacharacter in a deployed reference
    ],
    ids=["well-formed", "well-formed-but-wrong-image", "empty-legacy", "truncated", "too-long",
         "uppercase", "no-prefix", "wrong-algo", "literal-None", "trailing-junk", "metachar"],
)
def test_the_digest_shape_guard_ACTUALLY_fires(digest, expect_exit):
    """EXECUTES the real guard. Static text cannot prove a `grep -E` pattern matches what it
    claims, and the failure mode here — a guard that never fires — is indistinguishable from a
    correct deploy.

    `IMAGE_DIGEST` is assigned NON-exported and the fragment runs under `sh -c`, matching
    production: this buildspec's only `export`s are the three AWS credential lines, so an
    env-prefix form would validate an environment production never provides."""
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
    # A metacharacter must never reach a shell that would run it.
    assert "PWNED" not in out.stdout, out.stdout
    if expect_exit:
        # Refused BEFORE any reference is built, and recorded as a failure.
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
    start = next(
        i for i, l in enumerate(lines) if l.startswith('if [ "${SOURCE_ECR_REQUIRED:-}"')
    )
    end = next(i for i, l in enumerate(lines) if l.startswith("IMAGE_REF="))
    assert start < end
    return "\n".join(lines[start:end])


@pytest.mark.parametrize(
    "digest,target_digest,pull_rc,expect_exit,expect_pull,expect_ref",
    [
        # The digest path: pull the APPROVED bytes by digest, deploy the TARGET registry's digest
        # (which may legitimately differ — the daemon can re-serialize the manifest).
        (_GOOD_DIGEST, "sha256:" + "cd" * 32, 0, 0,
         "src.example/agp@" + _GOOD_DIGEST, "tgt.example/agp@sha256:" + "cd" * 32),
        # The legacy tag-only path is unchanged: pull by tag, deploy by tag.
        ("", "unused", 0, 0, "src.example/agp:a-1-abc1234", "tgt.example/agp:a-1-abc1234"),
        # The target read-back cannot resolve — the CLI prints "None". Deploying would produce an
        # unresolvable reference, so the build must fail INSTEAD of reporting a deploy.
        (_GOOD_DIGEST, "None", 0, 1, "src.example/agp@" + _GOOD_DIGEST, None),
        (_GOOD_DIGEST, "sha256:zz", 0, 1, "src.example/agp@" + _GOOD_DIGEST, None),
        # The pull itself failing must still be recorded, not fall through to a deploy.
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

    Variables are NON-exported under `sh -c` — the production condition."""
    import subprocess

    script = (
        "_st() { echo ST:$1; }\n"
        "_dep() { echo DEP:$1; }\n"
        f"{_bad_digest_helper()}\n"
        # `docker`: record the pull reference, honour the injected rc, no-op otherwise.
        f'docker() {{ case "$1" in pull) echo "PULLED:$2"; return {pull_rc};; '
        'push) echo "PUSHED:$2"; return 0;; *) return 0;; esac; }\n'
        # `aws`: sts assume-role emits three tab-separated creds; describe-images emits the
        # target digest under test; everything else succeeds silently.
        'aws() { for a in "$@"; do case "$a" in '
        f'describe-images) printf "%s\\n" "{target_digest}"; return 0;; '
        'assume-role) printf "k\\ts\\tt\\n"; return 0;; '
        'get-login-password) echo pw; return 0;; esac; done; return 0; }\n'
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
    """D-B3 is explicit that the digest fields sit ALONGSIDE the tag fields, never replacing them.

    The tag is what every existing surface renders, what a rollback validates against, and what a
    rollback to pre-E28B code needs in order to deploy at all — a digest-only record would be
    undeployable by the previous release."""
    from models.repository import Repository

    for field in ("last_dev_image_tag", "last_promoted_image_tag", "prod_candidate_image_tag"):
        assert field in Repository.model_fields, field


# --------------------------------------------------------------------------- #
# E28B/T4 fix round 1 — the target read-back must run IN THE TARGET ACCOUNT
# --------------------------------------------------------------------------- #
#
# The read-back's first version queried the WRONG ACCOUNT, and it was statically determinable:
#
# 1. This branch runs under the AMBIENT CodeBuild (tooling-account) role. The pre_build assume-role
#    is gated on `IAC_TYPE != "agentcore_runtime"`, so it never fires on this path, and `_lg`'s
#    credentials are an INLINE ENV PREFIX — which applies to ONE command and is unset immediately
#    after (verified in a real shell). `docker push` still succeeds only because `docker login`
#    persists to ~/.docker/config.json, which is precisely what hid the defect.
# 2. Without `--registry-id` the CLI reads the CALLER's registry. The tooling account owns a
#    SAME-NAMED repo (`modules/agent_ecr` → `<prefix>-agent-images`), so the usual outcome is
#    RepositoryNotFound → a failed promote, and the worse one is a same-tag HIT resolving a FOREIGN
#    digest, pinned onto $ECR_REPO, failing at ImagePull *after* a deploy was recorded.
#
# Both halves are required: `--registry-id` alone still needs the target account's credentials.


def _describe_images_call():
    """The single runtime line resolving TARGET_DIGEST, from `yaml.safe_load`."""
    hits = [l for l in _agentcore_code_lines() if l.startswith("TARGET_DIGEST=")]
    assert len(hits) == 1, hits
    return hits[0]


def test_the_target_read_back_names_the_target_REGISTRY_ID():
    """Structural half of the fix. The account id is derived from `$ECR_REPO`'s own host, never
    hardcoded (a literal account id is forbidden project-wide)."""
    line = _describe_images_call()
    assert '--registry-id "${ECR_REPO%%.*}"' in line, line
    # …and it is still scoped to the target REGION as well as the target account.
    assert '--region "$AWS_TARGET_REGION"' in line, line


def test_the_target_read_back_runs_under_ASSUMED_TARGET_credentials():
    """Credential half of the fix, and the half `--registry-id` cannot cover: a cross-account
    `describe-images` needs the target account's credentials regardless of the registry id."""
    line = _describe_images_call()
    assert "_with_target aws ecr describe-images" in line, line
    (helper,) = [l for l in _agentcore_code_lines() if l.startswith("_with_target()")]
    assert '--role-arn "$TARGET_ROLE_ARN"' in helper, helper
    # A SUBSHELL, so the exported credentials cannot leak into the terraform apply that follows.
    # Asserted on the body AFTER the function header — `removeprefix`, not `lstrip`, which strips a
    # CHARACTER SET and would have eaten into the body itself.
    assert "export AWS_ACCESS_KEY_ID=" in helper, helper
    body = helper.removeprefix("_with_target() {").strip()
    assert body.startswith("("), helper
    # …and the command is run INSIDE that subshell, forwarded whole rather than re-quoted.
    assert '"$@"' in helper, helper


def test_the_credential_helper_does_not_leak_into_the_terraform_apply():
    """EXECUTED. If `_with_target` exported into the calling shell, every later `aws`/`terraform`
    call in this build would run as the TARGET account — silently redirecting the state-bucket and
    registry reads that must stay in the tooling account. The subshell is what prevents that, and
    only running it proves the boundary holds."""
    import subprocess

    (helper,) = [l for l in _agentcore_code_lines() if l.startswith("_with_target()")]
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
        (True, True, 0),     # the fix: target creds + target registry id
        (False, True, 1),    # the ORIGINAL defect's credential half
        (True, False, 1),    # the ORIGINAL defect's registry half
    ],
    ids=["target-creds-and-registry", "no-target-creds", "wrong-registry"],
)
def test_the_read_back_FAILS_if_it_queries_the_wrong_account(
    creds_present, registry_id_correct, expect_exit
):
    """EXECUTES the block against a registry stub that ANSWERS ONLY the target account.

    This is the harness the first version lacked: the previous `aws` stub returned the target digest
    unconditionally, so it could not tell a correctly-scoped call from one aimed at the tooling
    account — a stub more generous than reality, which is why the defect passed 5/5 green.

    Here the stub inspects BOTH the credentials in its environment AND `--registry-id`, and returns
    the digest only when both name the target. The two failure rows are the original defect, each
    half in isolation: both must fail the build rather than deploy an unnamed or foreign image."""
    import subprocess

    lines = _agentcore_code_lines()
    (helper,) = [l for l in lines if l.startswith("_with_target()")]
    call = _describe_images_call()
    if not creds_present:
        # Simulate the ORIGINAL code: the call runs with NO assumed credentials.
        call = call.replace("_with_target aws ecr", "aws ecr")
    if not registry_id_correct:
        # Simulate the ORIGINAL code: no --registry-id, so the caller's own registry is queried.
        call = call.replace('--registry-id "${ECR_REPO%%.*}" ', "")

    target_digest = "sha256:" + "cd" * 32
    script = (
        "_st() { echo ST:$1; }\n"
        "_dep() { echo DEP:$1; }\n"
        f"{_bad_digest_helper()}\n"
        # A registry that serves ONLY the target account, and only when addressed as such.
        # `sts assume-role` hands out the target creds; `describe-images` demands them AND the
        # matching --registry-id, otherwise it behaves like ECR does: RepositoryNotFound → rc 254
        # with nothing on stdout.
        "aws() { case \"$*\" in\n"
        '  *assume-role*) printf "TGTKEY\\ts\\tt\\n"; return 0;;\n'
        "  *describe-images*)\n"
        '    case "$*" in *"--registry-id tgtacct"*) ;; *) return 254;; esac\n'
        '    [ "${AWS_ACCESS_KEY_ID:-}" = TGTKEY ] || return 254\n'
        f'    printf "%s\\n" "{target_digest}"; return 0;;\n'
        '  *) return 0;; esac; }\n'
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


# --- the module's own image-reference validation, pinned by BEHAVIOUR --------------------------- #
#
# `var.container_image_uri`'s validation is the module-side backstop for "name a specific image".
# It is asserted by EVALUATING the real condition rather than by matching its text: a regex is
# exactly the kind of expression that reads correct and admits the wrong strings, and two versions
# of this one did. Both bugs are rows in the table below.


def _container_uri_condition():
    """The real `condition` expression from the module's own `variable` block, comments stripped."""
    body = (_RUNTIME_MODULE / "variables.tf").read_text()
    code = "\n".join(l for l in body.split("\n") if not l.strip().startswith("#"))
    block = code.split('variable "container_image_uri"', 1)[1]
    # Cut at the NEXT variable — without this the split runs to end-of-file and picks up every
    # later variable's `condition` too, which is a `too many values to unpack` rather than a silent
    # wrong answer, but only by luck.
    if "\nvariable " in block:
        block = block[: block.index("\nvariable ")]
    (line,) = [l.strip() for l in block.split("\n") if l.strip().startswith("condition")]
    return line.split("=", 1)[1].strip()


@pytest.mark.parametrize(
    "uri,accepted",
    [
        # The digest form — what a promote deploys.
        ("acct.dkr.ecr.us-east-1.amazonaws.com/agp-agent-images@" + _GOOD_DIGEST, True),
        # The tag form — a rollback and any pre-digest caller.
        ("acct.dkr.ecr.us-east-1.amazonaws.com/agp-agent-images:a-1-abc1234", True),
        ("registry.example/agp:strands-latest", True),
        # Names no image at all: `<repo>` alone silently means `:latest`, which on a SHARED tenant
        # registry is almost certainly some other agent's image.
        ("registry.example/agp", False),
        # BUG 1 (found while writing this): an unanchored tag alternative matched the TAIL of a
        # malformed digest, so this passed as though `:zz` were a tag.
        ("registry.example/agp@sha256:zz", False),
        ("registry.example/agp@sha256:" + "b" * 65, False),
        ("registry.example/agp@garbage", False),
        ("registry.example/agp:tag@sha256:zz", False),
        # BUG 2 (review Minor): a permissive tag class admitted shell metacharacters. Inert today
        # (the value is only ever written into a quoted heredoc, never shell-evaluated) but a
        # validation whose job is "name a specific image" must not be what relies on that.
        ("registry.example/agp:tag; echo PWNED", False),
        ("registry.example/agp:a$(id)", False),
        ("registry.example/agp:`id`", False),
        ("registry.example/agp:tag with space", False),
        # ECR forbids a leading hyphen on a tag.
        ("registry.example/agp:-leading", False),
        # BUG 3 (found by MUTATION — dropping the digest branch's `^` anchor survived the table
        # above, so these are the rows that give it teeth). All three are references that name
        # something other than one image:
        ("registry.example/agp@evil@sha256:" + "b" * 64, False),   # two `@` — which host applies?
        ("@sha256:" + "b" * 64, False),                            # no repository at all
        # A tag AND a digest together: ambiguous about which one is deployed.
        ("registry.example/agp:tag@sha256:" + "b" * 64, False),
        # A repository part that is not a repository.
        ("junk stuff@sha256:" + "b" * 64, False),
    ],
    ids=["digest", "tag", "tag-latest-style", "no-tag-no-digest", "malformed-digest-as-tag",
         "over-long-digest", "garbage-digest", "tag-plus-bad-digest", "semicolon", "dollar-subst",
         "backtick", "space", "leading-hyphen", "double-at", "empty-repo", "tag-and-digest",
         "space-in-repo"],
)
def test_the_container_uri_validation_admits_only_a_named_image(tmp_path, uri, accepted):
    """EVALUATES the module's real condition through `terraform plan`.

    The condition is read OUT of `variables.tf` and dropped into a throwaway root, so this cannot
    pass if the real expression changes — and unlike a text match it fails when the expression is
    still *shaped* right but accepts the wrong strings, which is how both bugs above survived."""
    import shutil
    import subprocess

    if shutil.which("terraform") is None:
        pytest.skip("LOUD SKIP: terraform is not installed, so the real HCL condition cannot be "
                    "evaluated and this validation is unverified here.")

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
        cwd=tmp_path, capture_output=True, timeout=300, check=True,
    )
    out = subprocess.run(
        ["terraform", "plan", "-input=false", f"-var=container_image_uri={uri}"],
        cwd=tmp_path, capture_output=True, text=True, timeout=300,
    )
    assert (out.returncode == 0) is accepted, (
        f"{uri!r}: expected {'accept' if accepted else 'refuse'}, "
        f"got rc={out.returncode}\n{out.stdout}\n{out.stderr}"
    )


# --------------------------------------------------------------------------- #
# E28C/T5 (D-C5) — the two GRANTS that make the Langfuse + reclaim fixes real  #
# --------------------------------------------------------------------------- #
#
# Both fixes in T5 are half code and half IAM, and the IAM half lives in a file the code never
# imports. That is the same silent-drift class every guard above exists for, and it has already
# produced two live defects of exactly this shape: an `iam:DeleteRole` grant scoped to a name the
# reclaim could never target (so the reclaim was denied forever), and a runtime exec role with no
# Secrets Manager grant at all (so the container could not read the key the platform provisioned).
# Neither raised anywhere. Both are asserted here by MATCHING the real derived name against the
# real policy pattern, not by restating either.

_ECS_MODULE = Path(__file__).resolve().parents[2] / "infrastructure/modules/ecs"


def _iam_resource_patterns(body: str, sid: str) -> list[str]:
    """The `Resource` value(s) of the statement carrying `Sid = "<sid>"`, comments stripped.

    Reads the REAL policy text rather than a restatement, and keys off the Sid because statement
    ORDER in a jsonencode list is not a contract."""
    code = "\n".join(l for l in body.split("\n") if not l.strip().startswith("#"))
    block = code.split(f'Sid    = "{sid}"', 1)[1]
    # Cut at the end of this statement object — the next `Sid` or the closing of the list.
    for stop in ("Sid    =", "\n  })"):
        if stop in block:
            block = block.split(stop, 1)[0]
    return re.findall(r'Resource\s*=\s*"([^"]+)"', block)


def _iam_actions(body: str, sid: str) -> set[str]:
    """The `Action` entries of the statement carrying `Sid = "<sid>"`."""
    code = "\n".join(l for l in body.split("\n") if not l.strip().startswith("#"))
    block = code.split(f'Sid    = "{sid}"', 1)[1]
    for stop in ("Sid    =", "\n  })"):
        if stop in block:
            block = block.split(stop, 1)[0]
    return set(re.findall(r'"(iam:[A-Za-z]+)"', block))


_ACCOUNT = "000000000000"  # a stand-in; the real one is NEVER written down (hard project rule)
_REGION = "us-east-1"


def _resolved(pattern: str) -> str:
    """A policy `Resource` with the module's data-source interpolations substituted.

    The account and region reach these ARNs through `data.aws_caller_identity` /
    `data.aws_region` — never as literals — so the raw pattern is not a comparable ARN and a test
    matching against it unsubstituted fails for a reason that has nothing to do with the grant."""
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
    """THE DEFECT THIS CLOSES, asserted as the match it always should have been.

    E28A/T2 gave the backend an exec-role reclaim and the grant to run it was assumed to exist. It
    did not: the only `iam:DeleteRole` resource was `role/{prefix}-ecr-push-*`, which cannot match
    `{agent}-{stage}-agentcore-exec` under any inputs. The live answer was therefore ALWAYS
    AccessDenied, the code swallowed it as success, and six account-global roles leaked behind
    clean teardown reports.

    Both ends are REAL here — the name comes from the single Python producer, the pattern from the
    real module — so this fails if either drifts, which is the only way a name/grant pair can be
    kept honest across two files that never reference each other."""
    from services.project_service import agentcore_exec_role_name

    body = (_ECS_MODULE / "main.tf").read_text()
    patterns = _iam_resource_patterns(body, "ReclaimAgentcoreExecRoles")
    assert patterns, "the reclaim statement must exist and carry a Resource"

    for stage in ("dev", "prod", "uat"):
        role_arn = f"arn:aws:iam::{_ACCOUNT}:role/{agentcore_exec_role_name('my_agent', stage)}"
        assert any(_matches_iam_pattern(p, role_arn) for p in patterns), (stage, patterns)


def test_the_reclaim_grant_ALSO_covers_the_pre_T1b_UN_STAGE_SCOPED_name():
    """The cascade always attempts the legacy un-stage-scoped name too (the migration case — every
    repo deployed before T1b has a role there, and orphans exist in the account right now). A
    pattern covering only the stage-scoped shape would leave those denied forever, which is finding
    #9 surviving its own fix in a third form."""
    from services.project_service import legacy_agentcore_exec_role_name

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
    """Scope fence — added because a mutation proved it was missing.

    `Resource = "*"` satisfies every match assertion above while handing the ECS task role
    `iam:DeleteRole` over EVERY role in the account, including its own execution role and the
    Terraform deploy roles. "The reclaim now works" and "the reclaim is least-privilege" are
    different claims, and the match tests only prove the first. The `-agentcore-exec` suffix is the
    namespace, so the pattern must be anchored on it and must not match an unrelated role."""
    body = (_ECS_MODULE / "main.tf").read_text()
    patterns = _iam_resource_patterns(body, "ReclaimAgentcoreExecRoles")
    assert patterns
    for pattern in patterns:
        assert pattern != "*"
        assert pattern.endswith("-agentcore-exec"), pattern
    # Concretely: roles this grant must NOT be able to delete.
    for other in (
        f"arn:aws:iam::{_ACCOUNT}:role/agp-ecs-task",
        f"arn:aws:iam::{_ACCOUNT}:role/OrganizationAccountAccessRole",
        f"arn:aws:iam::{_ACCOUNT}:role/agp-terraform-deploy",
    ):
        assert not any(_matches_iam_pattern(p, other) for p in patterns), (other, patterns)


def test_the_ecr_push_grant_STILL_cannot_reach_an_agentcore_exec_role():
    """Non-vacuity for the test above, and a scope fence for the statement that was already there.

    The E22 statement is a SEPARATE, broader grant (it creates and re-trusts per-org push roles),
    and widening ITS resource to cover the exec roles would have been the lazy fix — handing the
    backend `CreateRole`/`UpdateAssumeRolePolicy` over every runtime exec role. It must stay unable
    to reach them. This also proves the reclaim statement is doing real work rather than
    duplicating a match that already existed."""
    from services.project_service import agentcore_exec_role_name

    body = (_ECS_MODULE / "main.tf").read_text()
    patterns = _iam_resource_patterns(body, "ManageEcrPushRoles")
    assert patterns
    exec_arn = f"arn:aws:iam::{_ACCOUNT}:role/{agentcore_exec_role_name('my_agent', 'dev')}"
    assert not any(_matches_iam_pattern(p, exec_arn) for p in patterns), patterns


def test_the_runtime_exec_role_CAN_READ_the_langfuse_secret_the_platform_writes():
    """The other half of D-C5. The backend provisions a per-agent Langfuse key into Secrets Manager
    and passes only its NAME to the container, which resolves the value itself — so without a
    `secretsmanager:GetSecretValue` grant on that name the agent AccessDenies into exactly the
    silent zero the provisioning was added to fix.

    The expected name comes from the real producer (`LangfuseProvisioningService._agent_secret_name`),
    so this is a cross-file match and not two copies of a literal agreeing with each other."""
    from services.langfuse_provisioning import LangfuseProvisioningService
    from types import SimpleNamespace

    secret_name = LangfuseProvisioningService._agent_secret_name(
        SimpleNamespace(_agp_project_name="agp"), SimpleNamespace(id="rec-abc123")
    )
    body = (_RUNTIME_MODULE / "main.tf").read_text()
    code = "\n".join(l for l in body.split("\n") if not l.strip().startswith("#"))
    (pattern,) = re.findall(
        r'Action\s*=\s*"secretsmanager:GetSecretValue"\s*\n\s*Resource\s*=\s*"([^"]+)"', code
    )
    # Secrets Manager appends its own 6-character random suffix to every secret ARN, so the
    # concrete ARN the container asks for is never just the name.
    secret_arn = f"arn:aws:secretsmanager:{_REGION}:{_ACCOUNT}:secret:{secret_name}-AbCdEf"
    assert _matches_iam_pattern(pattern, secret_arn), pattern


def test_the_runtime_exec_role_secret_grant_is_NOT_a_blanket_wildcard():
    """Scope fence. `Resource = "*"` would satisfy the test above while letting every agent runtime
    in the account read the platform's Graph client secret and every tenant credential. The grant
    has to be namespaced to the secrets the Langfuse provisioner writes."""
    body = (_RUNTIME_MODULE / "main.tf").read_text()
    code = "\n".join(l for l in body.split("\n") if not l.strip().startswith("#"))
    (pattern,) = re.findall(
        r'Action\s*=\s*"secretsmanager:GetSecretValue"\s*\n\s*Resource\s*=\s*"([^"]+)"', code
    )
    assert pattern != "*"
    assert "langfuse-agent-" in pattern, pattern
    # And it must not be able to read the platform's own Graph client secret.
    other = f"arn:aws:secretsmanager:{_REGION}:{_ACCOUNT}:secret:entra-backend-client-secret-AbCdEf"
    assert not _matches_iam_pattern(pattern, other), pattern


def test_the_runtime_exec_role_CARRIES_the_agentcore_identity_TOKEN_ACTIONS():
    """E36/T4 (item 8). An agent that makes an outbound OBO call asks the workload identity for a
    token, and all three of these are needed: `GetWorkloadAccessToken` /
    `...ForJWT` for the workload identity itself and `GetResourceOauth2Token` for an OAuth2
    credential provider in the token vault. The platform-provisioned role carried NO
    `bedrock-agentcore:*` action of any kind, so that call AccessDenied — invisibly to the deploy,
    which comes up green either way.

    Nothing in this repo can currently reach that denial: the scaffold template ships no OBO code and
    the acme agents run on roles hand-augmented outside Terraform (their own `deploy.sh` attaches
    exactly these three actions). This assertion IS the guard, so it also has to be un-fakeable —
    hence comments are stripped before matching, the same way the stage-scoping guards are, and the
    resources are matched as IAM patterns against concrete ARNs rather than as substrings."""
    body = (_RUNTIME_MODULE / "main.tf").read_text()
    code = "\n".join(l for l in body.split("\n") if not l.strip().startswith("#"))
    # The statement, isolated — and BOTH lists come out of the SAME match, so the action set below
    # is the statement's OWN and not the file's. A mutation proved the whole-file read this replaces
    # was fakeable: moving one action into a separate `{ Effect = "Allow", Action = "…",
    # Resource = "*" }` one-liner — this module's dominant statement idiom, cf. the four above —
    # left the whole-file set intact while the split-out grant was seen by neither the unpack below
    # nor the scope fence, which only ever inspects this statement's `Resource` list.
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
    # And the statement above is the ONLY place the module may hand this role a `bedrock-agentcore`
    # action, in ANY form — so a second, wildcard-scoped grant added elsewhere fails here instead of
    # riding along unfenced. The opening `"` anchor is what keeps the four `arn:aws:bedrock-agentcore:…`
    # resources of this same statement out of the count.
    assert len(re.findall(r'"bedrock-agentcore:', code)) == 3, re.findall(
        r'"bedrock-agentcore:[A-Za-z0-9]*', code
    )

    patterns = re.findall(r'"([^"]+)"', block)
    # Both the vault/directory itself and its children: some of these calls target the container,
    # others a resource inside it, and a pattern ending at `default` matches only the former.
    for arn in (
        f"arn:aws:bedrock-agentcore:{_REGION}:{_ACCOUNT}:token-vault/default",
        f"arn:aws:bedrock-agentcore:{_REGION}:{_ACCOUNT}:token-vault/default/oauth2credentialprovider/agp-obo",
        f"arn:aws:bedrock-agentcore:{_REGION}:{_ACCOUNT}:workload-identity-directory/default",
        f"arn:aws:bedrock-agentcore:{_REGION}:{_ACCOUNT}:workload-identity-directory/default/workload-identity/my_agent_dev",
    ):
        assert any(_matches_iam_pattern(p, arn) for p in patterns), (arn, patterns)
    # Scope fence: `Resource = "*"` would satisfy every match above while handing every agent runtime
    # in the account every token vault and directory the account will ever hold.
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
    code = "\n".join(l for l in body.split("\n") if not l.strip().startswith("#"))
    assert 'data "aws_caller_identity" "current"' in code
    assert 'data "aws_region" "current"' in code
    assert "data.aws_caller_identity.current.account_id" in code
    # No 12-digit literal anywhere in the module's real code.
    assert not re.search(r"\b\d{12}\b", code), re.findall(r"\b\d{12}\b", code)


# --------------------------------------------------------------------------- #
# GitHub is a PLATFORM capability, not a Terraform deploy-time object
# --------------------------------------------------------------------------- #
#
# The product decision: `terraform apply` ships ZERO GitHub artifacts, and the backend creates the
# account-global OIDC provider (plus the shared ECR-push role, whose trust names it) on the FIRST
# GitHub connection. Two things make that decision enforceable rather than aspirational, and both
# are cross-file matches of exactly the kind the guards above exist for:
#
#   1. Terraform must not reintroduce a GitHub resource or data source. A data source is the sneaky
#      one — it fails apply on a provider-less account just as surely as a resource does, while
#      looking like a read.
#   2. The names the backend derives must match the IAM grants the ECS task role carries, and the
#      shared role's name must match what the RETIRED Terraform resource produced — that name is
#      what makes an already-deployed account's live role get ADOPTED instead of duplicated beside
#      it, so a drifted name orphans a live role that repos are still pushing through. Terraform
#      forgot that role in an earlier apply and now declares NO resource that could name it; see
#      test_no_terraform_resource_can_produce_the_shared_push_role_AT_ALL, which supersedes the
#      `removed { destroy = false }` assertion this group used to carry.

_INFRA = Path(__file__).resolve().parents[2] / "infrastructure"


def _tf_code(path: Path) -> str:
    """A .tf file's real code, comments stripped (a comment MENTIONING a resource is fine)."""
    return "\n".join(
        l for l in path.read_text().split("\n") if not l.strip().startswith("#")
    )


def test_terraform_declares_NO_github_oidc_provider_anywhere():
    """The fresh-apply guarantee, asserted over the whole tree. Both a `resource` and a `data`
    block are forbidden: the data source is what the retired toggle used for its false branch, and
    it fails apply on an account that never onboarded GitHub Actions — which is precisely the
    customer this change exists for."""
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
    and the root builds the ECS env var from the same literal. All three ends are REAL here — the
    Python producer, the root local, and the ECS IAM grant — so this fails if any drifts."""
    from services.ecr_push_role_service import EcrPushRoleService

    svc = EcrPushRoleService(
        role_name_prefix="agp", oidc_provider_arn="x", ecr_repository_arn="y", iam_client=object()
    )
    name = svc.shared_role_name()
    assert name == "agp-agent-ecr-push", name

    # The root's deterministic ARN string resolves to that same name.
    root = _tf_code(_INFRA / "main.tf")
    (arn_expr,) = re.findall(r'agent_ecr_push_role_arn\s*=\s*"([^"]+)"', root)
    assert arn_expr.replace("${local.name_prefix}", "agp").endswith(f":role/{name}"), arn_expr

    # And the ECS task role can actually manage it.
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
    match test above while letting the backend mint any role in the account."""
    body = (_ECS_MODULE / "main.tf").read_text()
    for pattern in _iam_resource_patterns(body, "ManageSharedEcrPushRole"):
        assert "*" not in pattern.split(":role/")[1], pattern
    # Concretely: it must not reach the per-org roles' namespace or an unrelated role.
    patterns = _iam_resource_patterns(body, "ManageSharedEcrPushRole")
    for other in (
        f"arn:aws:iam::{_ACCOUNT}:role/agp-ecr-push-acme",
        f"arn:aws:iam::{_ACCOUNT}:role/OrganizationAccountAccessRole",
    ):
        assert not any(_matches_iam_pattern(p, other) for p in patterns), other


def test_the_oidc_provider_grant_MATCHES_the_arn_the_backend_derives():
    """The provider bootstrap's grant, matched against the ARN the Python producer computes. The
    deterministic ARN is the load-bearing part of the whole design (it is what lets Terraform pass
    a string with no resource behind it), so a drift here breaks the bootstrap silently — the
    backend would ask about one ARN while the grant covers another."""
    from services.github_oidc_provider_service import github_oidc_provider_arn

    body = (_ECS_MODULE / "main.tf").read_text()
    patterns = _iam_resource_patterns(body, "ManageGithubOidcProvider")
    assert patterns, "the provider bootstrap statement must exist"
    assert any(
        _matches_iam_pattern(p, github_oidc_provider_arn(_ACCOUNT)) for p in patterns
    ), patterns

    # The root passes that same derived ARN to ECS as a plain string — no resource reference.
    root = _tf_code(_INFRA / "main.tf")
    (arn_expr,) = re.findall(
        r'github_oidc_provider_arn\s*=\s*"(arn:aws:iam[^"]+)"', root
    )
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
        for block in re.finditer(
            r'resource\s+"' + kind + r'"\s+"([^"]+)"\s*\{(.*?)\n\}', code, re.DOTALL
        ):
            label, body = block.group(1), block.group(2)
            m = re.search(rf"^\s*{arg}\s*=\s*(.+?)\s*$", body, re.MULTILINE)
            assert m, f'{path}: {kind}."{label}" has no {arg} argument'
            expr = m.group(1)
            role_ref = re.fullmatch(r"aws_iam_role\.([A-Za-z0-9_]+)\.(?:id|name)", expr)
            if role_ref:
                # Attached to a role this config declares, so it can only ever reach the name
                # that role's own `name` produces — chase it there.
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
                "[^\"]*" if part.startswith("${") else re.escape(part)
                for part in re.split(r"(\$\{[^}]*\})", literal)
                if part
            )
            out.append((path.relative_to(_INFRA).as_posix(), f'{kind}."{label}"', pattern))
    return out


def test_no_terraform_resource_can_produce_the_shared_push_role_AT_ALL():
    """What actually keeps the live shared push role alive today — read this before "restoring"
    anything.

    THE PROPERTY IS UNCHANGED: `<prefix>-agent-ecr-push` is a LIVE role that GitHub Actions
    workflows assume on every agent build, and no `terraform apply` may ever destroy it.

    THE MECHANISM CHANGED, TWICE. E32 removed `modules/agent_ecr`'s role resource and put two
    `removed { lifecycle { destroy = false } }` blocks in the root to make Terraform FORGET the
    live role instead of deleting it, and this test used to assert those blocks existed. E32/T6
    then deleted the blocks, and that is the state this test now pins — because a `removed` block
    only does anything while the address is still IN STATE. It was verified absent from
    `terraform.tfstate` (serial 817: no `module.agent_ecr.aws_iam_role.github_push`, no
    `…aws_iam_role_policy.github_push`), while the live role itself was confirmed to still exist
    in IAM. An earlier apply had already forgotten it, so the blocks were no-ops by then.

    WHY THE GUARANTEE IS NOW STRONGER THAN THOSE BLOCKS WERE, and why re-adding them would be
    strictly worse: `removed` is a one-shot instruction that has to be spelled correctly, kept in
    the config, and eventually cleaned up. What replaces it is STRUCTURAL — Terraform declares NO
    resource that could ever be named `<prefix>-agent-ecr-push`, in any module, so the address
    cannot re-enter state and there is nothing to destroy. This test proves that over the WHOLE
    tree by resolving every `aws_iam_role`'s name expression, rather than trusting the one module
    the role used to live in. `aws_iam_role_policy` is covered for the same reason it needed a
    `removed` block: it takes the role by NAME, so a policy resource pointed at the adopted role
    would let an apply delete the inline permissions `ensure_shared_role` writes — the role would
    survive and every push would start failing on authorization instead.

    The role's real owner is `services/ecr_push_role_service.ensure_shared_role()`, which
    CREATE-or-ADOPTS it by that exact name and holds no delete path at all
    (`test_the_shared_role_grant_carries_the_verbs_the_ensure_uses_and_NO_delete` fences the IAM
    grant, `test_the_shared_push_role_name_MATCHES_the_terraform_resource_it_adopts` fences the
    name). Terraform's only remaining involvement is handing the backend a deterministic ARN
    STRING, asserted below: a string has no resource behind it, which is what lets a fresh apply
    succeed on an account that has never connected GitHub."""
    from services.ecr_push_role_service import EcrPushRoleService

    shared = EcrPushRoleService(
        role_name_prefix="agp", oidc_provider_arn="x", ecr_repository_arn="y", iam_client=object()
    ).shared_role_name()

    roles = _tf_name_patterns("aws_iam_role", "name")
    policies = _tf_name_patterns("aws_iam_role_policy", "role")
    # Non-vacuity, and proof the resolver resolves: the tree really does declare roles, and the
    # RETIRED resource's own expression — `"${var.name_prefix}-agent-ecr-push"` — is what this
    # would catch if anyone reintroduced it.
    assert len(roles) >= 6, roles
    assert re.fullmatch("[^\"]*" + re.escape("-agent-ecr-push"), shared), shared

    for where, addr, pattern in roles + policies:
        assert not re.fullmatch(pattern, shared), f"{where}: {addr} can produce {shared}"

    # And Terraform's only remaining hand in it: a deterministic STRING, no resource reference.
    root = _tf_code(_INFRA / "main.tf")
    (arn_expr,) = re.findall(r'agent_ecr_push_role_arn\s*=\s*"([^"]+)"', root)
    assert arn_expr.endswith(f":role/${{local.name_prefix}}{shared[len('agp'):]}"), arn_expr
    (passed,) = re.findall(r"project_ecr_push_role_arn\s*=\s*(\S+)", root)
    assert passed == "local.agent_ecr_push_role_arn", passed


# --------------------------------------------------------------------------- #
# AGENT_REGISTRY_ID emptiness — the one unguarded load-bearing variable (E32 fix)
# --------------------------------------------------------------------------- #
#
# The registry id stopped being a Terraform-supplied value: AWS mints registry ids, there is no
# Terraform resource for the `agent-registry` namespace, and reading one back from a plan-time
# capture file is what used to make a from-zero deploy need TWO applies. So `modules/codebuild`
# declares `AGENT_REGISTRY_ID` EMPTY and the real id arrives per build, as an
# `environmentVariablesOverride` from `RuntimeBuildService.start_runtime_build`.
#
# That is correct for every build the platform starts, and for every `retry-build` of one (retry
# replays the original overrides). It leaves exactly one reachable gap: a hand-run
# `aws codebuild start-build` that supplies `IAC_TYPE=agentcore_runtime` plus the `GIT_*` wiring
# but forgets the id. Because this buildspec has NO `set -e`, an empty id does not abort — it
# cascades: `get-registry-record --registry-id ""` fails non-fatally, every `jq` read of the empty
# result yields "" at rc 0, `terraform apply` RUNS, and an AgentCore runtime goes LIVE with an
# empty model id. The `NEW_ENV` guard downstream then reports "LIVE but UNTRACKED" — it detects the
# worst outcome this system can produce (a live, invocable, billing runtime whose ARN is recorded
# nowhere, which the E23 delete cascade can never reclaim) but cannot prevent it, because the apply
# already happened.
#
# These tests pin the fail-fast guard that closes it, and — the half that actually matters — pin
# its POSITION ahead of the first registry call and any `terraform apply`. A guard that exists but
# sits below the apply is worth nothing here.


def _registry_id_guard_line():
    """The runtime line that fails the build when `AGENT_REGISTRY_ID` is empty.

    Read out of `_agentcore_branch()` (`yaml.safe_load`), never off stripped file lines, and
    comments dropped first — the guard's own comment block explains at length that it refuses
    "BEFORE anything is provisioned", so a substring search over the raw body finds that PROSE and
    passes with no guard present at all. Five guards in this epic were defeated by their own
    comments; this one is asserted off code only."""
    hits = [
        l.strip()
        for l in _agentcore_branch().split("\n")
        if not l.strip().startswith("#") and 'z "$AGENT_REGISTRY_ID"' in l
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
    actionable is worth nothing if it degrades to "AGENT_REGISTRY_ID is empty"."""
    line = _registry_id_guard_line()
    assert "environmentVariablesOverride" in line, line
    assert "start_runtime_build" in line, line
    assert "--environment-variables-override" in line, line
    assert "list-registries" in line, line
    # And it must say WHY it refuses, not merely that it did.
    assert "BEFORE anything is provisioned" in line, line


def test_the_registry_id_guard_runs_BEFORE_the_first_registry_call_and_ANY_apply():
    """The load-bearing half. Position, off executable lines only.

    A guard below `get-registry-record` is pointless (the empty-id read has already happened and
    silently produced ""), and a guard below `terraform apply` is worse than pointless — the
    runtime is already live and unreclaimable, which is precisely the outcome the guard exists to
    make impossible. So it must be the FIRST executable line in the branch: everything above it
    cannot create a resource, everything below it can."""
    lines = _agentcore_code_lines()
    guard = _registry_id_guard_line()

    assert lines[0] == "agentcore_runtime)", lines[0]
    assert lines[1] == guard, (
        "the emptiness guard must be the first executable statement in the branch, "
        f"but that slot holds: {lines[1]}"
    )

    def index_of(needle):
        hits = [i for i, l in enumerate(lines) if needle in l]
        assert hits, f"no executable line containing {needle!r}"
        return hits[0]

    guard_at = lines.index(guard)
    # Non-vacuity: both of these really are in this branch, so the ordering is a real comparison.
    assert guard_at < index_of("agent-registry-control get-registry-record")
    assert guard_at < index_of("terraform apply")


def test_the_project_declares_the_registry_id_EMPTY_which_is_what_needs_the_guard():
    """Pins the other side of the contract across the file boundary.

    The guard is only load-bearing because the project-level value is `""`. If someone
    "helpfully" re-baked a real id into Terraform, they would reintroduce the plan-time capture
    file and the second apply this epic deleted — and the stale-id failure mode (write-back lands
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


# --------------------------------------------------------------------------- #
# E36/T9 — the per-build scratch clone token is PURGED, not accumulated
# --------------------------------------------------------------------------- #
#
# `GIT_SECRET_ARN` is a StartBuild override holding a READY git bearer token (a verbatim PAT, or a
# ~1h GitHub App installation token). Nothing ever deleted it, so one Secrets Manager secret piled
# up per build, forever, billing monthly. `post_build` is the reclaim point for every build that
# started — it runs after a SUCCESSFUL build and after a FAILED build phase alike. The complementary
# leg (a StartBuild that faults *before* any build exists) lives in the service and is pinned in
# tests/test_runtime_build_service.py.


def _scratch_purge_command():
    """The post_build purge command AS CODEBUILD RUNS IT (`yaml.safe_load`), plus its phase."""
    hits = [(p, c) for p, c in _runtime_commands() if "delete-secret" in c]
    assert len(hits) == 1, f"expected exactly one delete-secret command, got {len(hits)}"
    return hits[0]


def test_the_scratch_clone_token_is_PURGED_in_post_build():
    """Force delete, by ARN, guarded on the variable being set.

    `--force-delete-without-recovery` is the load-bearing flag: a plain `delete-secret` schedules
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
    """Position, and it is load-bearing rather than cosmetic.

    A failing command aborts the remainder of its phase. The command that used to open post_build,
    `cat /tmp/outputs.json`, fails whenever the build phase died before writing that file — i.e. on
    exactly the failed-deploy path where the leaked token most needs reclaiming. Anywhere below it,
    the purge silently never runs for a failed build."""
    import yaml

    commands = yaml.safe_load(BUILDSPEC.read_text())["phases"]["post_build"]["commands"]
    _phase, cmd = _scratch_purge_command()

    assert commands[0] == cmd, (
        "the scratch-token purge must be the first post_build command; that slot holds: "
        f"{commands[0]!r}"
    )
    # Non-vacuity: the command it must precede really is in this phase and really can fail.
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
    visible in the build log instead of silently swallowed."""
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
    # The delete-secret response is noise in the build log, and the token itself must never be
    # echoed: only the WARN (which carries the ARN, not the secret value) may reach stdout.
    if not expect_warn:
        assert out.stdout.strip() == "", ctx
