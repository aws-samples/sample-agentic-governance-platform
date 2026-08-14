#!/usr/bin/env python
"""Re-register the genuinely-wired AGP registry records into the NEW ``agent-registry``
namespace (Epic 32, Task 8).

THIS IS A LIVE-WRITE CONTROLLER STEP. It is not run by automation: the controller runs it
once, under explicit authorisation, and only after a ``--dry-run`` pass has been read. In
``--live`` mode it creates registry RECORDS in the new namespace — and NOTHING ELSE. It
mints no Entra app registration, applies no grant, and deletes nothing. ``--live`` is NOT
the default: a bare invocation is a dry run and writes NOTHING.

IT DOES NOT PROVISION ENTRA IDENTITY — RE-PROVISIONING IS A MANUAL FOLLOW-UP STEP
--------------------------------------------------------------------------------
This script drives the SERVICE layer (``AgentRegistryService.create()`` /
``McpServerRegistryService.create()``), and ``create()`` does NOT provision identity. All
it does is stamp ``identity_status="pending"`` into the governance envelope; there is no
Graph call and no app registration anywhere beneath it. The provisioning hook lives on the
ROUTE, not on the service — it is the FastAPI background task
``background_tasks.add_task(get_identity_service().provision, agent)`` at
``src/api/routes/agents.py:322`` (and ``src/api/routes/mcp_servers.py:237`` for MCP
servers). A service-layer script bypasses that hook entirely, by construction.

So EVERY record this script creates lands with NO Entra app registration, NO service
principal and NO app roles, and it stays that way until a human drives the platform's
existing re-provision route ONCE PER NEW RECORD:

    POST /api/v1/agents/{new_id}/reprovision       (src/api/routes/agents.py:478 → 202)
    POST /api/v1/mcp-servers/{new_id}/reprovision  (src/api/routes/mcp_servers.py:374 → 202)

That call is deliberately NOT made from here: this script speaks to the service layer, not
to HTTP, and re-provisioning is precisely the route-level behaviour it cannot reach. Take
the new ids out of the summary table this script prints and re-provision each one. Only
after ``identity_status`` reaches ``provisioned`` can agent→MCP grants be applied (they
need the new record's ``entra_sp_id``), and only after that is it safe to delete the
superseded OLD Entra app registrations.

WHY THIS SCRIPT EXISTS
----------------------
AWS moved the Registry off the ``bedrock-agentcore`` namespace onto its own
``agent-registry`` one (2026-08-06); the old namespace shuts down 2026-09-17. AWS's own
migration tool mints NEW recordIds — and in AGP the ``recordId`` IS the agent's identity
across FIVE external systems (the Entra identifier URI ``api://agp-agent-<id>``, the
DynamoDB mirror rows, the ECR image-tag prefixes, the Cedar policy-engine names, and the
Langfuse project names). Rather than rewrite all five to chase a remapped id, E32
RE-REGISTERS only the records that carry real wiring, so each one gets a FRESH,
self-consistent identity, and re-seeds the demo data from ``seed_agents.py`` /
``seed_mcp_servers.py``.

So there is deliberately NO id crosswalk here, and none should be added. A re-registered
record is a NEW record with a NEW id and a NEW Entra app; the old record and the old app
are what get superseded.

WHAT IS IN SCOPE (the "wired" gate — see :func:`build_plan`)
-----------------------------------------------------------
Only records whose governance envelope carries an ``entra_app_id`` OR a live resource
handle (``agent_arn`` / ``runtime_arn`` / ``gateway_arn``). Everything else is demo data
that ``seed_agents.py`` / ``seed_mcp_servers.py`` re-create from source, so copying it
would only duplicate seeds. DEPRECATED records are dropped outright — a deprecated record
is by definition superseded, and the ones in the live registry today are pre-rebrand
duplicates whose replacements are already APPROVED.

THE ONE PLACE BOTH NAMESPACES COEXIST
-------------------------------------
READS come from the OLD namespace through an explicit ``boto3.client(
"bedrock-agentcore-control")`` built right here (:func:`read_old_records`), using the OLD
record schema: ``descriptors.custom.inlineContent`` for agents and
``descriptors.mcp.server.inlineContent`` / ``descriptors.mcp.tools.inlineContent`` for MCP
servers, with ``descriptorType`` still a discrete ``ListRegistryRecords`` kwarg.

WRITES go through AGP's OWN service layer (``AgentRegistryService`` /
``McpServerRegistryService``), which E32 Tasks 2-3 already pointed at
``agent-registry-control`` and the new ``data``/``mcpServer`` descriptor shape. Those
services are NOT repointed by this script, and no second write path is built: the whole
value of driving the service layer is that the new records are written by the exact code
the running platform uses.

Because of that split, the SOURCE registry ids are CLI-only (``--agent-registry-id`` /
``--mcp-registry-id``) with NO settings fallback: the settings name the NEW (destination)
registries — via ``AGENT_REGISTRY_NAME`` / ``MCP_REGISTRY_NAME``, which the services resolve to
ids themselves — so silently defaulting the read side to settings would read the destination and
"migrate" it onto itself.

GATEWAY ARNs CARRY THROUGH VERBATIM
-----------------------------------
Registering an MCP server only REFERENCES a gateway by ARN (``mcp_identity_service`` calls
``GetGateway``); it never creates one. Gateway and Policy did NOT move namespaces. So the
new record points at the SAME live gateway, and that gateway's Cedar policy engine keeps
enforcing without being touched. ``gateway_arn`` (and ``runtime_arn`` / ``agent_arn`` /
``agent_arns``) are therefore copied byte-identically from the old envelope — never
rewritten, never re-derived.

GRANTS AND ENTRA CLEANUP ARE NOT DONE HERE — ``--apply-grants`` / ``--delete-old-apps``
---------------------------------------------------------------------------------------
Both flags still exist, and both REFUSE TO RUN (exit 2) with the procedure to follow
instead. They are kept as refusals rather than deleted so that an operator working from an
older runbook or shell history gets the correct instructions instead of an
``unrecognized arguments`` error. Neither flag has an implementation behind it any more:
this script contains no call to ``apply_agent_mcp_grant`` or ``delete_agent_app`` on any
path. Why each one cannot work from here follows directly from the section above:

  - ``--apply-grants`` — ``services.agent_mcp_grant.apply_agent_mcp_grant(agent, mcp)``
    (E7/E9) uses ``agent.entra_sp_id`` as the Graph grant principal. ``entra_sp_id`` is
    SERVICE-WRITTEN by ``provision()`` and is not a field on ``AgentCreate``, so a record
    created here can NEVER carry one: every grant would be skipped, 100% of the time.
  - ``--delete-old-apps`` — ``services.graph_service.GraphService.delete_agent_app(
    entra_app_id=…, entra_sp_id=…)`` (E23) would delete the superseded app while the
    replacement record has no identity at all. That is not a race it might win; it is the
    certain destruction of the only working identity for that wiring.

THE CORRECT ORDER, done by hand after a ``--live`` run:

  1. re-provision each new record — ``POST /api/v1/agents/{new_id}/reprovision`` (or the
     ``/mcp-servers/`` equivalent) — and wait for ``identity_status == "provisioned"``;
  2. apply the agent→MCP grants from the UI (the normal path) or the grants API, using the
     NEW MCP ids from this script's summary table. The old envelope's ``mcp_server_ids``
     name OLD-namespace records and there is deliberately no crosswalk (see above);
  3. only then delete the superseded OLD Entra app registrations, with the old ids read
     from the still-intact OLD registry.

Note that re-running this script with ``--delete-old-apps`` could never have performed
step 3 anyway: the second pass hits ``NameTakenError`` → ``status="skipped-exists"`` and
skips straight past any post-create work.

USAGE
-----
Run from the backend dir (``PYTHONPATH=src`` is required — ``src/`` is not a package):

    # DRY RUN (the default) — reads the OLD registries, writes NOTHING:
    cd platform/control_plane/backend && \\
        PYTHONPATH=src venv/bin/python scripts/reregister_records.py \\
            --agent-registry-id <old-agent-registry-id> \\
            --mcp-registry-id <old-mcp-registry-id>

    # MCP servers first (agents' grants reference them), then agents:
    ... --kind mcp  --mcp-registry-id <id>   --live
    ... --kind agent --agent-registry-id <id> --live

    # then, BY HAND, once per new id from the summary table:
    #   POST /api/v1/mcp-servers/{new_id}/reprovision
    #   POST /api/v1/agents/{new_id}/reprovision
    # then re-apply the agent→MCP grants from the UI, then delete the old Entra apps.
    # (--apply-grants / --delete-old-apps EXIT 2 — see the section above.)

``--region`` defaults to ``us-east-1``. The DESTINATION registries come from the services' own
config — normally ``settings.AGENT_REGISTRY_NAME`` / ``MCP_REGISTRY_NAME``, which each service
resolves to an id at first use, with ``AGENT_REGISTRY_ID`` / ``MCP_REGISTRY_ID`` still honoured
as explicit overrides — so no account id or registry id is baked into this file.
"""

