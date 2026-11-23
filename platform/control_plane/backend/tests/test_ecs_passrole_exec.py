"""Contract test: the ECS task role can PassRole the pipeline's runtime exec role.

MCP env wiring (grant + read-path reconcile) calls the full-replace UpdateAgentRuntime, which
replays the runtime's roleArn. The runtime module names that role `{agent_name}-{stage}-agentcore-exec`,
so PassAgentCoreRuntimeExecutionRole must match it, and only toward bedrock-agentcore.

Text-level, like `test_ecs_iam_studio_bedrock.py` (no `terraform init`/plan needed).
"""

import re
from pathlib import Path

INFRA = Path(__file__).resolve().parents[2] / "infrastructure/modules"
MAIN_TF = (INFRA / "ecs/main.tf").read_text()
PATTERN = '"arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/*-agentcore-exec"'


def _statement() -> str:
    start = MAIN_TF.index('Sid    = "PassAgentCoreRuntimeExecutionRole"')
    end = MAIN_TF.index("\n      },", start)
    return MAIN_TF[start:end]


def test_passrole_covers_the_runtime_module_exec_role():
    stmt = _statement()
    assert 'Action = "iam:PassRole"' in stmt
    assert PATTERN in stmt


def test_passrole_is_restricted_to_bedrock_agentcore():
    assert re.search(
        r'StringEquals\s*=\s*\{\s*"iam:PassedToService"\s*=\s*"bedrock-agentcore\.amazonaws\.com"',
        _statement(),
    )


def test_pattern_matches_the_runtime_module_role_name():
    runtime = (INFRA / "agentcore_runtime/main.tf").read_text()
    assert 'exec_role_name = "${var.agent_name}-${var.stage}-agentcore-exec"' in runtime
    role = "fnol_studio_t1-dev-agentcore-exec"
    assert re.fullmatch(".*-agentcore-exec", role)
