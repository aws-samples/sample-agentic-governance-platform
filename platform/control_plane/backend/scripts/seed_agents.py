#!/usr/bin/env python
"""Seed ~20 representative demo agents into the AWS Agent Registry.

This is a USER / CONTROLLER step: it makes live AWS calls (via
``AgentRegistryService`` -> ``bedrock-agentcore-control``, Bedrock AgentCore,
Public Preview) and therefore needs AWS credentials with the registry
permissions (research §8) AND a provisioned ``AGENT_REGISTRY_ID``. It is NOT run
by automation in no-commit mode; the controller runs it once to populate the
governance registry with demo data so the list view and the approval flow have a
credible mix of agents to show.

What it does:
  1. Build ~20 ``AgentCreate`` payloads (defined as data below) spread across
     platforms / business units / regions / data classifications / origins. Each
     entry's ``lifecycle`` key is a seed-only directive, not an ``AgentCreate`` field:
     ``"proposed"`` (the default; stays DRAFT), ``"pending_approval"`` (submit),
     ``"approved"`` (submit + approve) or ``"deprecated"`` (submit + approve +
     deprecate). It is dropped before the payload is built.
  2. For each: ``service.create(AgentCreate(**fields), created_by="seed")``.
     Idempotent — a name that already exists raises ``NameTakenError`` which is
     caught + logged ("skip, already exists"), so re-running does not duplicate.
  3. Drive a lifecycle spread for demo credibility: most agents stay ``proposed``
     (DRAFT); a few are submitted-for-approval + approved (-> APPROVED); and one
     is approved-then-deprecated (-> DEPRECATED). DEPRECATED requires going
     through APPROVED first per the native lifecycle (research §5), so the
     deprecate path approves first. Every transition is wrapped defensively: a
     failure logs + continues rather than aborting the whole seed.

Prerequisite — the registry must exist, and this script needs its ID. Note the asymmetry
with the RUNNING backend, which resolves the registry by NAME at first use and needs no id
configured at all: this script still takes an id, because ``--registry-id`` is its long-standing
interface and several sibling scripts share it. The normal way to get one is to ask AWS for the
registry Terraform created:

    aws agent-registry-control list-registries \
        --query "registries[?name=='agp-agents'].registryId" --output text

Terraform creates both registries, so provisioning one by hand is a fallback/diagnostic — it
prints the same id:

    cd platform/control_plane/backend && \
        PYTHONPATH=src venv/bin/python scripts/ensure_registry.py --name agp-agents

Pass the id with ``--registry-id`` (or export ``AGENT_REGISTRY_ID`` in your environment/.env,
which the backend still honours as an explicit override).

Run from the backend dir (PYTHONPATH=src is required — src/ is not a package):

    cd platform/control_plane/backend && \
        PYTHONPATH=src venv/bin/python scripts/seed_agents.py

Offline correctness check (NO AWS calls — validates every AgentCreate payload via
pure Pydantic and prints what it would create):

    cd platform/control_plane/backend && \
        PYTHONPATH=src venv/bin/python scripts/seed_agents.py --dry-run

``--region`` / ``--registry-id`` override the settings defaults. With no id from either source
(and not a dry-run) the script prints the lookup command above and exits non-zero rather than
addressing an empty registry.
"""

import argparse
import logging
import sys

logger = logging.getLogger("seed_agents")

