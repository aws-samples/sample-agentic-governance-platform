#!/usr/bin/env python
"""The DEMO_USE_CASES registry — PURE DATA for the three demo insurance gateways.

This module imports NOTHING from AWS/boto3 (it is the leaf of the dependency graph in
the design's §3). It declares one entry per demo domain:

  1. Contact Center        -> agp-contact-center-mcp
  2. First Notification    -> agp-fnol-mcp
     of Loss (FNOL)
  3. Insurance Support     -> agp-insurance-support-mcp

Each entry is a dict with:
  * ``key``          — the short selector used by ``bootstrap_demo_use_cases.py --only``.
  * ``prefix``       — the ``agp-*`` resource-name prefix (no ``-mcp`` suffix; the
                       bootstrap's ``_names(prefix)`` appends ``-mcp`` etc.). Unique.
  * ``display_name`` — the human label printed/written in the output file.
  * ``description``  — a one-line description of the domain.
  * ``tool_schema``  — the inline ``CreateGatewayTarget`` ``toolSchema.inlinePayload``
                       (a list of ``{name, description, inputSchema}`` dicts, exactly the
                       shape of ``create_example_gateway._DEMO_TOOL_SCHEMA``; every
                       ``inputSchema.type == "object"``). 6 tools per domain (design §6).
  * ``handler_src``  — the FULL inline Lambda handler SOURCE for the domain. It is built
                       by concatenating a shared ``_HANDLER_PREAMBLE`` (the module
                       docstring + the ``_tool_name(context)`` ``"___"``-splitter copied
                       VERBATIM from ``bootstrap_demo_gateway.py``'s ``LAMBDA_HANDLER_SRC``)
                       with a per-domain ``def handler(event, context):`` that branches per
                       tool and returns a DETERMINISTIC mock dict (no ``random``, no
                       ``datetime.now`` — fixed ISO strings; echoes back the input ids) and
                       raises ``ValueError`` on an unknown tool.

The handler responses are deterministic by design (design §6): demo runs are reproducible
and the mocks feel live by echoing back the caller's input identifiers. The bootstrap's
``--dry-run`` ``ast.parse``s each ``handler_src`` and asserts every ``tool_schema`` name
appears as a branch in it (drift guard), so the advertised tools and the mock
implementation can never silently diverge.
"""

# ---------------------------------------------------------------------------
# Shared handler preamble — the module docstring + the _tool_name("___"-splitter)
# copied VERBATIM from bootstrap_demo_gateway.py's LAMBDA_HANDLER_SRC. Each domain's
# handler_src = _HANDLER_PREAMBLE + a per-domain `def handler(event, context):`.
#
# AgentCore lambda-target contract (research §8.1, §6.2): the Gateway invokes the
# Lambda with the tool ARGS as the `event` and the tool NAME in the client context at
# context.client_context.custom["bedrockAgentCoreToolName"]. The gateway prefixes the
# tool name with the target name joined by "___" (e.g. "agp-...-target___echo"),
# so we split on "___" and take the last segment. This event/context shape IS the
# AgentCore lambda-target contract — keep it in sync with the gateway target config.
# ---------------------------------------------------------------------------
_HANDLER_PREAMBLE = '''\
"""agp MCP tools — Lambda backing the AgentCore Gateway inline-lambda target.

AgentCore lambda-target contract: the Gateway invokes this function with the tool
ARGUMENTS as the `event` dict and the tool NAME carried in the client context at
context.client_context.custom["bedrockAgentCoreToolName"]. The gateway prefixes the
tool name with the target name joined by "___", so we split on "___" and use the last
segment. This event/context shape is the AgentCore lambda-target contract.

The mock responses are DETERMINISTIC (no random, no datetime.now — fixed ISO strings,
input ids echoed back), so a demo run is reproducible.
"""


def _tool_name(context):
    """Extract the bare tool name from the AgentCore client context."""
    custom = {}
    client_context = getattr(context, "client_context", None)
    if client_context is not None:
        custom = getattr(client_context, "custom", None) or {}
    raw = custom.get("bedrockAgentCoreToolName", "")
    # The gateway namespaces the tool as "<targetName>___<toolName>".
    return raw.split("___")[-1] if raw else raw
'''


