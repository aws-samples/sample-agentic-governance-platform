"""Contract tests for the Terraform root module (`infrastructure/{main,variables,outputs}.tf`).

String-level, like test_gateway_module_contract.py: nothing offline applies the root, and
`terraform validate` cannot see what the pipeline depends on. What these pin is the
cross-task interface the delivery pipeline reads:

* state lives in the S3 backend configured at `init -backend-config=`, never in a block that
  hardcodes a bucket (and never commented out);
* the deployed backend image is state read from SSM, with no `:latest` fallback that would
  silently redeploy whatever was last pushed;
* the outputs the frontend build consumes exist under the names it reads.

Scoped to the three root files on purpose: `:latest` is legitimate elsewhere in the tree.
"""

import re
from pathlib import Path

import pytest

_CONTROL_PLANE = Path(__file__).resolve().parents[2]
INFRA = _CONTROL_PLANE / "infrastructure"

ROOT_FILES = ("main.tf", "variables.tf", "outputs.tf")
NAME_ENVIRONMENT_ASSIGNMENT_FLOOR = 11


def _read(name: str) -> str:
    return (INFRA / name).read_text()


def _block(text: str, header: str) -> str:
    assert header in text, header
    return text.split(header, 1)[1].split("\n}", 1)[0]


def test_s3_backend_block_is_active_and_empty():
    main_tf = _read("main.tf")
    assert re.search(r'^\s*backend "s3" \{\s*\}', main_tf, re.MULTILINE)
    assert not re.search(r'^\s*#\s*backend\s+"s3"', main_tf, re.MULTILINE)


def test_backend_image_is_read_from_ssm_only_when_no_image_is_passed():
    main_tf = _read("main.tf")
    block = _block(main_tf, 'data "aws_ssm_parameter" "backend_image" {')
    assert 'count = var.container_image == "" ? 1 : 0' in block
    assert 'name  = "/agp/${var.environment}/backend-image"' in block


def test_ecs_container_image_prefers_var_over_ssm_with_no_other_fallback():
    ecs = _block(_read("main.tf"), 'module "ecs" {')
    assert (
        'container_image = var.container_image != "" ? var.container_image'
        " : data.aws_ssm_parameter.backend_image[0].insecure_value" in ecs
    )
    assert "coalesce(" not in ecs
    assert "module.ecr.repository_url" not in ecs


def test_environment_has_no_default_and_create_vpc_is_gone():
    variables_tf = _read("variables.tf")
    block = _block(variables_tf, 'variable "environment" {')
    assert "type        = string" in block
    assert "default" not in block
    assert 'variable "create_vpc"' not in variables_tf


@pytest.mark.parametrize(
    "name,default",
    [
        ("create_langfuse", "true"),
        ("ephemeral", "false"),
        ("create_shared_resources", "false"),
    ],
)
def test_stack_switches_are_declared_as_bools(name, default):
    block = _block(_read("variables.tf"), f'variable "{name}" {{')
    assert "type        = bool" in block
    assert f"default     = {default}" in block


def test_langfuse_module_is_counted_and_moved_into_the_indexed_address():
    main_tf = _read("main.tf")
    langfuse = _block(main_tf, 'module "langfuse" {')
    assert "count  = var.create_langfuse ? 1 : 0" in langfuse
    moved = _block(main_tf, "moved {")
    assert "from = module.langfuse\n" in moved + "\n"
    assert "to   = module.langfuse[0]" in moved
    assert 'langfuse_host        = var.create_langfuse ? module.langfuse[0].langfuse_host : ""' in main_tf
    assert (
        'langfuse_secret_name = var.create_langfuse ? module.langfuse[0].langfuse_secret_name : ""' in main_tf
    )
    assert "module.langfuse.langfuse_host" not in main_tf
    assert "module.langfuse.langfuse_secret_name" not in main_tf