import argparse
import json
import logging
import sys

logger = logging.getLogger("reregister_records")

# OLD-namespace record constants. ``descriptorType`` was a discrete ListRegistryRecords
# kwarg before E32 (the new namespace replaced it with a structured ``filters`` list and
# renamed the concept to ``recordType``) — the READ path here must use the OLD spelling.
_OLD_DESCRIPTOR_TYPE = {"agent": "CUSTOM", "mcp": "MCP"}

# The governance envelope key inside an MCP record's server.json. Unchanged by E32.
_ENVELOPE_KEY = "com.agp/governance"

# Native statuses that are never re-registered. A DEPRECATED record is by definition
# superseded; the ones live today are pre-rebrand duplicates of already-APPROVED records.
_DROPPED_STATUSES = {"DEPRECATED"}

# The envelope keys that constitute "real wiring" (the build_plan gate).
_WIRING_KEYS = ("entra_app_id", "agent_arn", "runtime_arn", "gateway_arn")

# Tenant for a record whose envelope predates multi-tenancy (E24) — matches the seeds.
_DEFAULT_TENANT = "default"

# Envelope keys that must NEVER be replayed onto the new record. The Entra identity block
# is SERVICE-WRITTEN and belongs to the app registration this run supersedes; copying it
# would point the new record at an app that is about to be deleted. (They are not fields
# on AgentCreate/McpServerCreate either, so pydantic would drop them — this list is the
# explicit statement of intent, not the enforcement.)
_IDENTITY_KEYS = (
    "entra_app_id",
    "entra_api_app_id",
    "entra_sp_id",
    "entra_app_audience",
    "invoker_role_id",
    "admin_role_id",
    "identity_status",
    "oauth2_credential_provider_name",
    "cedar_policy_engine_id",
    "cedar_policy_engine_arn",
    "cedar_enforcement_mode",
    "langfuse_project_id",
    "langfuse_key_secret_name",
    "gateway_id",
    "gateway_url",
)

