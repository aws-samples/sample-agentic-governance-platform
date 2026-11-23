"""Contract tests for the `agentcore_gateway` Terraform module and its seed template.

String-level, like test_buildspec_contract.py: the module is applied by CodeBuild from the per-org
`agp-gateway-infra` copy, so nothing offline runs it. What these pin is the cross-task interface
and the three choices whose regression would only show up live:

* `ignore_changes` on BOTH backend-written gateway attributes — the backend attaches the Cedar
  policy engine and replays the authorizer with `UpdateGateway`, so a Terraform-owned value would
  silently detach ENFORCE (or reset the audience) on the next apply.
* the gateway role is named `agp-<name>-<stage>-gateway-role` — the ECS task role's PassRole is
  scoped to `role/agp-*-gateway-role`, so any other name AccessDenies the backend's UpdateGateway.
* `api_key_secret_source = "EXTERNAL"` — the key value never enters Terraform state (needs
  provider >= 6.66, hence the pinned floor).
"""

import re
from pathlib import Path

import pytest
import yaml

_CONTROL_PLANE = Path(__file__).resolve().parents[2]
MODULE = _CONTROL_PLANE / "infrastructure/modules/agentcore_gateway"
TEMPLATE = _CONTROL_PLANE / "agent-templates/gateway-agentcore"
RUNTIME_MODULE_TF = _CONTROL_PLANE / "infrastructure/runtime_module.tf"


def _main() -> str:
    return (MODULE / "main.tf").read_text()


def test_module_declares_exactly_the_C5_variables():
    """The buildspec's stale-module drift guard greps for the exact `gateway_config_path` block opener."""
    text = (MODULE / "variables.tf").read_text()
    declared = set(re.findall(r'^variable "([a-z_]+)"', text, re.MULTILINE))
    assert {
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
    } <= declared
    assert 'variable "gateway_config_path" {' in text


def test_module_outputs_are_named_per_C5():
    text = (MODULE / "outputs.tf").read_text()
    for name in ("gateway_arn", "gateway_id", "gateway_url"):
        assert f'output "{name}" {{' in text, name


def test_provider_floor_is_6_66_for_the_external_api_key_secret():
    text = (MODULE / "versions.tf").read_text()
    assert 'version = ">= 6.66, < 7.0"' in text


def test_gateway_ignores_both_backend_written_attributes():
    main_tf = _main()
    m = re.search(r"ignore_changes\s*=\s*\[([^\]]*)\]", main_tf)
    assert m, "the gateway resource needs a lifecycle ignore_changes"
    ignored = {a.strip() for a in m.group(1).split(",") if a.strip()}
    assert {"policy_engine_configuration", "authorizer_configuration"} <= ignored


def test_gateway_role_name_matches_the_ecs_passrole_pattern():
    """The role trusts the AgentCore service principal and nothing else, with the confused-deputy
    guard scoped to this account through the data source, never a literal."""
    main_tf = _main()
    assert re.search(r'"agp-\$\{[^}]+\}-\$\{[^}]+\}-gateway-role"', main_tf)
    assert '"bedrock-agentcore.amazonaws.com"' in main_tf
    assert '"aws:SourceAccount" = data.aws_caller_identity.current.account_id' in main_tf
    assert "aws:SourceArn" not in main_tf


def test_gateway_role_carries_the_cedar_and_api_key_outbound_actions():
    main_tf = _main()
    for action in (
        "bedrock-agentcore:GetPolicyEngine",
        "bedrock-agentcore:AuthorizeAction",
        "bedrock-agentcore:PartiallyAuthorizeActions",
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:GetResourceApiKey",
        "secretsmanager:GetSecretValue",
    ):
        assert f'"{action}"' in main_tf, action


def test_gateway_role_pins_the_three_api_key_outbound_grants_t1_measured():
    """Measured live: without ANY one of these the target still goes READY and every tools/call
    fails with a generic "internal error" (no AccessDenied text) — so pin each grant, and the
    secret read to exactly the MCP's namespace from data sources, never the developer's string."""
    main_tf = _main()
    secret_stmt = re.search(
        r'Action\s*=\s*\["secretsmanager:GetSecretValue"\]\s*Resource\s*=\s*([^\n]+)', main_tf
    )
    assert secret_stmt, "GetSecretValue must be its own statement"
    assert secret_stmt.group(1).strip() == '"${local.secret_arn_prefix}*"'
    vault_stmt = re.search(r"Action = \[([^\]]*GetWorkloadAccessToken[^\]]*)\]", main_tf)
    assert vault_stmt, "GetWorkloadAccessToken statement missing"
    vault_actions = set(re.findall(r'"([^"]+)"', vault_stmt.group(1)))
    assert vault_actions == {
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:GetResourceApiKey",
    }


