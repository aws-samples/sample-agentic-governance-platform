#!/usr/bin/env python
"""Seed representative demo MCP servers into the AWS Agent Registry (Epic 5, Task 7).

This is a USER / CONTROLLER step: it makes live AWS calls (via
``McpServerRegistryService`` -> ``bedrock-agentcore-control``, Bedrock AgentCore,
Public Preview) and therefore needs AWS credentials with the registry
permissions (research §8) AND a provisioned ``MCP_REGISTRY_ID``. It is NOT run
by automation in no-commit mode; the controller runs it once to populate the
dedicated ``agp-mcp-servers`` registry with demo data so the MCP-server
catalog, detail, and approval flow have a credible mix to show.

What it does:
  1. Build ~7 ``McpServerCreate`` payloads (defined as data below) spread across
     both kinds (gateway / standard), business units, regions, data
     classifications, and a lifecycle mix. Each carries 3-5 schema-valid declared
     tools (``name`` / ``description`` / ``input_schema`` as a real JSON Schema).
  2. For each: ``service.create(McpServerCreate(**fields), created_by="seed")``.
     Idempotent — a name that already exists raises ``NameTakenError`` which is
     caught + logged ("skip, already exists"), so re-running does not duplicate.
  3. Drive a lifecycle spread for demo credibility: most servers are
     submitted-for-approval + approved (-> APPROVED); one is approved-then-
     deprecated (-> DEPRECATED). DEPRECATED requires going through APPROVED first
     per the native lifecycle (research §5), so the deprecate path approves first.
     Every transition is wrapped defensively: a failure logs + continues rather
     than aborting the whole seed.

Prerequisite — the registry must exist, and this script needs its ID. The RUNNING backend does
not: it resolves the registry by NAME at first use. This script keeps its ``--registry-id``
interface, so get the id from AWS:

    aws agent-registry-control list-registries \
        --query "registries[?name=='agp-mcp-servers'].registryId" --output text

Terraform creates both registries, so provisioning one by hand is a fallback/diagnostic:

    cd platform/control_plane/backend && \
        PYTHONPATH=src venv/bin/python scripts/ensure_registry.py --name agp-mcp-servers

Pass the id with ``--registry-id`` (or export ``MCP_REGISTRY_ID``, still honoured as an
explicit override).

Run from the backend dir (PYTHONPATH=src is required — src/ is not a package):

    cd platform/control_plane/backend && \
        PYTHONPATH=src venv/bin/python scripts/seed_mcp_servers.py

Offline correctness check (NO AWS calls — validates every McpServerCreate payload
via pure Pydantic and prints what it would create):

    cd platform/control_plane/backend && \
        PYTHONPATH=src venv/bin/python scripts/seed_mcp_servers.py --dry-run

``--region`` / ``--registry-id`` override the settings defaults. With no id from either source
(and not a dry-run) the script prints the lookup command above and exits non-zero rather than
addressing an empty registry.
"""

import argparse
import logging
import sys

logger = logging.getLogger("seed_mcp_servers")


# ---------------------------------------------------------------------------
# Demo MCP-server data (module-level constants — pure data, no AWS, import-safe).
#
# Each dict is the kwargs for an McpServerCreate. Spread per the plan / Task 7:
#   - kind: both gateway and standard represented.
#   - business_unit: Claims, Sales/CRM, Underwriting, Platform, ...
#   - region: DE, EU.
#   - data_classification: Public / Internal / Confidential.
#   - lifecycle: mostly approved, >=1 deprecated, 1 pending_approval.
#   - names: realistic, unique, kebab-case (native record name charset).
#
# `available_tools` are plain dicts using the snake `input_schema` key — Pydantic
# coerces them to McpTool (the field is list[McpTool]) when the McpServerCreate
# is constructed. Each input_schema is a real JSON Schema object.
#
# The `lifecycle` key is NOT an McpServerCreate field — it is a seed-only
# directive telling the script which lifecycle to drive the created server into.
# It is popped off before constructing the McpServerCreate. Values:
#   "proposed" (stays DRAFT), "pending_approval" (submit),
#   "approved" (submit + approve), "deprecated" (submit + approve + deprecate).
# ---------------------------------------------------------------------------