DEMO_AGENTS: list[dict] = [
    {
        "name": "claims-triage-de",
        "purpose": "Triage inbound motor claims for the DE market and route to the right adjuster queue.",
        "sponsor_email": "maria.bauer@contoso.onmicrosoft.com",
        "business_unit": "Claims",
        "region": "DE",
        "data_classification": "Confidential",
        "platform": "aws_bedrock",
        "framework": "langgraph",
        "origin": "Registered",
        "lifecycle": "approved",
    },
    {
        "name": "fraud-watch-eu",
        "purpose": "Flag potentially fraudulent claims across EU markets using anomaly signals.",
        "sponsor_email": "lars.svensson@contoso.onmicrosoft.com",
        "business_unit": "Claims",
        "region": "EU",
        "data_classification": "Restricted",
        "platform": "aws_bedrock",
        "framework": "strands",
        "origin": "Deployed",
        "lifecycle": "approved",
    },
    {
        "name": "claims-doc-extract-it",
        "purpose": "Extract structured fields from scanned claim documents for the IT market.",
        "sponsor_email": "giulia.romano@contoso.onmicrosoft.com",
        "business_unit": "Claims",
        "region": "IT",
        "data_classification": "Confidential",
        "platform": "aws_bedrock",
        "framework": "langgraph",
        "origin": "Registered",
        "lifecycle": "proposed",
    },
    {
        "name": "claims-settlement-advisor-fr",
        "purpose": "Recommend settlement ranges for property claims in the FR market.",
        "sponsor_email": "antoine.dubois@contoso.onmicrosoft.com",
        "business_unit": "Claims",
        "region": "FR",
        "data_classification": "Confidential",
        "platform": "azure",
        "framework": "semantic-kernel",
        "origin": "Registered",
        "lifecycle": "pending_approval",
    },
    {
        "name": "underwriting-copilot-fr",
        "purpose": "Assist underwriters with risk scoring and policy recommendations for the FR market.",
        "sponsor_email": "antoine.dubois@contoso.onmicrosoft.com",
        "business_unit": "Underwriting",
        "region": "FR",
        "data_classification": "Confidential",
        "platform": "aws_bedrock",
        "framework": "langgraph",
        "origin": "Registered",
        "lifecycle": "approved",
    },
    {
        "name": "risk-appetite-checker-de",
        "purpose": "Check submissions against the current risk-appetite framework for the DE market.",
        "sponsor_email": "maria.bauer@contoso.onmicrosoft.com",
        "business_unit": "Underwriting",
        "region": "DE",
        "data_classification": "Internal",
        "platform": "aws_bedrock",
        "framework": "strands",
        "origin": "Registered",
        "lifecycle": "proposed",
    },
    {
        "name": "submission-intake-se",
        "purpose": "Normalize and enrich broker submissions before underwriting review in the SE market.",
        "sponsor_email": "erik.larsson@contoso.onmicrosoft.com",
        "business_unit": "Underwriting",
        "region": "SE",
        "data_classification": "Confidential",
        "platform": "aws_bedrock",
        "framework": "langgraph",
        "origin": "Deployed",
        "lifecycle": "proposed",
    },
    {
        "name": "finance-close-assistant-eu",
        "purpose": "Summarize month-end close exceptions and draft reconciliation notes for Finance.",
        "sponsor_email": "sophie.meier@contoso.onmicrosoft.com",
        "business_unit": "Finance",
        "region": "EU",
        "data_classification": "Confidential",
        "platform": "aws_bedrock",
        "framework": "langgraph",
        "origin": "Registered",
        "lifecycle": "approved",
    },
    {
        "name": "invoice-reconciler-de",
        "purpose": "Match supplier invoices to purchase orders and flag discrepancies for the DE entity.",
        "sponsor_email": "sophie.meier@contoso.onmicrosoft.com",
        "business_unit": "Finance",
        "region": "DE",
        "data_classification": "Internal",
        "platform": "sap",
        "framework": "other",
        "origin": "Registered",
        "lifecycle": "proposed",
    },
    {
        "name": "expense-policy-bot-it",
        "purpose": "Answer employee expense-policy questions for the IT market.",
        "sponsor_email": "giulia.romano@contoso.onmicrosoft.com",
        "business_unit": "Finance",
        "region": "IT",
        "data_classification": "Internal",
        "platform": "aws_bedrock",
        "framework": "strands",
        "origin": "Registered",
        "lifecycle": "deprecated",
    },
    {
        "name": "broker-assist-it",
        "purpose": "Answer broker questions about products and quote status for the IT market.",
        "sponsor_email": "giulia.romano@contoso.onmicrosoft.com",
        "business_unit": "Broker Ops",
        "region": "IT",
        "data_classification": "Internal",
        "platform": "salesforce",
        "framework": "other",
        "origin": "Deployed",
        "lifecycle": "approved",
    },
    {
        "name": "broker-onboarding-de",
        "purpose": "Guide new brokers through onboarding and document collection in the DE market.",
        "sponsor_email": "maria.bauer@contoso.onmicrosoft.com",
        "business_unit": "Broker Ops",
        "region": "DE",
        "data_classification": "Internal",
        "platform": "aws_bedrock",
        "framework": "langgraph",
        "origin": "Registered",
        "lifecycle": "proposed",
    },
    {
        "name": "quote-followup-fr",
        "purpose": "Draft follow-up messages for open broker quotes in the FR market.",
        "sponsor_email": "antoine.dubois@contoso.onmicrosoft.com",
        "business_unit": "Broker Ops",
        "region": "FR",
        "data_classification": "Internal",
        "platform": "aws_bedrock",
        "framework": "strands",
        "origin": "Registered",
        "lifecycle": "pending_approval",
    },
    {
        "name": "actuarial-data-prep-eu",
        "purpose": "Prepare and validate datasets for reserving models across EU markets.",
        "sponsor_email": "erik.larsson@contoso.onmicrosoft.com",
        "business_unit": "Actuarial",
        "region": "EU",
        "data_classification": "Confidential",
        "platform": "databricks",
        "framework": "other",
        "origin": "Registered",
        "lifecycle": "proposed",
    },
    {
        "name": "reserving-explainer-se",
        "purpose": "Generate plain-language explanations of reserving model outputs for the SE market.",
        "sponsor_email": "erik.larsson@contoso.onmicrosoft.com",
        "business_unit": "Actuarial",
        "region": "SE",
        "data_classification": "Confidential",
        "platform": "aws_bedrock",
        "framework": "langgraph",
        "origin": "Registered",
        "lifecycle": "approved",
    },
    {
        "name": "pricing-sensitivity-de",
        "purpose": "Run pricing sensitivity scenarios and summarize impacts for the DE market.",
        "sponsor_email": "maria.bauer@contoso.onmicrosoft.com",
        "business_unit": "Actuarial",
        "region": "DE",
        "data_classification": "Restricted",
        "platform": "aws_bedrock",
        "framework": "strands",
        "origin": "Registered",
        "lifecycle": "proposed",
    },
    {
        "name": "customer-care-bot-de",
        "purpose": "Answer policyholder questions and handle simple service requests for the DE market.",
        "sponsor_email": "maria.bauer@contoso.onmicrosoft.com",
        "business_unit": "Customer Service",
        "region": "DE",
        "data_classification": "Internal",
        "platform": "azure",
        "framework": "semantic-kernel",
        "origin": "Deployed",
        "lifecycle": "approved",
    },
    {
        "name": "complaint-router-it",
        "purpose": "Classify and route inbound complaints to the right resolution team in the IT market.",
        "sponsor_email": "giulia.romano@contoso.onmicrosoft.com",
        "business_unit": "Customer Service",
        "region": "IT",
        "data_classification": "Confidential",
        "platform": "aws_bedrock",
        "framework": "langgraph",
        "origin": "Registered",
        "lifecycle": "proposed",
    },
    {
        "name": "policy-faq-public-eu",
        "purpose": "Answer general, non-personalized product FAQs on the public EU website.",
        "sponsor_email": "sophie.meier@contoso.onmicrosoft.com",
        "business_unit": "Customer Service",
        "region": "EU",
        "data_classification": "Public",
        "platform": "aws_bedrock",
        "framework": "strands",
        "origin": "Registered",
        "lifecycle": "proposed",
    },
    {
        "name": "renewal-reminder-fr",
        "purpose": "Draft personalized renewal reminders for policyholders in the FR market.",
        "sponsor_email": "antoine.dubois@contoso.onmicrosoft.com",
        "business_unit": "Customer Service",
        "region": "FR",
        "data_classification": "Confidential",
        "platform": "aws_bedrock",
        "framework": "langgraph",
        "origin": "Registered",
        "lifecycle": "proposed",
    },
]