def test_gateway_authorizer_admits_delegated_tokens_only():
    """Measured live: an app-only (client_credentials) token for the MCP audience carries no
    `scp` and PASSES an audience-only authorizer; `allowed_scopes = ["Invoke"]` 403s it while
    the harness's OBO token (scp=Invoke) passes. A `custom_claim` on `scp` is rejected by
    AgentCore (gateway goes UPDATE_UNSUCCESSFUL) — never use it."""
    main_tf = _main()
    assert 'allowed_scopes   = ["Invoke"]' in main_tf
    assert not re.search(r"^\s*custom_claim\s*{", main_tf, re.MULTILINE)


def test_api_key_stays_out_of_terraform_state():
    """No inline key value anywhere."""
    main_tf = _main()
    assert 'resource "aws_bedrockagentcore_api_key_credential_provider"' in main_tf
    assert 'api_key_secret_source = "EXTERNAL"' in main_tf
    assert "api_key_secret_config {" in main_tf
    assert not re.search(r"^\s*api_key(_wo)?\s*=", main_tf, re.MULTILINE)


def test_gateway_is_mcp_with_the_same_entra_authorizer_as_the_identity_service():
    """The audience list keeps McpIdentityService._allowed_audience's order: api:// URI, then the
    app GUID."""
    main_tf = _main()
    assert 'protocol_type   = "MCP"' in main_tf
    assert 'authorizer_type = "CUSTOM_JWT"' in main_tf
    assert (
        'discovery_url    = "https://login.microsoftonline.com/${var.tenant_id}/v2.0/.well-known/openid-configuration"'
        in main_tf
    )
    assert "allowed_audience = compact([var.entra_app_audience, var.entra_app_id])" in main_tf


def test_target_is_an_inline_openapi_spec_with_api_key_credentials():
    main_tf = _main()
    assert 'resource "aws_bedrockagentcore_gateway_target"' in main_tf
    assert 'name               = "api"' in main_tf
    assert "open_api_schema {" in main_tf
    assert "payload = file(var.openapi_spec_path)" in main_tf
    assert "api_key {" in main_tf


def test_preconditions_refuse_an_empty_secret_and_the_placeholder_server():
    main_tf = _main()
    assert "https://REPLACE-ME.example.com" in main_tf
    assert main_tf.count("precondition {") >= 2


def test_module_zip_is_staged_like_the_harness_module():
    text = RUNTIME_MODULE_TF.read_text()
    assert 'data "archive_file" "gateway_module"' in text
    assert '"${path.module}/modules/agentcore_gateway"' in text
    assert 'key    = "gateway-module/agentcore_gateway.zip"' in text
    assert "etag   = data.archive_file.gateway_module.output_md5" in text


def test_template_metadata_chains_to_the_gateway_infra_template():
    import json

    meta = json.loads((TEMPLATE / "template.json").read_text())
    assert meta["id"] == "gateway-agentcore"
    assert meta["framework"] == "agentcore-gateway"
    assert meta["infra_template_id"] == "agentcore-gateway"
    assert meta["artifact_kind"] == "mcp_server"
    assert meta["preview"] is True


def test_template_gateway_yaml_carries_the_C5_keys():
    cfg = yaml.safe_load((TEMPLATE / "gateway.yaml").read_text())
    assert len(cfg["description"]) <= 100
    assert (TEMPLATE / cfg["openapi_spec"]).is_file()
    assert set(cfg["api_key"]) >= {"secret_arn", "json_key", "location", "parameter_name", "prefix"}
    assert cfg["api_key"]["location"] in ("HEADER", "QUERY_PARAMETER")
    assert "instructions" in cfg


def test_template_openapi_ships_the_placeholder_server_and_three_operations():
    spec = yaml.safe_load((TEMPLATE / "openapi.yaml").read_text())
    assert spec["servers"][0]["url"] == "https://REPLACE-ME.example.com"
    op_ids = {
        op["operationId"]
        for path in spec["paths"].values()
        for op in path.values()
        if isinstance(op, dict) and "operationId" in op
    }
    assert op_ids == {"getPolicy", "openClaim", "getClaimStatus"}