# ===========================================================================
# 1) Contact Center — agp-contact-center-mcp  (design §6.1)
# ===========================================================================
_CONTACT_CENTER_TOOL_SCHEMA = [
    {
        "name": "get_customer_profile",
        "description": "Fetch a customer's profile.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "description": "The customer identifier."},
            },
            "required": ["customer_id"],
        },
    },
    {
        "name": "get_interaction_history",
        "description": "List a customer's recent support interactions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "description": "The customer identifier."},
                "limit": {
                    "type": "integer",
                    "description": "Max interactions to return (default 3).",
                },
            },
            "required": ["customer_id"],
        },
    },
    {
        "name": "verify_customer_identity",
        "description": "Knowledge-based verification of a customer's identity.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "description": "The customer identifier."},
                "last_name": {"type": "string", "description": "The customer's last name."},
                "date_of_birth": {
                    "type": "string",
                    "description": "The customer's date of birth (YYYY-MM-DD).",
                },
            },
            "required": ["customer_id", "last_name", "date_of_birth"],
        },
    },
    {
        "name": "create_support_ticket",
        "description": "Open a new support ticket for a customer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "description": "The customer identifier."},
                "subject": {"type": "string", "description": "Short ticket subject."},
                "priority": {
                    "type": "string",
                    "description": "Ticket priority (e.g. LOW/MEDIUM/HIGH).",
                },
                "description": {"type": "string", "description": "Free-text issue description."},
            },
            "required": ["customer_id", "subject", "priority", "description"],
        },
    },
    {
        "name": "get_ticket_status",
        "description": "Fetch the current status of a support ticket.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string", "description": "The ticket identifier."},
            },
            "required": ["ticket_id"],
        },
    },
    {
        "name": "schedule_callback",
        "description": "Schedule a callback for a customer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "description": "The customer identifier."},
                "phone": {"type": "string", "description": "Phone number to call back."},
                "preferred_time": {
                    "type": "string",
                    "description": "Preferred callback time (ISO 8601).",
                },
            },
            "required": ["customer_id", "phone", "preferred_time"],
        },
    },
]

_CONTACT_CENTER_HANDLER = '''

def handler(event, context):
    """Branch on the tool name; return a DETERMINISTIC mock for each contact-center tool."""
    event = event or {}
    name = _tool_name(context)

    if name == "get_customer_profile":
        # Deterministic profile derived from the customer_id (echoed back).
        customer_id = event.get("customer_id")
        return {
            "customer_id": customer_id,
            "full_name": "Maria Bauer",
            "tier": "GOLD",
            "preferred_language": "de-DE",
            "email": "maria.bauer@example.com",
            "phone": "+49 89 1234567",
            "customer_since": "2018-04-12",
        }

    if name == "get_interaction_history":
        # Honor `limit` (default 3) over a fixed sample of interactions.
        customer_id = event.get("customer_id")
        limit = event.get("limit", 3)
        interactions = [
            {
                "date": "2026-05-30T09:15:00Z",
                "channel": "phone",
                "topic": "billing question",
                "sentiment": "neutral",
                "agent": "agent-017",
            },
            {
                "date": "2026-05-22T14:02:00Z",
                "channel": "email",
                "topic": "policy renewal",
                "sentiment": "positive",
                "agent": "agent-004",
            },
            {
                "date": "2026-05-10T11:40:00Z",
                "channel": "chat",
                "topic": "address change",
                "sentiment": "positive",
                "agent": "agent-022",
            },
            {
                "date": "2026-04-28T16:25:00Z",
                "channel": "phone",
                "topic": "claim status",
                "sentiment": "neutral",
                "agent": "agent-017",
            },
        ]
        return {"customer_id": customer_id, "interactions": interactions[:limit]}

    if name == "verify_customer_identity":
        customer_id = event.get("customer_id")
        return {
            "customer_id": customer_id,
            "verified": True,
            "method": "knowledge-based",
            "confidence": 0.97,
        }

    if name == "create_support_ticket":
        customer_id = event.get("customer_id")
        priority = event.get("priority")
        return {
            "ticket_id": "TKT-100245",
            "customer_id": customer_id,
            "status": "OPEN",
            "priority": priority,
            "sla_due": "2026-06-07T17:00:00Z",
        }

    if name == "get_ticket_status":
        ticket_id = event.get("ticket_id")
        return {
            "ticket_id": ticket_id,
            "status": "IN_PROGRESS",
            "assigned_team": "Tier-2 Support",
            "last_update": "2026-06-05T08:30:00Z",
            "resolution_eta": "2026-06-06T12:00:00Z",
        }

    if name == "schedule_callback":
        customer_id = event.get("customer_id")
        scheduled_for = event.get("preferred_time")
        return {
            "callback_id": "CB-55012",
            "customer_id": customer_id,
            "scheduled_for": scheduled_for,
            "status": "SCHEDULED",
        }

    raise ValueError(
        "unknown tool: %r (expected one of: get_customer_profile, "
        "get_interaction_history, verify_customer_identity, create_support_ticket, "
        "get_ticket_status, schedule_callback)" % name
    )
'''


