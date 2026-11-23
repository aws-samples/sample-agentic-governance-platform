"""Environment-name parity between the ECS task definition and the backend's `Settings`.

String-level, like test_infra_root_contract.py: nothing offline applies the module, and
`terraform validate` cannot know which names the backend reads. What these pin:

* every name the ECS container receives is a `Settings` field (or consumed by the AWS SDK
  itself), so no dead entry survives for an agent to copy;
* every `Settings` default that names dev's secret namespace is overridden per environment,
  so a branch stack never reads or writes `agp-dev/*`;
* every key in `.env.example` is a real field;
* the task role's Secrets Manager grant carries no literal dev namespace.
"""

import re
from pathlib import Path

import pytest

_CONTROL_PLANE = Path(__file__).resolve().parents[2]
ECS_MAIN_TF = _CONTROL_PLANE / "infrastructure" / "modules" / "ecs" / "main.tf"
CONFIG_PY = _CONTROL_PLANE / "backend" / "src" / "core" / "config.py"
ENV_EXAMPLE = _CONTROL_PLANE / "backend" / ".env.example"

SDK_CONSUMED = {"AWS_DEFAULT_REGION"}

ENVIRONMENT_SCOPED_PREFIXES = {
    "CONNECTIONS_SECRET_PREFIX": "agp-${var.environment}/git-connections/",
    "RUNTIME_BUILD_TOKEN_PREFIX": "agp-${var.environment}/runtime-build-token/",
    "GITHUB_USER_LINK_SECRET_PREFIX": "agp-${var.environment}/github-user-link/",
}


def _settings_fields() -> dict[str, str]:
    text = CONFIG_PY.read_text().split("class Settings(", 1)[1]
    fields: dict[str, str] = {}
    for match in re.finditer(r"^\s{4}([A-Z][A-Z0-9_]+):\s(.*)$", text, re.MULTILINE):
        fields[match.group(1)] = match.group(2)
    return fields


def _ecs_container_block() -> str:
    text = ECS_MAIN_TF.read_text()
    assert "environment = [" in text
    return text.split("environment = [", 1)[1].split("] : []", 1)[0]


def _ecs_names() -> set[str]:
    return set(re.findall(r'name\s*=\s*"([A-Z0-9_]+)"', _ecs_container_block()))


def _secrets_policy_block() -> str:
    text = ECS_MAIN_TF.read_text()
    header = 'resource "aws_iam_role_policy" "ecs_task_secrets_manager" {'
    assert header in text
    return text.split(header, 1)[1].split("\n}", 1)[0]


def test_every_ecs_name_is_a_settings_field():
    unknown = sorted(_ecs_names() - set(_settings_fields()) - SDK_CONSUMED)
    assert unknown == [], f"ECS environment entries to delete: {unknown}"


def test_every_dev_namespaced_default_is_set_by_ecs():
    dev_defaults = {name for name, rhs in _settings_fields().items() if "agp-dev" in rhs}
    missing = sorted(dev_defaults - _ecs_names())
    assert missing == [], f"add to the ECS environment block with agp-${{var.environment}}/...: {missing}"


def test_every_env_example_key_is_a_settings_field():
    keys = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", ENV_EXAMPLE.read_text(), re.MULTILINE))
    unknown = sorted(keys - set(_settings_fields()))
    assert unknown == [], f"not a Settings field: {unknown}"


def test_secrets_policy_has_no_literal_dev_namespace():
    assert "agp-dev/" not in _secrets_policy_block()


@pytest.mark.parametrize("name,value", sorted(ENVIRONMENT_SCOPED_PREFIXES.items()))
def test_secret_prefixes_are_environment_scoped(name, value):
    block = _ecs_container_block()
    assert f'name  = "{name}"\n          value = "{value}"' in block, name