# The two refused flags. Both name post-create work this script structurally cannot do,
# because `service.create()` provisions no identity (the hook is route-level — see the
# module docstring). They exit 2 (this script's usage-error code) with the real procedure
# rather than running and warning, which is what previously hid the defect.
_APPLY_GRANTS_REFUSAL = (
    "--apply-grants is REFUSED and does nothing. apply_agent_mcp_grant() needs the NEW "
    "record's entra_sp_id as its Graph principal, and a record created through the service "
    "layer can never have one: entra_sp_id is written by provision(), it is not a field on "
    "AgentCreate, and provision() is scheduled by the ROUTE "
    "(api/routes/agents.py:322), not by create(). Every grant would be skipped."
)
_DELETE_OLD_APPS_REFUSAL = (
    "--delete-old-apps is REFUSED and does nothing. Records created by this script have NO "
    "Entra identity at all (identity_status=pending), so deleting the superseded app would "
    "destroy the only working identity for that wiring — a certainty, not a race."
)
_REFUSAL_PROCEDURE = (
    "Do this instead, in this order: (1) re-register the records with --live; (2) for each "
    "NEW id in the summary table, re-provision identity via the API — "
    "POST /api/v1/agents/{id}/reprovision (or /api/v1/mcp-servers/{id}/reprovision) — and "
    "wait for identity_status=provisioned; (3) apply the agent->MCP grants from the UI or "
    "the grants API, using the NEW MCP ids (the old envelope's mcp_server_ids name "
    "OLD-namespace records and there is deliberately no crosswalk); (4) only then delete "
    "the superseded OLD Entra app registrations."
)


# ---------------------------------------------------------------------------
# OLD-namespace read path (explicit bedrock-agentcore-control client)
# ---------------------------------------------------------------------------

