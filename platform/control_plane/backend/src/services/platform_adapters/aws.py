"""The AWS (Bedrock AgentCore) platform adapter.

Four verbs delegate to ``AgentIdentityService``: ``provision_runtime_binding`` wires the inbound JWT
authorizer on every runtime the agent owns (AgentCore has no platform-side ACL, so a runtime without
one accepts unauthenticated invocations), ``runtime_status`` reads without raising,
``named_artifacts`` is empty (AgentCore names no platform-side teardown artifact; the Entra, OBO
and Langfuse teardown both platforms share is the route's) and ``remove_artifact`` therefore
refuses. ``probe`` runs the per-stage STS reach check the tenant service shares. ``invoke`` is not a
delegation: AgentCore's JWT-inbound runtimes reject SigV4, so it is an httpx bearer-POST, to a
runtime (an SSE or JSON reply) or to a harness (the AWS binary event stream), and every refusal is an
``InvokeError`` kind. A contained agent is refused before any token use or HTTP.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import quote
from uuid import uuid4

import anyio
import httpx
from botocore.eventstream import EventStreamBuffer

from core.errors import Outcome
from models.agent import AWS_ARN_REGION_RE, is_harness_arn
from models.tenant import TenantPlatform, TenantStageConfig
from services.agent_mcp_env import ungranted_tool_mcps
from services.containment_store import get_containment_store
from services.platform_adapter import UPSTREAM_ERROR, AdapterError, InvokeError
from services.platform_registry import AWS_DEPLOY_REACH, aws_reach_key, declaration_for
from services.tenant_credentials import probe_stage_reach

if TYPE_CHECKING:
    from api.routes.agents import InvokeRequest
    from core.rbac import Principal
    from models.agent import Agent
    from models.tenant import Tenant
    from services.agent_identity_service import AgentIdentityService, RuntimeStatusWithEnv
    from services.platform_adapter import Artifact
    from services.platform_registry import PlatformDeclaration

_AWS_REGION_RE = AWS_ARN_REGION_RE
_ACTOR_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,254}")
_EXCEPTION_TYPE_RE = re.compile(r"[A-Za-z]{1,64}")
_MAX_HARNESS_REPLY_BYTES = 4 * 1024 * 1024
_MAX_ERROR_BODY_BYTES = 4096
_TIMEOUT_S = 120.0
_NO_BLOCK = object()
_BLOCK_BOUNDARY_EVENTS = frozenset({"contentBlockStart", "contentBlockStop", "messageStart", "messageStop"})


def probe_aws_reach(stages, *, reach_prober=probe_stage_reach) -> dict:
    """The connect-time reach probe behind ``AwsPlatformAdapter.probe``, a module function so the
    tenant service can share it without importing the adapter (``aws`` imports
    ``platform_registry``, which imports ``tenant_service``).

    Per stage, in sorted order so the readiness derivation always names the same first failing
    stage, an ``sts:AssumeRole`` reach check against ``deploy_role_arn`` writes one ``bool`` under
    ``aws_reach_key(AWS_DEPLOY_REACH, stage)``. A stage naming no deploy role is skipped: deploy
    in place needs no assume, and an absent key is what the registry's grandfather rule reads as
    "never probed". ``push_role_arn`` is never probed: its trust policy names only GitHub's OIDC
    provider, so AGP can never assume it and a failure would say nothing about deploy readiness.
    """
    capabilities: dict = {}
    for key in sorted(stages):
        stage = stages[key]
        if not isinstance(stage, TenantStageConfig) or not stage.deploy_role_arn:
            continue
        reachable, _reason = reach_prober(stage.deploy_role_arn, stage.region)
        capabilities[aws_reach_key(AWS_DEPLOY_REACH, key)] = reachable
    return capabilities


def _bearer_headers(obo_token: str, session_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {obo_token}",
        "Content-Type": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
    }


def _session_id() -> str:
    """At least 33 characters, the documented minimum for the session header."""
    return uuid4().hex + uuid4().hex


class _HarnessReply:
    """The text blocks and tool blocks read off one harness event stream, in order.

    Text deltas join directly within one content block and with a paragraph break between blocks
    (a block or message start or stop frame, or a new ``contentBlockIndex``, ends a block). Tool
    blocks open on ``contentBlockStart.start.toolUse`` and collect ``delta.toolUse.input`` pieces;
    Converse restarts ``contentBlockIndex`` at 0 per message, so a repeated index starts a new
    block. A non-``event`` frame (``exception`` / ``error``) is the upstream kind, with its
    ``:exception-type`` or ``:error-code`` echoed only when it is a bare identifier.
    """

    def __init__(self) -> None:
        self.texts: list[list[str]] = []
        self.text_index: object = _NO_BLOCK
        self.tools: list = []
        self.open_tools: dict = {}

    def frame(self, message) -> None:
        if message.headers.get(":message-type") != "event":
            etype = str(message.headers.get(":exception-type") or message.headers.get(":error-code") or "")
            suffix = f" [{etype}]" if _EXCEPTION_TYPE_RE.fullmatch(etype) else ""
            raise InvokeError(f"{UPSTREAM_ERROR}{suffix}", "upstream_error")
        event_type = message.headers.get(":event-type")
        if event_type in _BLOCK_BOUNDARY_EVENTS:
            self.text_index = _NO_BLOCK
        if event_type == "contentBlockStart":
            _note_tool_start(message.payload, self.tools, self.open_tools)
        elif event_type == "contentBlockDelta":
            self._delta(json.loads(message.payload))

    def _delta(self, payload: dict) -> None:
        delta = payload.get("delta") or {}
        text = delta.get("text")
        if isinstance(text, str):
            index = payload.get("contentBlockIndex")
            if self.text_index is _NO_BLOCK or index != self.text_index:
                self.texts.append([])
                self.text_index = index
            self.texts[-1].append(text)
        _note_tool_input(payload, delta, self.open_tools)

    def text(self) -> str:
        return "\n\n".join(block for block in ("".join(pieces) for pieces in self.texts) if block)

    def tool_calls(self) -> list[dict]:
        return _tool_calls(self.tools)


async def _error_sniff(resp: httpx.Response) -> str:
    """The error-type header plus at most ``_MAX_ERROR_BODY_BYTES`` of a non-2xx body."""
    body = b""
    async for chunk in resp.aiter_bytes():
        body += chunk
        if len(body) >= _MAX_ERROR_BODY_BYTES:
            break
    return resp.headers.get("x-amzn-errortype", "") + body.decode("utf-8", "replace")


async def _refuse_harness_status(resp: httpx.Response) -> None:
    """401/403, and the 500 ``UnrecognizedClientException`` AgentCore answers a bad token signature
    with, are the auth kind; any other non-2xx is the upstream kind."""
    if resp.status_code in (401, 403):
        raise InvokeError("agent rejected the token", "token_rejected")
    if 200 <= resp.status_code < 300:
        return
    if resp.status_code == 500 and "UnrecognizedClientException" in await _error_sniff(resp):
        raise InvokeError("agent rejected the token", "token_rejected")
    raise InvokeError(UPSTREAM_ERROR, "upstream_error")


async def _read_harness_stream(resp: httpx.Response, reply: _HarnessReply) -> None:
    """Feed the body chunk by chunk into botocore's decoder, which verifies both CRCs; a reply over
    the byte cap, or trailing bytes that never formed a frame, is the upstream kind."""
    buffer = EventStreamBuffer()
    seen = consumed = 0
    async for chunk in resp.aiter_bytes():
        seen += len(chunk)
        if seen > _MAX_HARNESS_REPLY_BYTES:
            raise InvokeError(UPSTREAM_ERROR, "upstream_error")
        buffer.add_data(chunk)
        for message in buffer:
            prelude = message.prelude
            consumed += prelude.total_length if prelude is not None else 0
            reply.frame(message)
    if consumed != seen:
        raise InvokeError(UPSTREAM_ERROR, "upstream_error")


async def _invoke_harness(
    region: str, harness_arn: str, prompt: str, obo_token: str, actor_id: str | None = None
) -> dict:
    """``InvokeHarness`` with the OBO bearer, as a raw httpx bearer-POST (botocore models it
    SigV4-only and a harness with a JWT authorizer refuses SigV4). The caller has already checked
    ``region``. The envelope carries the joined text, the session id sent as the header, and the
    tool calls read off the same stream (``[]`` when it had no tool blocks). ``actor_id`` names the
    managed memory's actor when it is well formed (an Entra oid, or the eval runner's
    ``agp-eval-<hex>``); without one the harness's own default actor is shared by every caller. A
    timeout, a transport failure, a corrupt frame or payload, and a reply with no text are each
    their kind; no upstream body ever reaches the detail."""
    url = (
        f"https://bedrock-agentcore.{region}.amazonaws.com/harnesses/invoke"
        f"?harnessArn={quote(harness_arn, safe='')}"
    )
    session_id = _session_id()
    body: dict[str, Any] = {"messages": [{"role": "user", "content": [{"text": prompt}]}]}
    if actor_id and _ACTOR_ID_RE.fullmatch(actor_id):
        body["actorId"] = actor_id
    reply = _HarnessReply()
    try:
        async with (
            httpx.AsyncClient(timeout=_TIMEOUT_S) as client,
            client.stream("POST", url, headers=_bearer_headers(obo_token, session_id), json=body) as resp,
        ):
            await _refuse_harness_status(resp)
            await _read_harness_stream(resp, reply)
    except InvokeError:
        raise
    except httpx.TimeoutException as err:
        raise InvokeError("agent invocation timed out", "timed_out") from err
    except httpx.HTTPError as err:
        raise InvokeError("failed to reach the agent", "unreachable") from err
    except Exception as err:
        raise InvokeError(UPSTREAM_ERROR, "upstream_error") from err
    text = reply.text()
    if not text:
        raise InvokeError(UPSTREAM_ERROR, "upstream_error")
    return {"response": text, "session_id": session_id, "tool_calls": reply.tool_calls()}


async def _invoke_harness_naming_missing_grants(
    agent: Agent, region: str, harness_arn: str, prompt: str, obo_token: str, actor_id: str | None
) -> dict:
    """The harness leg, with the one refinement a bare upstream error needs: a harness whose
    ``tools`` names a gateway the agent holds no grant for refuses every invoke, even a prompt that
    uses no tool, so when that is the likely cause the detail names the missing grant and keeps the
    upstream code."""
    try:
        return await _invoke_harness(region, harness_arn, prompt, obo_token, actor_id)
    except InvokeError as err:
        if err.kind != "upstream_error":
            raise
        missing = await anyio.to_thread.run_sync(ungranted_tool_mcps, agent, [harness_arn])
        if not missing:
            raise
        names = ", ".join(sorted(m.name for m in missing))
        raise InvokeError(
            f"the agent lists MCP tools it has no grant for: {names}. Grant it access on "
            f"the agent's MCP Servers tab, then invoke again{err.message[len(UPSTREAM_ERROR) :]}",
            "tools_ungranted",
        ) from err


def _note_tool_start(raw: bytes, tools: list, open_tools: dict) -> None:
    """Open a tool block on ``contentBlockStart.start.toolUse``. Never raises: the tool-call read is
    best effort and must not fail an invoke whose text is fine, so a malformed start frame is
    skipped. Any start at an index closes the block open there."""
    try:
        payload = json.loads(raw)
        index = payload.get("contentBlockIndex")
        if not isinstance(index, int):
            return
        open_tools.pop(index, None)
        start = payload.get("start")
        tool_use = start.get("toolUse") if isinstance(start, dict) else None
        if isinstance(tool_use, dict) and isinstance(tool_use.get("name"), str):
            block = [tool_use["name"], []]
            tools.append(block)
            open_tools[index] = block
    except (ValueError, AttributeError):
        return


def _note_tool_input(payload, delta, open_tools: dict) -> None:
    """Append a ``delta.toolUse.input`` piece to its open block; an orphan piece is ignored."""
    tool_use = delta.get("toolUse") if isinstance(delta, dict) else None
    index = payload.get("contentBlockIndex") if isinstance(payload, dict) else None
    block = open_tools.get(index) if isinstance(index, int) else None
    if block is not None and isinstance(tool_use, dict) and isinstance(tool_use.get("input"), str):
        block[1].append(tool_use["input"])


def _tool_calls(tools: list) -> list[dict]:
    calls = []
    for name, pieces in tools:
        try:
            args = json.loads("".join(pieces))
        except ValueError:
            args = {}
        calls.append({"name": name, "args": args if isinstance(args, dict) else {}})
    return calls


def _region_of(agent_arn: str) -> str:
    """The ARN's region segment, which becomes the request host, so it is refused unless it has the
    closed shape of a region: a dot, a slash, an ``@``, a colon or an empty label cannot match, and
    the ARN itself (caller-settable, and it can carry an account id) is never echoed."""
    arn_parts = agent_arn.split(":")
    if len(arn_parts) < 4 or not arn_parts[3]:
        raise InvokeError("agent ARN is malformed", "arn_malformed")
    if not _AWS_REGION_RE.fullmatch(arn_parts[3]):
        raise InvokeError("the agent ARN's region segment is not a region", "agent_arn_invalid")
    return arn_parts[3]


async def _post_runtime(
    region: str, agent_arn: str, session_id: str, obo_token: str, body: dict
) -> httpx.Response:
    """Bearer-POST the runtime; the URL path carries the URL-encoded full ARN. A runtime 401/403 (a
    wrong-audience token included) is ``token_rejected``, distinct from the OBO 403 upstream."""
    url = (
        f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
        f"{quote(agent_arn, safe='')}/invocations?qualifier=DEFAULT"
    )
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.post(url, headers=_bearer_headers(obo_token, session_id), json=body)
    except httpx.TimeoutException as err:
        raise InvokeError("agent invocation timed out", "timed_out") from err
    except httpx.HTTPError as err:
        raise InvokeError("failed to reach the agent", "unreachable") from err
    if resp.status_code in (401, 403):
        raise InvokeError("agent rejected the token", "token_rejected")
    if not (200 <= resp.status_code < 300):
        raise InvokeError(UPSTREAM_ERROR, "upstream_error")
    return resp


def _split_sse_tool_calls(body: str) -> tuple[list[str], list[dict] | None]:
    """A streaming agent sends its tool calls as one ``{"tool_calls": [...]}`` event, last, on
    success. The last such event wins and every one is kept out of the reply text; an entry without
    a string name is dropped and a non-object ``args`` becomes ``{}``."""
    tool_calls = None
    text_lines = []
    for line in body.splitlines():
        data = line.strip()
        if data.startswith("data:"):
            try:
                payload = json.loads(data[len("data:") :].strip())
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("tool_calls"), list):
                tool_calls = [
                    {"name": c["name"], "args": c["args"] if isinstance(c.get("args"), dict) else {}}
                    for c in payload["tool_calls"]
                    if isinstance(c, dict) and isinstance(c.get("name"), str)
                ]
                continue
        text_lines.append(line)
    return text_lines, tool_calls


def _runtime_envelope(rt_resp: httpx.Response, session_id: str) -> dict:
    """The invoke envelope from a runtime reply: SSE (the expected path; parsed by the route's
    platform-neutral ``_extract_sse_text``, imported here because a service importing a route at
    load is a cycle), a JSON body (its own ``tool_calls`` list when it has one), or the raw text.
    ``tool_calls`` is ``None`` when the reply had no tool-call source, which sends the eval runner
    to the spans."""
    from api.routes.agents import _extract_sse_text

    if "text/event-stream" in rt_resp.headers.get("content-type", ""):
        text_lines, tool_calls = _split_sse_tool_calls(rt_resp.text)
        return {
            "response": _extract_sse_text("\n".join(text_lines)),
            "session_id": session_id,
            "tool_calls": tool_calls,
        }
    try:
        parsed = rt_resp.json()
    except (ValueError, TypeError):
        return {"response": rt_resp.text, "session_id": session_id, "tool_calls": None}
    tool_calls = parsed.get("tool_calls") if isinstance(parsed, dict) else None
    return {
        "response": parsed,
        "session_id": session_id,
        "tool_calls": tool_calls if isinstance(tool_calls, list) else None,
    }


class AgentContainedError(InvokeError):
    """The agent is contained, so AGP refuses to invoke it, before any token use or HTTP."""

    kinds: ClassVar[Mapping[str, Outcome]] = {"contained": Outcome(409, "agent is contained")}

    def __init__(self) -> None:
        super().__init__("agent is contained", "contained")


class AwsPlatformAdapter:
    """AGP's verbs against AWS Bedrock AgentCore, delegated to ``AgentIdentityService``. ``identity``
    is required: every deployment wires the AgentCore service (it is also the Entra identity
    service), so an absent one is a wiring bug to fail on at construction."""

    def __init__(self, *, identity: AgentIdentityService) -> None:
        self._identity = identity

    def declaration(self) -> PlatformDeclaration:
        return declaration_for(TenantPlatform.AWS)

    async def probe(self, tenant: Tenant, admin_creds: tuple[str, str] | None = None) -> tuple[dict, str]:
        """``probe_aws_reach`` over the tenant's stages, paired with the empty ``binding_mode`` AWS
        always carries: an AgentCore agent needs no identity-binding mode, and the readiness
        derivation, not a stored mode, carries the deploy-reach verdict. ``admin_creds`` is unused;
        AWS has no create-time credential."""
        return probe_aws_reach(tenant.stages), ""

    async def provision_runtime_binding(self, agent: Agent) -> None:
        """Wire the inbound JWT authorizer on every runtime the agent owns, then mark provisioned.
        Raises on failure, ``provision_runtime``'s own contract: an agent reported provisioned while
        one of its runtimes accepts unauthenticated invocations is the dangerous outcome."""
        await self._identity.provision_runtime(agent)

    def runtime_status(self, agent: Agent, stage: str | None = None) -> RuntimeStatusWithEnv:
        """The AgentCore status read: never raises, and answers ``not_deployed`` for a record naming
        no runtime without an AWS call."""
        return self._identity.runtime_status(agent, stage)

    async def invoke(
        self,
        agent: Agent,
        req: InvokeRequest,
        principal: Principal,
        obo_token: str,
        stage: str | None = None,
        actor_id: str | None = None,
    ) -> dict:
        """The AgentCore direct invoke. The runtime ARN is resolved here from ``stage`` through
        ``Agent.runtime_arns()``, the one place a stage becomes an ARN (the route has already
        refused a stage the agent owns no runtime for; ``None`` is the scalar, whichever stage
        deployed last). Its region is checked before the token is attached even though the route
        checks it before the exchange: this adapter must not be safe only behind one caller. A
        harness ARN takes the harness leg. The runtime body carries the invoker's Entra oid as
        ``user_oid`` (``None`` allowed) so the agent can attribute telemetry without decoding the
        inbound token, which AgentCore's edge authorizer consumes."""
        if get_containment_store().is_contained(agent.id):
            raise AgentContainedError()
        agent_arn = (agent.agent_arn or "") if stage is None else (agent.runtime_arns().get(stage) or "")
        region = _region_of(agent_arn)
        if is_harness_arn(agent_arn):
            return await _invoke_harness_naming_missing_grants(
                agent, region, agent_arn, req.prompt, obo_token, actor_id
            )
        session_id = _session_id()
        body = {"prompt": req.prompt, "user_oid": principal.oid}
        return _runtime_envelope(
            await _post_runtime(region, agent_arn, session_id, obo_token, body), session_id
        )

    def named_artifacts(self, agent: Agent) -> list[Artifact]:
        """Empty: the per-agent Entra app, OBO credential provider and Langfuse project are
        platform-neutral and torn down by the route's cascade, and AgentCore's own runtime is torn
        down by the repository cascade, so there is nothing platform-specific to name."""
        return []

    async def remove_artifact(self, agent: Agent, artifact: Artifact) -> None:
        """Unreachable in normal flow, since ``named_artifacts`` enumerates nothing; raises if called
        anyway, because answering silently would imply a removal that did not happen."""
        raise AdapterError(
            f"AGP has no removable AgentCore platform artifact for kind {artifact.kind!r}",
            kind="artifact_unremovable",
        )
