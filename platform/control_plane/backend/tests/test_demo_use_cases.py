"""Offline tests for the three demo use-case AgentCore gateways (design §8).

All tests are OFFLINE (mocks / dry-run), matching the project's mock<->live division of
labor (the live create is the user's AWS step). Following the suite idiom
(`test_script_properties.py`, `conftest.py`): a MagicMock `boto3` is injected into
sys.modules so the scripts import without a real boto3, and clients are MagicMocks (NO
moto — research §10).

Coverage (design §8):
  1. Registry integrity + invoke each handler via a fake context
     bedrockAgentCoreToolName=="x___<tool>" returns a dict; unknown raises ValueError.
  2. build_create_target_kwargs(tool_schema=X) lands at
     targetConfiguration.mcp.lambda.toolSchema.inlinePayload; omitting it yields
     _DEMO_TOOL_SCHEMA (backward-compat).
  3. bootstrap_demo_use_cases --dry-run makes ZERO boto3 calls + the drift guard raises
     on a bad (in-test) registry entry.
  4. Continue-on-error: one domain raises, the others are still processed, the summary
     marks the failure, and the exit code is non-zero.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_mock_boto3 = MagicMock(name="boto3")
_had_boto3 = "boto3" in sys.modules
_original_boto3 = sys.modules.get("boto3")
sys.modules["boto3"] = _mock_boto3

_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

bootstrap = importlib.import_module("scripts.bootstrap_demo_use_cases")
_create_example_gateway = importlib.import_module("scripts.create_example_gateway")
_DEMO_TOOL_SCHEMA = _create_example_gateway._DEMO_TOOL_SCHEMA
build_create_target_kwargs = _create_example_gateway.build_create_target_kwargs
DEMO_USE_CASES = importlib.import_module("scripts.demo_use_cases").DEMO_USE_CASES

if _had_boto3:
    assert _original_boto3 is not None
    sys.modules["boto3"] = _original_boto3
else:
    del sys.modules["boto3"]


class _FakeClientContext:
    """``context.client_context.custom`` carries the tool name namespaced as
    ``<targetName>___<toolName>``; the shared handler preamble splits on ``___``."""

    def __init__(self, tool_name: str):
        self.custom = {"bedrockAgentCoreToolName": f"x___{tool_name}"}


class _FakeContext:
    def __init__(self, tool_name: str):
        self.client_context = _FakeClientContext(tool_name)


def _load_handler(domain: dict, tmp_path: Path):
    """Write the domain's handler_src to a module file, import it the way the Lambda runtime
    does, and return its `handler` callable."""
    path = tmp_path / f"{domain['key']}_index.py"
    path.write_text(domain["handler_src"])
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.handler


_SAMPLE_EVENT = {
    "customer_id": "CUST-1",
    "policy_number": "POL-1",
    "claim_id": "CLM-1",
    "ticket_id": "TKT-1",
    "postal_code": "80331",
    "service_type": "body",
    "last_name": "Bauer",
    "date_of_birth": "1980-01-01",
    "subject": "demo",
    "priority": "HIGH",
    "description": "demo issue",
    "phone": "+49 89 1",
    "preferred_time": "2026-06-10T10:00:00Z",
    "loss_type": "collision",
    "loss_date": "2026-06-01",
    "document_type": "photo",
    "file_name": "a.jpg",
    "product": "auto",
    "coverage_level": "standard",
    "customer_age": 40,
    "change_type": "add_driver",
    "details": "demo",
    "email": "x@example.com",
    "address": "Demo Str 1",
    "limit": 2,
}


class TestRegistryIntegrity:
    def test_three_domains_with_unique_prefixes_and_keys(self):
        assert len(DEMO_USE_CASES) == 3
        prefixes = [d["prefix"] for d in DEMO_USE_CASES]
        keys = [d["key"] for d in DEMO_USE_CASES]
        assert len(set(prefixes)) == 3, f"prefixes not unique: {prefixes}"
        assert len(set(keys)) == 3, f"keys not unique: {keys}"

    def test_default_run_yields_the_three_expected_gateway_names(self):
        """Default run (no --only) must produce the three -mcp gateway names (design §5)."""
        gateways = {bootstrap._names(d["prefix"])["gateway"] for d in DEMO_USE_CASES}
        assert gateways == {
            "agp-contact-center-mcp",
            "agp-fnol-mcp",
            "agp-insurance-support-mcp",
        }

    @pytest.mark.parametrize("domain", DEMO_USE_CASES, ids=lambda d: d["key"])
    def test_tool_schema_shape(self, domain):
        """Every tool is {name, description, inputSchema} with inputSchema.type=='object'
        and there are exactly 6 tools (design §6)."""
        schema = domain["tool_schema"]
        assert len(schema) == 6, f"{domain['key']} has {len(schema)} tools (expected 6)"
        names = [t["name"] for t in schema]
        assert len(set(names)) == 6, f"{domain['key']} tool names not unique: {names}"
        for tool in schema:
            assert set(tool) >= {"name", "description", "inputSchema"}
            assert tool["inputSchema"]["type"] == "object"

    @pytest.mark.parametrize("domain", DEMO_USE_CASES, ids=lambda d: d["key"])
    def test_handler_parses_and_every_tool_has_a_branch(self, domain):
        """ast.parse succeeds and the bootstrap's drift guard passes (no schema/handler
        drift)."""
        bootstrap._validate_handler_src(domain)

    def test_use_case_resource_names_carry_no_demo(self):
        """The use-case gateways are not the echo/add demo: no created resource name, the
        gateway role's inline policy included, carries ``demo``."""
        assert "demo" not in bootstrap._GATEWAY_INVOKE_POLICY_NAME
        for domain in DEMO_USE_CASES:
            for name in bootstrap._names(domain["prefix"]).values():
                assert "demo" not in name, name

    @pytest.mark.parametrize("domain", DEMO_USE_CASES, ids=lambda d: d["key"])
    def test_every_tool_returns_a_dict(self, domain, tmp_path):
        """Invoking the handler for each tool (via a fake context) returns a dict. The sample
        event is a superset of every input id any tool reads; handlers only ``.get()`` what
        they need."""
        handler = _load_handler(domain, tmp_path)
        for tool in domain["tool_schema"]:
            result = handler(dict(_SAMPLE_EVENT), _FakeContext(tool["name"]))
            assert isinstance(result, dict), f"{domain['key']}/{tool['name']} not a dict"

    @pytest.mark.parametrize("domain", DEMO_USE_CASES, ids=lambda d: d["key"])
    def test_unknown_tool_raises_value_error(self, domain, tmp_path):
        handler = _load_handler(domain, tmp_path)
        with pytest.raises(ValueError):
            handler({}, _FakeContext("definitely_not_a_real_tool"))