def test_template_deploy_workflow_posts_the_mcp_server_id_not_an_agent_id():
    text = (TEMPLATE / ".github/workflows/deploy.yml").read_text()
    assert "mcp_server_id: '${{ vars.MCP_SERVER_ID }}'" in text
    assert "agent_id:" not in text
    assert "image_tag: '${{ github.sha }}'" in text
    assert "stage: 'dev'" in text
    assert "core.getIDToken('agp-runtime-build')" in text


_ACCT = "111122223333"
_OTHER_ACCT = "444455556666"


def test_secret_namespace_is_built_from_data_sources_only():
    """The developer's string is used only by the precondition (regex + message) and the provider."""
    main_tf = _main()
    assert 'secret_name_prefix = "agp-mcp/${var.mcp_name}/"' in main_tf
    assert (
        'secret_arn_prefix  = "arn:${data.aws_partition.current.partition}:secretsmanager:'
        "${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:secret:"
        '${local.secret_name_prefix}"'
    ) in main_tf
    assert 'data "aws_partition" "current" {}' in main_tf
    assert main_tf.count("local.api_key_secret_arn") == 3


def _secret_precondition_regex(partition="aws", region="us-east-1", account=_ACCT, mcp_name="claims-api"):
    """The precondition's pattern with its interpolations resolved as Terraform would (raw)."""
    (pattern,) = re.findall(
        r'condition\s*=\s*can\(regex\("(\^\$\{local\.secret_arn_prefix\}[^"]+)", '
        r"local\.api_key_secret_arn\)\)",
        _main(),
    )
    prefix = f"arn:{partition}:secretsmanager:{region}:{account}:secret:agp-mcp/{mcp_name}/"
    return pattern.replace("${local.secret_arn_prefix}", prefix)


@pytest.mark.parametrize(
    "arn",
    [
        f"arn:aws:secretsmanager:us-east-1:{_ACCT}:secret:agp-mcp/claims-api/api-key-AbCdEf",
        f"arn:aws:secretsmanager:us-east-1:{_ACCT}:secret:agp-mcp/claims-api/prod/key_1.v2-Zz9Zz9",
    ],
    ids=["canonical", "nested-name"],
)
def test_secret_precondition_accepts_a_namespaced_full_arn(arn):
    assert re.fullmatch(_secret_precondition_regex(), arn)


@pytest.mark.parametrize(
    "arn",
    [
        "",
        f"arn:aws:secretsmanager:us-east-1:{_OTHER_ACCT}:secret:agp-mcp/claims-api/api-key-AbCdEf",
        f"arn:aws:secretsmanager:eu-west-1:{_ACCT}:secret:agp-mcp/claims-api/api-key-AbCdEf",
        f"arn:aws:secretsmanager:us-east-1:{_ACCT}:secret:agp-dev/git-connections/abc-AbCdEf",
        f"arn:aws:secretsmanager:us-east-1:{_ACCT}:secret:agp-mcp/other-mcp/api-key-AbCdEf",
        f"arn:aws:secretsmanager:us-east-1:{_ACCT}:secret:agp-mcp/claims-api-x/api-key-AbCdEf",
        f"arn:aws:secretsmanager:us-east-1:{_ACCT}:secret:agp-mcp/claims-api/api-key",
        f"arn:aws:secretsmanager:us-east-1:{_ACCT}:secret:agp-mcp/claims-api/*",
        f"arn:aws:secretsmanager:us-east-1:{_ACCT}:secret:agp-mcp/claims-api/api-?ey-AbCdEf",
        f"arn:aws:secretsmanager:us-east-1:{_ACCT}:secret:agp-mcp/claims-api/-AbCdEf",
    ],
    ids=[
        "empty",
        "other-account",
        "other-region",
        "platform-secret",
        "other-mcp",
        "prefix-sibling",
        "no-suffix",
        "wildcard",
        "question-mark",
        "empty-name",
    ],
)
def test_secret_precondition_refuses_anything_outside_the_namespace(arn):
    assert not re.fullmatch(_secret_precondition_regex(), arn)


def test_secret_precondition_message_names_the_namespace_and_the_create_command():
    main_tf = _main()
    msg = main_tf.split("local.api_key_secret_arn))", 1)[1].split("error_message", 1)[1].split("\n", 1)[0]
    assert "agp-mcp/${var.mcp_name}/" in msg
    assert "aws secretsmanager create-secret --name agp-mcp/${var.mcp_name}/api-key" in msg
    assert "6-character suffix" in msg


