"""Contract tests for the agent scaffold's GitHub Actions workflow (E27A/T3, E28B/T5).

The scaffold's `build.yml` is the *only* place the trigger shape, the concurrency guard and
the image-digest handover live — the backend never reads ECR (the ECS task role holds only
`sts:GetCallerIdentity`), so the workflow is what guarantees the platform is handed a digest
at all. That makes these assertions the sole offline guard on E28B's load-bearing mechanic.

E28B/T5 replaces E27A's two-path shape. What is pinned here now:
  - ONE path: quality -> build -> push image -> resolve digest -> POST to AGP. No `candidate`
    job, no `github.ref` branch gate, no tree-sha existence probe / conditional rebuild.
  - `concurrency:` keyed on the ref with `cancel-in-progress: false`. A live test saw two
    builds start 11s apart and race the agent's single terraform state lock; the loser failed
    while the winner deployed a stale image and the platform recorded success. Queueing is the
    fix, and NOT cancelling is half of it — a killed apply abandons the lock half-applied.
  - The handover is an image DIGEST, not just a mutable tag, and the build FAILS rather than
    reporting a deploy for an image it could not name.
  - HARD invariant from E27/T9, unchanged: no job may ever post `stage: 'prod'`. The backend
    refuses it with a 403; production is reachable only through AGP's promote route.

A note on method, carried from E28/E28A where it cost three fix rounds and six defeated
guards: every assertion below reads the PARSED document (`yaml.safe_load`), and every
assertion about a shell body reads that body with `#` comment lines STRIPPED. The workflow's
header comment legitimately names `workflow_dispatch`, `describe-images`, `[skip ci]` and
`sha256:` while explaining why each is handled the way it is — a whole-text substring check
would be satisfied by that prose and would pass over a workflow that had lost the behaviour
entirely. `test_the_comment_strip_is_not_vacuous` pins the strip itself.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / "agent-templates/strands-agentcore/.github/workflows/build.yml"
)

TEXT = WORKFLOW.read_text()
DOC = yaml.safe_load(TEXT)
# PyYAML follows YAML 1.1, where the bare key `on` resolves to the boolean True.
TRIGGERS = DOC.get("on", DOC.get(True))
JOBS = DOC["jobs"]


def _job_text(name: str) -> str:
    """The job's full YAML block as text — the shell/JS bodies live in scalars, so
    string assertions on them are the honest way to pin the wiring."""
    # width=10**6 so long shell lines are not re-wrapped mid-token by the dumper.
    return yaml.safe_dump(JOBS[name], width=10**6)


def _strip_comments(body: str) -> str:
    """A shell/JS body with whole-line comments removed — the code that actually RUNS."""
    return "\n".join(
        line
        for line in body.splitlines()
        if not (line.lstrip().startswith("#") or line.lstrip().startswith("//"))
    )


def _step(job: str, step_id: str) -> dict:
    return next(s for s in JOBS[job]["steps"] if s.get("id") == step_id)


def test_the_comment_strip_is_not_vacuous():
    """Non-vacuity for every `_strip_comments` assertion below.

    Six guards across E28+E28A were defeated by a comment quoting the very string the guard
    searched for, and this workflow's digest step deliberately explains `describe-images` in
    prose. So the strip is load-bearing: if it ever stopped removing comment lines, the
    assertions that rely on it would silently start passing on prose.
    """
    fixture = "# aws ecr describe-images --repository-name r\nDIGEST=real\n  // js comment\n"
    assert _strip_comments(fixture) == "DIGEST=real"
    # …and the workflow really DOES carry that literal in prose, so the strip is doing work
    # here and not merely on a synthetic fixture.
    assert "describe-images" in TEXT
    assert "describe-images" not in _strip_comments(TEXT)


# --- triggers: one path -----------------------------------------------------------


def test_push_triggers_exactly_one_branch():
    """E28B: a repo has ONE branch. Two deploying branches would race the single dev runtime
    (last-push-wins) AND the single terraform state lock behind it."""
    assert TRIGGERS["push"]["branches"] == ["main"], TRIGGERS["push"]


def test_no_job_is_gated_on_a_branch_ref():
    """The `candidate` job's `github.ref == 'refs/heads/main'` gate is gone, and so is the
    dev build's mirror-image `!=`. Stage is no longer derived from the branch name at all —
    a push is dev, full stop, and production is an AGP action over a recorded digest.

    Read off each job's parsed `if:` expression, so a ref test cannot hide in one job while
    the others look clean."""
    for name, job in JOBS.items():
        cond = str(job.get("if", ""))
        assert "github.ref" not in cond, f"{name} still branches on the ref: {cond}"


def test_pull_request_still_runs_quality_only():
    """Every job except `quality` is reachable on `push` only — directly or via `needs`."""
    assert "pull_request" in TRIGGERS
    for name, job in JOBS.items():
        if name == "quality":
            assert "if" not in job, "quality must stay branch-agnostic and unconditional"
            continue
        gated = "github.event_name == 'push'" in str(job.get("if", ""))
        inherited = any(
            "github.event_name == 'push'" in str(JOBS[n].get("if", ""))
            for n in ([job["needs"]] if isinstance(job["needs"], str) else job["needs"])
        )
        assert gated or inherited, f"{name} is reachable on pull_request"


def test_skip_ci_is_not_defeated_by_a_message_blind_trigger():
    """`[skip ci]` is honoured by GitHub natively on `push`/`pull_request`, and the platform
    relies on it: materialize's own initialize commit carries the marker so seeding a repo
    does not immediately fire a build.

    GitHub does NOT honour the marker on `workflow_dispatch`, `repository_dispatch` or
    `schedule` — those have no commit message to read — so adding one of those triggers is
    exactly how the marker gets defeated. Asserted on the parsed trigger keys."""
    message_blind = {"workflow_dispatch", "repository_dispatch", "schedule", "workflow_call"}
    assert message_blind.isdisjoint(set(TRIGGERS)), TRIGGERS


# --- concurrency: the direct fix for the raced state lock -------------------------


def test_the_workflow_declares_a_concurrency_group():
    """The defect, observed live: two builds 11 seconds apart raced one terraform state lock.
    The loser failed; the winner deployed a stale image while the repo record claimed success.
    Without this key, GitHub runs both."""
    assert "concurrency" in DOC, "no concurrency guard — two builds can race the state lock"


def test_the_concurrency_group_is_keyed_on_the_ref():
    """Keyed on the ref so pushes to the same branch QUEUE. A constant group would serialize
    unrelated refs; a group that omits the ref does not separate them at all."""
    group = DOC["concurrency"]["group"]
    assert "github.ref" in group, group


def test_in_flight_builds_are_never_cancelled():
    """The other half of the fix, and the easy one to get backwards. `cancel-in-progress: true`
    would kill a `terraform apply` mid-flight, abandoning the state lock over a half-applied
    runtime — strictly worse than the race it replaces. Compared against `False` identically:
    an ABSENT key is `None`, which is falsy but not a declared decision."""
    assert DOC["concurrency"]["cancel-in-progress"] is False, DOC["concurrency"]


# --- quality job (must stay unchanged) ------------------------------------------


def test_quality_job_is_untouched():
    quality = JOBS["quality"]
    assert quality["name"] == "Lint, test, audit"
    assert "environment" not in quality and "permissions" not in quality
    assert [s["name"] for s in quality["steps"]] == [
        "Checkout",
        "Set up Python 3.11",
        "Install dependencies",
        "Lint (ruff)",
        "Test (pytest)",
        "Dependency audit (pip-audit)",
    ]


# --- the deleted candidate path --------------------------------------------------


def test_the_candidate_job_is_gone():
    """E28B deletes it: promotion is a recorded digest, so there is nothing for a branch-
    triggered 'register a candidate' job to do. Asserted on the parsed job map — a `candidate`
    job reintroduced under any `if:` would reinstate the second writer this epic removes."""
    assert "candidate" not in JOBS, sorted(JOBS)
    assert set(JOBS) == {"quality", "build", "trigger"}, sorted(JOBS)


def test_nothing_posts_to_the_prod_candidate_route():
    assert "/builds/prod-candidate" not in _strip_comments(TEXT)


def test_the_tree_sha_existence_probe_and_conditional_rebuild_are_gone():
    """E27A inferred "same tree ⇒ same image" from an ECR existence probe, then rebuilt when
    the probe said absent. A digest makes the identity true by construction, so the probe,
    its three-way fail-closed branching and the conditional rebuild all go — and with them the
    only path in this workflow that could push a DIFFERENT image under an existing tag."""
    code = _strip_comments(TEXT)
    assert "batch-get-image" not in code
    assert "ImageNotFound" not in code
    # Exactly one build+push remains: the unconditional one.
    assert code.count("docker buildx build") == 1, code


def test_no_ecr_read_call_the_push_role_lacks():
    """The ECR-push OIDC role grants `ecr:BatchGetImage` but NOT `ecr:DescribeImages` /
    `ecr:ListImages` (`ecr_push_role_service.py`). The digest is therefore read from buildx's
    own push metadata, not from a registry lookup — a `describe-images` here would 403 on
    every run. Comments stripped: the digest step names the call in prose to explain this."""
    code = _strip_comments(TEXT)
    assert "describe-images" not in code
    assert "list-images" not in code


# --- the digest handover ---------------------------------------------------------


def test_the_build_exports_the_digest_as_a_job_output():
    """The `trigger` job is a separate job, so the digest can only reach it as an output."""
    assert "image_digest" in JOBS["build"]["outputs"]


def test_the_digest_comes_from_the_push_itself():
    """Taken from buildx's `--metadata-file`, which names the exact bytes THIS run uploaded.
    A read-back of the tag can already have been overwritten by a concurrent push — the very
    class of bug the digest exists to close."""
    pushed = next(
        s for s in JOBS["build"]["steps"] if "docker buildx build" in str(s.get("run", ""))
    )
    assert "--metadata-file" in pushed["run"]
    assert "containerimage.digest" in _strip_comments(_step("build", "digest")["run"])


def test_the_digest_step_refuses_anything_that_is_not_a_sha256_reference():
    """"Never report success for an image you could not name" (the E28A/T1b idiom). Both the
    empty and the malformed case must hard-exit, announced as a workflow error."""
    code = _strip_comments(_step("build", "digest")["run"])
    assert "sha256:*)" in code, code
    assert "exit 1" in code
    assert "::error::" in code


def test_the_trigger_posts_the_digest_and_still_posts_stage_dev():
    body = _strip_comments(_job_text("trigger"))
    assert "image_digest" in body
    assert "stage: 'dev'" in body
    assert "/builds/runtime" in body


def test_the_trigger_validates_the_digest_before_posting_it():
    """Belt and braces across the job boundary: an output that arrives empty (a skipped or
    partially-failed upstream step) must not be POSTed as a successful deploy."""
    body = _strip_comments(_job_text("trigger"))
    assert "sha256:[0-9a-f]{64}" in body, body
    assert "core.setFailed" in body


def test_the_digest_reaches_github_script_through_env_not_string_interpolation():
    """`${{ }}` inside a `script:` body splices the value into the JS SOURCE; an `env:` var
    read via `process.env` cannot break out of a string. The tag already used this shape."""
    step = next(
        s for s in JOBS["trigger"]["steps"] if s.get("uses", "").startswith("actions/github-script")
    )
    assert set(step["env"]) >= {"IMAGE_TAG", "IMAGE_DIGEST"}, step["env"]
    assert "process.env.IMAGE_DIGEST" in step["with"]["script"]


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required to run the fragment")
@pytest.mark.parametrize(
    "metadata,expect_exit",
    [
        ('{"containerimage.digest": "sha256:' + "ab" * 32 + '"}', 0),
        ('{"containerimage.digest": ""}', 1),  # present but empty
        ("{}", 1),  # key absent entirely — buildx wrote no digest
        ('{"containerimage.digest": "not-a-digest"}', 1),  # malformed
        ('{"containerimage.digest": null}', 1),  # explicit null
        ("", 1),  # unparseable metadata file
    ],
    ids=["valid", "empty-string", "key-absent", "malformed", "null", "unparseable"],
)
def test_the_digest_guard_actually_fires(tmp_path, metadata, expect_exit):
    """EXECUTES the real fragment. Static text cannot prove a `case` pattern matches what it
    claims, and the failure mode here — a guard that never fires — is indistinguishable from a
    correct build until a stale image ships to production.

    Extracted via `yaml.safe_load` (never by stripping lines) and run under `sh -c` with
    NON-EXPORTED vars, matching the production condition. The `key-absent` and `null` cases
    are the ones that make this non-vacuous: `jq -r` renders both as the string "null" without
    the `// empty` fallback, and "null" is not empty — so a guard testing only for emptiness
    would pass them straight through as a digest.
    """
    (tmp_path / "metadata.json").write_text(metadata)
    out_file = tmp_path / "gh_output"
    script = (
        f"cd {tmp_path}\n"
        f'GITHUB_OUTPUT="{out_file}"\n'
        f"{_step('build', 'digest')['run']}\n"
        "echo REACHED_POST\n"
    )
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == expect_exit, f"stdout={out.stdout!r} stderr={out.stderr!r}"
    if expect_exit:
        assert "REACHED_POST" not in out.stdout, "the guard did not stop the build"
        assert "::error::" in out.stdout, out.stdout
        assert not out_file.exists() or "digest=" not in out_file.read_text()
    else:
        assert "REACHED_POST" in out.stdout, out.stdout
        assert f"digest=sha256:{'ab' * 32}" in out_file.read_text()


# --- invariants that outlive the rewrite ----------------------------------------


def test_no_job_ever_posts_stage_prod():
    """E27/T9 invariant: CI cannot deploy production. The backend 403s `stage=prod` on the
    OIDC path; production is reachable only through AGP's promote route."""
    code = _strip_comments(TEXT)
    assert re.search(r"stage\s*[:=]\s*['\"]?prod", code, re.IGNORECASE) is None
    # Non-vacuous: the pattern DOES catch the dev stage the trigger job really sends.
    assert re.search(r"stage\s*[:=]\s*['\"]?dev", code) is not None