_VALID_LIFECYCLES = {"proposed", "pending_approval", "approved", "deprecated"}


def _build_agent_create(fields: dict):
    """Construct an AgentCreate from a DEMO_AGENTS dict (minus the `lifecycle`
    directive). Pure Pydantic — no AWS. Imported lazily so importing this module
    triggers no settings/AWS setup. ``tenant_id`` is required on AgentCreate: demo
    agents belong to the seed ``"default"`` tenant unless the entry says otherwise."""
    from models.agent import AgentCreate

    payload = {k: v for k, v in fields.items() if k != "lifecycle"}
    payload.setdefault("tenant_id", "default")
    return AgentCreate(**payload)


def dry_run() -> int:
    """Validate every demo agent as an AgentCreate (pure Pydantic, NO AWS) and
    print what would be created. Returns a process exit code."""
    logger.info("DRY RUN — validating %d demo agent payloads (no AWS calls) ...", len(DEMO_AGENTS))

    names: set[str] = set()
    validated = 0
    for fields in DEMO_AGENTS:
        lifecycle = fields.get("lifecycle", "proposed")
        if lifecycle not in _VALID_LIFECYCLES:
            logger.error("  %r has unknown lifecycle directive %r", fields.get("name"), lifecycle)
            return 1
        agent = _build_agent_create(fields)
        if agent.name in names:
            logger.error("  duplicate name detected: %r", agent.name)
            return 1
        names.add(agent.name)
        validated += 1
        logger.info(
            "  [%-14s] %-28s bu=%-16s region=%-3s class=%-12s platform=%-11s origin=%s",
            lifecycle,
            agent.name,
            agent.business_unit,
            agent.region,
            agent.data_classification.value if agent.data_classification else "-",
            agent.platform.value if agent.platform else "-",
            agent.origin.value,
        )

    logger.info(
        "DRY RUN OK — %d/%d AgentCreate payloads validated, all names unique.", validated, len(DEMO_AGENTS)
    )
    print(f"validated {validated} agent payloads (dry run, no AWS)")
    return 0


