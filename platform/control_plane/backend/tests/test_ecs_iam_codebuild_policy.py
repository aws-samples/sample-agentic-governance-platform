"""Contract test: the ECS task role can list and describe its own in-flight CodeBuild builds.

`_has_in_flight_build` in `runtime_build_service.py` calls `codebuild:ListBuildsForProject` and
`codebuild:BatchGetBuilds` before every `StartBuild`, so two pushes cannot race the same agent and
stage. A task role that grants only `codebuild:StartBuild` makes that check AccessDenied and fail
open on every call, so both actions are pinned onto the same policy statement block.

Reads the module source directly, the `test_buildspec_contract.py` idiom; no `terraform init` or plan.
"""

from pathlib import Path

MAIN_TF = (Path(__file__).resolve().parents[2] / "infrastructure/modules/ecs/main.tf").read_text()


def test_ecs_task_role_can_list_and_describe_codebuild_builds():
    assert "codebuild:ListBuildsForProject" in MAIN_TF
    assert "codebuild:BatchGetBuilds" in MAIN_TF


def test_the_batch_get_builds_grant_is_scoped_to_build_arns_not_wide_open():
    """BatchGetBuilds authorizes on build ARNs (`arn:...:build/<name>:*`), never the project ARN, so
    the grant derives the build ARN from the project ARN instead of a `"*"` or project-only resource."""
    assert 'replace(var.codebuild_project_arn, ":project/", ":build/")' in MAIN_TF


def test_describe_log_groups_is_granted_on_star_because_it_has_no_resource_level_permissions():
    statement = MAIN_TF[MAIN_TF.index('Action   = ["logs:DescribeLogGroups"]') :]
    resource_line = statement.splitlines()[1].strip()
    assert resource_line == 'Resource = "*"', resource_line