def test_the_tag_is_still_derived_from_the_git_tree_sha():
    """The digest replaced the tree sha as the IDENTITY, but the tag remains the human-readable
    label and stays content-addressed, so re-pushing identical content does not churn tags."""
    assert re.search(r"rev-parse HEAD\^\{tree\}", TEXT) is not None


def test_commit_sha_tagging_is_gone():
    """A merge commit has a new commit sha but the same tree; a commit-keyed tag would make
    identical content look like a different artifact on every merge."""
    assert re.search(r"rev-parse HEAD(?!\^\{tree\})", TEXT) is None
    assert "GITHUB_SHA::7" not in TEXT
    assert "context.sha.substring" not in TEXT


def test_nothing_hardcodes_an_aws_account_or_region():
    """Account and region come from the environment's CI vars — the repo URI is the authority
    on who owns the shared tenant registry."""
    code = _strip_comments(TEXT)
    assert not re.search(r"\b\d{12}\b", code), "no hardcoded AWS account id"
    assert "${{ vars.AWS_REGION }}" in code
    assert "${{ vars.ECR_REPOSITORY }}" in code


# --- environments (what makes vars.* resolve at all) ----------------------------


def test_both_ecr_jobs_declare_the_dev_environment():
    """`environment: dev` is what makes `vars.ECR_REPOSITORY` / `vars.AWS_ECR_PUSH_ROLE_ARN`
    resolve — removing it silently breaks the build."""
    for name in ("build", "trigger"):
        assert JOBS[name]["environment"] == "dev", name
        assert JOBS[name]["permissions"] == {"id-token": "write", "contents": "read"}
