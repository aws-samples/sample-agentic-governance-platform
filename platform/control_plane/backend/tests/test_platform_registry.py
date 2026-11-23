"""Tests for the platform registry + the agent↔tenant vocabulary bridge.

Three independent things are under test, and the split matters:

1. **The declarations** — exhaustive over ``TenantPlatform`` AND over ``Verb``, because a
   declaration missing one verb is exactly the hole a closed vocabulary exists to close: the lookup would
   ``KeyError`` at read time, in a projection, for one platform only.
2. **``tenant_readiness``** — the rule table from the design, written as a table here on
   purpose. This function is the whole honesty surface: it is what makes a tenant page say
   "discovery works, invoke refused, trust-binding does not exist here" instead of one word.
3. **The bridge** — ``GOVERNABLE_PLATFORMS`` / ``INVENTORY_ONLY_PLATFORMS`` partition the eight
   agent-platform labels with no overlap and no gap. The partition is asserted as a partition
   rather than as two hardcoded lists, so adding a ninth label fails here and nowhere else.

No fakes are needed: the registry reads a ``Tenant`` and nothing else. That is the point of
deriving readiness instead of storing it — the unit under test has no collaborators to stub.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from typing import Any, get_args

import pytest

from models.agent import (
    GOVERNABLE_PLATFORMS,
    INVENTORY_ONLY_PLATFORMS,
    Platform,
    is_inventory_only,
)
from models.tenant import (
    DatabricksStageConfig,
    ReadinessReason,
    Tenant,
    TenantBindingMode,
    TenantPlatform,
    TenantStageConfig,
    Verb,
    VerbReadiness,
)
from services import platform_registry
from services.platform_registry import (
    AWS_DEPLOY_REACH,
    DECLARATIONS,
    READINESS_REASONS,
    REASON_DEPLOY_ROLE_UNREACHABLE,
    REASON_PROBE_UNREACHABLE,
    REASON_UNSUPPORTED,
    DoorOwnership,
    GroupGrantSupport,
    aws_reach_key,
    declaration_for,
    tenant_readiness,
)
from services.tenant_service import (
    BINDING_FEDERATION,
    BINDING_INVOKE_UNAVAILABLE,
    BINDING_SP_SECRET,
)

WS = "https://dbc-test.cloud.databricks.com"


def _tenant(
    platform: TenantPlatform,
    *,
    capabilities: dict | None = None,
    binding_mode: TenantBindingMode = "",
    stages: dict | None = None,
) -> Tenant:
    if stages is None:
        stages = (
            {"dev": TenantStageConfig(account_id="0" * 12, region="us-east-1")}
            if platform is TenantPlatform.AWS
            else {"dev": DatabricksStageConfig(workspace_url=WS)}
        )
    return Tenant(
        id="ten-abcd1234",
        name="LoB",
        line_of_business="Retail",
        entra_group_ids=["g1"],
        platform=platform,
        stages=stages,
        capabilities=capabilities or {},
        binding_mode=binding_mode,
        created_by="tester",
        created_at="2026-08-19T00:00:00Z",
        updated_at="2026-08-19T00:00:00Z",
    )


def test_declarations_cover_every_tenant_platform():
    assert set(DECLARATIONS) == set(TenantPlatform)


def test_every_declaration_covers_every_verb():
    for platform, decl in DECLARATIONS.items():
        assert set(decl.verbs) == set(Verb), platform


def test_declaration_verb_support_values_are_from_the_closed_set():
    allowed = {"supported", "conditional", "not_applicable", "unsupported"}
    for decl in DECLARATIONS.values():
        assert set(decl.verbs.values()) <= allowed


def test_declaration_platform_field_matches_its_key():
    for platform, decl in DECLARATIONS.items():
        assert decl.platform is platform


def test_aws_declaration_facts():
    decl = DECLARATIONS[TenantPlatform.AWS]
    assert decl.door_ownership is DoorOwnership.AGP_INSTALLS_THE_ONLY_DOOR
    assert decl.groups is GroupGrantSupport.UNSUPPORTED
    assert decl.trace_auto_delivery is True
    assert decl.ops_lane is True
    assert decl.marketplace_publish is True
    assert decl.verbs[Verb.MIRROR] == "not_applicable"
    assert all(support == "supported" for verb, support in decl.verbs.items() if verb is not Verb.MIRROR)


def test_databricks_declaration_facts():
    decl = DECLARATIONS[TenantPlatform.DATABRICKS]
    assert decl.door_ownership is DoorOwnership.PLATFORM_ENFORCES_ITS_OWN
    assert decl.groups is GroupGrantSupport.UNSUPPORTED
    assert decl.trace_auto_delivery is False
    assert decl.ops_lane is False
    assert decl.marketplace_publish is False
    assert decl.verbs == {
        Verb.DISCOVER: "supported",
        Verb.INVOKE: "conditional",
        Verb.MIRROR: "conditional",
        Verb.OBSERVABILITY: "supported",
        Verb.DEPLOY: "unsupported",
        Verb.PUBLISH: "unsupported",
        Verb.GUARDRAILS: "unsupported",
        Verb.MCP_DELIVERY: "unsupported",
    }


def test_declaration_is_frozen():
    untyped: Any = DECLARATIONS[TenantPlatform.AWS]
    with pytest.raises(FrozenInstanceError):
        untyped.ops_lane = False


def test_declaration_for_accepts_enum_and_wire_string():
    assert declaration_for(TenantPlatform.DATABRICKS) is DECLARATIONS[TenantPlatform.DATABRICKS]
    assert declaration_for("databricks") is DECLARATIONS[TenantPlatform.DATABRICKS]


def test_declaration_for_unknown_platform_raises_the_fixed_kind():
    with pytest.raises(ValueError) as exc:
        declaration_for("azure")
    assert str(exc.value) == "platform_unsupported"


def test_declaration_for_reserved_door_ownership_is_unused():
    # The reserved word ships (vocabulary), but nothing declares it yet.
    assert DoorOwnership.AGP_DOOR_OVER_INHERITED not in {d.door_ownership for d in DECLARATIONS.values()}


def test_readiness_keys_are_the_verb_wire_values():
    readiness = tenant_readiness(_tenant(TenantPlatform.AWS))
    assert set(readiness) == {v.value for v in Verb}


def test_readiness_entries_carry_state_and_reason():
    for entry in tenant_readiness(_tenant(TenantPlatform.AWS)).values():
        assert isinstance(entry, VerbReadiness)
        assert entry.state in {"ready", "unavailable", "not_applicable"}
        assert entry.stage is None


@pytest.mark.parametrize("platform", list(TenantPlatform))
def test_every_readiness_entry_names_its_own_verb(platform):
    for key, entry in tenant_readiness(_tenant(platform)).items():
        assert entry.verb.value == key
        assert entry.model_dump(mode="json")["verb"] == key


def test_the_registry_module_still_exposes_the_verb_vocabulary_its_declarations_range_over():
    assert platform_registry.Verb is Verb
    assert platform_registry.VerbReadiness is VerbReadiness


def test_every_reason_produced_is_in_the_closed_set():
    tenants = [
        _tenant(TenantPlatform.AWS),
        _tenant(TenantPlatform.AWS, capabilities={aws_reach_key(AWS_DEPLOY_REACH, "dev"): False}),
        _tenant(TenantPlatform.DATABRICKS, binding_mode=BINDING_FEDERATION),
        _tenant(TenantPlatform.DATABRICKS, binding_mode=BINDING_INVOKE_UNAVAILABLE),
        _tenant(TenantPlatform.DATABRICKS),
    ]
    for tenant in tenants:
        for entry in tenant_readiness(tenant).values():
            assert entry.reason in READINESS_REASONS, entry


def test_reason_literal_set_is_closed_and_matches_the_imported_binding_word():
    # The ``Literal`` must spell its members inline, so THIS is the anti-drift guard: a rename of
    # ``tenant_service.BINDING_INVOKE_UNAVAILABLE`` fails here, not silently on the wire.
    assert set(get_args(ReadinessReason)) == set(READINESS_REASONS)
    assert BINDING_INVOKE_UNAVAILABLE in READINESS_REASONS


def test_no_reason_embeds_a_stage_key():
    # A reason carrying ``:<stage>`` is infinitely many strings wearing one meaning — the FE
    # could not map it exhaustively. The stage belongs in its own field.
    for reason in READINESS_REASONS:
        assert ":" not in reason


def test_ready_and_not_applicable_carry_an_empty_reason():
    for platform in TenantPlatform:
        for entry in tenant_readiness(_tenant(platform)).values():
            if entry.state != "unavailable":
                assert entry.reason == ""


def test_declared_not_applicable_stays_not_applicable_regardless_of_probe():
    for capabilities in ({}, {aws_reach_key(AWS_DEPLOY_REACH, "dev"): False}):
        readiness = tenant_readiness(_tenant(TenantPlatform.AWS, capabilities=capabilities))
        assert readiness[Verb.MIRROR.value] == VerbReadiness(
            verb=Verb.MIRROR, state="not_applicable", reason=""
        )


def test_declared_unsupported_is_unavailable_regardless_of_probe():
    for binding in (BINDING_FEDERATION, BINDING_INVOKE_UNAVAILABLE, ""):
        readiness = tenant_readiness(_tenant(TenantPlatform.DATABRICKS, binding_mode=binding))
        for verb in (Verb.DEPLOY, Verb.PUBLISH, Verb.GUARDRAILS, Verb.MCP_DELIVERY):
            assert readiness[verb.value] == VerbReadiness(
                verb=verb, state="unavailable", reason=REASON_UNSUPPORTED
            )


def test_databricks_federation_makes_invoke_and_mirror_ready():
    readiness = tenant_readiness(
        _tenant(
            TenantPlatform.DATABRICKS,
            binding_mode=BINDING_FEDERATION,
            capabilities={"can_discover": True, "account_admin": True, "user_sync": True},
        )
    )
    assert readiness[Verb.INVOKE.value] == VerbReadiness(verb=Verb.INVOKE, state="ready", reason="")
    assert readiness[Verb.MIRROR.value] == VerbReadiness(verb=Verb.MIRROR, state="ready", reason="")


@pytest.mark.parametrize("binding", [BINDING_INVOKE_UNAVAILABLE, BINDING_SP_SECRET, ""])
def test_databricks_without_federation_refuses_invoke_with_the_existing_copy(binding):
    readiness = tenant_readiness(_tenant(TenantPlatform.DATABRICKS, binding_mode=binding))
    assert readiness[Verb.INVOKE.value] == VerbReadiness(
        verb=Verb.INVOKE, state="unavailable", reason=BINDING_INVOKE_UNAVAILABLE
    )
    assert readiness[Verb.MIRROR.value].state == "unavailable"


def test_databricks_discovery_and_observability_do_not_depend_on_binding_mode():
    for binding in (BINDING_FEDERATION, BINDING_INVOKE_UNAVAILABLE, ""):
        readiness = tenant_readiness(_tenant(TenantPlatform.DATABRICKS, binding_mode=binding))
        assert readiness[Verb.DISCOVER.value].state == "ready"
        assert readiness[Verb.OBSERVABILITY.value].state == "ready"


def test_unprobed_aws_tenant_renders_exactly_as_today():
    # Grandfather rule: no probe stored ⇒ nothing is badged red.
    readiness = tenant_readiness(_tenant(TenantPlatform.AWS, capabilities={}))
    assert readiness[Verb.INVOKE.value] == VerbReadiness(verb=Verb.INVOKE, state="ready", reason="")
    assert readiness[Verb.DEPLOY.value] == VerbReadiness(verb=Verb.DEPLOY, state="ready", reason="")


def test_aws_passing_probe_keeps_deploy_ready():
    readiness = tenant_readiness(
        _tenant(
            TenantPlatform.AWS,
            capabilities={aws_reach_key(AWS_DEPLOY_REACH, "dev"): True},
        )
    )
    assert readiness[Verb.DEPLOY.value] == VerbReadiness(verb=Verb.DEPLOY, state="ready", reason="")


def test_aws_unreachable_deploy_role_names_the_stage_and_the_role_kind():
    readiness = tenant_readiness(
        _tenant(
            TenantPlatform.AWS,
            capabilities={aws_reach_key(AWS_DEPLOY_REACH, "prod"): False},
        )
    )
    assert readiness[Verb.DEPLOY.value] == VerbReadiness(
        verb=Verb.DEPLOY, state="unavailable", reason=REASON_DEPLOY_ROLE_UNREACHABLE, stage="prod"
    )


def test_aws_push_role_reach_is_not_probed_or_derived_from(monkeypatch):
    # The push role trusts GitHub's OIDC provider, never AGP, so a stray
    # ``can_push_reach:*`` key (e.g. a stale record from before this fix) must not influence
    # deploy readiness any more, since nothing can ever write one again.
    readiness = tenant_readiness(
        _tenant(
            TenantPlatform.AWS,
            capabilities={
                "can_push_reach:dev": False,
                aws_reach_key(AWS_DEPLOY_REACH, "dev"): True,
            },
        )
    )
    assert readiness[Verb.DEPLOY.value] == VerbReadiness(verb=Verb.DEPLOY, state="ready", reason="")


def test_aws_deploy_failure_is_deterministic_across_stages():
    # Broken on two stages: the alphabetically-first stage is always named.
    readiness = tenant_readiness(
        _tenant(
            TenantPlatform.AWS,
            capabilities={
                aws_reach_key(AWS_DEPLOY_REACH, "prod"): False,
                aws_reach_key(AWS_DEPLOY_REACH, "dev"): False,
            },
        )
    )
    assert readiness[Verb.DEPLOY.value].reason == REASON_DEPLOY_ROLE_UNREACHABLE
    assert readiness[Verb.DEPLOY.value].stage == "dev"


def test_only_stage_specific_verdicts_carry_a_stage_field():
    readiness = tenant_readiness(
        _tenant(
            TenantPlatform.AWS,
            capabilities={aws_reach_key(AWS_DEPLOY_REACH, "dev"): False},
        )
    )
    assert readiness[Verb.DEPLOY.value].stage == "dev"
    for verb, entry in readiness.items():
        if verb != Verb.DEPLOY.value:
            assert entry.stage is None, verb


def test_aws_probe_failure_does_not_leak_into_other_verbs():
    readiness = tenant_readiness(
        _tenant(
            TenantPlatform.AWS,
            capabilities={aws_reach_key(AWS_DEPLOY_REACH, "dev"): False},
        )
    )
    for verb in (
        Verb.INVOKE,
        Verb.DISCOVER,
        Verb.PUBLISH,
        Verb.GUARDRAILS,
        Verb.MCP_DELIVERY,
        Verb.OBSERVABILITY,
    ):
        assert readiness[verb.value].state == "ready", verb


def test_reason_literals_are_fixed_lowercase_kinds():
    for literal in (REASON_UNSUPPORTED, REASON_DEPLOY_ROLE_UNREACHABLE):
        assert literal == literal.lower()
        assert " " not in literal


def test_unhandled_conditional_verb_is_refused_not_silently_ready(monkeypatch):
    # AWS has no ``conditional`` derivation. Declaring one must FAIL CLOSED: the old trailing
    # ``else`` arm would have rendered this ``ready`` — a capability offered with nothing
    # enforcing it, which is exactly what a refusal-as-allowlist forbids.
    aws = DECLARATIONS[TenantPlatform.AWS]
    verbs = dict(aws.verbs)
    verbs[Verb.GUARDRAILS] = "conditional"
    monkeypatch.setitem(
        platform_registry.DECLARATIONS,
        TenantPlatform.AWS,
        replace(aws, verbs=verbs),
    )
    readiness = tenant_readiness(_tenant(TenantPlatform.AWS))
    assert readiness[Verb.GUARDRAILS.value] == VerbReadiness(
        verb=Verb.GUARDRAILS, state="unavailable", reason=REASON_PROBE_UNREACHABLE
    )


def test_unknown_support_value_raises_instead_of_defaulting(monkeypatch):
    aws = DECLARATIONS[TenantPlatform.AWS]
    verbs: dict[Verb, str] = dict(aws.verbs)
    verbs[Verb.INVOKE] = "maybe"
    monkeypatch.setitem(
        platform_registry.DECLARATIONS,
        TenantPlatform.AWS,
        replace(aws, verbs=verbs),
    )
    with pytest.raises(ValueError) as exc:
        tenant_readiness(_tenant(TenantPlatform.AWS))
    assert str(exc.value) == "verb_support_unknown"


def test_each_call_returns_fresh_entries_that_cannot_leak():
    first = tenant_readiness(_tenant(TenantPlatform.AWS))
    # No two verbs may share one dict object either — a shared "ready" would let one verb's
    # mutation rewrite every other verb's verdict inside the same projection.
    ids = [id(entry) for entry in first.values()]
    assert len(ids) == len(set(ids))

    first[Verb.INVOKE.value].reason = REASON_UNSUPPORTED
    first[Verb.MIRROR.value].state = "ready"

    second = tenant_readiness(_tenant(TenantPlatform.AWS))
    assert second[Verb.INVOKE.value] == VerbReadiness(verb=Verb.INVOKE, state="ready", reason="")
    assert second[Verb.MIRROR.value] == VerbReadiness(verb=Verb.MIRROR, state="not_applicable", reason="")


def test_readiness_never_mutates_the_declaration():
    before = dict(DECLARATIONS[TenantPlatform.AWS].verbs)
    tenant_readiness(_tenant(TenantPlatform.AWS))
    assert dict(DECLARATIONS[TenantPlatform.AWS].verbs) == before


def test_governable_platforms_map_onto_tenant_platforms():
    assert GOVERNABLE_PLATFORMS == {
        Platform.AWS_BEDROCK: TenantPlatform.AWS,
        Platform.DATABRICKS: TenantPlatform.DATABRICKS,
    }


def test_every_tenant_platform_has_a_governable_agent_label():
    assert set(GOVERNABLE_PLATFORMS.values()) == set(TenantPlatform)


def test_the_two_sets_partition_the_agent_platform_enum():
    assert INVENTORY_ONLY_PLATFORMS | set(GOVERNABLE_PLATFORMS) == set(Platform)
    assert not INVENTORY_ONLY_PLATFORMS & set(GOVERNABLE_PLATFORMS)
    assert len(INVENTORY_ONLY_PLATFORMS) == 6


def test_inventory_only_platforms_is_immutable():
    assert isinstance(INVENTORY_ONLY_PLATFORMS, frozenset)


@pytest.mark.parametrize("platform", sorted(p.value for p in Platform))
def test_is_inventory_only_agrees_with_the_sets_for_enum_and_string(platform):
    expected = Platform(platform) in INVENTORY_ONLY_PLATFORMS
    assert is_inventory_only(platform) is expected
    assert is_inventory_only(Platform(platform)) is expected


def test_is_inventory_only_rejects_an_unknown_label():
    with pytest.raises(ValueError):
        is_inventory_only("mainframe")
