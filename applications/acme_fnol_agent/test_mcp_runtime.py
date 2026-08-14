import json
from mcp_runtime import parse_mcp_servers, namespace_tool_name


def test_parse_reads_mcp_servers_json():
    env = {"MCP_SERVERS": json.dumps([
        {"id": "A", "audience": "api://agp-mcp-A", "gateway_url": "https://a/mcp", "label": "alpha"}])}
    out = parse_mcp_servers(env)
    assert len(out) == 1 and out[0]["label"] == "alpha"


def test_parse_legacy_fallback():
    env = {"MCP_AUDIENCE": "api://agp-mcp-X", "MCP_GATEWAY_URL": "https://x/mcp"}
    out = parse_mcp_servers(env)
    assert len(out) == 1
    assert out[0]["audience"] == "api://agp-mcp-X"
    assert out[0]["gateway_url"] == "https://x/mcp"
    assert out[0]["label"] == "mcp"


def test_parse_empty_when_nothing_set():
    assert parse_mcp_servers({}) == []


def test_parse_empty_list_is_empty():
    assert parse_mcp_servers({"MCP_SERVERS": "[]"}) == []


def test_parse_malformed_json_degrades_to_empty():
    assert parse_mcp_servers({"MCP_SERVERS": "{not json"}) == []


def test_parse_valid_json_but_not_a_list_degrades_to_empty():
    # A JSON object (not a list) is well-formed but the wrong shape — degrade to [], never crash.
    assert parse_mcp_servers({"MCP_SERVERS": '{"a": 1}'}) == []


def test_namespace_tool_name():
    assert namespace_tool_name("alpha", "search") == "alpha__search"
