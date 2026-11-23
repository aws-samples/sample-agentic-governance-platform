"""The platform registry: what each platform is, and what one tenant can therefore do.

Two kinds of fact with different lifetimes. Declared facts are architectural truth about a platform
(who owns the access door, whether group grants exist, whether AGP can inject trace delivery, whether
an ops lane exists, whether records are publishable, which verbs apply) and change only when an
engineer edits ``DECLARATIONS``, so the support matrix is rendered, never written as prose. Probed
facts are the state of one tenant's grant, written by the connect-time probe onto
``Tenant.capabilities`` and ``Tenant.binding_mode``. What a UI may show is their intersection, and
``tenant_readiness`` is the only place it is computed; nothing here is persisted and this module has
no collaborators. ``DECLARATIONS`` is exhaustive over ``TenantPlatform`` and every ``verbs`` map over
``Verb`` (the tests assert both as set equalities), and ``declaration_for`` refuses an unknown
platform rather than defaulting, so a platform gets no capability with no enforcement behind it. The
binding-mode words are imported from ``tenant_service``, which declares them beside the probe that
writes them; the wire vocabulary (``Verb``, ``ReadinessReason``, ``VerbReadiness``) is declared in
``models.tenant`` beside the ``TenantView`` that carries it, and every verdict built here is a
``VerbReadiness`` naming its own verb.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Literal

from models.tenant import TenantPlatform, Verb, VerbReadiness
from services.tenant_service import BINDING_FEDERATION, BINDING_INVOKE_UNAVAILABLE

if TYPE_CHECKING:
    from models.tenant import Tenant

logger = logging.getLogger(__name__)


class DoorOwnership(str, Enum):
    """Who controls whether a caller reaches the agent at all.

    ``AGP_INSTALLS_THE_ONLY_DOOR`` (AgentCore): no platform-side ACL, so AGP's Entra check is the
    access control; if AGP does not refuse, nothing does. ``PLATFORM_ENFORCES_ITS_OWN`` (Databricks
    Apps): the workspace runs its own permission model, which is why an ACL mirror exists and why a
    grant that skips it is a lie. ``AGP_DOOR_OVER_INHERITED``: reserved, no declaration uses it; the
    shape of a door installed over permissions the project already grants, where revoking AGP's
    grant does not necessarily revoke access.
    """

    AGP_INSTALLS_THE_ONLY_DOOR = "agp_installs_the_only_door"
    PLATFORM_ENFORCES_ITS_OWN = "platform_enforces_its_own"
    AGP_DOOR_OVER_INHERITED = "agp_door_over_inherited"


class GroupGrantSupport(str, Enum):
    """Whether a grant can name a group rather than a user, and how honestly. ``EMULATED`` means
    AGP would have to expand the group itself and re-expand it forever, a synchronisation promise
    rather than a feature. Both shipped platforms declare ``UNSUPPORTED``."""

    NATIVE = "native"
    EMULATED = "emulated"
    UNSUPPORTED = "unsupported"


VerbSupport = Literal["supported", "conditional", "not_applicable", "unsupported"]


@dataclass(frozen=True)
class PlatformDeclaration:
    """Everything AGP knows about a platform without looking at a tenant. Frozen because it is a
    constant: a caller that could mutate one would edit the support matrix for every other caller
    in the process.

    ``trace_auto_delivery`` is whether AGP can inject OTLP env onto a runtime it does not own;
    ``ops_lane`` is whether materialize, build, deploy and promote are offered;
    ``marketplace_publish`` is whether records are publishable. ``verbs`` maps each verb to
    ``supported``, ``conditional`` (this tenant's grant decides, the one sentence an operator can
    act on), ``not_applicable`` (the verb does not exist on the platform, so badging it red would
    send an operator hunting for a grant) or ``unsupported``.
    """

    platform: TenantPlatform
    door_ownership: DoorOwnership
    groups: GroupGrantSupport
    trace_auto_delivery: bool
    ops_lane: bool
    marketplace_publish: bool
    verbs: Mapping[Verb, VerbSupport]


DECLARATIONS: dict[TenantPlatform, PlatformDeclaration] = {
    TenantPlatform.AWS: PlatformDeclaration(
        platform=TenantPlatform.AWS,
        door_ownership=DoorOwnership.AGP_INSTALLS_THE_ONLY_DOOR,
        groups=GroupGrantSupport.UNSUPPORTED,
        trace_auto_delivery=True,
        ops_lane=True,
        marketplace_publish=True,
        verbs={
            Verb.DISCOVER: "supported",
            Verb.INVOKE: "supported",
            Verb.MIRROR: "not_applicable",
            Verb.DEPLOY: "supported",
            Verb.PUBLISH: "supported",
            Verb.GUARDRAILS: "supported",
            Verb.MCP_DELIVERY: "supported",
            Verb.OBSERVABILITY: "supported",
        },
    ),
    TenantPlatform.DATABRICKS: PlatformDeclaration(
        platform=TenantPlatform.DATABRICKS,
        door_ownership=DoorOwnership.PLATFORM_ENFORCES_ITS_OWN,
        groups=GroupGrantSupport.UNSUPPORTED,
        trace_auto_delivery=False,
        ops_lane=False,
        marketplace_publish=False,
        verbs={
            Verb.DISCOVER: "supported",
            Verb.INVOKE: "conditional",
            Verb.MIRROR: "conditional",
            Verb.DEPLOY: "unsupported",
            Verb.PUBLISH: "unsupported",
            Verb.GUARDRAILS: "unsupported",
            Verb.MCP_DELIVERY: "unsupported",
            Verb.OBSERVABILITY: "supported",
        },
    ),
}

AWS_DEPLOY_REACH = "can_deploy_reach"


def aws_reach_key(reach: str, stage: str) -> str:
    """The flat ``capabilities`` key for one stage's reach probe, spelled here for both the writer
    (the AWS adapter) and the reader (``tenant_readiness``). ``Tenant.capabilities`` is a flat
    ``dict[str, bool]`` shared with the Databricks flags, so the stage is encoded in the key;
    ``:`` separates because no stage name may contain one."""
    return f"{reach}:{stage}"


REASON_NONE = ""
REASON_UNSUPPORTED = "unsupported"
REASON_DEPLOY_ROLE_UNREACHABLE = "deploy_role_not_assumable"
REASON_PROBE_UNREACHABLE = "probe_unreachable"

READINESS_REASONS: frozenset = frozenset(
    {
        REASON_NONE,
        REASON_UNSUPPORTED,
        REASON_DEPLOY_ROLE_UNREACHABLE,
        REASON_PROBE_UNREACHABLE,
        BINDING_INVOKE_UNAVAILABLE,
    }
)


def _ready(verb: Verb) -> VerbReadiness:
    return VerbReadiness(verb=verb, state="ready", reason=REASON_NONE)


def _not_applicable(verb: Verb) -> VerbReadiness:
    return VerbReadiness(verb=verb, state="not_applicable", reason=REASON_NONE)


def declaration_for(platform: TenantPlatform | str) -> PlatformDeclaration:
    """The declaration for a platform, by enum or by stored wire string. Raises
    ``ValueError("platform_unsupported")`` for anything else; never a default declaration."""
    try:
        return DECLARATIONS[TenantPlatform(platform)]
    except (KeyError, ValueError):
        raise ValueError("platform_unsupported") from None


def _aws_deploy_readiness(verb: Verb, capabilities: Mapping[str, bool]) -> VerbReadiness:
    """Can the control plane reach the tenant's deploy role? Empty ``capabilities`` means the
    tenant was never probed and renders as it did before probing existed: the red badge is evidence
    of a failed probe, not of a missing one. Only the deploy role is probed; the push role's trust
    policy names GitHub's OIDC provider, never AGP. Stages are read in sorted order so a tenant with
    several gaps always names the same one, in ``stage``."""
    prefix = f"{AWS_DEPLOY_REACH}:"
    for key in sorted(k for k in capabilities if k.startswith(prefix)):
        if capabilities[key] is False:
            return VerbReadiness(
                verb=verb,
                state="unavailable",
                reason=REASON_DEPLOY_ROLE_UNREACHABLE,
                stage=key[len(prefix) :],
            )
    return _ready(verb)


def _databricks_binding_readiness(verb: Verb, binding_mode: str) -> VerbReadiness:
    """Federation is the only binding AGP will act on. ``sp_secret`` is dormant and ``""`` is a
    record that never probed; both are refused rather than treated as probably fine, because the
    shared-service-principal path produces an audit trail that stops at AGP's boundary."""
    if binding_mode == BINDING_FEDERATION:
        return _ready(verb)
    return VerbReadiness(verb=verb, state="unavailable", reason=BINDING_INVOKE_UNAVAILABLE)