class TestBuildTargetKwargsToolSchema:
    def test_passed_tool_schema_lands_in_inline_payload(self):
        domain = DEMO_USE_CASES[0]
        kwargs = build_create_target_kwargs(
            gateway_identifier="gw-1",
            target_name="t-1",
            lambda_arn="arn:aws:lambda:us-east-1:123456789012:function:f",
            tool_schema=domain["tool_schema"],
        )
        inline = kwargs["targetConfiguration"]["mcp"]["lambda"]["toolSchema"]["inlinePayload"]
        assert inline is domain["tool_schema"]

    def test_omitting_tool_schema_preserves_demo_schema(self):
        kwargs = build_create_target_kwargs(
            gateway_identifier="gw-1",
            target_name="t-1",
            lambda_arn="arn:aws:lambda:us-east-1:123456789012:function:f",
        )
        inline = kwargs["targetConfiguration"]["mcp"]["lambda"]["toolSchema"]["inlinePayload"]
        assert inline == _DEMO_TOOL_SCHEMA


class TestDryRunZeroBoto3:
    def test_dry_run_makes_zero_boto3_calls(self, monkeypatch, capsys):
        """--dry-run must construct NO client. Inject a tripwire boto3 that fails if
        boto3.client is ever called, run main(["--dry-run"]), assert exit 0."""
        tripwire = MagicMock(name="boto3-tripwire")
        tripwire.client.side_effect = AssertionError("boto3.client must NOT be called on the --dry-run path")
        monkeypatch.setitem(sys.modules, "boto3", tripwire)

        rc = bootstrap.main(["--dry-run"])

        assert rc == 0
        tripwire.client.assert_not_called()
        out = capsys.readouterr().out
        assert "agp-contact-center-mcp" in out
        assert "agp-fnol-mcp" in out
        assert "agp-insurance-support-mcp" in out

    def test_drift_guard_raises_on_a_bad_entry(self):
        """A tool advertised in tool_schema with no branch in handler_src must fail the
        drift guard (ast.parse passes; the branch check catches the drift)."""
        bad_domain = {
            "key": "bad",
            "prefix": "agp-demo-bad",
            "display_name": "Bad",
            "description": "bad",
            "tool_schema": [
                {
                    "name": "tool_without_a_branch",
                    "description": "no matching branch",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ],
            "handler_src": "def handler(event, context):\n    return {}\n",
        }
        with pytest.raises(ValueError) as exc:
            bootstrap._validate_handler_src(bad_domain)
        assert "tool_without_a_branch" in str(exc.value)

    def test_dry_run_fails_when_a_selected_domain_drifts(self, monkeypatch):
        """If a selected domain drifts, the --dry-run path returns non-zero (exit 1)."""
        bad_domain = {
            "key": "bad",
            "prefix": "agp-demo-bad",
            "display_name": "Bad",
            "description": "bad",
            "tool_schema": [
                {
                    "name": "tool_without_a_branch",
                    "description": "no matching branch",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ],
            "handler_src": "def handler(event, context):\n    return {}\n",
        }
        monkeypatch.setattr(bootstrap, "DEMO_USE_CASES", [bad_domain])
        rc = bootstrap.main(["--dry-run"])
        assert rc == 1


class TestContinueOnError:
    def test_one_domain_fails_others_succeed_and_exit_nonzero(self, monkeypatch, capsys, tmp_path):
        processed = []

        def fake_bootstrap_one(region, tenant_id, domain):
            processed.append(domain["key"])
            if domain["key"] == "fnol":
                raise RuntimeError("simulated AWS failure for fnol")
            return {
                "key": domain["key"],
                "display_name": domain["display_name"],
                "exec_role_name": f"{domain['prefix']}-mcp-lambda-exec-role",
                "exec_role_arn": "arn:aws:iam::123456789012:role/exec",
                "lambda_name": f"{domain['prefix']}-mcp-tools",
                "lambda_arn": "arn:aws:lambda:us-east-1:123456789012:function:f",
                "gateway_role_name": f"{domain['prefix']}-gateway-role",
                "gateway_role_arn": "arn:aws:iam::123456789012:role/gw",
                "gateway_name": f"{domain['prefix']}-mcp",
                "gateway_url": f"https://{domain['key']}.example/mcp",
                "gateway_arn": f"arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/{domain['key']}",
            }

        monkeypatch.setattr(bootstrap, "bootstrap_one", fake_bootstrap_one)
        out_file = tmp_path / "out.txt"

        rc = bootstrap.main(["--output-file", str(out_file)])

        assert rc == 1
        assert processed == ["contact-center", "fnol", "insurance-support"]

        out = capsys.readouterr().out
        assert "2 succeeded, 1 failed" in out
        assert "FAIL fnol" in out
        assert "OK   contact-center" in out
        assert "OK   insurance-support" in out

        body = out_file.read_text(encoding="utf-8")
        assert "agp-contact-center-mcp" in body
        assert "agp-insurance-support-mcp" in body
        assert "agp-fnol-mcp" not in body