def read_old_records(
    registry_id: str,
    kind: str,
    *,
    region: str = "us-east-1",
    control_client=None,
) -> list[dict]:
    """Read every record of ``kind`` from the OLD ``bedrock-agentcore`` registry.

    Returns the ``GetRegistryRecord`` responses with the parsed governance envelope added
    under ``_envelope`` (plus ``_server_json`` / ``_tools`` for MCP records). This is the
    ONLY function in the codebase that still talks to the old namespace, hence the
    explicit ``boto3.client("bedrock-agentcore-control")`` — the services are pointed at
    ``agent-registry-control`` and must stay there.

    ``control_client`` is injectable so the offline tests never construct a real client.
    boto3 is imported lazily so importing this module needs no AWS credentials.

    A record whose payload is missing or non-JSON is LOGGED AND SKIPPED — one malformed
    record must not abort the read of the other 30.
    """
    if kind not in _OLD_DESCRIPTOR_TYPE:
        raise ValueError(f"kind must be 'agent' or 'mcp', got {kind!r}")

    ctl = control_client
    if ctl is None:
        import boto3

        ctl = boto3.client("bedrock-agentcore-control", region_name=region)

    record_ids: list[str] = []
    kwargs = {
        "registryId": registry_id,
        "maxResults": 100,
        # OLD spelling — a discrete kwarg, not a structured filter (see module docstring).
        "descriptorType": _OLD_DESCRIPTOR_TYPE[kind],
    }
    while True:
        page = ctl.list_registry_records(**kwargs)
        for summary in page.get("registryRecords", []):
            record_ids.append(summary["recordId"])
        token = page.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token

    logger.info(
        "read %d %s record id(s) from OLD registry %s", len(record_ids), kind, registry_id
    )

    records: list[dict] = []
    for record_id in record_ids:
        try:
            resp = ctl.get_registry_record(registryId=registry_id, recordId=record_id)
            if kind == "agent":
                resp["_envelope"] = _parse_old_agent_envelope(resp)
            else:
                server_json, tools = _parse_old_mcp_payload(resp)
                resp["_server_json"] = server_json
                resp["_tools"] = tools
                resp["_envelope"] = (
                    server_json.get("_meta", {}).get(_ENVELOPE_KEY) or {}
                )
        except Exception as exc:  # noqa: BLE001 — skip the bad record, keep the run alive
            logger.warning("  skip %s: could not read/parse the record: %s", record_id, exc)
            continue
        records.append(resp)

    return records


def _parse_old_agent_envelope(resp: dict) -> dict:
    """OLD agent schema: ``descriptors.custom.inlineContent`` (the ported service now
    WRITES ``descriptors.custom.data``)."""
    envelope = json.loads(resp["descriptors"]["custom"]["inlineContent"])
    if not isinstance(envelope, dict):
        raise ValueError(f"custom.inlineContent decoded to {type(envelope).__name__}")
    return envelope


def _parse_old_mcp_payload(resp: dict) -> tuple[dict, list]:
    """OLD MCP schema: ``descriptors.mcp.server.inlineContent`` (stringified server.json)
    plus the optional ``descriptors.mcp.tools.inlineContent`` (stringified
    ``{"tools": [...]}``). The ported service now writes ``descriptors.mcpServer.data``
    with the tools under ``additionalData``."""
    branch = resp["descriptors"]["mcp"]
    server_json = json.loads(branch["server"]["inlineContent"])
    if not isinstance(server_json, dict):
        raise ValueError(f"mcp.server.inlineContent decoded to {type(server_json).__name__}")
    tools: list = []
    if "tools" in branch:
        tools = json.loads(branch["tools"]["inlineContent"]).get("tools", []) or []
    return server_json, tools


# ---------------------------------------------------------------------------
# Plan construction (pure — no AWS, no services)
# ---------------------------------------------------------------------------