DEMO_MCP_SERVERS: list[dict] = [
    # --- Claims (gateway, DE, Confidential, approved) --------------------
    {
        "name": "internal-claims-mcp",
        "description": "Read-only access to motor and property claims records for the DE market.",
        "kind": "gateway",
        "owner_email": "maria.bauer@contoso.onmicrosoft.com",
        "business_unit": "Claims",
        "region": "DE",
        "data_classification": "Confidential",
        "endpoint_url": "https://mcp.claims.acme.internal/mcp",
        "version": "1.0.0",
        "available_tools": [
            {
                "name": "get_claim",
                "description": "Fetch a single claim by its claim number.",
                "input_schema": {
                    "type": "object",
                    "properties": {"claim_number": {"type": "string"}},
                    "required": ["claim_number"],
                },
            },
            {
                "name": "search_claims",
                "description": "Search claims by policy holder, status, or date range.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "policy_holder": {"type": "string"},
                        "status": {"type": "string", "enum": ["open", "closed", "in_review"]},
                        "from_date": {"type": "string", "format": "date"},
                        "to_date": {"type": "string", "format": "date"},
                    },
                },
            },
            {
                "name": "list_claim_documents",
                "description": "List the documents attached to a claim.",
                "input_schema": {
                    "type": "object",
                    "properties": {"claim_number": {"type": "string"}},
                    "required": ["claim_number"],
                },
            },
        ],
        "lifecycle": "approved",
    },
    # --- Sales / CRM (gateway, EU, Internal, approved, read+write) -------
    {
        "name": "salesforce-mcp",
        "description": "Salesforce CRM access for sales agents: read accounts/opportunities and log activities.",
        "kind": "gateway",
        "owner_email": "lars.svensson@contoso.onmicrosoft.com",
        "business_unit": "Sales",
        "region": "EU",
        "data_classification": "Internal",
        "endpoint_url": "https://mcp.salesforce.acme.internal/mcp",
        "version": "2.1.0",
        "available_tools": [
            {
                "name": "get_account",
                "description": "Fetch a Salesforce account by its record id.",
                "input_schema": {
                    "type": "object",
                    "properties": {"account_id": {"type": "string"}},
                    "required": ["account_id"],
                },
            },
            {
                "name": "search_opportunities",
                "description": "Search open opportunities by owner, stage, or amount.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "owner_email": {"type": "string", "format": "email"},
                        "stage": {"type": "string"},
                        "min_amount": {"type": "number", "minimum": 0},
                    },
                },
            },
            {
                "name": "log_activity",
                "description": "Log a call or meeting activity against an account.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string"},
                        "activity_type": {"type": "string", "enum": ["call", "meeting", "email"]},
                        "notes": {"type": "string", "maxLength": 4000},
                    },
                    "required": ["account_id", "activity_type"],
                },
            },
            {
                "name": "create_task",
                "description": "Create a follow-up task assigned to a sales rep.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "subject": {"type": "string"},
                        "assignee_email": {"type": "string", "format": "email"},
                        "due_date": {"type": "string", "format": "date"},
                    },
                    "required": ["subject", "assignee_email"],
                },
            },
        ],
        "lifecycle": "approved",
    },
    # --- Web search (standard, EU, Public, approved) ---------------------
    {
        "name": "web-search-mcp",
        "description": "General-purpose public web search and page retrieval for grounding agents.",
        "kind": "standard",
        "owner_email": "lars.svensson@contoso.onmicrosoft.com",
        "business_unit": "Platform",
        "region": "EU",
        "data_classification": "Public",
        "endpoint_url": "https://mcp.websearch.acme.internal/mcp",
        "version": "1.4.2",
        "available_tools": [
            {
                "name": "web_search",
                "description": "Run a public web search and return ranked result snippets.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 25},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "fetch_page",
                "description": "Fetch and extract the readable text content of a public URL.",
                "input_schema": {
                    "type": "object",
                    "properties": {"url": {"type": "string", "format": "uri"}},
                    "required": ["url"],
                },
            },
            {
                "name": "search_news",
                "description": "Search recent news articles by topic and time window.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                        "since_days": {"type": "integer", "minimum": 1, "maximum": 90},
                    },
                    "required": ["topic"],
                },
            },
        ],
        "lifecycle": "approved",
    },
    # --- Underwriting / Pricing (gateway, EU, Confidential, pending) -----
    {
        "name": "pricing-tools-mcp",
        "description": "Pricing and rating calculators for underwriting quote generation.",
        "kind": "gateway",
        "owner_email": "maria.bauer@contoso.onmicrosoft.com",
        "business_unit": "Underwriting",
        "region": "EU",
        "data_classification": "Confidential",
        "endpoint_url": "https://mcp.pricing.acme.internal/mcp",
        "version": "0.9.0",
        "available_tools": [
            {
                "name": "calculate_premium",
                "description": "Calculate an indicative premium for a given risk profile.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "product": {"type": "string"},
                        "sum_insured": {"type": "number", "minimum": 0},
                        "risk_factors": {
                            "type": "object",
                            "additionalProperties": {"type": "number"},
                        },
                    },
                    "required": ["product", "sum_insured"],
                },
            },
            {
                "name": "get_rate_table",
                "description": "Retrieve the current rate table for a product and region.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "product": {"type": "string"},
                        "region": {"type": "string"},
                    },
                    "required": ["product"],
                },
            },
            {
                "name": "apply_discount",
                "description": "Apply an eligible discount code to a draft quote.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "quote_id": {"type": "string"},
                        "discount_code": {"type": "string"},
                    },
                    "required": ["quote_id", "discount_code"],
                },
            },
        ],
        "lifecycle": "pending_approval",
    },
    # --- Directory graph (standard, EU, Internal, approved) --------------
    {
        "name": "directory-graph-mcp",
        "description": "Microsoft Graph directory lookups: users, groups, and org structure.",
        "kind": "standard",
        "owner_email": "lars.svensson@contoso.onmicrosoft.com",
        "business_unit": "Platform",
        "region": "EU",
        "data_classification": "Internal",
        "endpoint_url": "https://mcp.directory.acme.internal/mcp",
        "version": "1.2.0",
        "available_tools": [
            {
                "name": "get_user",
                "description": "Look up a directory user by email or object id.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "email": {"type": "string", "format": "email"},
                        "object_id": {"type": "string"},
                    },
                },
            },
            {
                "name": "list_group_members",
                "description": "List the members of a directory group.",
                "input_schema": {
                    "type": "object",
                    "properties": {"group_id": {"type": "string"}},
                    "required": ["group_id"],
                },
            },
            {
                "name": "get_manager_chain",
                "description": "Return the management chain for a given user.",
                "input_schema": {
                    "type": "object",
                    "properties": {"user_id": {"type": "string"}},
                    "required": ["user_id"],
                },
            },
        ],
        "lifecycle": "approved",
    },
    # --- Document store (standard, DE, Confidential, approved) -----------
    {
        "name": "document-store-mcp",
        "description": "Read-only retrieval over the DE document repository for grounding and citations.",
        "kind": "standard",
        "owner_email": "maria.bauer@contoso.onmicrosoft.com",
        "business_unit": "Claims",
        "region": "DE",
        "data_classification": "Confidential",
        "endpoint_url": "https://mcp.docstore.acme.internal/mcp",
        "version": "1.0.3",
        "available_tools": [
            {
                "name": "search_documents",
                "description": "Semantic search across the document repository.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1},
                        "top_k": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "get_document",
                "description": "Fetch a single document's metadata and text by id.",
                "input_schema": {
                    "type": "object",
                    "properties": {"document_id": {"type": "string"}},
                    "required": ["document_id"],
                },
            },
            {
                "name": "list_collections",
                "description": "List the available document collections.",
                "input_schema": {"type": "object", "properties": {}},
            },
        ],
        "lifecycle": "approved",
    },
    # --- Legacy policy (standard, DE, Internal, DEPRECATED) --------------
    {
        "name": "legacy-policy-mcp",
        "description": "Legacy policy-administration system bridge — superseded by the new policy API.",
        "kind": "standard",
        "owner_email": "lars.svensson@contoso.onmicrosoft.com",
        "business_unit": "Underwriting",
        "region": "DE",
        "data_classification": "Internal",
        "endpoint_url": "https://mcp.legacy-policy.acme.internal/mcp",
        "version": "0.5.0",
        "available_tools": [
            {
                "name": "get_policy",
                "description": "Fetch a policy record from the legacy mainframe bridge.",
                "input_schema": {
                    "type": "object",
                    "properties": {"policy_number": {"type": "string"}},
                    "required": ["policy_number"],
                },
            },
            {
                "name": "list_endorsements",
                "description": "List endorsements attached to a legacy policy.",
                "input_schema": {
                    "type": "object",
                    "properties": {"policy_number": {"type": "string"}},
                    "required": ["policy_number"],
                },
            },
            {
                "name": "get_billing_status",
                "description": "Return the billing status for a legacy policy.",
                "input_schema": {
                    "type": "object",
                    "properties": {"policy_number": {"type": "string"}},
                    "required": ["policy_number"],
                },
            },
        ],
        "lifecycle": "deprecated",
    },
]


