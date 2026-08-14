"""The PLATFORM half of provisioning for a Databricks-hosted agent (E29/T6).

``agent_identity_service.provision_identity`` is platform-NEUTRAL — a per-agent Entra app +
SP + the two appRoles, which a Databricks agent needs identically to an AgentCore one. This
module is the half that follows it, and it is the whole of the difference between the two
platforms: AgentCore gets an inbound JWT authorizer on its runtime; Databricks gets an entry on
the account's Entra federation policy.

FEDERATION IS THE ONLY BINDING AGP OFFERS (design §3B, E29/T14a). In **federation** mode the
agent and Unity Catalog see the REAL caller, which is what makes per-caller tools and honest
audit possible. There is no fallback: a Databricks tenant that cannot federate is badged
``invoke_unavailable`` by the connect-time probe (T3) and this module REFUSES it
(``federation_unavailable``), naming the two grants federation needs — an account-admin
credential on the tenant, and this Entra tenant's identities synced into the Databricks account.
A federation tenant whose user sync is missing likewise fails loudly with
``user_sync_missing``. Refusing is the honest answer; provisioning a weaker binding would hand
an operator a working agent whose audit story is the opposite of the one their tenant page
promises.

**sp_secret IS DORMANT, NOT DELETED.** In that mode every call is attributed to a per-agent
Databricks service principal, so the Databricks audit trail dies at AGP's boundary. Nothing
selects it any more — the probe cannot produce it and the caller never could (OB-2) — and the
leg below is reachable only for a record that deliberately carries the mode AND only when
``settings.DATABRICKS_ALLOW_SP_SECRET_BINDING`` is on. With the flag off (the default) it is
refused as ``sp_secret_disabled``. TEARDOWN IS NEVER GATED: deleting SP artifacts that already
exist is always allowed, because leaving live trust state behind is the harm the gate exists to
avoid, not a state it should protect.

UNTRUSTED INPUT (ledger OB-2). ``binding_mode``, ``databricks_sp_id`` and
``databricks_sp_secret_arn`` are fields on ``AgentBase``, so they arrive CLIENT-SETTABLE from
registration. All three are overwritten here from the tenant/provisioning truth on every
run, and the two SP fields are FORCED EMPTY in federation mode — a client-planted
``databricks_sp_secret_arn`` that survived would be a Secrets Manager ARN of the caller's
choosing sitting on the invoke path.

SECRETS (Global Constraints). The tenant's workspace SP secret is read from Secrets Manager
by the ARN the TENANT RECORD stores, at CALL TIME, into a local. It is never written to the
agent record, never logged, and never interpolated into an error — pinned by a
sentinel-driven test over the record, the log capture, and the exception text. The same holds
for the per-agent SP secret this module MINTS in sp_secret mode: it goes create → store →
persist-ARN, and the value exists only as a local between the mint and the store.

ACCOUNT-LEVEL CALLS PREFER THE ACCOUNT-ADMIN CREDENTIAL. Writing a federation policy is an
account-admin act, and T3 stores that credential per TENANT (``Tenant.account_admin_secret_arn``,
both halves inside the secret body). It is preferred over the workspace SP for every
account-level call, falling back to the workspace SP only when no account-admin credential was
supplied — federation is an extra grant a customer may not have made, and the fallback is what
turns that into one actionable error instead of an AttributeError.

ERRORS (ledger OB-6). A ``DatabricksError`` whose ``.kind`` is
``federation_policy_missing`` / ``_unreadable`` / ``_ambiguous`` is not a server fault: it is
a statement about the customer's Databricks account that somebody can act on. Those kinds get
a composed, actionable sentence naming the issuer and the role that must act. The upstream
message is NEVER forwarded (C-2 composes safe messages, but this layer does not depend on
that — it uses ``.kind`` only).

IDEMPOTENT + RESUMABLE, like every other provisioning path in the platform: the audience
write is a contractual no-op when already present, the app-ACL assert PUTs a list computed
from the tenant rather than a delta, and the SP create is skipped when the record already names
one. A re-provision after a mid-sequence failure re-runs safely (pinned by test).

Mechanics: contracts C-1 (tenant), C-2 (``databricks_workspace_service``), C-4 (agent
fields); design §3. Three live-proven traps shape this module: the federation policy's
issuer/subject/audience triple must match the token exactly; Databricks AIM is lazy/JIT, so
resolve-then-grant; and federation-policy writes need Databricks ACCOUNT admin — a
separate, optional grant.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from botocore.exceptions import BotoCoreError, ClientError

from core.config import settings
from models.agent import (
    UNKNOWN_STAGE,
    Agent,
    IdentityStatus,
    RuntimeStatus,
    is_databricks_governed_agent,
)
from models.tenant import (
    ACCOUNT_ADMIN_ID_KEY,
    ACCOUNT_ADMIN_SECRET_KEY,
    SP_SECRET_KEY,
)
from services.agent_identity_service import ProvisioningError
from services.databricks_workspace_service import DatabricksError
from services.tenant_service import (
    BINDING_FEDERATION,
    BINDING_INVOKE_UNAVAILABLE,
    BINDING_SP_SECRET,
)

logger = logging.getLogger(__name__)

_STORE_FAULTS = (ClientError, BotoCoreError)

# The binding modes this module dispatches on — IMPORTED, not restated (E29/T14a). The
# vocabulary is declared once, beside its only writer (``tenant_service._probe``); three copies
# of the literals was how "federation | sp_secret" could mean two different things in two
# modules. A tenant carrying anything else (including the AWS "") is refused rather than
# defaulted: defaulting would pick a mode, and picking the wrong one changes what the audit log
# can prove.
_MODE_FEDERATION = BINDING_FEDERATION
_MODE_SP_SECRET = BINDING_SP_SECRET
_MODE_INVOKE_UNAVAILABLE = BINDING_INVOKE_UNAVAILABLE

# Only a Databricks App exposes the permissions surface the asserted ACL needs. A serving
# endpoint is a different ACL family (research §4), so it is refused loudly — granting
# nothing while reporting 'provisioned' would ship an agent nobody can call.
_KIND_APP = "app"

# Design §3A — the ACL provisioning ASSERTS on an AGP-governed app.
#
# `admins` at CAN_MANAGE is non-negotiable: the workspace admins stay above the governance
# layer (true on AgentCore too), and the client REFUSES a PUT without it (`acl_missing_admins`)
# because dropping it locks them out of their own app irrecoverably. The literals are repeated
# here rather than imported from the workspace service's privates — if they ever drift, that
# guard turns it into a loud failure, not a silent lockout.
_CAN_USE = "CAN_USE"
_CAN_MANAGE = "CAN_MANAGE"
_ADMINS_ENTRY = {"principal": "admins", "kind": "group", "level": _CAN_MANAGE}
_KIND_SERVICE_PRINCIPAL = "service_principal"

# The JSON keys of a PER-AGENT service-principal secret body (sp_secret mode).
#
# WHY THE SCIM ID LIVES IN THE SECRET BODY. Minting a secret needs the SP's SCIM ``id``, while
# the agent record carries its ``application_id`` (``databricks_sp_id`` — the id the invoke path
# and the app's ACL use). The two are different identifiers for one principal, and there is
# no envelope field for the SCIM one. Adding a seventh C-4 field for a value only this module
# reads would widen a client-settable model (OB-2's whole problem) for no caller's benefit, so
# it rides in the secret body: written when the secret is created, read on a re-provision. The
# body is the right home — it is the one place already scoped to "credential material for this
# agent's SP", it is never returned by an API, and it disappears when the secret does.
_AGENT_SECRET_KEY = "client_secret"
_AGENT_SCIM_ID_KEY = "scim_id"
# The owning agent's id, recorded in the body as well as in the secret's ``agent_id`` TAG. The
# body copy is what the paths that already read the body verify, so the authoritative ownership
# check costs no extra API call (see :meth:`_owns_secret_arn` for why the tag is not read).
_AGENT_OWNER_KEY = "agent_id"

# Secrets Manager appends "-" + six random alphanumerics to a secret's name in its ARN. Matched
# at END-OF-STRING only, and as an EXACT six, so stripping it cannot swallow part of an agent id
# that legitimately contains "-" (agent ids are AWS-generated recordIds — their shape is not
# ours to assume, so nothing here may treat "-" as a field separator).
_SM_RANDOM_SUFFIX_RE = re.compile(r"-[A-Za-z0-9]{6}$")

# A Secrets Manager SECRET arn, with the name captured. ``fullmatch`` semantics via ``\Z`` (not
# ``$``, which also matches before a trailing newline — the ``models/tenant`` lesson). No account
# id is pinned here: accounts come from ambient credentials, never from a literal.
_SM_SECRET_ARN_RE = re.compile(
    r"^arn:[a-z0-9-]+:secretsmanager:[a-z0-9-]*:\d*:secret:(?P<name>[^\s]+)\Z"
)

# Account-console hosts by cloud (research §5.1 — NOT derivable from the workspace URL).
# The workspace client's own default covers AWS; the other two are named here because a
# tenant records its cloud and an account-level call to the wrong console just 404s.
_ACCOUNT_HOSTS = {
    "aws": "https://accounts.cloud.databricks.com",
    "azure": "https://accounts.azuredatabricks.net",
    "gcp": "https://accounts.gcp.databricks.com",
}

# The Entra issuer form the account federation policy must carry (research §3.3/§4.1). Named
# in the actionable error so the admin being asked knows WHICH trust statement is meant.
_ENTRA_ISSUER_FORM = "https://login.microsoftonline.com/<entra-tenant-id>/v2.0"

# The DatabricksError kinds that describe the CUSTOMER's account, not a fault in AGP. Each
# maps to a sentence naming who must act — OB-6: never a bare 500.
_ACTIONABLE_KINDS = {
    "federation_policy_missing": (
        "the Databricks account has no Entra OIDC federation policy, so this agent's "
        "audience cannot be trusted — ask your Databricks account admin to create the "
        f"OIDC federation policy for {_ENTRA_ISSUER_FORM}"
    ),
    "federation_policy_ambiguous": (
        "the Databricks account carries more than one Entra OIDC federation policy, so AGP "
        "cannot tell which one governs this agent — ask your Databricks account admin to "
        f"leave exactly one policy for {_ENTRA_ISSUER_FORM}"
    ),
    "federation_policy_unreadable": (
        "the Databricks account's Entra OIDC federation policy could not be read in a shape "
        "AGP recognises, so it refused to write to it rather than risk replacing the "
        "account's trust entries — ask your Databricks account admin to review the policy "
        f"for {_ENTRA_ISSUER_FORM}"
    ),
    "unauthorized": (
        "Databricks refused AGP's credential — ask your Databricks account admin to confirm "
        "the account-admin service principal AGP was given is still active"
    ),
    "forbidden": (
        "Databricks accepted AGP's credential but refused the operation — ask your "
        "Databricks account admin to confirm it holds account-admin (federation policies) "
        "and CAN_MANAGE on the app"
    ),
}


def _actionable(err: DatabricksError, what: str) -> str:
    """A safe, actionable sentence for a Databricks failure — ``.kind`` only, never the body.

    The upstream ``.message`` is not forwarded even though C-2 composes it safely: this layer
    must be correct on its own, because an error string is the one place a workspace path or
    an echoed request form would become a UI element. The kind is appended verbatim (it is
    already constrained to ``^[A-Za-z_]{1,64}$`` upstream) so support can grep for it.
    """
    hint = _ACTIONABLE_KINDS.get(err.kind)
    if hint:
        return f"{hint} [{err.kind}]"
    return f"Databricks could not {what} [{err.kind}]"


# ---------------------------------------------------------------------------
# Runtime status (E29/T10) — the Databricks half of the E28/T5 closed union
# ---------------------------------------------------------------------------

# Databricks App ``status.state`` → the CLOSED six-value union
# (``models.agent.RUNTIME_STATUSES``). The exact structural mirror of
# ``agent_identity_service._NATIVE_TO_RUNTIME_STATUS``: an explicit dict keyed on the upstream
# strings, so a state Databricks adds later degrades visibly instead of being absorbed by a
# clever heuristic.
#
# NO SEVENTH VALUE. The frontend mirrors the union verbatim in ``Record<RuntimeStatusKey, …>``
# tables with NO default branch (contract C3), so adding one here is a design change that
# breaks the compiler on the other side — not a free addition. Every state Databricks has that
# the union cannot name is therefore mapped onto an existing member, and the RAW state is
# carried in ``detail`` so nothing is actually lost.
#
# WHY THE NON-SERVING STATES ARE ``unknown`` AND NOT ``failed``. ``STOPPED``/``STOPPING`` are
# somebody switching an app OFF; ``DELETING`` is a deliberate teardown; ``UNAVAILABLE`` says
# the platform cannot currently serve it and does not say why. None of those is a FAULT, and
# the AgentCore side already made this call for ``DELETING`` for the same reason: a governance
# product that renders "switched off on purpose" as "failed" pages an on-call for a healthy
# fleet. Only the two unambiguous fault states map to ``failed``.
#
# THE VALUES ARE NOT PINNED BY DOCUMENTATION. Research §2.1 confirms the app record carries
# ``status.{message,state}`` but does not enumerate the states, and marks the record's field
# names UNVERIFIED — T12's first live action is ``databricks apps get <app> --output json``.
# That is exactly why the unmapped branch is a first-class behaviour here rather than an
# afterthought: this dict is expected to gain entries after the live test, and until it does,
# an unrecognised state must read as "we do not know", never as a verdict.
_APP_STATE_TO_RUNTIME_STATUS = {
    "RUNNING": "ready",
    "ACTIVE": "ready",
    "DEPLOYING": "creating",
    "STARTING": "creating",
    "UPDATING": "updating",
    "CRASHED": "failed",
    "ERROR": "failed",
    "STOPPED": "unknown",
    "STOPPING": "unknown",
    "DELETING": "unknown",
    "UNAVAILABLE": "unknown",
}

# The containers an app record may carry its state in. Research §2.1's sample shows ``status``;
# the field names are UNVERIFIED, so both plausible names are read — the same two-key tolerance
# ``runtime_catalog._app_handle`` already applies to ``url``/``app_url``. Reading only one would
# report a live agent as "unknown" over a field-name difference.
_APP_STATUS_KEYS = ("status", "app_status")

# What may be ECHOED into a status ``detail``. The AgentCore producer's ``_SAFE_ERROR_CODE``
# rule, restated here so this module is correct on its own: a value is only rendered when it
# LOOKS like a code, because ``detail`` reaches a UI and an upstream could otherwise smuggle a
# workspace path, a token, or an account id through the field we chose to quote.
_SAFE_STATUS_TOKEN = re.compile(r"^[A-Za-z_]{1,64}$")


def _safe_token(value: object) -> str:
    """``value`` if it is a bare code-shaped string, else ``""`` (never a partial echo)."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    return candidate if _SAFE_STATUS_TOKEN.match(candidate.replace("_", "")) else ""


