"""Contract test for the agent scaffold's GitHub Actions build.yml (E25C/T6, E27/T10).

Pins the AGP-governed promotion model: a push builds and deploys **dev only**; prod is an
AGP action (`POST /projects/{id}/repos/{repo_id}/promote`, OWNER-gated) that resolves the
image tag SERVER-SIDE. The scaffold therefore carries NO promote job and NO operator-supplied
`image_tag` input, and `/builds/runtime` refuses `stage=prod` over GitHub OIDC (E27/T9).

Both GitHub Environments (`dev` / `prod`) are deliberately RETAINED — they carry the
per-stage CI vars (`ECR_REPOSITORY` / `AWS_REGION` / `AWS_ECR_PUSH_ROLE_ARN` are popped from
repo scope so `set_env_vars` can write them environment-scoped), so `environment: dev` is what
makes `vars.ECR_REPOSITORY` resolve at all.
"""

import pathlib

import yaml

# Resolve relative to this test file so it works regardless of pytest's cwd:
# tests/ -> backend/ -> control_plane/ -> agent-templates/...
BUILD_YML = (
    pathlib.Path(__file__).resolve().parents[2]
    / "agent-templates/strands-agentcore/.github/workflows/build.yml"
)


def test_no_workflow_dispatch_image_tag_input():
    """The prod tag is resolved by AGP, never hand-entered.

    Asserted on the PARSED `on:` block rather than a bare `"image_tag" not in text`, because
    the `trigger` job legitimately POSTs an `image_tag` FIELD for the dev build (that is the
    tag it just pushed). What must not exist is an operator-supplied dispatch INPUT and any
    `inputs.image_tag` expression reading one.
    """
    wf = yaml.safe_load(BUILD_YML.read_text())
    # PyYAML parses the bareword `on:` key as the boolean True; accept either.
    on = wf.get("on", wf.get(True))
    assert "workflow_dispatch" not in on
    assert "inputs.image_tag" not in BUILD_YML.read_text()


def test_no_promote_job():
    assert "promote:" not in BUILD_YML.read_text()


def test_push_never_deploys_prod():
    """No path through the scaffold posts `stage: 'prod'` — the promote job is gone and
    `/builds/runtime` refuses prod over OIDC (E27/T9), so AGP is the only prod path."""
    assert "'prod'" not in BUILD_YML.read_text()


def test_dev_environment_is_retained():
    """Environments carry the per-stage CI vars — deleting them breaks the dev build."""
    text = BUILD_YML.read_text()
    assert "environment: dev" in text


def test_trigger_job_is_intact():
    text = BUILD_YML.read_text()
    assert "agp-runtime-build" in text
    assert "/builds/runtime" in text