# Lifecycle directives the seed understands (the `lifecycle` key in
# DEMO_MCP_SERVERS). "proposed" / "pending_approval" leave the record short of
# APPROVED; "approved" drives submit+approve; "deprecated" drives
# submit+approve+deprecate (DEPRECATED requires APPROVED first per the native
# lifecycle — research §5).
_VALID_LIFECYCLES = {"proposed", "pending_approval", "approved", "deprecated"}


def _build_mcp_create(fields: dict):
    """Construct an McpServerCreate from a DEMO_MCP_SERVERS dict (minus the
    `lifecycle` directive). Pure Pydantic — no AWS. The plain-dict tools in
    `available_tools` are coerced to McpTool by Pydantic (the field is
    list[McpTool]). Imported lazily so importing this module triggers no
    settings/AWS setup."""
    from models.mcp_server import McpServerCreate

    payload = {k: v for k, v in fields.items() if k != "lifecycle"}
    # E24: tenant_id is REQUIRED on McpServerCreate — demo MCP servers belong to
    # the seed "default" tenant unless the DEMO_MCP_SERVERS entry says otherwise.
    payload.setdefault("tenant_id", "default")
    return McpServerCreate(**payload)


def dry_run() -> int:
    """Validate every demo MCP server as an McpServerCreate (pure Pydantic, NO
    AWS) and print what would be created. Returns a process exit code."""
    logger.info(
        "DRY RUN — validating %d demo MCP-server payloads (no AWS calls) ...",
        len(DEMO_MCP_SERVERS),
    )

    names: set[str] = set()
    validated = 0
    for fields in DEMO_MCP_SERVERS:
        lifecycle = fields.get("lifecycle", "proposed")
        if lifecycle not in _VALID_LIFECYCLES:
            logger.error("  %r has unknown lifecycle directive %r", fields.get("name"), lifecycle)
            return 1
        mcp = _build_mcp_create(fields)
        if mcp.name in names:
            logger.error("  duplicate name detected: %r", mcp.name)
            return 1
        names.add(mcp.name)
        validated += 1
        logger.info(
            "  [%-16s] %-22s kind=%-8s bu=%-12s region=%-3s class=%-12s tools=%d",
            lifecycle,
            mcp.name,
            mcp.kind.value if mcp.kind else "-",
            mcp.business_unit or "-",
            mcp.region or "-",
            mcp.data_classification.value if mcp.data_classification else "-",
            len(mcp.available_tools),
        )

    logger.info(
        "DRY RUN OK — %d/%d McpServerCreate payloads validated, all names unique.",
        validated,
        len(DEMO_MCP_SERVERS),
    )
    print(f"validated {validated} mcp-server payloads (dry run, no AWS)")
    return 0