def build_plan(records: list[dict], kind: str) -> list[dict]:
    """Keep only the records that carry REAL WIRING; skip the rest with a logged reason.

    "Wired" means the governance envelope has an ``entra_app_id`` OR one of the live
    resource handles ``agent_arn`` / ``runtime_arn`` / ``gateway_arn``. Unwired records are
    demo data that the seed scripts re-create from source. DEPRECATED records are dropped
    outright (superseded by definition).

    Returns one plan entry per kept record::

        {"old_id", "name", "kind", "envelope"[, "purpose"][, "server_json", "tools"]}
    """
    plan: list[dict] = []
    for record in records:
        old_id = record.get("recordId") or ""
        # E32 added the native displayName and our writes populate both; prefer it and
        # fall back to `name` so a record written before the migration still resolves.
        name = record.get("displayName") or record.get("name") or ""
        envelope = record.get("_envelope") or {}
        status = record.get("status")

        if status in _DROPPED_STATUSES:
            logger.info(
                "  skip %s (%s): status=%s — superseded, deliberately not re-registered",
                old_id,
                name,
                status,
            )
            continue

        wiring = [key for key in _WIRING_KEYS if envelope.get(key)]
        if not wiring:
            logger.info(
                "  skip %s (%s): no wiring in the envelope (no %s) — seed data, "
                "re-created by the seed scripts",
                old_id,
                name,
                "/".join(_WIRING_KEYS),
            )
            continue

        entry = {
            "old_id": old_id,
            "name": name,
            "kind": kind,
            "envelope": envelope,
        }
        if kind == "agent":
            # `purpose` is the NATIVE record description, not an envelope key.
            entry["purpose"] = record.get("description") or ""
        else:
            # description / version / endpoint live in server.json, tools alongside it.
            entry["server_json"] = record.get("_server_json") or {}
            entry["tools"] = record.get("_tools") or []

        logger.info("  keep %s (%s): wired via %s", old_id, name, ", ".join(wiring))
        plan.append(entry)

    logger.info("plan: %d of %d %s record(s) will be re-registered", len(plan), len(records), kind)
    return plan


# ---------------------------------------------------------------------------
# Create-payload builders (pure pydantic — no AWS)
# ---------------------------------------------------------------------------

def _drop_none(fields: dict) -> dict:
    """Drop null values so the model's own defaults apply (an explicit ``None`` would fail
    validation on the non-Optional fields like ``auth_type`` / ``origin`` / ``kind``)."""
    return {key: value for key, value in fields.items() if value is not None}


def _build_agent_create(entry: dict):
    """Build an ``AgentCreate`` from the OLD envelope. Imported lazily so importing this
    module triggers no settings/AWS setup.

    Carries the governance metadata and the RESOURCE HANDLES verbatim (``agent_arn`` /
    ``agent_arns`` name runtimes that did not move namespaces). Deliberately omits the
    Entra identity block (``_IDENTITY_KEYS``) — the new record gets a FRESH identity, from
    the manual re-provision step — and ``mcp_server_ids``, because the old ids name
    OLD-namespace MCP records; the grants are re-applied by hand afterwards (see the module
    docstring).
    """
    from models.agent import AgentCreate

    envelope = entry.get("envelope") or {}
    fields = _drop_none(
        {
            "name": entry["name"],
            "purpose": entry.get("purpose") or envelope.get("purpose") or "",
            "sponsor_oid": envelope.get("sponsor_oid"),
            "sponsor_email": envelope.get("sponsor_email"),
            "business_unit": envelope.get("business_unit"),
            "region": envelope.get("region"),
            "data_classification": envelope.get("data_classification"),
            "platform": envelope.get("platform"),
            "framework": envelope.get("framework"),
            "model_id": envelope.get("model_id"),
            "origin": envelope.get("origin"),
            "endpoint_url": envelope.get("endpoint_url"),
            "auth_type": envelope.get("auth_type"),
            # Resource handles — verbatim; these runtimes did not move.
            "agent_arn": envelope.get("agent_arn"),
            "agent_arns": envelope.get("agent_arns"),
            "published": envelope.get("published"),
            "tenant_id": envelope.get("tenant_id") or _DEFAULT_TENANT,
        }
    )
    return AgentCreate(**fields)


def _build_mcp_create(entry: dict):
    """Build an ``McpServerCreate`` from the OLD envelope + server.json.

    ``gateway_arn`` / ``runtime_arn`` are copied BYTE-IDENTICALLY: creating an MCP record
    only REFERENCES the gateway, so the new record points at the SAME live gateway and its
    (unmoved) Cedar policy engine keeps enforcing.
    """
    from models.mcp_server import McpServerCreate

    envelope = entry.get("envelope") or {}
    server_json = entry.get("server_json") or {}
    remotes = server_json.get("remotes") or []
    endpoint_url = remotes[0].get("url") if remotes and isinstance(remotes[0], dict) else None

    # OLD tools use the MCP wire spelling `inputSchema`; McpTool's field is `input_schema`.
    tools = [
        {
            "name": tool.get("name"),
            "description": tool.get("description") or "",
            "input_schema": tool.get("inputSchema") or tool.get("input_schema") or {},
        }
        for tool in (entry.get("tools") or [])
        if isinstance(tool, dict) and tool.get("name")
    ]

    fields = _drop_none(
        {
            "name": entry["name"],
            "description": server_json.get("description") or "",
            "kind": envelope.get("kind"),
            "owner_oid": envelope.get("owner_oid"),
            "owner_email": envelope.get("owner_email"),
            "business_unit": envelope.get("business_unit"),
            "region": envelope.get("region"),
            "data_classification": envelope.get("data_classification"),
            "endpoint_url": endpoint_url,
            "version": server_json.get("version"),
            "available_tools": tools,
            # Resource handles — verbatim; the gateway/runtime did not move.
            "gateway_arn": envelope.get("gateway_arn"),
            "runtime_arn": envelope.get("runtime_arn"),
            "published": envelope.get("published"),
            "shared": envelope.get("shared"),
            "tenant_id": envelope.get("tenant_id") or _DEFAULT_TENANT,
        }
    )
    return McpServerCreate(**fields)


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------