def _app_state(app: dict) -> str:
    """The raw ``state`` string from whichever status container the record carries."""
    for key in _APP_STATUS_KEYS:
        container = app.get(key)
        if isinstance(container, dict):
            state = container.get("state")
            if isinstance(state, str) and state.strip():
                return state.strip()
    return ""


class DatabricksIdentityService:
    """Provision / deprovision the Databricks half of an agent's identity binding.

    Collaborators are INJECTED, and credentials are never sourced by the collaborator that
    uses them — the C-2 rule that keeps an adapter from widening its own authority. This
    service is the one place that turns a stored Secrets Manager ARN into a live credential,
    and it hands the value straight to a token mint.

    ``tenants`` is optional and only used to resolve a tenant from ``agent.tenant_id`` when
    the caller does not already hold one (the background-task path).
    """

    def __init__(
        self,
        *,
        databricks,
        registry,
        tenants=None,
        secrets_client=None,
        # Test convenience only — production wiring always passes
        # f"{settings.DATABRICKS_TENANT_SECRET_PREFIX}agents/" (routes/agents.py).
        secret_prefix: str = "agp-dev/databricks-tenants/agents/",
        region: str = "us-east-1",
    ) -> None:
        self._databricks = databricks
        self._registry = registry
        self._tenants = tenants
        self._secret_prefix = secret_prefix
        self._region = region
        if secrets_client is not None:
            self._sm = secrets_client
        else:  # pragma: no cover — the live path; unit tests always inject.
            import boto3

            self._sm = boto3.client("secretsmanager", region_name=region)

    # ------------------------------------------------------------------ #
    # Public: provision
    # ------------------------------------------------------------------ #
    async def provision_databricks_runtime(
        self, agent: Agent, tenant=None
    ) -> Agent:
        """The platform half for a Databricks agent. Returns the agent, or raises.

        Owns its OWN persist-'failed' envelope rather than relying on
        :meth:`AgentIdentityService.provision`'s: this is also called directly (a re-provision
        of the platform half alone), and an agent left 'pending' after a failure is invisible
        to the recovery affordance that reads 'failed'. Persisting twice is harmless — the
        write is idempotent — while persisting zero times is a stranded record.
        """
        try:
            return await self._provision(agent, tenant)
        except ProvisioningError:
            self._persist_failed(agent)
            raise
        except DatabricksError as err:
            self._persist_failed(agent)
            logger.warning(
                "[databricks_identity] provisioning failed for agent %s (%s)",
                agent.id,
                err.kind,
            )
            raise ProvisioningError(
                f"provisioning failed for agent {agent.id}: "
                f"{_actionable(err, 'complete provisioning')}"
            ) from err
        except Exception as err:  # noqa: BLE001 — any failure → failed status + re-raise
            self._persist_failed(agent)
            logger.warning(
                "[databricks_identity] provisioning failed for agent %s: %s",
                agent.id,
                type(err).__name__,
            )
            raise ProvisioningError(
                f"provisioning failed for agent {agent.id}"
            ) from err

    async def _provision(self, agent: Agent, tenant) -> Agent:
        if not is_databricks_governed_agent(agent):
            raise ProvisioningError(
                f"agent {agent.id} is not a Databricks-governed agent"
            )
        if agent.runtime_kind and agent.runtime_kind != _KIND_APP:
            # Refused, not skipped: a serving_endpoint has no app-permissions surface, so
            # "provisioned" would mean "nobody was granted anything".
            raise ProvisioningError(
                f"agent {agent.id} names a {agent.runtime_kind!r} runtime; only "
                f"{_KIND_APP!r} runtimes can be identity-bound on Databricks today"
            )

        tenant = self._resolve_tenant(agent, tenant)
        mode = (getattr(tenant, "binding_mode", "") or "").strip()

        # OB-2: the tenant is the ONLY source of the mode, written before any branch so a
        # failure leaves the record carrying the truth rather than the caller's claim.
        agent.binding_mode = mode

        # OB-2: a client-planted OAuth client id is cleared on BOTH paths. Only the federation
        # leg reads it back from a workspace listing, so on the sp_secret leg a caller's value
        # would otherwise survive untouched onto a provisioned record.
        agent.oauth2_app_client_id = None

        # OB-2, hoisted ABOVE the dispatch (E29/T14a fix). Every refusal below raises
        # ``ProvisioningError``, which ``provision_databricks_runtime`` turns into
        # ``_persist_failed(agent)`` — an envelope write of the WHOLE record. Scrubbing only
        # inside the sp_secret leg therefore let a client-planted ``databricks_sp_id`` /
        # ``databricks_sp_secret_arn`` survive onto a refused record (and into the
        # "service principal %s survives teardown" residue log, which T12's orphan list reads).
        # Harmless for the federation leg (it clears both fields anyway) and it preserves the
        # sp_secret leg's resumability, because an agent's OWN ARN passes the ownership check.
        self._drop_untrusted_sp_pointers(agent)

        if mode == _MODE_FEDERATION:
            # OB-2: no per-agent SP exists on this path, so a client-planted id/ARN is
            # cleared BEFORE anything else can read it.
            agent.databricks_sp_id = None
            agent.databricks_sp_secret_arn = None
            await self._provision_federation(agent, tenant)
        elif mode == _MODE_INVOKE_UNAVAILABLE:
            # E29/T14a (design §3B). The tenant's probe could not badge it ``federation``, and
            # there is no longer a weaker mode to fall to — so provisioning refuses, naming the
            # TWO grants federation needs. The actionable part matters: the fix is on the
            # customer's Databricks account, not on the agent, and a bare "no usable binding
            # mode" sends the operator to the wrong record. Same idiom as
            # ``federation_policy_missing`` / ``user_sync_missing``.
            raise ProvisioningError(
                f"agent {agent.id} cannot be bound: its tenant "
                f"({getattr(tenant, 'id', '?')!r}) is not able to invoke on Databricks "
                f"(federation_unavailable). Federation needs BOTH an account-admin credential "
                f"on the tenant and this Entra tenant's identities synced into the Databricks "
                f"account (user sync) — add them and re-connect the tenant so its capabilities "
                f"are re-probed. AGP will not fall back to service-principal binding, because "
                f"that would silently change what the audit log can prove"
            )
        elif mode == _MODE_SP_SECRET and not settings.DATABRICKS_ALLOW_SP_SECRET_BINDING:
            # DORMANT, and refused LOUDLY rather than skipped or downgraded (design §3B). A
            # record deliberately carrying this mode on a default deployment is a decision
            # somebody has to make explicitly, so the refusal names the flag that would make it.
            raise ProvisioningError(
                f"agent {agent.id} carries the dormant per-agent service-principal Databricks "
                f"binding (sp_secret_disabled), which this deployment does not allow: every "
                f"call would be attributed to a service principal instead of the caller. Set "
                f"DATABRICKS_ALLOW_SP_SECRET_BINDING=true to enable it deliberately, or give "
                f"the tenant an account-admin credential + user sync and use federation"
            )
        elif mode == _MODE_SP_SECRET:
            # OB-2, and the reason the scrub above is unconditional rather than per-branch.
            # The sp_secret leg uses these two fields AS ITS RESUMABILITY STATE, and they arrive
            # client-settable — so a caller could hand agent B agent A's pointers and have B
            # provision onto A's service principal, then have B's teardown DELETE A's secret.
            # Pointing the ARN at the tenant's WORKSPACE-SP secret was worse: provisioning
            # refused, but teardown still deleted the tenant's own credential.
            #
            # An ARN is therefore only trusted when it is one THIS service would have written for
            # THIS agent — the same trusted-derivation rule the read path already documents. An
            # ARN that fails the check is DROPPED (with both fields), so the agent provisions its
            # own principal and its teardown can only ever reach its own resources. (The call
            # itself now lives above the dispatch, so the refusal legs are covered too.)
            await self._provision_sp_secret(agent, tenant)
        else:
            raise ProvisioningError(
                f"tenant {getattr(tenant, 'id', '?')!r} has no usable Databricks binding "
                f"mode ({mode!r}); connect the tenant so its capabilities are probed"
            )

        agent.identity_status = IdentityStatus.PROVISIONED
        self._registry.persist_identity(agent)
        return agent

    # ------------------------------------------------------------------ #
    # Federation mode
    # ------------------------------------------------------------------ #
    async def _provision_federation(self, agent: Agent, tenant) -> None:
        """Design §3 + §3A: user sync verified → audience on the account policy → ACL asserted.

        Step order is load-bearing. The user-sync check is a PRECONDITION and runs before any
        Databricks call, because the honest failure for "your Entra users are not in this
        workspace" is "nothing was changed", not "some things were".
        """
        # (1) User sync — the AIM/SCIM prerequisite (design §3, research §4).
        #
        # Read from the tenant's connect-time capability probe. ABSENT is treated exactly like
        # False: a tenant that was never probed has produced no evidence of sync, and
        # fail-closed is the rule every capability flag in this epic follows. There is
        # deliberately no live re-probe here — that needs the account-admin credential, which
        # is an OPTIONAL separate grant (research §7 B9), so a re-probe would turn a missing
        # optional grant into a different failure than the one the operator can act on.
        capabilities = getattr(tenant, "capabilities", None) or {}
        if not capabilities.get("user_sync"):
            raise ProvisioningError(
                f"agent {agent.id} cannot be bound in federation mode: the Databricks "
                f"account does not have this Entra tenant's identities synced "
                f"(user_sync_missing). Ask your Databricks account admin to enable "
                f"automatic identity management for "
                f"{_ENTRA_ISSUER_FORM}. AGP will not fall back to service-principal "
                f"binding, because that would silently change what the audit log can prove"
            )

        if not agent.entra_app_id:
            raise ProvisioningError(
                f"agent {agent.id} has no Entra app client id, so there is nothing to add to "
                f"the Databricks federation policy — provision the Entra identity first"
            )

        stage, workspace_token, app = await self._resolve_stage_and_app(agent, tenant)

        account_id = (getattr(stage, "account_id", "") or "").strip()
        if not account_id:
            raise ProvisioningError(
                f"tenant {getattr(tenant, 'id', '?')!r} has no Databricks account id on the "
                f"workspace hosting agent {agent.id}, and federation mode writes an "
                f"account-level policy — add the account id to the tenant's stage"
            )

        # (2) The agent's OWN audience on the account-wide policy. Idempotent in both
        # directions by C-2 contract, so a re-provision cannot append a duplicate.
        # THE CLIENT-ID GUID, not the api:// URI (E29 livefix-6, proven live 2026-08-12):
        # the per-agent apps request v2 access tokens, whose `aud` is the client id —
        # the URI form made every exchange fail invalid_grant. `entra_app_audience`
        # (the URI) remains AgentCore's allowedAudience contract, untouched here.
        account_token = await self._account_token(tenant, stage, account_id)
        try:
            await self._databricks.ensure_federation_audience(
                self._account_host(stage),
                account_id,
                account_token,
                agent.entra_app_id,
                present=True,
            )
        except DatabricksError as err:
            raise ProvisioningError(
                f"agent {agent.id} could not be bound in federation mode: "
                f"{_actionable(err, 'update the federation policy')}"
            ) from err

        # (3) ASSERT the app's access list (design §3A). The tenant's ``entra_group_ids`` are
        # deliberately NOT read: a group-wide CAN_USE opened the platform's own door to every
        # member, so an AGP revoke could not close it. Per-user entries arrive from grants.
        app_name = str(app.get("name") or "")
        await self._assert_app_acl(agent, stage, workspace_token, app_name)

        # (4) The app's OAuth client id, when READABLE. The field name is UNVERIFIED
        # (research §2.1), so a missing value is a gap in a convenience field, never a reason
        # to leave a correctly-bound agent unprovisioned.
        client_id = app.get("oauth2_app_client_id")
        if isinstance(client_id, str) and client_id:
            agent.oauth2_app_client_id = client_id
        else:
            # CLEARED, not left alone (OB-2). This field is on ``AgentBase``, so it arrives
            # client-settable; leaving a caller's value in place when the workspace does not
            # confirm it would let a provisioned record assert a Databricks OAuth client id that
            # AGP never read. Absent evidence, the honest value is empty.
            agent.oauth2_app_client_id = None
            logger.info(
                "[databricks_identity] app record for agent %s carries no readable "
                "oauth2 client id",
                agent.id,
            )

    # ------------------------------------------------------------------ #
    # sp_secret mode
    # ------------------------------------------------------------------ #
    async def _provision_sp_secret(self, agent: Agent, tenant) -> None:
        """Per-agent Databricks SP + its OAuth secret, then the asserted ACL (design §3, B + §3A).

        The documented cost of this mode is in the design and the UI: every call is attributed to
        this service principal, so the Databricks audit trail dies at AGP's boundary. That is why
        the mode is never CHOSEN here — only executed when the tenant's probe already concluded it.

        ORDER IS THE SECURITY PROPERTY. create SP → persist its application id → mint secret →
        store the secret → persist the ARN → assert the ACL. Every write happens before the step
        that could fail after it, so no step can leave a live resource the record does not name:

          - the SP id is persisted BEFORE the mint, so a failed mint cannot make the next
            re-provision create a SECOND service principal (the CRITIQUE-FIX-A idiom);
          - the secret is stored in Secrets Manager BEFORE the ARN is persisted, and a failed
            persist deletes it best-effort (the ``connection_service`` create→persist→rollback
            idiom), so a live credential is never orphaned with no record pointing at it;
          - the ACL assert comes LAST because it is the only step that is safely repeatable
            from scratch, and because it needs the SP's application id to put on the list.

        The minted secret exists as a local between the mint and the store. It is never returned,
        never persisted on the agent, and never logged — pinned by a sentinel test.
        """
        stage, workspace_token, app = await self._resolve_stage_and_app(agent, tenant)

        # (1) The SP, then its SCIM id — recorded BEFORE any credential exists.
        #
        # THE ORDERING BUG THIS FIXES (found by the resumability test). Minting needs the SCIM
        # id, and the SCIM id is only ever returned by the create call. If it were written only
        # ALONGSIDE the minted secret, a failure BETWEEN create and mint would lose it forever:
        # the record would name a live service principal that could never be credentialed, and
        # the only recovery would be deleting the binding. So the secret entry is created as soon
        # as the SP is — carrying the SCIM id and NO credential — and the mint fills it in. A
        # secret body with no ``client_secret`` is the honest representation of "this principal
        # exists and has no credential yet", and it is what makes the retry work.
        scim_id = self._stored_scim_id(agent)
        if not agent.databricks_sp_id:
            created = await self._databricks.create_service_principal(
                stage.workspace_url, workspace_token, f"agp-agent-{agent.id}"
            )
            application_id = str(created.get("application_id") or "")
            scim_id = str(created.get("id") or "")
            if not application_id or not scim_id:
                raise ProvisioningError(
                    f"Databricks returned an incomplete service principal for agent "
                    f"{agent.id}, so it can be neither credentialed nor granted access"
                )
            agent.databricks_sp_id = application_id
            # Store the SCIM id, then persist BOTH pointers before the mint can fail.
            created_arn = self._store_agent_secret(agent, secret="", scim_id=scim_id)
            agent.databricks_sp_secret_arn = created_arn
            try:
                self._registry.persist_identity(agent)
            except Exception:
                # This entry is genuinely NEW, so a failed persist orphans it — remove it
                # best-effort (the ``connection_service`` create→persist→rollback idiom). Safe to
                # delete precisely because it holds no credential yet, only the SCIM id, and the
                # service principal it points at is re-derivable by creating a fresh one.
                agent.databricks_sp_secret_arn = None
                try:
                    self._delete_secret_best_effort(created_arn)
                except Exception:  # noqa: BLE001 — never mask the persist failure
                    logger.exception(
                        "[databricks_identity] orphaned per-agent secret entry for agent %s",
                        agent.id,
                    )
                raise

        # (2) The credential — minted only when the stored body carries none. Keyed on the BODY,
        # not on the ARN: the ARN now exists from step 1, so gating on it would skip the mint
        # forever. A record whose body already holds a secret must not mint a second one, which
        # would leave the first live and unreferenced.
        if not self._stored_agent_secret_present(agent):
            if not scim_id:
                raise ProvisioningError(
                    f"agent {agent.id} names a Databricks service principal whose SCIM id AGP "
                    f"no longer holds, so a credential cannot be minted for it — delete the "
                    f"agent's Databricks binding and re-provision to create a fresh principal"
                )
            minted = await self._databricks.create_service_principal_secret(
                stage.workspace_url, workspace_token, scim_id
            )
            secret_arn = self._store_agent_secret(
                agent, secret=minted["secret"], scim_id=scim_id
            )
            agent.databricks_sp_secret_arn = secret_arn
            # NO delete-on-persist-failure here, deliberately — and this is the opposite of the
            # rollback in step 1. This write UPDATES the entry step 1 already created and already
            # persisted, so the record ALREADY points at this ARN: nothing is orphaned by a failed
            # persist. Deleting it would destroy the stored SCIM id, which is the one value that
            # makes the retry able to reach this service principal at all — a "cleanup" that
            # converts a retryable failure into a permanent one. The failure propagates and the
            # envelope marks the agent 'failed'; the credential is stored, recorded, and reused.
            self._registry.persist_identity(agent)

        # (3) ASSERT the app's access list, WITH this agent's own SP at CAN_USE — keyed on the
        # application id (the ACL's ``service_principal_name`` form; the SCIM id is not an ACL
        # principal). In this mode that SP is the invoke identity, so it is the one entry the
        # asserted baseline gains here: dropping it would leave a "provisioned" agent whose
        # every call 401s at the apps proxy.
        await self._assert_app_acl(
            agent,
            stage,
            workspace_token,
            str(app.get("name") or ""),
            extra_entries=[
                {
                    "principal": agent.databricks_sp_id,
                    "kind": _KIND_SERVICE_PRINCIPAL,
                    "level": _CAN_USE,
                }
            ],
        )

    # ------------------------------------------------------------------ #
    # Public: runtime status (E29/T10)
    # ------------------------------------------------------------------ #
    def runtime_status(
        self, agent: Agent, tenant=None, *, stage: str | None = None
    ) -> RuntimeStatus:
        """One Databricks agent's live app status, in the E28/T5 closed union. NEVER raises.

        The Databricks sibling of ``AgentIdentityService.runtime_status``, dispatched from it by
        platform. Same contract, deliberately: the value is a member of
        ``models.agent.RUNTIME_STATUSES``, ``detail`` is a SAFE hint only, and EVERY failure
        degrades to a status rather than an exception. A read surface that 5xx'd on a rotated
        credential would blank the fleet view — which is the "no honest answer" E28 existed to
        fix, reintroduced on a second platform.

        ``unknown`` IS NEVER ``failed``, and ``unknown`` IS NEVER ``not_deployed``. Those are
        three different claims and this method is careful about which one it makes:

          - ``not_deployed`` — a POSITIVE claim that nothing is running. Made only from
            evidence: the record names no ``runtime_handle`` (nothing was ever bound), the
            asked-for stage does not exist on the tenant, or EVERY workspace listing ANSWERED
            and none of them was the app. A positive claim needs a COMPLETE negative result,
            so a sweep in which any workspace failed to answer is ``unknown``, not this
            (fix round 1, F1: mixed evidence used to report ``not_deployed``).
          - ``unknown`` — nobody was able to look, or the app named a state the union cannot
            express. A denying/unreachable workspace, an unresolvable credential, a missing
            tenant, a ``STOPPED``/``DELETING`` app.
          - ``failed`` — only ``CRASHED``/``ERROR``, the two states that say the app itself is
            broken.

        SYNC, like its AgentCore sibling, because the route dispatches this producer off the
        event loop via ``anyio.to_thread.run_sync``. The workspace client is async, so the
        listing runs on a fresh loop via ``asyncio.run`` — legal precisely BECAUSE we are on a
        threadpool thread with no running loop (the ``project_service._provision_identity``
        idiom). Each call gets its own client, so no loop-bound client is shared across loops
        (``graph_service``'s lesson).

        WHICH WORKSPACE, AND WHY A LISTING. A Databricks app URL carries no workspace identity,
        so ``runtime_handle`` cannot be matched against a stage's ``workspace_url`` — the only
        honest evidence for "which workspace hosts this app" is which workspace ANSWERS with it
        (see :meth:`_resolve_stage_and_app`, which makes the same argument for the write path).
        Stage-less therefore probes the tenant's workspaces in deterministic order and reports
        the stage that listed the app; given an explicit ``stage`` it probes THAT one only,
        because answering "dev" with prod's reading would manufacture per-stage evidence — the
        error the frontend's ``runtimeScope`` treats an attributed stage as proof against.

        TWO KNOWN LIMITS OF THAT SWEEP, both accepted deliberately (fix round 1, F5/F6):

          - AN EMPTY SUCCESSFUL ``apps list`` IS TREATED AS ABSENCE. C-2 documents that ``apps
            list`` visibility follows app permissions, so an empty list can also mean "the
            discovery SP has no grant on this app" — indistinguishable from "the app is gone"
            without a second, differently-credentialed call. Absence is the reading that
            matches what a customer sees in their own workspace UI with the same credential,
            and the alternative (report ``unknown`` whenever a list is empty) would make a
            genuinely deleted app permanently unreportable. The mitigation is the connect-time
            ``can_discover`` capability flag, which is where a missing discovery grant is
            supposed to surface — a per-read guess is not a better place for it.
          - A DUPLICATE APP URL ACROSS WORKSPACES resolves to the FIRST stage in sorted order,
            since the loop returns on its first match. Two workspaces serving the same app URL
            should not be possible (the host is per-app), so this is a tie-break for a shape
            reality does not produce rather than a policy; it is deterministic, which is what
            keeps a reported stage reproducible.

        ``runtime_arn`` and ``image_tag`` stay ``None``. The former is an ARN field that the
        delete cascade and the per-stage map both PARSE as one (``models/agent.py``: two fields,
        two platforms, no overloading); the latter has no Databricks Apps equivalent.
        """
        checked_at = datetime.now(timezone.utc).isoformat()

        def _result(status: str, resolved_stage: str, detail: str | None = None):
            return RuntimeStatus(
                agent_id=agent.id,
                stage=resolved_stage or UNKNOWN_STAGE,
                status=status,
                runtime_arn=None,
                checked_at=checked_at,
                detail=detail,
            )

        reported_stage = stage or UNKNOWN_STAGE

        # The producer is not the dispatcher. An AgentCore agent arriving here means the seam
        # broke; answering from a Databricks listing would report the wrong runtime's status,
        # which is worse than reporting none.
        if not is_databricks_governed_agent(agent):
            if not (agent.runtime_handle or "").strip():
                return _result(
                    "not_deployed", reported_stage, "no runtime has been deployed yet"
                )
            return _result(
                "unknown",
                reported_stage,
                "runtime status could not be read (not a Databricks-governed agent)",
            )

        try:
            tenant = self._resolve_tenant(agent, tenant)
        except Exception:  # noqa: BLE001 — see below; a read may not raise
            # BROAD ON PURPOSE. ``_resolve_tenant`` raises ``ProvisioningError`` for a record
            # with no tenant, but the live ``TenantService.get`` raises its OWN ``TenantError``
            # for a tenant that was deleted — and catching only the former would let the most
            # likely "missing tenant" in production escape a method whose whole contract is
            # that it never does. Importing ``TenantError`` to name it would also make this
            # module depend on the tenant SERVICE (it deliberately depends only on the tenant
            # MODEL's key names), for no gain: every outcome here is the same "unknown".
            tenant = None
        if tenant is None:
            return _result(
                "unknown",
                reported_stage,
                "runtime status could not be read (the agent's tenant is unavailable)",
            )

        stages = getattr(tenant, "stages", None) or {}
        if stage is not None:
            selected = stages.get(stage)
            if selected is None:
                # Answered LOCALLY, and never by falling through to another stage — a stage the
                # tenant does not have has no runtime, and borrowing one would look like an
                # answer to the question that was asked.
                return _result(
                    "not_deployed", stage, "this stage has no Databricks workspace"
                )
            candidates = [(stage, selected)]
        else:
            candidates = sorted(stages.items(), key=lambda kv: kv[0])

        if not candidates:
            return _result(
                "unknown",
                reported_stage,
                "runtime status could not be read (the tenant has no Databricks workspace)",
            )

        handle = (agent.runtime_handle or "").strip()
        listed_stages: list[str] = []
        error_kinds: list[str] = []

        # ATTEMPT-ALL, the multi-target idiom this module already uses: one workspace behind a
        # rotated credential must not turn a healthy agent on another workspace into "unknown".
        for name, workspace_stage in candidates:
            workspace_url = (getattr(workspace_stage, "workspace_url", "") or "").strip()
            if not workspace_url:
                error_kinds.append("no_workspace_url")
                continue
            try:
                apps = asyncio.run(self._list_apps(workspace_stage, workspace_url))
            except DatabricksError as err:
                error_kinds.append(_safe_token(err.kind) or "unreadable")
                continue
            except ProvisioningError:
                # A missing/unreadable stored credential. Actionable for provisioning; for a
                # READ it is simply "we could not look".
                error_kinds.append("credential_unavailable")
                continue
            except Exception:  # noqa: BLE001 — a read surface has no honest way to raise
                logger.warning(
                    "[databricks_identity] runtime-status read failed for agent %s",
                    agent.id,
                    exc_info=True,
                )
                error_kinds.append("probe_failed")
                continue

            listed_stages.append(name)
            app = _match_app(apps, handle)
            if app is None:
                continue

            state = _app_state(app)
            status = _APP_STATE_TO_RUNTIME_STATUS.get(state.upper(), "unknown")
            detail = None
            if status == "unknown":
                # Either a deliberate non-serving state or one this platform version does not
                # know. Naming it is what makes the state debuggable — but only when it is
                # code-shaped, so an upstream cannot smuggle a payload through the field.
                safe_state = _safe_token(state)
                detail = (
                    f"the app is {safe_state.lower()}"
                    if safe_state
                    else "the app reported no recognizable status"
                )
            return _result(status, name, detail)

        if listed_stages and not error_kinds:
            # EVERY workspace answered and NONE of them listed the app: evidence of absence, the
            # same distinction ``runtime_exists`` draws between NotFound and an ambiguous error.
            #
            # ``not error_kinds`` IS LOAD-BEARING (fix round 1, F1). Without it, MIXED evidence —
            # dev answers empty, prod is forbidden, and the app is actually RUNNING on prod —
            # returned "not_deployed", which is the one claim that evidence cannot support: a
            # workspace we could not read is not a workspace with nothing in it. "not_deployed"
            # is a POSITIVE claim, so it requires a COMPLETE negative result; anything less
            # falls through to the ``unknown`` arm below, which already names the failing kinds.
            return _result(
                "not_deployed",
                stage or (listed_stages[0] if len(listed_stages) == 1 else UNKNOWN_STAGE),
                "no Databricks workspace on this tenant lists this app",
            )

        # Either nobody could look, or only SOME could. "not_deployed" would be a claim the
        # evidence cannot support, so the honest answer is "unknown" naming what went wrong.
        # ``error_kinds`` is non-empty on every path that reaches here: the loop only skips a
        # workspace by appending a kind first, and an all-answered-and-absent sweep was already
        # returned above — so there is no third "no evidence at all" case to default for (F2).
        kinds = sorted(set(error_kinds))
        return _result(
            "unknown",
            reported_stage,
            f"runtime status could not be read ({', '.join(kinds)})",
        )

    async def _list_apps(self, stage, workspace_url: str) -> list[dict]:
        """Mint a workspace token for ``stage`` and list its apps (one loop, one client)."""
        token = await self._workspace_token(stage)
        return await self._databricks.list_apps(workspace_url, token)

    # ------------------------------------------------------------------ #
    # Public: deprovision
    # ------------------------------------------------------------------ #
    async def delete_databricks_runtime(self, agent: Agent, tenant=None) -> None:
        """The inverse of provisioning: remove the audience / delete the per-agent secret.

        ATTEMPT-ALL over independent line items (the ``_configure_runtime_authorizers``
        idiom): a failing audience removal must not skip the secret deletion, because each
        surviving item is a live grant or a live credential.

        THE TWO ITEMS ARE DELIBERATELY ASYMMETRIC ABOUT WHAT THEY DO AT THE END, and the
        asymmetry is the point:

        * **The federation AUDIENCE raises — it is BLOCKING.** An audience entry lives in the
          account-wide federation policy on the CUSTOMER'S DATABRICKS ACCOUNT: it is live trust
          state, not a resource of ours. A surviving entry means a token minted for this agent's
          Entra audience is still exchangeable at that account after AGP says the agent is gone —
          i.e. the record that named the audience is deleted while the grant it created is still
          honoured, and nothing left in the system knows which audience to withdraw. So a delete
          that cannot remove it FAILS LOUDLY and the record stays for a retry, which is exactly
          what makes the retry possible.
        * **The service-principal SECRET does not raise — it is best-effort.** A leaked secret is
          OUR resource in OUR account, it is reported by ARN in the log for cleanup, and (unlike
          the audience) blocking on it would be the ``_NON_BLOCKING_ITEMS`` mistake E28C paid
          for: a Secrets Manager fault no operator retry addresses would trap the row forever.
          The same reasoning already applies to the surviving service principal, which the
          workspace client cannot delete at all and which is only ever REPORTED.

        Safe to call unconditionally on any agent: one that was never provisioned carries no
        audience and no SP pointers, so this is a no-op — including an ``invoke_unavailable``
        agent, whose truthy ``binding_mode`` alone brings it here with nothing to remove.

        NOT GATED BY ``DATABRICKS_ALLOW_SP_SECRET_BINDING`` (E29/T14a, design §3B), deliberately.
        The flag governs whether AGP will CREATE a service-principal binding; it must never stop
        AGP from removing one that already exists. Refusing to tear down because a flag is off
        would leave exactly the live credential + live principal the flag exists to prevent.

        A stored secret ARN is only acted on when it is one this service wrote FOR THIS AGENT
        (:meth:`_owns_secret_arn`). Teardown can be the first operation an agent ever sees, so it
        cannot inherit that guarantee from the provision path — see the inline note.
        """
        failures: list[str] = []
        mode = (agent.binding_mode or "").strip()

        if mode == _MODE_FEDERATION and (agent.entra_app_id or agent.entra_app_audience):
            try:
                await self._remove_audience(agent, tenant)
            except Exception as err:  # noqa: BLE001 — per-item tolerance; re-raised below
                logger.warning(
                    "[databricks_identity] audience removal failed for agent %s: %s",
                    agent.id,
                    err.kind if isinstance(err, DatabricksError) else type(err).__name__,
                )
                failures.append("federation audience")

        # KEYED ON THE SP ID, NOT THE SECRET ARN. sp_secret provisioning creates the SP first
        # and stores its secret second, so a run that failed in between leaves an agent with a
        # live service principal and NO ARN. Gating teardown on the ARN skipped exactly that
        # state — the partial one — and left the principal behind permanently. The SP id is the
        # field that means "something exists in the workspace".
        if agent.databricks_sp_id or agent.databricks_sp_secret_arn:
            arn = (agent.databricks_sp_secret_arn or "").strip()
            if arn and not self._owns_secret_arn(agent, arn):
                # THE SAME UNTRUSTED-POINTER RULE AS PROVISIONING, and it has to be repeated
                # HERE rather than relied upon from there. Teardown can be the FIRST operation an
                # agent ever sees: a record registered with someone else's ARN and then deleted
                # never passes through provisioning at all, so a check that lived only in the
                # provision path would still have let a delete reach another agent's — or the
                # tenant's own workspace — credential. Refused, not merely skipped, so the
                # cascade reports an item it did not silently decline.
                # NOT a failure — a DISCARDED CLAIM. There is nothing here for a retry to fix:
                # the ARN is not this agent's, so no amount of re-running makes it deletable, and
                # `_NON_BLOCKING_ITEMS` exists precisely because a step no retry addresses must
                # not trap the DDB row (the E28C exec-role lesson, one line up in this file's
                # history). Raising here would let a caller who planted a foreign ARN make the
                # agent permanently undeletable — turning a rejected write into a denial of
                # service. So it is logged loudly, the pointer is dropped, and the cascade
                # proceeds; the record is reclaimable and nobody else's secret was touched.
                # BOTH fields, via the same dropper the provision path uses (item E). Clearing
                # only the ARN left ``databricks_sp_id`` holding ANOTHER agent's principal — and
                # the surviving-principal line below is the source for T12's orphan list, so it
                # would have reported someone else's SP as this agent's residue and sent an
                # operator to delete a live principal.
                logger.warning(
                    "[databricks_identity] agent %s names a Databricks secret this service did "
                    "not write for it; ignoring the pointer instead of deleting",
                    agent.id,
                )
                self._drop_untrusted_sp_pointers(agent)
                arn = ""
            if arn:
                # Best-effort: a missing secret is a success (the connection idiom).
                try:
                    self._delete_secret_best_effort(arn)
                    agent.databricks_sp_secret_arn = None
                except Exception:  # noqa: BLE001 — NON-BLOCKING; see the docstring's asymmetry
                    # NOT added to ``failures``: this item must not gate the record. The secret
                    # is ours, in our account, and it is REPORTED here so an operator can reclaim
                    # it — the same treatment as the surviving service principal below. Raising
                    # would let a Secrets Manager fault no retry addresses trap the DDB row
                    # (the E28C exec-role lesson). The audience, which is the customer's live
                    # trust state, is the one item that DOES raise.
                    #
                    # Reported by AGENT ID, never by ARN: a secretsmanager ARN carries the AWS
                    # account id, which a hard project rule bans everywhere including logs (the
                    # ``_probe_runtime`` precedent in ``project_service``). The id is enough to
                    # find it — ownership was just established by the ``/{agent.id}`` suffix
                    # (:meth:`_owns_secret_arn`), which is the only non-configuration part of
                    # the name.
                    logger.exception(
                        "[databricks_identity] agent %s's Databricks service-principal secret "
                        "survives teardown (its name ends with this agent's id) and must be "
                        "deleted by hand",
                        agent.id,
                    )
            if agent.databricks_sp_id:
                # The SP ITSELF survives: the workspace client has no SCIM delete, and adding
                # one is not in this task's manifest. Logged as a NAMED residue rather than
                # silently implied to be gone — an undeleted principal that nothing reports is
                # how a workspace accumulates them (the E28C exec-role lesson).
                logger.info(
                    "[databricks_identity] agent %s's Databricks service principal %s survives "
                    "teardown (the workspace client has no SCIM delete)",
                    agent.id,
                    agent.databricks_sp_id,
                )

        # Only BLOCKING items reach ``failures`` (today: the federation audience alone). The
        # non-blocking residues above report themselves to the log and deliberately leave this
        # list empty, so a caller that treats a raise as "do not delete the record" is right.
        if failures:
            raise ProvisioningError(
                f"Databricks teardown incomplete for agent {agent.id}: "
                f"{', '.join(sorted(failures))} could not be removed"
            )

    async def _remove_audience(self, agent: Agent, tenant) -> None:
        tenant = self._resolve_tenant(agent, tenant)
        stage = self._stage_with_account(tenant)
        account_id = (getattr(stage, "account_id", "") or "").strip()
        if not account_id:
            raise ProvisioningError(
                f"agent {agent.id}'s tenant has no Databricks account id, so its federation "
                f"audience cannot be removed"
            )
        account_token = await self._account_token(tenant, stage, account_id)
        # BOTH forms (livefix-6): the client-id GUID is what provisioning appends now;
        # the api:// URI is what pre-fix records left on customer policies. ONE client
        # call for both (livefix-7): two back-to-back single-audience removals tripped
        # the account API's rate limit live — the second policy list 429'd after the
        # entry was already gone, so teardown reported failure over a success. Absent
        # forms are a silent part of the same call, so legacy residue still self-cleans.
        forms = [a for a in (agent.entra_app_id, agent.entra_app_audience) if a]
        if forms:
            await self._databricks.ensure_federation_audience(
                self._account_host(stage),
                account_id,
                account_token,
                forms,
                present=False,
            )

    # ------------------------------------------------------------------ #
    # Shared steps
    # ------------------------------------------------------------------ #
    async def _assert_app_acl(
        self,
        agent: Agent,
        stage,
        workspace_token: str,
        app_name: str,
        extra_entries: Optional[list[dict]] = None,
    ) -> None:
        """ASSERT the app's access list: AGP owns it from here (design §3A).

        WHAT THIS REPLACED, AND WHY. Provisioning used to grant the tenant's Entra groups
        ``CAN_USE`` on the app. The live test proved the apps proxy enforces that list itself
        (a federated token without ``CAN_USE`` is refused before the app), so a group-wide
        grant governed AGP's invoke PATH while leaving every group member a direct route with
        their own token — an AGP revoke closed one door of two. The Entra app-role assignment
        stays the single source of truth for "may X invoke this agent"; the ACL becomes its
        one-way MIRROR, written per user as grants happen (T13c). Provisioning's job is only to
        establish the baseline AGP owns:

          * ``admins`` at ``CAN_MANAGE`` — workspace admins remain above the governance layer,
            exactly as they are on AgentCore. The honest claim is "no non-admin path around
            AGP", not "no path".
          * the tenant's WORKSPACE service principal at ``CAN_MANAGE`` — the credential AGP
            itself connects with. Without it the very next ACL write (a per-user grant, or a
            re-assert) is refused and the app becomes ungovernable.
          * ``extra_entries`` — the sp_secret leg's own service principal at ``CAN_USE``, the
            one caller-supplied addition, because in that mode the agent's SP *is* the invoke
            identity. KEPT, and this is the DORMANT sp_secret leg's only surface here (design
            §3B): its single caller is gated off by default, so the parameter is live-but-unused
            on a default deployment. Folding it would delete the dormant capability rather than
            dormanting it — and the federation path's desired list is unaffected either way,
            since it passes nothing.

        NOBODY ELSE IS ON THE LIST, and that is the point: a freshly provisioned agent is
        callable by no user until a grant mirrors an Entra assignment onto it.

        LOUD, NEVER SILENT. Pre-existing entries that the assert removes are logged one line
        per principal plus a count. Principal names are logged deliberately here (they are
        directory identities, not secrets, and a takeover whose record omits WHO lost access is
        not a record) — the same rule the rest of the module follows for tokens and bodies still
        holds: neither ever reaches a log line. There is no persisted "stripped" field: the
        state is derivable at any time by re-reading the ACL, which is what the drift read does.

        IDEMPOTENT by construction: the desired list is computed from the tenant and the agent,
        not from a delta, so a re-provision after a mid-sequence failure re-asserts the same
        list. The read is not optional — AGP refuses to replace a list it could not see, since
        the log line is the only record of what the takeover removed.
        """
        # The stage's SP client id is non-empty by construction: ``_resolve_stage_and_app``
        # reached this stage by MINTING a token from it, and ``_workspace_token`` refuses a
        # stage without one. No second guard here, so there is only one rule about it.
        desired: list[dict] = [
            dict(_ADMINS_ENTRY),
            {
                "principal": str(stage.sp_client_id).strip(),
                "kind": _KIND_SERVICE_PRINCIPAL,
                "level": _CAN_MANAGE,
            },
        ]
        for entry in extra_entries or []:
            principal = str(entry.get("principal") or "").strip()
            if not principal:
                raise ProvisioningError(
                    f"agent {agent.id}'s app access list cannot be asserted: an entry it must "
                    f"carry names no principal"
                )
            if any(e["principal"] == principal for e in desired):
                continue
            desired.append({**entry, "principal": principal})

        try:
            current = await self._databricks.get_app_permissions(
                stage.workspace_url, workspace_token, app_name
            )
        except DatabricksError as err:
            raise ProvisioningError(
                f"agent {agent.id}'s app access list could not be read, so AGP refused to "
                f"replace it: {_actionable(err, 'read the app access list')}"
            ) from err

        self._log_stripped_acl_entries(agent, app_name, current, desired)

        try:
            await self._databricks.set_app_permissions(
                stage.workspace_url, workspace_token, app_name, desired
            )
        except DatabricksError as err:
            raise ProvisioningError(
                f"agent {agent.id}'s app access list could not be asserted, so AGP will not "
                f"report the agent governed: "
                f"{_actionable(err, 'assert the app access list')}"
            ) from err

    def _log_stripped_acl_entries(
        self, agent: Agent, app_name: str, current: list[dict], desired: list[dict]
    ) -> None:
        """Name every entry the assert is about to remove, then count them.

        INHERITED entries are counted apart, because a PUT cannot remove them: reporting one as
        "stripped" would claim a takeover that did not happen. They survive the assert and show
        up as drift, which is the honest place for access AGP cannot close.
        """
        wanted = {(e["principal"], e["kind"], e["level"]) for e in desired}
        unwanted = [
            e
            for e in current
            if (e.get("principal"), e.get("kind"), e.get("level")) not in wanted
        ]
        stripped = [e for e in unwanted if not e.get("inherited")]
        inherited = len(unwanted) - len(stripped)
        if inherited:
            logger.warning(
                "[databricks_identity] asserted ACL on app %s for agent %s: %d inherited "
                "entries survive the assert — AGP cannot remove them",
                app_name,
                agent.id,
                inherited,
            )
        if not stripped:
            return
        for entry in stripped:
            logger.warning(
                "[databricks_identity] agent %s: stripping app %s ACL entry — %s (%s) held %s",
                agent.id,
                app_name,
                entry.get("principal"),
                entry.get("kind"),
                entry.get("level"),
            )
        logger.warning(
            "[databricks_identity] asserted ACL on app %s for agent %s: stripped %d "
            "pre-existing entries",
            app_name,
            agent.id,
            len(stripped),
        )

    async def _resolve_stage_and_app(self, agent: Agent, tenant):
        """Find the workspace that ACTUALLY hosts this agent's app; return (stage, token, app).

        WHY A LISTING AND NOT A HOST MATCH. A Databricks app URL is
        ``<app>-<n>.<region>.databricksapps.com`` — it carries no workspace identity, so
        ``runtime_handle`` cannot be matched against a stage's ``workspace_url``. And the app
        NAME is what every write here is keyed on (the permissions API takes a name, not a
        URL), so a name has to come from somewhere: reading it from the workspace's own
        listing is the only source that is evidence rather than a parse of a URL shape the
        docs do not pin (research §2.1 — the app record's field names are UNVERIFIED).

        Getting this wrong is not cosmetic: the account token minted from the chosen stage
        writes ACCOUNT-LEVEL trust state, so picking a stage positionally would let a
        two-workspace tenant have another workspace's account policy edited.
        """
        stages = getattr(tenant, "stages", None) or {}
        if not stages:
            raise ProvisioningError(
                f"tenant {getattr(tenant, 'id', '?')!r} has no Databricks workspace stage, "
                f"so agent {agent.id} cannot be bound"
            )

        handle = (agent.runtime_handle or "").strip()
        errors: list[str] = []
        # Deterministic order so a failure message is reproducible.
        for _name, stage in sorted(stages.items(), key=lambda kv: kv[0]):
            workspace_url = getattr(stage, "workspace_url", "") or ""
            if not workspace_url:
                continue
            try:
                token = await self._workspace_token(stage)
                apps = await self._databricks.list_apps(workspace_url, token)
            except DatabricksError as err:
                errors.append(err.kind)
                continue
            app = _match_app(apps, handle)
            if app is not None:
                return stage, token, app

        raise ProvisioningError(
            f"no Databricks workspace on this tenant lists agent {agent.id}'s app, so AGP "
            f"cannot resolve the app it must grant access on"
            + (f" (workspace read issues: {sorted(set(errors))})" if errors else "")
        )

    def _stage_with_account(self, tenant):
        """The stage carrying a Databricks account id — the teardown path's stage pick.

        Teardown cannot use the listing (the app may already be gone), and the audience it
        removes is an ACCOUNT-level entry, so the account id is the only field that matters.
        A tenant whose stages disagree on account id is refused rather than guessed at.
        """
        stages = getattr(tenant, "stages", None) or {}
        with_account = [
            s for _n, s in sorted(stages.items(), key=lambda kv: kv[0])
            if (getattr(s, "account_id", "") or "").strip()
        ]
        if not with_account:
            raise ProvisioningError("the tenant has no Databricks account id")
        account_ids = {s.account_id for s in with_account}
        if len(account_ids) > 1:
            raise ProvisioningError(
                "the tenant's workspaces name more than one Databricks account, so AGP "
                "cannot tell which account's federation policy to edit"
            )
        return with_account[0]

    def _account_host(self, stage) -> str:
        cloud = (getattr(stage, "cloud", "") or "aws").strip().lower()
        return _ACCOUNT_HOSTS.get(cloud, _ACCOUNT_HOSTS["aws"])

    async def _workspace_token(self, stage) -> str:
        """Mint a WORKSPACE M2M token from the stage's stored SP credential."""
        client_id = (getattr(stage, "sp_client_id", "") or "").strip()
        secret = self._stage_secret(stage)
        if not client_id:
            raise ProvisioningError(
                "the tenant's Databricks workspace stage has no service-principal client id"
            )
        return await self._databricks.mint_m2m_token(
            stage.workspace_url, client_id, secret
        )

    async def _account_token(self, tenant, stage, account_id: str) -> str:
        """Mint an ACCOUNT-scoped token — a federation policy does not accept a workspace one.

        PREFERS the tenant's account-admin credential. Writing an account federation policy is
        an account-admin act (research §7 B9 — a deliberately separate, optional grant), and T3
        stores exactly that credential at TENANT level: ``Tenant.account_admin_secret_arn``, with
        BOTH halves inside the secret body under ``ACCOUNT_ADMIN_ID_KEY`` /
        ``ACCOUNT_ADMIN_SECRET_KEY`` (neither is a read field on the model, so neither can leak
        through a tenant response).

        The workspace SP is the FALLBACK, not an equal: it is the credential a customer who never
        made the account-admin grant has, and attempting the call with it produces the actionable
        ``unauthorized``/``forbidden`` message rather than a crash. Preference order is pinned by
        test, because silently using the workspace SP when an account-admin credential exists
        would make a working tenant fail for no visible reason.

        The account console host is threaded through per call: it is per-cloud and not derivable
        from the workspace URL (research §5.1), and the tenant's stage records the cloud.
        """
        admin_arn = (getattr(tenant, "account_admin_secret_arn", "") or "").strip()
        if admin_arn:
            body = self._read_secret_body(admin_arn)
            client_id = str(body.get(ACCOUNT_ADMIN_ID_KEY) or "").strip()
            secret = str(body.get(ACCOUNT_ADMIN_SECRET_KEY) or "")
            if not client_id or not secret:
                raise ProvisioningError(
                    "the tenant's stored Databricks account-admin credential is incomplete, so "
                    "it cannot mint an account token"
                )
        else:
            client_id = (getattr(stage, "sp_client_id", "") or "").strip()
            secret = self._stage_secret(stage)
            if not client_id:
                raise ProvisioningError(
                    "the tenant has no Databricks credential able to reach its account, and "
                    "federation mode writes an account-level policy"
                )
        return await self._databricks.mint_account_token(
            account_id, client_id, secret, account_host=self._account_host(stage)
        )

    # ------------------------------------------------------------------ #
    # Secrets Manager
    # ------------------------------------------------------------------ #
    def _stage_secret(self, stage) -> str:
        arn = (getattr(stage, "sp_client_secret_arn", "") or "").strip()
        if not arn:
            raise ProvisioningError(
                "the tenant's Databricks workspace stage has no stored service-principal "
                "secret, so AGP cannot authenticate to the workspace"
            )
        return self._read_secret(arn, SP_SECRET_KEY, _AGENT_SECRET_KEY)

    def _read_secret_body(self, secret_id: str) -> dict:
        """Read a stored secret's JSON body BY THE ARN THE RECORD HOLDS.

        Never by a name rebuilt from a prefix: the ARN is what was written when the secret was
        created, and reconstructing a name would silently read the wrong secret (or none) if a
        prefix ever changed.

        A non-JSON body reads as ``{"": raw}`` via :meth:`_read_secret`'s bare-string branch
        rather than here; this returns ``{}`` for it, so a caller that needs a KEYED value fails
        with "no usable credential" instead of an opaque parse error. The exception path logs a
        traceback ONLY, with no interpolated value (the ``connection_service`` idiom) — a boto3
        error can quote the secret's name but must never be given the chance to quote more.
        """
        raw = self._read_secret_string(secret_id)
        try:
            body = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return body if isinstance(body, dict) else {}

    def _read_secret_string(self, secret_id: str) -> str:
        try:
            resp = self._sm.get_secret_value(SecretId=secret_id)
        except Exception:  # noqa: BLE001 — includes the client's own ResourceNotFound class
            logger.exception(
                "[databricks_identity] could not read a stored Databricks secret"
            )
            raise ProvisioningError(
                "the tenant's stored Databricks secret could not be read"
            ) from None
        return resp.get("SecretString") or ""

    def _read_secret(self, secret_id: str, *keys: str) -> str:
        """One credential VALUE from a stored secret, by the first of ``keys`` that carries one.

        The body may be a JSON envelope or a bare string: T3 writes the envelope, but a secret
        rotated by hand in the console is a bare string, and refusing that would turn a
        console-side fix into an outage. The VALUE is returned into a local and never logged.
        """
        raw = self._read_secret_string(secret_id)
        try:
            body = json.loads(raw)
        except (ValueError, TypeError):
            body = None
        if isinstance(body, dict):
            for key in keys:
                value = body.get(key)
                if isinstance(value, str) and value:
                    return value
            raise ProvisioningError(
                "the tenant's stored Databricks secret carries no usable credential"
            )
        if isinstance(raw, str) and raw:
            return raw
        raise ProvisioningError("the tenant's stored Databricks secret is empty")

    def _owns_secret_arn(self, agent: Agent, arn: str) -> bool:
        """Is this ARN one THIS service wrote for THIS agent? PREFIX-INDEPENDENT.

        WHAT WENT WRONG BEFORE (fix round 3, item C). This rebuilt the expected name from
        ``self._secret_prefix``, which made a SETTABLE config value
        (``DATABRICKS_TENANT_SECRET_PREFIX``) load-bearing for recognising an agent's OWN state —
        exactly what :meth:`_read_secret_body` warns against one screen down. After a prefix
        change every agent stopped recognising its own secret: re-provision minted a SECOND
        service principal, the old secret stayed live and unreferenced, the old principal kept its
        CAN_USE grant, and teardown refused to delete the agent's own secret and then dropped the
        pointer, so nothing named the leak. A rename in config became silent resource leakage.

        So ownership keys on the ONE part of the name that is not configuration: the trailing
        ``/{agent.id}`` segment. The prefix may be anything, today's or yesterday's.

        BOUNDARIES AT BOTH ENDS, which is what makes it injective (item D). The id must be
        preceded by ``/`` and followed by either end-of-string or Secrets Manager's ``-`` + six
        alphanumerics. The previous ``rsplit("-", 1)[0]`` was non-injective — it let id
        ``rec-agent`` adopt ``rec-agent-a``'s principal, because stripping one ``-``-segment from
        ``rec-agent-a`` yields ``rec-agent``. Anchoring both ends removes that whole class.

        WHY NOT THE ``agent_id`` TAG that :meth:`_store_agent_secret` already writes (the other
        option). The tag is authoritative, but reading it costs a ``describe_secret`` call against
        a CALLER-SUPPLIED ARN — which needs a new IAM permission and, worse, turns this predicate
        into an existence oracle: a caller could probe which secrets AGP's role can see by
        watching one attack succeed differently from another. This check makes NO API call at all,
        so a foreign ARN is rejected before anything is contacted. The tag remains on the secret
        for out-of-band auditing (and the body carries ``agent_id`` too — see
        :data:`_AGENT_OWNER_KEY` — which the paths that ALREADY read the body verify, so the
        authoritative check still happens where it is free).
        """
        agent_id = (agent.id or "").strip()
        if not agent_id:
            return False
        # Must be a Secrets Manager SECRET arn at all. Cheap, and it stops a caller handing us
        # some other service's ARN whose tail happens to end in the right characters.
        match = _SM_SECRET_ARN_RE.match(arn.strip())
        if not match:
            return False
        name = _SM_RANDOM_SUFFIX_RE.sub("", match.group("name"))
        # A "." or ".." path segment is never something this service writes, and Secrets Manager
        # names are LITERAL (never path-normalised), so ".../sp/../rec-abc" is a genuinely
        # DIFFERENT secret that nonetheless ends with the right segment. Refusing the shape is
        # cheaper and safer than reasoning about which literal names could collide.
        segments = name.split("/")
        if "." in segments or ".." in segments:
            return False
        # Anchored at BOTH ends: a "/" immediately before the id, and nothing after it.
        return name.endswith(f"/{agent_id}")

    def _drop_untrusted_sp_pointers(self, agent: Agent) -> None:
        """Discard ``databricks_sp_id``/``databricks_sp_secret_arn`` unless the ARN is ours.

        Both fields go together: the SP id without a matching secret is not resumability state,
        it is an assertion about which principal to grant CAN_USE to and to report as surviving at
        teardown. Keeping half of an untrusted pair is how the cross-agent case leaked in the
        first place.

        A record with NEITHER field is untouched — that is a first provision, not a claim.
        """
        arn = (agent.databricks_sp_secret_arn or "").strip()
        sp_id = (agent.databricks_sp_id or "").strip()
        if not arn and not sp_id:
            return
        if arn and self._owns_secret_arn(agent, arn):
            return
        # Never log the ARN: it names another agent's (or the tenant's) secret.
        logger.warning(
            "[databricks_identity] agent %s carried Databricks service-principal pointers this "
            "service did not write; discarding them and provisioning its own",
            agent.id,
        )
        agent.databricks_sp_id = None
        agent.databricks_sp_secret_arn = None

    def _agent_secret_name(self, agent: Agent) -> str:
        """The per-agent SP secret's NAME (used only at CREATE time; reads go by stored ARN)."""
        return f"{self._secret_prefix}{agent.id}"

    def _store_agent_secret(self, agent: Agent, *, secret: str, scim_id: str) -> str:
        """Store a freshly-minted per-agent SP secret; return its ARN.

        ``create_secret`` with ``ResourceExistsException → put_secret_value`` (the
        ``connection_service._create_secret`` idiom), so a re-provision after a crash between
        mint and persist overwrites rather than dying on the name. The SCIM id rides in the body
        (see :data:`_AGENT_SCIM_ID_KEY`). NOTHING here logs the value: the ``except`` interpolates
        only the agent id and relies on ``logger.exception``'s traceback.
        """
        name = self._agent_secret_name(agent)
        # ``secret=""`` writes the id-only entry (see the ordering note in
        # :meth:`_provision_sp_secret`); the key is omitted entirely rather than stored empty, so
        # "has a credential" is a key-presence question with no falsy-value ambiguity.
        payload = {_AGENT_SCIM_ID_KEY: scim_id, _AGENT_OWNER_KEY: agent.id}
        if secret:
            payload[_AGENT_SECRET_KEY] = secret
        body = json.dumps(payload)
        try:
            resp = self._sm.create_secret(
                Name=name,
                SecretString=body,
                Tags=[
                    {"Key": "managed_by", "Value": "agp"},
                    {"Key": "agent_id", "Value": agent.id},
                ],
            )
            return resp["ARN"]
        except getattr(
            getattr(self._sm, "exceptions", None), "ResourceExistsException", ()
        ):
            resp = self._sm.put_secret_value(SecretId=name, SecretString=body)
            return resp["ARN"]
        except _STORE_FAULTS:
            logger.exception(
                "[databricks_identity] could not store the per-agent Databricks secret for "
                "agent %s",
                agent.id,
            )
            raise ProvisioningError(
                f"agent {agent.id}'s Databricks credential could not be stored, so it was not "
                f"recorded"
            ) from None

    def _stored_agent_secret_present(self, agent: Agent) -> bool:
        """Does the per-agent secret entry actually CARRY a credential?

        The gate for "mint or not". It reads the BODY rather than the ARN because the ARN is
        written as soon as the service principal is (to preserve the SCIM id), so an ARN proves
        the entry exists — not that it holds a credential. An unreadable body reads as "no
        credential", which fails toward minting one; the alternative is an agent stuck with no
        way to authenticate.
        """
        arn = (agent.databricks_sp_secret_arn or "").strip()
        if not arn:
            return False
        try:
            body = self._read_secret_body(arn)
        except ProvisioningError:
            return False
        # Defence in depth, free here because the body is already in hand: a body naming a
        # DIFFERENT owner is not this agent's credential, whatever its name looks like. Only
        # this service ever writes these bodies, so the value is authoritative. A body with no
        # owner key predates this field and is judged by the name alone.
        owner = body.get(_AGENT_OWNER_KEY)
        if isinstance(owner, str) and owner and owner != agent.id:
            return False
        return bool(body.get(_AGENT_SECRET_KEY))

    def _stored_scim_id(self, agent: Agent) -> str:
        """The SP's SCIM id from the per-agent secret body, or "" — best-effort by design.

        Only needed to MINT a secret, and a record that already names one never mints again, so
        an unreadable body here is not an error: it degrades to "" and the caller decides. The
        caller's decision is to refuse rather than substitute the application id, which would
        404 as "the service principal does not exist".
        """
        arn = (agent.databricks_sp_secret_arn or "").strip()
        if not arn:
            return ""
        try:
            body = self._read_secret_body(arn)
        except ProvisioningError:
            return ""
        owner = body.get(_AGENT_OWNER_KEY)
        if isinstance(owner, str) and owner and owner != agent.id:
            # Another agent's principal — never resumability state for this one.
            return ""
        return str(body.get(_AGENT_SCIM_ID_KEY) or "")

    def _delete_secret_best_effort(self, secret_id: str) -> None:
        """Delete a per-agent secret; an already-missing one is a success (spec §5 idiom)."""
        try:
            self._sm.delete_secret(
                SecretId=secret_id, ForceDeleteWithoutRecovery=True
            )
        except getattr(
            getattr(self._sm, "exceptions", None), "ResourceNotFoundException", ()
        ):
            return
        except _STORE_FAULTS:
            logger.exception(
                "[databricks_identity] delete_secret failed for a per-agent secret"
            )
            raise

    # ------------------------------------------------------------------ #
    # Misc
    # ------------------------------------------------------------------ #
    def _resolve_tenant(self, agent: Agent, tenant):
        if tenant is not None:
            return tenant
        if self._tenants is None or not agent.tenant_id:
            raise ProvisioningError(
                f"agent {agent.id} has no tenant, so its Databricks binding cannot be "
                f"resolved"
            )
        return self._tenants.get(agent.tenant_id)

    def _persist_failed(self, agent: Agent) -> None:
        agent.identity_status = IdentityStatus.FAILED
        try:
            self._registry.persist_identity(agent)
        except Exception:  # noqa: BLE001 — never mask the original failure
            logger.exception(
                "[databricks_identity] failed to persist 'failed' status for agent %s",
                agent.id,
            )


def _match_app(apps, handle: str) -> Optional[dict]:
    """The app record whose ``url`` matches this agent's handle, else None.

    Matched on the URL because that is what the record stores and what discovery read — the
    same field, never a reconstruction. Compared with trailing slashes and case normalised so
    a cosmetic difference between what discovery wrote and what the listing returns today does
    not read as "the app is gone".
    """
    if not handle:
        return None
    target = handle.rstrip("/").lower()
    for app in apps:
        if not isinstance(app, dict) or not app.get("name"):
            continue
        url = str(app.get("url") or "").rstrip("/").lower()
        if url and url == target:
            return app
    return None