def _drive_lifecycle(service, mcp, lifecycle: str) -> None:
    """Drive a freshly-created MCP server into its target lifecycle state.

    Defensive: any transition failure is logged and swallowed so one bad
    transition can't abort the whole seed. DEPRECATED requires APPROVED first
    (research §5), so the deprecate path approves before deprecating.
    """
    try:
        if lifecycle == "proposed":
            return  # stays DRAFT — nothing to do
        if lifecycle == "pending_approval":
            service.submit_for_approval(mcp.id)
            logger.info("    submitted %r for approval -> pending_approval", mcp.name)
            return
        if lifecycle in ("approved", "deprecated"):
            service.submit_for_approval(mcp.id)
            service.transition(mcp.id, "approve", "Seed: approved for demo")
            logger.info("    approved %r", mcp.name)
            if lifecycle == "deprecated":
                # DEPRECATED is reachable only from APPROVED (research §5).
                service.transition(mcp.id, "deprecate", "Seed: deprecated for demo")
                logger.info("    deprecated %r", mcp.name)
    except Exception as exc:  # noqa: BLE001 — keep seeding even if a transition fails
        logger.warning(
            "    lifecycle transition for %r (target %s) failed: %s — continuing",
            mcp.name,
            lifecycle,
            exc,
        )