def _drive_lifecycle(service, agent, lifecycle: str) -> None:
    """Drive a freshly-created agent into its target lifecycle state.

    Seeding is best-effort: any failed transition, whatever its class (the service's own
    errors include a plain ``AgentNotFoundError``), is logged with its traceback and
    swallowed so one bad transition can't abort the whole seed. DEPRECATED requires
    APPROVED first (research §5), so the deprecate path approves before deprecating.
    """
    try:
        if lifecycle == "proposed":
            return
        if lifecycle == "pending_approval":
            service.submit_for_approval(agent.id)
            logger.info("    submitted %r for approval -> pending_approval", agent.name)
            return
        if lifecycle in ("approved", "deprecated"):
            service.submit_for_approval(agent.id)
            service.transition(agent.id, "approve", "Seed: approved for demo")
            logger.info("    approved %r", agent.name)
            if lifecycle == "deprecated":
                service.transition(agent.id, "deprecate", "Seed: deprecated for demo")
                logger.info("    deprecated %r", agent.name)
    except Exception as exc:
        logger.exception(
            "    lifecycle transition for %r (target %s) failed: %s — continuing",
            agent.name,
            lifecycle,
            exc,
        )


def seed(registry_id: str, region: str) -> int:
    """Create all demo agents and drive their lifecycle. Returns an exit code.

    Imports + service construction + AWS calls all happen here (NOT at module
    top-level) so importing this module is side-effect-free.
    """
    from services.agent_registry_service import AgentRegistryService, NameTakenError

    service = AgentRegistryService(registry_id=registry_id, region=region)

    created = 0
    skipped = 0
    for fields in DEMO_AGENTS:
        lifecycle = fields.get("lifecycle", "proposed")
        name = fields.get("name")
        try:
            agent = service.create(_build_agent_create(fields), created_by="seed")
        except NameTakenError:
            logger.info("  skip, already exists: %r", name)
            skipped += 1
            continue
        except Exception as exc:
            logger.exception("  failed to create %r: %s — continuing", name, exc)
            continue

        logger.info("  created %r -> id=%s (target lifecycle: %s)", name, agent.id, lifecycle)
        created += 1
        _drive_lifecycle(service, agent, lifecycle)

    logger.info(
        "Seed complete: %d created, %d skipped (already existed), %d total demo agents.",
        created,
        skipped,
        len(DEMO_AGENTS),
    )
    print(f"seed complete: created={created} skipped={skipped} total={len(DEMO_AGENTS)}")
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Seed ~20 representative demo agents into the AWS Agent Registry.",
    )
    parser.add_argument(
        "--registry-id",
        default=None,
        help="AWS Agent Registry registryId (default: settings.AGENT_REGISTRY_ID).",
    )
    parser.add_argument(
        "--region",
        default=None,
        help="AWS region hosting the registry (default: settings.AGENT_REGISTRY_REGION).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate + print the demo agent payloads without touching AWS.",
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

    if args.dry_run:
        return dry_run()

    from core.config import settings

    registry_id = args.registry_id or settings.AGENT_REGISTRY_ID
    region = args.region or settings.AGENT_REGISTRY_REGION

    if not registry_id:
        logger.error("No registry id — cannot seed.")
        logger.error(
            "Terraform creates the registry; ask AWS for the id it minted:\n"
            "    aws agent-registry-control list-registries "
            "--query \"registries[?name=='agp-agents'].registryId\" --output text\n"
            "then pass it as --registry-id <id> (or export AGENT_REGISTRY_ID=<id>). "
            "To create the registry by hand instead:\n"
            "    PYTHONPATH=src venv/bin/python scripts/ensure_registry.py --name agp-agents"
        )
        return 2

    try:
        return seed(registry_id=registry_id, region=region)
    except Exception as exc:
        logger.exception("Seeding failed: %s", exc)
        logger.error(
            "Check that: (1) AWS credentials are configured with the registry "
            "permissions (research §8); (2) AGENT_REGISTRY_ID=%r is a real, "
            "provisioned registry in region %r; (3) the AWS Agent Registry "
            "(Bedrock AgentCore, Preview) is available in that region (NOT "
            "eu-central-1; research §11).",
            registry_id,
            region,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