# ===========================================================================
# 2) First Notification of Loss (FNOL) — agp-fnol-mcp  (design §6.2)
# ===========================================================================
_FNOL_TOOL_SCHEMA = [
    {
        "name": "start_claim",
        "description": "Open a new claim from a first notification of loss.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "policy_number": {"type": "string", "description": "The policy number."},
                "loss_type": {
                    "type": "string",
                    "description": "Type of loss (e.g. collision, theft, water).",
                },
                "loss_date": {"type": "string", "description": "Date of loss (YYYY-MM-DD)."},
                "description": {"type": "string", "description": "Free-text loss description."},
            },
            "required": ["policy_number", "loss_type", "loss_date", "description"],
        },
    },
    {
        "name": "get_policy_coverage",
        "description": "List the coverages on a policy.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "policy_number": {"type": "string", "description": "The policy number."},
            },
            "required": ["policy_number"],
        },
    },
    {
        "name": "upload_loss_document",
        "description": "Attach a supporting document to a claim.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "claim_id": {"type": "string", "description": "The claim identifier."},
                "document_type": {
                    "type": "string",
                    "description": "Document type (e.g. photo, police_report, invoice).",
                },
                "file_name": {"type": "string", "description": "The uploaded file name."},
            },
            "required": ["claim_id", "document_type", "file_name"],
        },
    },
    {
        "name": "get_claim_status",
        "description": "Fetch the current status of a claim.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "claim_id": {"type": "string", "description": "The claim identifier."},
            },
            "required": ["claim_id"],
        },
    },
    {
        "name": "find_repair_shops",
        "description": "Find approved repair shops near a postal code.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "postal_code": {"type": "string", "description": "Postal code to search near."},
                "service_type": {
                    "type": "string",
                    "description": "Type of repair service needed.",
                },
            },
            "required": ["postal_code", "service_type"],
        },
    },
    {
        "name": "estimate_payout",
        "description": "Estimate the payout for a claim.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "claim_id": {"type": "string", "description": "The claim identifier."},
            },
            "required": ["claim_id"],
        },
    },
]

