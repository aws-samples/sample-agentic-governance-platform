import asyncio
import contextlib
import json

from mcp.types import Tool as MCPTool
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool

import src.main as main_module
from src.main import handler
from src.mcp_runtime import namespace_tool_name, parse_mcp_servers


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


# --- the wiring in src/main.py --------------------------------------------------------------
# The parser tests above prove the contract is read correctly; these prove the agent still ANSWERS
# whatever the MCP wiring does. That is the property worth pinning: an agent whose tools are
# missing is degraded, an agent that raises out of an invoke is down, and every failure mode here
# (unconfigured, half-configured, unreachable gateway) must land in the first category.

_TERMINAL_MESSAGE = {"role": "assistant", "content": [{"text": "answered"}]}
_ONE_SERVER = json.dumps(
    [{"id": "A", "audience": "api://agp-mcp-A", "gateway_url": "https://a/mcp", "label": "alpha"}]
)


class _StubAgent:
    """Stands in for the module-level agent — including the `messages` list `_build_agent` reuses."""

    def __init__(self):
        self.messages: list = []

    async def stream_async(self, _prompt):
        yield {"data": "answered"}
        yield {"message": _TERMINAL_MESSAGE}


def _drain(prompt="hello"):
    async def _collect():
        return [chunk async for chunk in handler({"prompt": prompt})]

    return asyncio.run(_collect())


def _mcp_agent_tool(client, name="raw-tool"):
    """A REAL strands ``MCPAgentTool`` over a real ``mcp.types.Tool`` — the wrap is never stubbed.

    ``_namespaced_tool``'s whole contract lives inside this object (``name_override`` vs the wire
    ``mcp_tool.name``), so a test that substitutes it can only re-assert its own substitute.
    """
    return MCPAgentTool(MCPTool(name=name, inputSchema={"type": "object"}), client)


async def _one_word_model_stream(_self, _messages, tool_specs=None, system_prompt=None, **_kwargs):
    """Bedrock's ``stream`` reduced to the four events strands needs to emit a one-word answer.

    Stubbed at the MODEL, not at the agent, so the tests below drive the real ``_build_agent`` —
    the tool registry and the shared ``messages`` list are then the real ones.
    """
    yield {"messageStart": {"role": "assistant"}}
    yield {"contentBlockDelta": {"delta": {"text": "hi"}}}
    yield {"contentBlockStop": {}}
    yield {"messageStop": {"stopReason": "end_turn"}}


def test_handler_answers_with_no_mcp_configured(monkeypatch):
    """Bare template: nothing MCP is set, so the module-level agent answers untouched."""
    monkeypatch.setattr(main_module, "agent", _StubAgent())
    monkeypatch.setattr(main_module.config, "MCP_SERVERS", "")
    monkeypatch.setattr(main_module.config, "MCP_AUDIENCE", "")
    monkeypatch.setattr(main_module.config, "MCP_GATEWAY_URL", "")
    assert [c["message"] for c in _drain() if "message" in c] == [_TERMINAL_MESSAGE]


def test_no_mcp_means_no_tools_and_no_token_call(monkeypatch):
    monkeypatch.setattr(main_module.config, "MCP_SERVERS", "")
    monkeypatch.setattr(main_module.config, "MCP_AUDIENCE", "")
    monkeypatch.setattr(main_module.config, "MCP_GATEWAY_URL", "")

    def _fail(**_kwargs):
        raise AssertionError("no MCP configured — the OBO token must not be requested")

    monkeypatch.setattr(main_module, "get_mcp_obo_token", _fail)
    with contextlib.ExitStack() as stack:
        assert main_module._mcp_tools(stack) == []


def test_missing_credential_provider_degrades_to_no_tools(monkeypatch):
    """Servers granted but no provider to exchange a token with: drop the tools, never guess."""
    monkeypatch.setattr(main_module.config, "MCP_SERVERS", _ONE_SERVER)
    monkeypatch.setattr(main_module.config, "CREDENTIAL_PROVIDER_NAME", "")
    with contextlib.ExitStack() as stack:
        assert main_module._mcp_tools(stack) == []


def test_unreachable_mcp_is_dropped_and_the_agent_still_answers(monkeypatch):
    """A refused token / dead gateway drops that MCP; the invoke still returns an answer."""
    monkeypatch.setattr(main_module, "agent", _StubAgent())
    monkeypatch.setattr(main_module.config, "MCP_SERVERS", _ONE_SERVER)
    monkeypatch.setattr(main_module.config, "CREDENTIAL_PROVIDER_NAME", "agp-provider")
    monkeypatch.setattr(
        main_module.BedrockAgentCoreContext,
        "get_workload_access_token",
        staticmethod(lambda: "wat-token"),
    )

    def _boom(**_kwargs):
        raise RuntimeError("AccessDeniedException: bedrock-agentcore:GetResourceOauth2Token")

    monkeypatch.setattr(main_module, "get_mcp_obo_token", _boom)

    with contextlib.ExitStack() as stack:
        assert main_module._mcp_tools(stack) == []
    assert [c["message"] for c in _drain() if "message" in c] == [_TERMINAL_MESSAGE]


