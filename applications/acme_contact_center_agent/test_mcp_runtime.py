import json

from mcp_runtime import namespace_tool_name, parse_mcp_servers


def test_parse_reads_mcp_servers_json():
    env = {
        "MCP_SERVERS": json.dumps(
            [{"id": "A", "audience": "api://agp-mcp-A", "gateway_url": "https://a/mcp", "label": "alpha"}]
        )
    }
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
    assert parse_mcp_servers({"MCP_SERVERS": '{"a": 1}'}) == []


def test_namespace_tool_name():
    assert namespace_tool_name("alpha", "search") == "alpha__search"


LIVE = ("acme_contact_center_mcp", "agp-contact-center-mcp-target___create_support_ticket")


def test_short_names_are_unchanged():
    assert namespace_tool_name("alpha", "lookup") == "alpha__lookup"


def test_a_long_gateway_name_drops_the_target_prefix():
    assert namespace_tool_name(*LIVE) == "acme_contact_center_mcp__create_support_ticket"


def test_names_never_exceed_bedrock_limit_and_stay_unique_and_stable():
    a = namespace_tool_name("l" * 40, "t" * 40 + "_a")
    b = namespace_tool_name("l" * 40, "t" * 40 + "_b")
    assert len(a) <= 64 and len(b) <= 64
    assert a != b
    assert a == namespace_tool_name("l" * 40, "t" * 40 + "_a")