def reregister(
    plan: list[dict],
    *,
    dry_run: bool,
    agent_service,
    mcp_service,
) -> list[dict]:
    """Re-register every planned record through AGP's own service layer.

    Returns one row per plan entry::

        {"old_id", "new_id", "name", "kind", "status"}

    ``status`` is one of ``dry-run`` / ``created`` / ``skipped-exists`` / ``failed``.

    CREATES RECORDS ONLY. ``service.create()`` stamps ``identity_status="pending"``; it
    provisions no Entra identity (that hook is route-level — ``api/routes/agents.py:322``),
    so there is nothing here that could apply a grant or supersede an old app registration.
    Both follow-ups are manual — see the module docstring.

    ZERO-WRITES CONTRACT: when ``dry_run`` is true this function calls NOTHING on
    ``agent_service`` / ``mcp_service`` — not a create, not even a read. It logs the
    intended call and returns ``status="dry-run"`` with ``new_id=None``.
    ``tests/test_reregister_records.py`` asserts that mechanically on the mocks
    (``mock_calls == []``), because a log line proves nothing about a call.

    IDEMPOTENT: a name that already exists raises ``NameTakenError`` → logged and skipped,
    the run continues (mirrors ``scripts/seed_agents.py``). Any other create failure is
    logged as ``failed`` and the run continues too — one bad record must not abandon the
    others half-migrated.
    """
    from services.agent_registry_service import NameTakenError as AgentNameTakenError
    from services.mcp_server_service import NameTakenError as McpNameTakenError

    results: list[dict] = []

    for entry in plan:
        old_id = entry.get("old_id")
        name = entry.get("name")
        kind = entry.get("kind")
        envelope = entry.get("envelope") or {}

        row = {"old_id": old_id, "new_id": None, "name": name, "kind": kind, "status": "failed"}

        # -- build the payload (pure pydantic; safe in dry-run) ---------------
        try:
            if kind == "agent":
                payload = _build_agent_create(entry)
            elif kind == "mcp":
                payload = _build_mcp_create(entry)
            else:
                raise ValueError(f"unknown kind {kind!r} (expected 'agent' or 'mcp')")
        except Exception as exc:  # noqa: BLE001 — report + continue
            logger.error("  %s %r: could not build the create payload: %s", kind, name, exc)
            results.append(row)
            continue

        if dry_run:
            # ZERO WRITES. Nothing below this branch touches a collaborator.
            logger.info(
                "  [dry-run] WOULD create %s %r (from old id %s) — handles: %s",
                kind,
                name,
                old_id,
                _handles_summary(envelope) or "none",
            )
            row["status"] = "dry-run"
            results.append(row)
            continue

        # -- LIVE: create through the service layer --------------------------
        service = agent_service if kind == "agent" else mcp_service
        try:
            created = service.create(payload, created_by="e32-reregister")
        except (AgentNameTakenError, McpNameTakenError):
            logger.info("  skip, already exists: %s %r (old id %s)", kind, name, old_id)
            row["status"] = "skipped-exists"
            results.append(row)
            continue
        except Exception as exc:  # noqa: BLE001 — log + continue, don't abort the run
            logger.error("  failed to create %s %r: %s — continuing", kind, name, exc)
            results.append(row)
            continue

        row["new_id"] = created.id
        row["status"] = "created"
        logger.info(
            "  created %s %r -> new id=%s (old id %s) — identity_status=pending: it has NO "
            "Entra app yet. Re-provision it by hand: POST /api/v1/%s/%s/reprovision",
            kind,
            name,
            created.id,
            old_id,
            "agents" if kind == "agent" else "mcp-servers",
            created.id,
        )
        results.append(row)

    return results