def _conditional_readiness(platform: TenantPlatform, verb: Verb, tenant: Tenant) -> VerbReadiness:
    """A ``conditional`` verb's verdict, one branch per platform that has a derivation. A pair
    declared conditional with no derivation wired is refused with ``REASON_PROBE_UNREACHABLE``,
    never granted: "AGP cannot verify this" is the honest answer."""
    if platform is TenantPlatform.DATABRICKS:
        return _databricks_binding_readiness(verb, tenant.binding_mode)
    logger.warning(
        "readiness: conditional verb with no derivation (platform=%s verb=%s), refusing",
        platform.value,
        verb.value,
    )
    return VerbReadiness(verb=verb, state="unavailable", reason=REASON_PROBE_UNREACHABLE)


def _supported_readiness(
    platform: TenantPlatform, verb: Verb, capabilities: Mapping[str, bool]
) -> VerbReadiness:
    """A ``supported`` verb is ready unless a probe result speaks against it; only AWS ``DEPLOY``
    has a probe today."""
    if platform is TenantPlatform.AWS and verb is Verb.DEPLOY:
        return _aws_deploy_readiness(verb, capabilities)
    return _ready(verb)


def tenant_readiness(tenant: Tenant) -> dict[str, VerbReadiness]:
    """Per-verb readiness for one tenant: the declaration intersected with that tenant's stored
    probe results, keyed by the verb's wire value. ``not_applicable`` stays ``not_applicable`` with
    no reason; ``unsupported`` is ``unavailable`` with the ``unsupported`` reason; ``supported`` and
    ``conditional`` ask the tenant's probe. The dispatch is exhaustive over ``VerbSupport`` and an
    unrecognised value raises ``ValueError("verb_support_unknown")`` rather than picking a state."""
    decl = declaration_for(tenant.platform)
    capabilities = tenant.capabilities or {}
    readiness: dict[str, VerbReadiness] = {}

    for verb, support in decl.verbs.items():
        if support == "not_applicable":
            readiness[verb.value] = _not_applicable(verb)
        elif support == "unsupported":
            readiness[verb.value] = VerbReadiness(verb=verb, state="unavailable", reason=REASON_UNSUPPORTED)
        elif support == "conditional":
            readiness[verb.value] = _conditional_readiness(decl.platform, verb, tenant)
        elif support == "supported":
            readiness[verb.value] = _supported_readiness(decl.platform, verb, capabilities)
        else:
            raise ValueError("verb_support_unknown")

    return readiness
