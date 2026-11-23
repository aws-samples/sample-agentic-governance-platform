"""Native tool discovery for an OpenAPI gateway target.

An Ops-built gateway has ONE target named ``api`` whose ``openApiSchema.inlinePayload`` is
the repo's spec (Terraform ``file(openapi.yaml)`` — so YAML is the common case, JSON is legal).
``_read_gateway_tools_native`` used to read only Lambda inline ``toolSchema`` targets, so an
OpenAPI gateway showed 0 tools and the Cedar policy picker was empty. These pin the mapping:
one ``McpTool`` per operation WITH an ``operationId`` (the invocable name is
``<target>___<operationId>``), description = ``summary`` or ``description``, input schema an
object built from the parameters + the JSON request body.

No live AWS: the control client is a MagicMock.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from services.mcp_identity_service import McpIdentityService

GATEWAY_ID = "demo-claims-gw-aBcDeFgHiJ"

_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Claims", "version": "1"},
    "servers": [{"url": "https://claims.example.com"}],
    "paths": {
        "/claims/{claim_id}": {
            "parameters": [
                {
                    "name": "claim_id",
                    "in": "path",
                    "required": True,
                    "description": "The claim id",
                    "schema": {"type": "string"},
                }
            ],
            "get": {
                "operationId": "getClaim",
                "summary": "Fetch one claim",
                "parameters": [{"name": "verbose", "in": "query", "schema": {"type": "boolean"}}],
            },
            "delete": {"summary": "Delete a claim"},
        },
        "/claims": {
            "post": {
                "operationId": "createClaim",
                "description": "Open a new claim",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"policy_no": {"type": "string"}},
                                "required": ["policy_no"],
                            }
                        }
                    },
                },
            }
        },
    },
}

_YAML_SPEC = """\
openapi: 3.0.3
info: {title: Claims, version: "1"}
paths:
  /claims/{claim_id}:
    get:
      operationId: getClaim
      summary: Fetch one claim
      parameters:
        - name: claim_id
          in: path
          required: true
          schema: {type: string}
    delete:
      summary: Delete a claim
"""


def _service(payload: str) -> tuple[McpIdentityService, MagicMock]:
    control = MagicMock(name="bedrock-agentcore-control")
    control.list_gateway_targets.return_value = {"items": [{"targetId": "tgt-1", "name": "api"}]}
    control.get_gateway_target.return_value = {
        "name": "api",
        "targetConfiguration": {"mcp": {"openApiSchema": {"inlinePayload": payload}}},
    }
    svc = McpIdentityService(
        graph=AsyncMock(),
        registry=MagicMock(),
        control_client=control,
        region="us-east-1",
        tenant_id="00000000-0000-0000-0000-000000000001",
        login_base="https://login.microsoftonline.com",
    )
    return svc, control


def test_json_spec_yields_one_tool_per_operation_with_an_operation_id():
    svc, _ = _service(json.dumps(_SPEC))

    tools = {t.name: t for t in svc._read_gateway_tools_native(GATEWAY_ID)}

    assert sorted(tools) == ["api___createClaim", "api___getClaim"]
    assert tools["api___getClaim"].description == "Fetch one claim"
    assert tools["api___createClaim"].description == "Open a new claim"


def test_input_schema_merges_path_and_operation_parameters():
    svc, _ = _service(json.dumps(_SPEC))

    tools = {t.name: t for t in svc._read_gateway_tools_native(GATEWAY_ID)}

    assert tools["api___getClaim"].input_schema == {
        "type": "object",
        "properties": {
            "claim_id": {"type": "string", "description": "The claim id"},
            "verbose": {"type": "boolean"},
        },
        "required": ["claim_id"],
    }


def test_input_schema_carries_the_json_request_body():
    svc, _ = _service(json.dumps(_SPEC))

    tools = {t.name: t for t in svc._read_gateway_tools_native(GATEWAY_ID)}

    assert tools["api___createClaim"].input_schema == {
        "type": "object",
        "properties": {"policy_no": {"type": "string"}},
        "required": ["policy_no"],
    }


def test_yaml_spec_is_parsed_too():
    svc, _ = _service(_YAML_SPEC)

    tools = svc._read_gateway_tools_native(GATEWAY_ID)

    assert [t.name for t in tools] == ["api___getClaim"]
    assert tools[0].input_schema["required"] == ["claim_id"]


def test_unparseable_or_s3_only_spec_is_skipped_not_fatal():
    svc, control = _service("{ not: json: nor: [yaml")
    assert svc._read_gateway_tools_native(GATEWAY_ID) == []

    control.get_gateway_target.return_value = {
        "name": "api",
        "targetConfiguration": {"mcp": {"openApiSchema": {"s3": {"uri": "s3://bucket/openapi.yaml"}}}},
    }
    assert svc._read_gateway_tools_native(GATEWAY_ID) == []


def test_yaml_dates_are_normalised_so_the_tools_serialise():
    spec = _YAML_SPEC.replace(
        "schema: {type: string}",
        "schema: {type: string, example: 2026-10-05, default: 2026-10-05T12:00:00Z}",
    )
    svc, _ = _service(spec)

    tools = svc._read_gateway_tools_native(GATEWAY_ID)

    prop = tools[0].input_schema["properties"]["claim_id"]
    assert isinstance(prop["example"], str) and prop["example"].startswith("2026-10-05")
    assert isinstance(prop["default"], str) and prop["default"].startswith("2026-10-05")
    json.dumps([t.model_dump() for t in tools])


def test_yaml_date_as_a_mapping_key_stays_a_string():
    spec = _YAML_SPEC.replace("schema: {type: string}", "schema: {type: string, examples: {2026-10-05: x}}")
    svc, _ = _service(spec)

    tools = svc._read_gateway_tools_native(GATEWAY_ID)

    examples = tools[0].input_schema["properties"]["claim_id"]["examples"]
    assert examples == {"2026-10-05": "x"}


def test_a_self_referencing_spec_skips_that_target_only():
    looped = "openapi: 3.0.3\nloop: &a [*a]\npaths: {}\n"
    svc, control = _service(looped)
    control.list_gateway_targets.return_value = {
        "items": [{"targetId": "tgt-bad", "name": "bad"}, {"targetId": "tgt-ok", "name": "api"}]
    }
    control.get_gateway_target.side_effect = [
        {"name": "bad", "targetConfiguration": {"mcp": {"openApiSchema": {"inlinePayload": looped}}}},
        {
            "name": "api",
            "targetConfiguration": {"mcp": {"openApiSchema": {"inlinePayload": json.dumps(_SPEC)}}},
        },
    ]

    tools = svc._read_gateway_tools_native(GATEWAY_ID)

    assert sorted(t.name for t in tools) == ["api___createClaim", "api___getClaim"]