def _handles_summary(envelope: dict) -> str:
    """The live resource handles a record carries — the dry-run's most important line."""
    return ", ".join(f"{key}={envelope[key]}" for key in _WIRING_KEYS if envelope.get(key))


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def print_summary(results: list[dict], *, dry_run: bool) -> None:
    """Print the old id → new id table to STDOUT (logs go to stderr)."""
    print()
    print(f"{'KIND':<6} {'STATUS':<15} {'OLD ID':<22} {'NEW ID':<22} NAME")
    print("-" * 100)
    for row in results:
        print(
            f"{(row['kind'] or '?'):<6} {row['status']:<15} {(row['old_id'] or '-'):<22} "
            f"{(row['new_id'] or '-'):<22} {row['name']}"
        )
    print("-" * 100)
    counts: dict[str, int] = {}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    tally = "  ".join(f"{status}={count}" for status, count in sorted(counts.items()))
    print(f"{len(results)} record(s): {tally or 'none'}")
    if dry_run:
        print("DRY RUN — nothing was written. Re-run with --live to re-register.")
        return
    # A created record has NO Entra identity yet (see the module docstring), so the run is
    # not finished when this script exits. Name the next step where the operator is looking.
    if any(row["status"] == "created" for row in results):
        print(
            "NEXT (manual, once per NEW ID above): POST /api/v1/agents/{new_id}/reprovision "
            "(or /api/v1/mcp-servers/{new_id}/reprovision) to provision the Entra identity "
            "— it is still 'pending'. Then re-apply the agent->MCP grants from the UI, and "
            "only then delete the superseded OLD Entra app registrations."
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Re-register the wired AGP registry records into the new agent-registry "
            "namespace (E32/T8). DRY RUN BY DEFAULT — --live is required to write."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "SAFETY: a bare invocation is a DRY RUN and writes NOTHING (no AWS create, no\n"
            "Graph call). --live is required to create records.\n"
            "\n"
            "THIS SCRIPT CREATES RECORDS ONLY. It provisions NO Entra identity: create()\n"
            "just stamps identity_status=pending, because provisioning is a route-level\n"
            "background task (api/routes/agents.py:322) that a service-layer script cannot\n"
            "reach. Every new record therefore needs a manual\n"
            "POST /api/v1/agents/{new_id}/reprovision (api/routes/agents.py:478) — or the\n"
            "/mcp-servers/ equivalent — afterwards. --apply-grants and --delete-old-apps\n"
            "EXIT 2: they cannot work from here (see their help text).\n"
            "\n"
            "The SOURCE (old) registry ids are CLI-only on purpose: settings.*_REGISTRY_ID\n"
            "now name the NEW registries, so there is no safe settings fallback for a read\n"
            "of the old namespace."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Read the old registries and print what WOULD be created. THE DEFAULT.",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="Actually create the records (LIVE WRITES). Not the default.",
    )
    parser.add_argument(
        "--kind",
        choices=("agent", "mcp", "both"),
        default="both",
        help="Which registry to re-register (default: both; MCP servers run first).",
    )
    parser.add_argument(
        "--agent-registry-id",
        default=None,
        help="The OLD (bedrock-agentcore) AGENT registry id to read from. Required for "
             "--kind agent/both. No settings fallback — settings names the NEW registry.",
    )
    parser.add_argument(
        "--mcp-registry-id",
        default=None,
        help="The OLD (bedrock-agentcore) MCP registry id to read from. Required for "
             "--kind mcp/both. No settings fallback — settings names the NEW registry.",
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region hosting both registries (default: us-east-1).",
    )
    parser.add_argument(
        "--apply-grants",
        action="store_true",
        help="REFUSED (exit 2). Grants need the NEW record's entra_sp_id, which this "
             "script can never produce: it drives the service layer, and identity is "
             "provisioned by a route-level background task. Re-provision each new record "
             "(POST /api/v1/agents/{id}/reprovision), then apply grants from the UI/API.",
    )
    parser.add_argument(
        "--delete-old-apps",
        action="store_true",
        help="REFUSED (exit 2). The new record has no Entra identity when this script "
             "finishes, so deleting the superseded app would destroy the only working "
             "identity. Re-provision, re-grant, and only then delete the old apps.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logging.")
    return parser.parse_args(argv)


