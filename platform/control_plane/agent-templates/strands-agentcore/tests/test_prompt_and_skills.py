"""E45: the system prompt lives in `prompt/system.md`, skills in `skills/<name>/SKILL.md`, and the
agent is told which configured MCP servers refused it this invoke."""

import asyncio
import contextlib
import json

from mcp.types import Tool as MCPTool
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool

import src.main as main_module
from src.main import handler, load_system_prompt, refusal_note

_SKILL_MD = """---
name: claims-intake
description: Collect the facts of a new insurance claim.
---
SKILL-BODY-SENTINEL-7f3a
"""


def _capturing_stream(seen: list):
    async def _stream(_self, _messages, tool_specs=None, system_prompt=None, **_kwargs):
        seen.append(system_prompt)
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"delta": {"text": "hi"}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}

    return _stream


def _drain(payload):
    async def _collect():
        return [chunk async for chunk in handler(payload)]

    return asyncio.run(_collect())


# --- (a) prompt/system.md ------------------------------------------------------------------


def test_load_system_prompt_reads_prompt_file(tmp_path):
    (tmp_path / "prompt").mkdir()
    (tmp_path / "prompt" / "system.md").write_text("You are the claims assistant.\n")
    assert load_system_prompt(tmp_path) == "You are the claims assistant.\n"


def test_load_system_prompt_falls_back_to_default(tmp_path):
    assert load_system_prompt(tmp_path) == main_module.DEFAULT_SYSTEM_PROMPT


def test_shipped_prompt_file_is_the_prompt_the_agent_runs_with():
    """The repo's `prompt/system.md` is the prompt the agent runs with. Its text is not asserted:
    a Studio repo commits its own prompt, and this test runs in that repo's CI too."""
    assert (main_module.APP_ROOT / "prompt" / "system.md").is_file()
    assert main_module.SYSTEM_PROMPT == load_system_prompt(main_module.APP_ROOT)


# --- (b)/(c) skills/ ------------------------------------------------------------------------


def test_skill_folder_gives_a_skills_tool_and_lists_it_in_the_prompt(tmp_path, monkeypatch):
    (tmp_path / "skills" / "claims-intake").mkdir(parents=True)
    (tmp_path / "skills" / "claims-intake" / "SKILL.md").write_text(_SKILL_MD)
    monkeypatch.setattr(main_module, "APP_ROOT", tmp_path)
    seen: list = []
    monkeypatch.setattr(main_module.BedrockModel, "stream", _capturing_stream(seen))

    built = main_module._build_agent()
    assert "skills" in built.tool_names
    built("hello")  # one invocation fires BeforeInvocationEvent, which injects the skill list

    assert "claims-intake" in built.system_prompt
    assert "claims-intake" in seen[0]
    # Only name + description go into the prompt; the body is loaded on demand by the tool.
    assert "SKILL-BODY-SENTINEL-7f3a" not in built.system_prompt


def test_no_skill_folders_still_builds(tmp_path, monkeypatch):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "README.md").write_text("how to add a skill")
    monkeypatch.setattr(main_module, "APP_ROOT", tmp_path)
    built = main_module._build_agent()
    assert sorted(built.tool_names) == ["calculator", "get_current_datetime"]


def test_missing_skills_dir_still_builds(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "APP_ROOT", tmp_path)
    built = main_module._build_agent()
    assert sorted(built.tool_names) == ["calculator", "get_current_datetime"]


# --- (d) refusal_note ------------------------------------------------------------------------


def test_refusal_note_names_the_server_and_says_to_admit_it():
    note = refusal_note(["claims-api"])
    assert "claims-api" in note
    assert "cannot access" in note
    assert note.count(".") == 1 and note.endswith(".")


def test_refusal_note_empty_when_nothing_refused():
    assert refusal_note([]) == ""


# --- (e) _mcp_tools records refused servers -------------------------------------------------

_TWO_SERVERS = json.dumps(
    [
        {"id": "A", "audience": "api://agp-mcp-A", "gateway_url": "https://a/mcp", "label": "alpha"},
        {"id": "B", "audience": "api://agp-mcp-B", "gateway_url": "https://b/mcp", "label": "beta"},
        {"id": "C", "audience": "api://agp-mcp-C", "gateway_url": "https://c/mcp", "label": "gamma"},
    ]
)


