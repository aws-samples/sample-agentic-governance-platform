"""Contract test: the ECS task role can call Bedrock Converse for Studio's "Compose with AI".

The backend calls Converse (authorized as `bedrock:InvokeModel`) on a cross-region inference
profile. IAM evaluates both the profile ARN (this account + region) and the foundation-model ARN in
whichever region the profile routes to, so the foundation model is granted in any region. No account
id is ever inlined.

Text-level, like `test_ecs_iam_codebuild_policy.py` (no `terraform init`/plan needed).
"""

import re
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[2] / "infrastructure/modules/ecs"
MAIN_TF = (MODULE_DIR / "main.tf").read_text()
VARIABLES_TF = (MODULE_DIR / "variables.tf").read_text()


def _policy_block() -> str:
    start = MAIN_TF.index('resource "aws_iam_role_policy" "ecs_task_bedrock_studio"')
    end = MAIN_TF.find('\nresource "', start + 1)
    return MAIN_TF[start : end if end != -1 else len(MAIN_TF)]


def test_studio_policy_grants_exactly_invoke_model():
    block = _policy_block()
    assert re.findall(r'"bedrock:[A-Za-z*]+"', block) == ['"bedrock:InvokeModel"']


def test_studio_policy_scopes_to_the_inference_profile_and_foundation_model():
    block = _policy_block()
    assert (
        '"arn:aws:bedrock:${data.aws_region.current.region}:'
        "${data.aws_caller_identity.current.account_id}:"
        'inference-profile/${var.studio_model_id}"'
    ) in block
    assert '"arn:aws:bedrock:*::foundation-model/${replace(var.studio_model_id, ' in block
    assert 'Resource = "*"' not in block


def test_studio_model_id_variable_and_env():
    assert 'variable "studio_model_id"' in VARIABLES_TF
    assert re.search(r'default\s*=\s*"us\.anthropic\.claude-sonnet-4-6"', VARIABLES_TF)
    assert re.search(r'name\s*=\s*"STUDIO_MODEL_ID"\s*\n\s*value\s*=\s*var\.studio_model_id', MAIN_TF)


def _foundation_model_id(model_id: str) -> str:
    """Apply the module's own regex (RE2 and Python agree on this subset) to a model id."""
    m = re.search(
        r'foundation-model/\$\{replace\(var\.studio_model_id, "/(.+?)/", "\$1"\)\}',
        _policy_block(),
    )
    assert m, "foundation-model ARN must strip the geo prefix with one regex replace()"
    return re.sub(m.group(1).replace("\\\\", "\\"), r"\1", model_id)


def test_geo_prefix_is_stripped_for_any_cross_region_profile():
    for prefix in ("us", "eu", "apac", "global", "us-gov"):
        assert _foundation_model_id(f"{prefix}.anthropic.claude-sonnet-4-6") == "anthropic.claude-sonnet-4-6"


def test_bare_foundation_model_id_passes_through():
    assert _foundation_model_id("anthropic.claude-sonnet-4-6") == "anthropic.claude-sonnet-4-6"


def test_no_hardcoded_account_id():
    assert not re.search(r"(?<!\d)\d{12}(?!\d)", MAIN_TF)
    assert not re.search(r"(?<!\d)\d{12}(?!\d)", VARIABLES_TF)
    assert not re.search(r"(?<!\d)\d{12}(?!\d)", Path(__file__).read_text())