def test_branch_image_repository_is_gated_on_create_shared_resources():
    main_tf = _read("main.tf")
    repo = _block(main_tf, 'resource "aws_ecr_repository" "branch_images" {')
    assert "count                = var.create_shared_resources ? 1 : 0" in repo
    assert 'name                 = "${var.project_name}-cp-${local.account_id_short}-branch-images"' in repo
    assert "force_delete         = true" in repo
    policy = _block(main_tf, 'resource "aws_ecr_lifecycle_policy" "branch_images" {')
    assert "repository = aws_ecr_repository.branch_images[0].name" in policy
    assert '"pr-"' in policy


@pytest.mark.parametrize("name", ["s3", "dynamodb", "secrets_manager"])
def test_destroyable_modules_receive_the_ephemeral_switch(name):
    block = _block(_read("main.tf"), f'module "{name}" {{')
    assert re.search(r"^\s+ephemeral\s+= var\.ephemeral$", block, re.MULTILINE)


@pytest.mark.parametrize(
    "name,value",
    [
        (
            "branch_image_repository_url",
            'var.create_shared_resources ? aws_ecr_repository.branch_images[0].repository_url : ""',
        ),
        ("langfuse_host", "local.langfuse_host"),
        ("langfuse_secret_name", "local.langfuse_secret_name"),
    ],
)
def test_switch_driven_outputs_read_the_gated_values(name, value):
    block = _block(_read("outputs.tf"), f'output "{name}" {{')
    assert f"value       = {value}" in block


def test_resource_names_derive_from_the_naming_token_and_the_ssm_path_from_environment():
    main_tf = _read("main.tf")
    variables_tf = _read("variables.tf")
    assert "name_environment = coalesce(var.naming_environment, var.environment)" in main_tf
    assert (
        'name_prefix      = "${var.project_name}-cp-${local.name_environment}-${local.account_id_short}"'
        in main_tf
    )
    assert re.findall(r"^\s*environment\s*=\s*var\.environment\b", main_tf, re.M) == []
    assert (
        len(re.findall(r"^\s*environment\s*=\s*local\.name_environment", main_tf, re.M))
        >= NAME_ENVIRONMENT_ASSIGNMENT_FLOOR
    )
    assert 'name_prefix  = "agp-${local.name_environment}"' in main_tf
    assert 'name  = "/agp/${var.environment}/backend-image"' in main_tf
    assert "Environment = var.environment" in main_tf
    assert "Environment = var.environment" in _read("providers.tf")
    assert "promote_pull" not in main_tf
    assert "promote_pull" not in variables_tf


@pytest.mark.parametrize("name", ROOT_FILES)
def test_no_latest_tag_in_root_files(name):
    assert ":latest" not in _read(name)


@pytest.mark.parametrize(
    "name,default",
    [
        ("entra_tenant_domain", '""'),
        ("entra_spa_scope", '""'),
        ("product_mode", '"governance"'),
        ("naming_environment", "null"),
    ],
)
def test_pipeline_variables_are_declared_with_placeholder_defaults(name, default):
    block = _block(_read("variables.tf"), f'variable "{name}" {{')
    assert "type        = string" in block
    assert f"default     = {default}" in block


@pytest.mark.parametrize(
    "name",
    [
        "api_endpoint",
        "auth_provider",
        "entra_tenant_id",
        "entra_tenant_domain",
        "entra_spa_client_id",
        "entra_spa_redirect_uri",
        "entra_spa_scope",
        "product_mode",
        "frontend_bucket_name",
        "cloudfront_distribution_id",
    ],
)
def test_frontend_build_outputs_are_declared(name):
    assert f'output "{name}" {{' in _read("outputs.tf"), name


def test_spa_redirect_uri_is_the_frontend_url_plus_auth_callback():
    block = _block(_read("outputs.tf"), 'output "entra_spa_redirect_uri" {')
    assert 'value       = "${module.cloudfront.frontend_url}/auth/callback"' in block


@pytest.mark.parametrize("name", ROOT_FILES)
def test_root_files_carry_no_tenant_values(name):
    text = _read(name)
    assert ".onmicrosoft.com" not in text
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text)
    assert not re.search(r"(?<![0-9])[0-9]{12}(?![0-9])", text)
