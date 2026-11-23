"""Contract tests for the Langfuse module's ECS services (`modules/langfuse/ecs.tf`).

String-level, like test_infra_root_contract.py: nothing offline applies the module. What these
pin is the order of a roll, which Langfuse fixes and Terraform would otherwise leave to chance:
ClickHouse serves before the web container migrates against it, and the web container has
migrated before the worker validates its background migrations.
"""

import re
from pathlib import Path

import pytest

ECS_TF = Path(__file__).resolve().parents[2] / "infrastructure" / "modules" / "langfuse" / "ecs.tf"


def _code(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(("#", "//")))


def _block(text: str, header: str) -> str:
    assert text.count(header) == 1, header
    return text.split(header, 1)[1].split("\n}", 1)[0]


def _depends_on(block: str) -> set[str]:
    found = re.search(r"^\s*depends_on\s*=\s*\[([^\]]*)\]", block, re.MULTILINE)
    return {item.strip() for item in found.group(1).split(",") if item.strip()} if found else set()


def _waits(block: str) -> bool:
    return re.search(r"^\s*wait_for_steady_state\s*=\s*true\s*$", block, re.MULTILINE) is not None


def roll_order_findings(text: str) -> list[str]:
    code = _code(text)
    clickhouse = _block(code, 'resource "aws_ecs_service" "clickhouse" {')
    web = _block(code, 'resource "aws_ecs_service" "langfuse" {')
    worker = _block(code, 'resource "aws_ecs_service" "langfuse-worker" {')
    findings = []
    if not _waits(clickhouse):
        findings.append("the clickhouse service does not wait for its steady state")
    if "aws_ecs_service.clickhouse" not in _depends_on(web):
        findings.append("the web service does not depend on the clickhouse service")
    if not _waits(web):
        findings.append("the web service does not wait for its steady state")
    if "aws_ecs_service.langfuse" not in _depends_on(worker):
        findings.append("the worker service does not depend on the web service")
    return findings


def test_the_three_services_roll_in_order_and_each_waits_for_the_one_before():
    assert roll_order_findings(ECS_TF.read_text()) == []


@pytest.mark.parametrize(
    ("old", "new", "finding"),
    [
        (
            '  wait_for_steady_state = true\n\n  tags = {\n    Name = "${local.tag_name} ClickHouse Service"',
            '  tags = {\n    Name = "${local.tag_name} ClickHouse Service"',
            "the clickhouse service does not wait for its steady state",
        ),
        (
            "depends_on = [aws_lb_listener.langfuse, aws_ecs_service.clickhouse]",
            "depends_on = [aws_lb_listener.langfuse]",
            "the web service does not depend on the clickhouse service",
        ),
        (
            "  wait_for_steady_state = true\n\n  depends_on = [aws_lb_listener.langfuse",
            "  wait_for_steady_state = false\n\n  depends_on = [aws_lb_listener.langfuse",
            "the web service does not wait for its steady state",
        ),
        (
            "  depends_on = [aws_ecs_service.langfuse]",
            "  # depends_on = [aws_ecs_service.langfuse]",
            "the worker service does not depend on the web service",
        ),
    ],
)
def test_a_roll_out_of_order_is_a_finding(old, new, finding):
    text = ECS_TF.read_text()
    assert text.count(old) == 1, old
    assert roll_order_findings(text.replace(old, new)) == [finding]


def test_web_and_worker_carry_the_same_v4_migration_environment():
    code = _code(ECS_TF.read_text())
    for name in ("langfuse_worker", "langfuse"):
        definition = _block(code, f'resource "aws_ecs_task_definition" "{name}" {{')
        assert definition.count("], local.v4_migration_env)") == 1, name