def test_template_docs_name_the_secret_inside_the_namespace():
    readme = (TEMPLATE / "README.md").read_text()
    assert "aws secretsmanager create-secret --name agp-mcp/<mcp_name>/api-key" in readme
    assert "agp-mcp/<mcp_name>/" in (TEMPLATE / "gateway.yaml").read_text()


def test_module_readme_gives_a_scoped_cmk_statement_never_decrypt_on_star():
    readme = (MODULE / "README.md").read_text()
    for needle in (
        '"kms:ViaService": "secretsmanager.<region>.amazonaws.com"',
        '"kms:EncryptionContext:SecretARN"',
        "agp-<mcp_name>-<stage>-gateway-role",
    ):
        assert needle in readme, needle


CLAIMS_API = _CONTROL_PLANE / "infrastructure/examples/claims-api"


def test_claims_api_fixture_names_its_secret_in_the_mcp_namespace():
    main_tf = (CLAIMS_API / "main.tf").read_text()
    assert 'name                    = "agp-mcp/${var.mcp_name}/api-key"' in main_tf
    assert (
        "name_prefix"
        not in main_tf.split('resource "aws_secretsmanager_secret" "api_key"', 1)[1].split("\n}", 1)[0]
    )
    assert "Resource = aws_secretsmanager_secret.api_key.arn" in main_tf
    variables_tf = (CLAIMS_API / "variables.tf").read_text()
    block = variables_tf.split('variable "mcp_name"', 1)[1].split("\n}", 1)[0]
    assert 'default     = "claims-api"' in block


def _resource_block(main_tf: str, header: str) -> str:
    return main_tf.split(header, 1)[1].split("\n}\n", 1)[0]


def test_auth_mode_defaults_to_api_key_when_absent():
    assert 'auth_mode = try(coalesce(tostring(local.gateway_config["auth"])), "api_key")' in _main()


def test_blank_auth_also_defaults_to_api_key():
    """A blank `auth:` decodes to null; bare tostring(null) would reach the precondition as null
    and fail with a generic "argument must not be null". coalesce() errors on null and "", so
    try() falls back to api_key."""
    assert yaml.safe_load("auth:\n")["auth"] is None
    assert "try(coalesce(tostring(" in _main()


def test_auth_mode_precondition_refuses_anything_but_none_or_api_key():
    main_tf = _main()
    assert 'condition     = contains(["none", "api_key"], local.auth_mode)' in main_tf
    msg = main_tf.split('contains(["none", "api_key"], local.auth_mode)', 1)[1].split("\n", 2)[1]
    assert "gateway.yaml" in msg and "`auth`" in msg


def test_credential_provider_exists_only_for_api_key():
    """The secret-ARN precondition rides on the counted provider: with auth none it is never checked."""
    block = _resource_block(_main(), 'resource "aws_bedrockagentcore_api_key_credential_provider" "this" {')
    assert 'count                 = local.auth_mode == "api_key" ? 1 : 0' in block
    assert 'can(regex("^${local.secret_arn_prefix}' in block
    assert "aws_bedrockagentcore_api_key_credential_provider.this[0].credential_provider_arn" in _main()


def test_secret_read_is_granted_only_for_api_key():
    """The precondition does not live on the always-present policy."""
    block = _resource_block(_main(), 'resource "aws_iam_role_policy" "gateway" {')
    assert 'local.auth_mode == "api_key" ? [' in block
    assert block.index('local.auth_mode == "api_key" ? [') < block.index('"secretsmanager:GetSecretValue"')
    assert "precondition {" not in block


def test_target_credential_block_is_dynamic_and_api_key_only():
    block = _resource_block(_main(), 'resource "aws_bedrockagentcore_gateway_target" "api" {')
    assert 'dynamic "credential_provider_configuration" {' in block
    assert 'for_each = local.auth_mode == "api_key" ? [1] : []' in block
    assert not re.search(r"^\s*credential_provider_configuration\s*{", block, re.MULTILINE)
    assert (
        'local.auth_mode != "api_key" || contains(["HEADER", "QUERY_PARAMETER"], local.api_key_location)'
        in block
    )


def test_template_gateway_yaml_documents_auth_and_defaults_to_api_key():
    text = (TEMPLATE / "gateway.yaml").read_text()
    assert yaml.safe_load(text)["auth"] == "api_key"
    assert "none" in text.split("auth:", 1)[0].rsplit("\n\n", 1)[1]