def seed(registry_id: str, region: str) -> int:
    """Create all demo MCP servers and drive their lifecycle. Returns an exit code.

    Imports + service construction + AWS calls all happen here (NOT at module
    top-level) so importing this module is side-effect-free.
    """
    from models.mcp_server import McpServerCreate  # noqa: F401 — built via _build_mcp_create
    from services.mcp_server_service import McpServerRegistryService, NameTakenError

    service = McpServerRegistryService(registry_id=registry_id, region=region)

    created = 0
    skipped = 0
    for fields in DEMO_MCP_SERVERS:
        lifecycle = fields.get("lifecycle", "proposed")
        name = fields.get("name")
        try:
            mcp = service.create(_build_mcp_create(fields), created_by="seed")
        except NameTakenError:
            logger.info("  skip, already exists: %r", name)
            skipped += 1
            continue
        except Exception as exc:  # noqa: BLE001 — log + continue, don't abort the seed
            logger.error("  failed to create %r: %s — continuing", name, exc)
            continue

        logger.info("  created %r -> id=%s (target lifecycle: %s)", name, mcp.id, lifecycle)
        created += 1
        _drive_lifecycle(service, mcp, lifecycle)

    logger.info(
        "Seed complete: %d created, %d skipped (already existed), %d total demo MCP servers.",
        created,
        skipped,
        len(DEMO_MCP_SERVERS),
    )
    print(f"seed complete: created={created} skipped={skipped} total={len(DEMO_MCP_SERVERS)}")
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Seed representative demo MCP servers into the AWS Agent Registry.",
    )
    parser.add_argument(
        "--registry-id",
        default=None,
        help="AWS Agent Registry registryId for MCP servers (default: settings.MCP_REGISTRY_ID).",
    )
    parser.add_argument(
        "--region",
        default=None,
        help="AWS region hosting the MCP registry (default: settings.MCP_REGISTRY_REGION).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate + print the demo MCP-server payloads without touching AWS.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # --dry-run never constructs the service or calls AWS — pure Pydantic.
    if args.dry_run:
        return dry_run()

    # Defaults from settings; imported here (not at module top) so importing this
    # module never triggers Settings() validation or any AWS setup.
    from core.config import settings

    registry_id = args.registry_id or settings.MCP_REGISTRY_ID
    region = args.region or settings.MCP_REGISTRY_REGION

    if not registry_id:
        logger.error("No registry id — cannot seed.")
        logger.error(
            "Terraform creates the registry; ask AWS for the id it minted:\n"
            "    aws agent-registry-control list-registries "
            "--query \"registries[?name=='agp-mcp-servers'].registryId\" --output text\n"
            "then pass it as --registry-id <id> (or export MCP_REGISTRY_ID=<id>). "
            "To create the registry by hand instead:\n"
            "    PYTHONPATH=src venv/bin/python scripts/ensure_registry.py --name agp-mcp-servers"
        )
        return 2

    try:
        return seed(registry_id=registry_id, region=region)
    except Exception as exc:  # noqa: BLE001 — surface a clean, actionable message
        logger.error("Seeding failed: %s", exc)
        logger.error(
            "Check that: (1) AWS credentials are configured with the registry "
            "permissions (research §8); (2) MCP_REGISTRY_ID=%r is a real, "
            "provisioned registry in region %r; (3) the AWS Agent Registry "
            "(Bedrock AgentCore, Preview) is available in that region (NOT "
            "eu-central-1; research §6/§7).",
            registry_id,
            region,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