def test_wired_mcp_tools_are_namespaced_for_the_model_not_the_wire(monkeypatch):
    """The happy path, with only the CONNECTION stubbed: tools arrive namespaced `{label}__{name}`.

    The real ``_namespaced_tool`` runs over a real ``MCPAgentTool``, so this pins strands' own
    ``name_override`` semantics: the model is shown the namespaced name while the gateway is still
    called with the raw one. That split is the thing a strands upgrade could break silently, and it
    is unassertable if the wrap itself is monkeypatched.
    """
    monkeypatch.setattr(main_module.config, "MCP_SERVERS", _ONE_SERVER)
    monkeypatch.setattr(main_module.config, "CREDENTIAL_PROVIDER_NAME", "agp-provider")
    monkeypatch.setattr(
        main_module.BedrockAgentCoreContext,
        "get_workload_access_token",
        staticmethod(lambda: "wat-token"),
    )
    monkeypatch.setattr(main_module, "get_mcp_obo_token", lambda **_kwargs: "obo-token")

    entered: list = []

    class _StubMCPClient:
        def __init__(self, _transport_factory):
            self.closed = False

        def __enter__(self):
            entered.append(self)
            return self

        def __exit__(self, *_exc):
            self.closed = True
            return False

        def list_tools_sync(self):
            return [_mcp_agent_tool(self)]

    monkeypatch.setattr(main_module, "MCPClient", _StubMCPClient)

    with contextlib.ExitStack() as stack:
        tools = main_module._mcp_tools(stack)
    assert len(tools) == 1
    # What the model is offered, and what the traces name: namespaced.
    assert tools[0].tool_name == "alpha__raw-tool"
    assert tools[0].tool_spec["name"] == "alpha__raw-tool"
    # What the gateway is actually called with: the tool's own name, unrenamed — and over the same
    # connection, so a re-wrap costs no second session.
    assert tools[0].mcp_tool.name == "raw-tool"
    assert tools[0].mcp_client is entered[0]
    # The connection is opened for the invoke and closed when the stack unwinds — never leaked.
    assert len(entered) == 1
    assert entered[0].closed is True


def test_wired_mcp_tools_rebuild_the_agent_and_continue_the_conversation(monkeypatch):
    """The SUCCESS path through the handler: tools wired ⇒ a rebuilt agent answers, same history.

    Two properties, both invisible in a degrade test: the rebuilt agent gets the MCP tool ALONGSIDE
    the built-ins, and it is handed the module-level agent's OWN ``messages`` list (by reference),
    so granting an MCP does not silently cost this template the conversation memory it advertises.
    """
    stub = _StubAgent()
    prior_turn = {"role": "user", "content": [{"text": "earlier"}]}
    stub.messages.append(prior_turn)
    monkeypatch.setattr(main_module, "agent", stub)
    # Exactly what `_mcp_tools` returns on the wired path (test above), minus the live connection.
    wired_tool = main_module._namespaced_tool(_mcp_agent_tool(None), "alpha")
    monkeypatch.setattr(main_module, "_mcp_tools", lambda _stack: [wired_tool])
    monkeypatch.setattr(main_module.BedrockModel, "stream", _one_word_model_stream)

    built: list = []
    real_build = main_module._build_agent

    def _spy_build(extra_tools=(), messages=None):
        built.append(real_build(extra_tools, messages=messages))
        return built[-1]

    monkeypatch.setattr(main_module, "_build_agent", _spy_build)

    chunks = _drain()

    assert len(built) == 1, "MCP tools were wired, so the agent must be rebuilt for this invoke"
    rebuilt = built[0]
    assert rebuilt is not stub
    # Reference-shared, not copied: strands appends to this very list, so the next turn continues.
    assert rebuilt.messages is stub.messages
    assert stub.messages[0] is prior_turn, "the earlier turn must survive the rebuild"
    assert sorted(rebuilt.tool_names) == ["alpha__raw-tool", "calculator", "get_current_datetime"]
    # And it still answers, in both event shapes the response contract requires.
    assert [c["data"] for c in chunks if c.get("type") == "content"] == ["hi"]
    terminal = [c["message"] for c in chunks if "message" in c][-1]
    assert "".join(p["text"] for p in terminal["content"] if "text" in p) == "hi"