_FNOL_HANDLER = '''

def handler(event, context):
    """Branch on the tool name; return a DETERMINISTIC mock for each FNOL tool."""
    event = event or {}
    name = _tool_name(context)

    if name == "start_claim":
        policy_number = event.get("policy_number")
        loss_type = event.get("loss_type")
        return {
            "claim_id": "CLM-2026-004471",
            "policy_number": policy_number,
            "status": "OPEN",
            "loss_type": loss_type,
            "next_steps": [
                "Upload photos of the damage.",
                "Provide a police report if applicable.",
                "An adjuster will be assigned within 2 business days.",
            ],
        }

    if name == "get_policy_coverage":
        policy_number = event.get("policy_number")
        return {
            "policy_number": policy_number,
            "coverages": [
                {"name": "Collision", "limit": 50000, "deductible": 500},
                {"name": "Comprehensive", "limit": 50000, "deductible": 300},
                {"name": "Liability", "limit": 1000000, "deductible": 0},
            ],
            "effective_from": "2026-01-01",
            "effective_to": "2026-12-31",
        }

    if name == "upload_loss_document":
        claim_id = event.get("claim_id")
        document_type = event.get("document_type")
        return {
            "document_id": "DOC-778120",
            "claim_id": claim_id,
            "document_type": document_type,
            "accepted": True,
            "received_at": "2026-06-05T08:45:00Z",
        }

    if name == "get_claim_status":
        claim_id = event.get("claim_id")
        return {
            "claim_id": claim_id,
            "status": "UNDER_REVIEW",
            "adjuster": "adjuster-031",
            "last_update": "2026-06-05T07:10:00Z",
            "estimated_settlement": "2026-06-18",
        }

    if name == "find_repair_shops":
        postal_code = event.get("postal_code")
        return {
            "postal_code": postal_code,
            "shops": [
                {"name": "AutoFix München", "distance_km": 2.3, "rating": 4.7, "approved": True},
                {"name": "CarCare Center", "distance_km": 5.1, "rating": 4.4, "approved": True},
                {"name": "Schnell Repair", "distance_km": 8.8, "rating": 4.2, "approved": True},
            ],
        }

    if name == "estimate_payout":
        claim_id = event.get("claim_id")
        return {
            "claim_id": claim_id,
            "estimated_amount": 4200.0,
            "currency": "EUR",
            "deductible_applied": 500.0,
            "confidence": 0.82,
        }

    raise ValueError(
        "unknown tool: %r (expected one of: start_claim, get_policy_coverage, "
        "upload_loss_document, get_claim_status, find_repair_shops, "
        "estimate_payout)" % name
    )
'''


# ===========================================================================
# 3) Insurance Support — agp-insurance-support-mcp  (design §6.3)
# ===========================================================================
_INSURANCE_SUPPORT_TOOL_SCHEMA = [
    {
        "name": "list_policies",
        "description": "List a customer's policies.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "description": "The customer identifier."},
            },
            "required": ["customer_id"],
        },
    },
    {
        "name": "get_policy_details",
        "description": "Fetch the details of a single policy.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "policy_number": {"type": "string", "description": "The policy number."},
            },
            "required": ["policy_number"],
        },
    },
    {
        "name": "get_premium_quote",
        "description": "Quote a premium for a product/coverage.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "product": {"type": "string", "description": "Insurance product (e.g. auto, home)."},
                "coverage_level": {
                    "type": "string",
                    "description": "Coverage level (e.g. basic, standard, premium).",
                },
                "customer_age": {"type": "integer", "description": "The customer's age."},
            },
            "required": ["product", "coverage_level", "customer_age"],
        },
    },
    {
        "name": "update_contact_info",
        "description": "Update the contact info on a policy.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "policy_number": {"type": "string", "description": "The policy number."},
                "email": {"type": "string", "description": "New email address."},
                "phone": {"type": "string", "description": "New phone number."},
                "address": {"type": "string", "description": "New postal address."},
            },
            "required": ["policy_number"],
        },
    },
    {
        "name": "initiate_policy_change",
        "description": "Open a policy-change request.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "policy_number": {"type": "string", "description": "The policy number."},
                "change_type": {
                    "type": "string",
                    "description": "Type of change (e.g. add_driver, change_address).",
                },
                "details": {"type": "string", "description": "Free-text change details."},
            },
            "required": ["policy_number", "change_type", "details"],
        },
    },
    {
        "name": "get_billing_history",
        "description": "List the billing/payment history for a policy.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "policy_number": {"type": "string", "description": "The policy number."},
            },
            "required": ["policy_number"],
        },
    },
]