def _build_services(region: str):
    """Construct the two registry services, which Tasks 2-3 pointed at the NEW namespace.

    The DESTINATION registry ids come from settings (so no id is baked into this file).
    Imported lazily so importing this module triggers no Settings() validation.
    """
    from core.config import settings
    from services.agent_registry_service import AgentRegistryService
    from services.mcp_server_service import McpServerRegistryService

    # NAME + optional id, exactly as the running backend constructs these. The id settings are
    # now normally EMPTY — the platform resolves each registry by name at first use, so nothing
    # populates them any more — and passing the name is what keeps this script pointed at the
    # same destination registries the platform writes to. An explicitly-set *_REGISTRY_ID still
    # wins, since the service treats it as an override.
    agent_service = AgentRegistryService(
        registry_id=getattr(settings, "AGENT_REGISTRY_ID", ""),
        registry_name=getattr(settings, "AGENT_REGISTRY_NAME", ""),
        region=region,
    )
    mcp_service = McpServerRegistryService(
        registry_id=getattr(settings, "MCP_REGISTRY_ID", ""),
        registry_name=getattr(settings, "MCP_REGISTRY_NAME", ""),
        region=region,
    )
    return agent_service, mcp_service


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    dry_run = not args.live

    # REFUSALS FIRST — before any registry id validation, before any client is built, and
    # before a single AWS or Graph call. Both flags name work this script structurally
    # cannot do (see the module docstring): they are kept only so that an operator working
    # from an older runbook is told the correct procedure instead of getting an argparse
    # "unrecognized arguments" error. There is no implementation behind either one.
    if args.apply_grants or args.delete_old_apps:
        if args.apply_grants:
            logger.error("%s", _APPLY_GRANTS_REFUSAL)
        if args.delete_old_apps:
            logger.error("%s", _DELETE_OLD_APPS_REFUSAL)
        logger.error("%s", _REFUSAL_PROCEDURE)
        return 2

    # MCP servers run BEFORE agents: an agent's grants reference MCP records, so the MCPs
    # must exist in the new registry first.
    kinds = ["mcp", "agent"] if args.kind == "both" else [args.kind]
    sources = {"agent": args.agent_registry_id, "mcp": args.mcp_registry_id}
    missing = [kind for kind in kinds if not sources[kind]]
    if missing:
        for kind in missing:
            logger.error(
                "--%s-registry-id is required for --kind %s: it names the OLD "
                "(bedrock-agentcore) registry to READ, and there is no settings fallback "
                "because settings.%s_REGISTRY_ID now names the NEW registry.",
                kind,
                args.kind,
                kind.upper(),
            )
        return 2

    logger.info(
        "mode: %s   kind: %s   region: %s   (records only — identity re-provisioning is a "
        "manual follow-up per new id)",
        "DRY RUN (nothing will be written)" if dry_run else "LIVE (writing)",
        args.kind,
        args.region,
    )

    # Services are built AFTER validation so a misconfigured invocation constructs no
    # client at all. In dry-run they are built but never called (the zero-writes contract
    # lives in reregister(), and the tests assert it on the mocks).
    try:
        agent_service, mcp_service = _build_services(args.region)
    except Exception as exc:  # noqa: BLE001 — surface a clean, actionable message
        logger.error("could not construct the registry services: %s", exc)
        return 1

    results: list[dict] = []
    try:
        for kind in kinds:
            logger.info("--- %s records ---", kind)
            records = read_old_records(sources[kind], kind, region=args.region)
            plan = build_plan(records, kind)
            results.extend(
                reregister(
                    plan,
                    dry_run=dry_run,
                    agent_service=agent_service,
                    mcp_service=mcp_service,
                )
            )
    except Exception as exc:  # noqa: BLE001 — surface a clean, actionable message
        logger.error("re-registration failed: %s", exc)
        logger.error(
            "Check that: (1) AWS credentials carry BOTH the old bedrock-agentcore "
            "registry read permissions and the new agent-registry write permissions; "
            "(2) --agent-registry-id / --mcp-registry-id name the OLD registries; "
            "(3) settings.AGENT_REGISTRY_NAME / MCP_REGISTRY_NAME name the NEW ones and those "
            "registries exist (the services resolve name -> id themselves)."
        )
        if results:
            print_summary(results, dry_run=dry_run)
        return 1

    print_summary(results, dry_run=dry_run)
    return 0 if not any(row["status"] == "failed" for row in results) else 1


if __name__ == "__main__":
    sys.exit(main())