def _wire(monkeypatch):
    """A refused token for `alpha`, a failed connect for `gamma`, a working `beta`."""
    monkeypatch.setattr(main_module.config, "MCP_SERVERS", _TWO_SERVERS)
    monkeypatch.setattr(main_module.config, "CREDENTIAL_PROVIDER_NAME", "agp-provider")
    monkeypatch.setattr(
        main_module.BedrockAgentCoreContext, "get_workload_access_token", staticmethod(lambda: "wat")
    )

    def _token(*, scopes, **_kwargs):
        if scopes == ["api://agp-mcp-A/.default"]:
            raise RuntimeError("AADSTS65001: consent required")
        return "obo-token"

    monkeypatch.setattr(main_module, "get_mcp_obo_token", _token)

    class _StubMCPClient:
        def __init__(self, transport_factory):
            self.url = transport_factory.__defaults__[1]

        def __enter__(self):
            if self.url == "https://c/mcp":
                raise ConnectionError("gateway unreachable")
            return self

        def __exit__(self, *_exc):
            return False

        def list_tools_sync(self):
            return [MCPAgentTool(MCPTool(name="lookup", inputSchema={"type": "object"}), self)]

    monkeypatch.setattr(main_module, "MCPClient", _StubMCPClient)


def test_mcp_tools_records_refused_servers_and_keeps_the_rest(monkeypatch):
    _wire(monkeypatch)
    refused: list = []
    with contextlib.ExitStack() as stack:
        tools = main_module._mcp_tools(stack, refused)
    assert [t.tool_name for t in tools] == ["beta__lookup"]
    assert refused == ["alpha", "gamma"]


def test_handler_tells_the_model_which_servers_refused(monkeypatch):
    """End to end: a refused server's name reaches that invoke's system prompt; the next
    invoke without refusals does not carry it over."""
    seen: list = []
    monkeypatch.setattr(main_module.BedrockModel, "stream", _capturing_stream(seen))
    monkeypatch.setattr(main_module, "agent", main_module._build_agent())
    monkeypatch.setattr(main_module, "_mcp_tools", lambda _stack, refused: refused.append("claims-api") or [])

    _drain({"prompt": "what is my claim status?"})
    assert seen[-1].startswith(main_module.SYSTEM_PROMPT)
    assert refusal_note(["claims-api"]) in seen[-1]

    monkeypatch.setattr(main_module, "_mcp_tools", lambda _stack, _refused: [])
    _drain({"prompt": "and now?"})
    assert "claims-api" not in seen[-1]


def test_missing_credential_provider_marks_every_server_refused(monkeypatch):
    monkeypatch.setattr(main_module.config, "MCP_SERVERS", _TWO_SERVERS)
    monkeypatch.setattr(main_module.config, "CREDENTIAL_PROVIDER_NAME", "")
    refused: list = []
    with contextlib.ExitStack() as stack:
        assert main_module._mcp_tools(stack, refused) == []
    assert refused == ["alpha", "beta", "gamma"]


# --- one conversation, one invoke at a time ---------------------------------------------------


def test_concurrent_invoke_fails_fast_instead_of_sharing_the_conversation(monkeypatch):
    """A rebuilt agent shares `agent.messages`, so the THROW guard must span rebuilds too."""
    seen: list = []
    monkeypatch.setattr(main_module.BedrockModel, "stream", _capturing_stream(seen))
    monkeypatch.setattr(main_module, "agent", main_module._build_agent())
    monkeypatch.setattr(main_module, "_mcp_tools", lambda _stack, refused: refused.append("x") or [])

    assert main_module._conversation_lock.acquire(blocking=False)  # an invoke is in flight
    try:
        chunks = _drain({"prompt": "second caller"})
    finally:
        main_module._conversation_lock.release()
    assert [c["type"] for c in chunks] == ["error"]
    assert "Concurrent invocations are not supported" in chunks[0]["data"]
    assert seen == [], "the model must not be called while another invoke holds the conversation"

    # Released on the way out: the next invoke runs normally.
    _drain({"prompt": "next"})
    _drain({"prompt": "and next"})
    assert len(seen) == 2
    assert not main_module._conversation_lock.locked()