_INSURANCE_SUPPORT_HANDLER = '''

def handler(event, context):
    """Branch on the tool name; return a DETERMINISTIC mock for each support tool."""
    event = event or {}
    name = _tool_name(context)

    if name == "list_policies":
        customer_id = event.get("customer_id")
        return {
            "customer_id": customer_id,
            "policies": [
                {
                    "policy_number": "POL-AUTO-99231",
                    "product": "auto",
                    "status": "ACTIVE",
                    "renewal_date": "2026-12-31",
                },
                {
                    "policy_number": "POL-HOME-55180",
                    "product": "home",
                    "status": "ACTIVE",
                    "renewal_date": "2026-09-30",
                },
            ],
        }

    if name == "get_policy_details":
        policy_number = event.get("policy_number")
        return {
            "policy_number": policy_number,
            "product": "auto",
            "status": "ACTIVE",
            "premium": 780.0,
            "renewal_date": "2026-12-31",
            "insured_items": ["VW Golf (M-AB 1234)"],
        }

    if name == "get_premium_quote":
        product = event.get("product")
        coverage_level = event.get("coverage_level")
        return {
            "quote_id": "QTE-300194",
            "product": product,
            "coverage_level": coverage_level,
            "premium": 642.5,
            "currency": "EUR",
            "valid_until": "2026-07-05",
        }

    if name == "update_contact_info":
        policy_number = event.get("policy_number")
        updated = [
            field for field in ("email", "phone", "address") if event.get(field) is not None
        ]
        return {
            "policy_number": policy_number,
            "updated": updated,
            "confirmation_id": "CNF-880421",
            "effective_at": "2026-06-05T09:00:00Z",
        }

    if name == "initiate_policy_change":
        policy_number = event.get("policy_number")
        change_type = event.get("change_type")
        return {
            "change_request_id": "CHG-220517",
            "policy_number": policy_number,
            "change_type": change_type,
            "status": "PENDING",
            "effective_date": "2026-07-01",
        }

    if name == "get_billing_history":
        policy_number = event.get("policy_number")
        return {
            "policy_number": policy_number,
            "payments": [
                {
                    "date": "2026-05-01",
                    "amount": 65.0,
                    "currency": "EUR",
                    "method": "SEPA direct debit",
                    "status": "PAID",
                },
                {
                    "date": "2026-04-01",
                    "amount": 65.0,
                    "currency": "EUR",
                    "method": "SEPA direct debit",
                    "status": "PAID",
                },
            ],
        }

    raise ValueError(
        "unknown tool: %r (expected one of: list_policies, get_policy_details, "
        "get_premium_quote, update_contact_info, initiate_policy_change, "
        "get_billing_history)" % name
    )
'''


# ===========================================================================
# The registry — one entry per demo domain. handler_src = preamble + per-domain handler.
# ===========================================================================
DEMO_USE_CASES = [
    {
        "key": "contact-center",
        "prefix": "agp-contact-center",
        "display_name": "Contact Center",
        "description": "Agentic Governance Platform Contact Center MCP tools (dev helper).",
        "tool_schema": _CONTACT_CENTER_TOOL_SCHEMA,
        "handler_src": _HANDLER_PREAMBLE + _CONTACT_CENTER_HANDLER,
    },
    {
        "key": "fnol",
        "prefix": "agp-fnol",
        "display_name": "First Notification of Loss",
        "description": "Agentic Governance Platform First Notification of Loss (FNOL) MCP tools (dev helper).",
        "tool_schema": _FNOL_TOOL_SCHEMA,
        "handler_src": _HANDLER_PREAMBLE + _FNOL_HANDLER,
    },
    {
        "key": "insurance-support",
        "prefix": "agp-insurance-support",
        "display_name": "Insurance Support",
        "description": "Agentic Governance Platform Insurance Support MCP tools (dev helper).",
        "tool_schema": _INSURANCE_SUPPORT_TOOL_SCHEMA,
        "handler_src": _HANDLER_PREAMBLE + _INSURANCE_SUPPORT_HANDLER,
    },
]
