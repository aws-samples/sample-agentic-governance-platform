"""``TenantService`` behaviour: validation, the merge semantics of update, platform typing, the
connect-time probe and the write-only credential path.

Persistence stays in memory (``table_name=""``) with an injected clock and id source; the
Databricks tests add a moto Secrets Manager client and a fake probe, so the secret path is
exercised for real while nothing reaches AWS.
"""

from __future__ import annotations

import json as _json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from moto.core.decorator import mock_aws
from seed import seed

from core import aws_clients
from core.state_store import StateStore
from models.tenant import (
    ACCOUNT_ADMIN_ID_KEY,
    ACCOUNT_ADMIN_SECRET_KEY,
    SP_SECRET_KEY,
    DatabricksStageConfig,
    StageConfig,
    Tenant,
    TenantCreate,
    TenantPlatform,
    TenantStageConfig,
    TenantUpdate,
)
from services.tenant_service import TenantError, TenantService

FIXED = datetime(2026, 7, 13, tzinfo=timezone.utc)
LATER = datetime(2026, 7, 14, tzinfo=timezone.utc)


def _svc(ids=None, now=None, aws_reach_prober=None):
    ids = iter(ids or ["ten-aaaa0001", "ten-aaaa0002", "ten-aaaa0003"])
    return TenantService(
        table_name="",
        region="us-east-1",
        aws_reach_prober=aws_reach_prober or (lambda role_arn, region: (True, "")),
        new_id=lambda: next(ids),
        now=now or (lambda: FIXED),
    )


def _stages(dev="111111111111", prod="222222222222", region="us-east-1"):
    return {
        "dev": TenantStageConfig(account_id=dev, region=region),
        "prod": TenantStageConfig(account_id=prod, region=region),
    }


def _create_body(**overrides) -> TenantCreate:
    body: dict[str, Any] = {
        "name": "Retail Banking",
        "line_of_business": "Consumer",
        "entra_group_ids": ["grp-1"],
        "stages": _stages(),
    }
    body.update(overrides)
    return TenantCreate(**body)


def _as_aws(stage: StageConfig) -> TenantStageConfig:
    assert isinstance(stage, TenantStageConfig)
    return stage


def _as_dbx(stage: StageConfig) -> DatabricksStageConfig:
    assert isinstance(stage, DatabricksStageConfig)
    return stage


def test_create_round_trip_and_timestamps():
    svc = _svc()
    t = svc.create(
        _create_body(description="EMEA retail", stages=_stages(region="eu-west-1")),
        created_by="admin@example.com",
    )

    assert t.id == "ten-aaaa0001"
    assert t.name == "Retail Banking"
    assert t.line_of_business == "Consumer"
    assert t.entra_group_ids == ["grp-1"]
    assert t.stages["dev"].account_id == "111111111111"
    assert t.stages["prod"].account_id == "222222222222"
    assert t.stages["dev"].region == "eu-west-1"
    assert t.description == "EMEA retail"
    assert t.created_by == "admin@example.com"
    assert t.created_at == FIXED.isoformat()
    assert t.updated_at == FIXED.isoformat()

    assert svc.get("ten-aaaa0001") == t
    assert [x.id for x in svc.list()] == ["ten-aaaa0001"]
    assert "pk" not in t.model_dump() and "sk" not in t.model_dump()


def test_create_defaults_region_and_description():
    svc = _svc()
    t = svc.create(
        _create_body(
            stages={
                "dev": TenantStageConfig(account_id="111111111111"),
                "prod": TenantStageConfig(account_id="222222222222"),
            }
        ),
        created_by="a@b.com",
    )
    assert t.stages["dev"].region == "us-east-1"
    assert t.description == ""


def test_create_rejects_empty_entra_group_ids():
    svc = _svc()
    with pytest.raises(TenantError) as ei:
        svc.create(_create_body(entra_group_ids=[]), created_by="a@b.com")
    assert ei.value.kind == "validation"
    assert svc.list() == []


def test_create_accepts_arbitrary_stage_names():
    """Stages are an open axis: a tenant may carry ``uat`` or any other stage name."""
    svc = _svc()
    t = svc.create(
        _create_body(stages={"uat": TenantStageConfig(account_id="333333333333")}),
        created_by="a@b.com",
    )
    assert set(t.stages) == {"uat"}
    assert t.stages["uat"].account_id == "333333333333"
    assert svc.get(t.id).stages["uat"].account_id == "333333333333"


def test_create_requires_at_least_one_stage():
    svc = _svc()
    with pytest.raises(TenantError) as ei:
        svc.create(_create_body(stages={}), created_by="a@b.com")
    assert ei.value.kind == "validation"
    assert svc.list() == []


def test_create_rejects_bad_account_in_stage():
    svc = _svc()
    with pytest.raises(TenantError) as ei:
        svc.create(
            _create_body(
                stages={
                    "dev": TenantStageConfig(account_id="bad"),
                    "prod": TenantStageConfig(account_id="222222222222"),
                }
            ),
            created_by="a@b.com",
        )
    assert ei.value.kind == "validation"
    assert svc.list() == []


def test_create_rejects_bad_account_in_arbitrary_stage():
    """An arbitrary stage name is still held to the 12-digit account-id rule."""
    svc = _svc()
    with pytest.raises(TenantError) as ei:
        svc.create(
            _create_body(stages={"uat": TenantStageConfig(account_id="12345")}),
            created_by="a@b.com",
        )
    assert ei.value.kind == "validation"
    assert svc.list() == []


def test_create_rejects_non_numeric_prod_account():
    svc = _svc()
    with pytest.raises(TenantError) as ei:
        svc.create(
            _create_body(
                stages={
                    "dev": TenantStageConfig(account_id="111111111111"),
                    "prod": TenantStageConfig(account_id="12345678901x"),
                }
            ),
            created_by="a@b.com",
        )
    assert ei.value.kind == "validation"
    assert svc.list() == []


_INJECTION_SHAPED_ROLE_ARNS = [
    'arn:aws:iam::111111111111:role/"; rm -rf / ; echo "/x',
    'arn:aws:iam::111111111111:role/a"}]}/x',
    "arn:aws:iam::111111111111:role/${var.anything}/x",
    "arn:aws:iam::111111111111:role/" + "p" * 300 + "/x",
    "arn:aws:iam::111111111111:role/agp-deployment-x\n",
]


@pytest.mark.parametrize(
    "bad",
    [
        "not-an-arn",
        "agp-deployment-dev",
        "arn:aws:iam::123:role/agp-deployment-dev",
        "arn:aws:iam::1111111111111:role/x",
        "arn:aws:iam::111111111111:role/",
        "arn:aws:iam::111111111111:user/someone",
        "arn:aws:sts::111111111111:assumed-role/x/y",
        "arn:aws:iam::111111111111:role/bad name",
        " arn:aws:iam::111111111111:role/agp-deployment-dev",
        "arn:aws:iam::111111111111:role/arn:aws:iam::999999999999:role/x",
        *_INJECTION_SHAPED_ROLE_ARNS,
    ],
)
def test_create_rejects_malformed_deploy_role_arn(bad):
    svc = _svc()
    with pytest.raises(TenantError) as ei:
        svc.create(
            _create_body(stages={"dev": TenantStageConfig(account_id="111111111111", deploy_role_arn=bad)}),
            created_by="a@b.com",
        )
    assert ei.value.kind == "validation"
    assert svc.list() == []


def test_create_rejects_malformed_deploy_role_arn_in_arbitrary_stage():
    """The rule follows the open stage axis, like the account-id rule."""
    svc = _svc()
    with pytest.raises(TenantError) as ei:
        svc.create(
            _create_body(
                stages={"uat": TenantStageConfig(account_id="333333333333", deploy_role_arn="nope")}
            ),
            created_by="a@b.com",
        )
    assert ei.value.kind == "validation"
    assert svc.list() == []


def test_create_deploy_role_arn_error_names_the_stage_and_no_account_id():
    """The message is the operator's only clue, and the hard project rule bans an
    account id in any message/log — so it names the STAGE and the expected FORMAT,
    never the value it rejected (which is where the account id lives)."""
    svc = _svc()
    bad = "arn:aws:iam::999999999999:role"
    with pytest.raises(TenantError) as ei:
        svc.create(
            _create_body(stages={"prod": TenantStageConfig(account_id="222222222222", deploy_role_arn=bad)}),
            created_by="a@b.com",
        )
    assert "prod" in ei.value.message
    assert "deploy_role_arn" in ei.value.message
    assert "999999999999" not in ei.value.message


@pytest.mark.parametrize(
    "ok",
    [
        "",
        "arn:aws:iam::111111111111:role/agp-deployment-acme-default",
        "arn:aws:iam::111111111111:role/tenants/emea/agp-deployment-x",
        "arn:aws:iam::111111111111:role/service-role/foo",
        "arn:aws-us-gov:iam::111111111111:role/agp-deployment-x",
        "arn:aws-cn:iam::111111111111:role/agp-deployment-x",
        "arn:aws:iam::111111111111:role/Deploy+Role=1,2.3@x-_",
    ],
)
def test_create_accepts_well_formed_or_empty_deploy_role_arn(ok):
    svc = _svc()
    t = svc.create(
        _create_body(stages={"dev": TenantStageConfig(account_id="111111111111", deploy_role_arn=ok)}),
        created_by="a@b.com",
    )
    assert _as_aws(svc.get(t.id).stages["dev"]).deploy_role_arn == ok


def test_accepted_deploy_role_arns_are_exactly_what_the_assume_seam_can_parse():
    """The write rule and ``tenant_credentials``' parse are ONE contract: anything this
    service stores must yield a role NAME at teardown, or the seam falls back to the
    generic ``deploy role (malformed ARN)`` label on a value that got past validation."""
    from services.tenant_credentials import _ROLE_ARN_RE, _safe_role_label

    stored = "arn:aws:iam::111111111111:role/tenants/emea/agp-deployment-x"
    svc = _svc()
    svc.create(
        _create_body(stages={"dev": TenantStageConfig(account_id="111111111111", deploy_role_arn=stored)}),
        created_by="a@b.com",
    )
    assert _ROLE_ARN_RE.match(stored) is not None
    assert _safe_role_label(stored) == "agp-deployment-x"


def test_the_two_copies_of_the_role_arn_pattern_are_byte_identical():
    """Pattern equality, not a spot check on one accepted value: changing either copy alone must
    fail here rather than at a teardown six months on."""
    import re

    from services.tenant_credentials import _ROLE_ARN_RE as seam_re
    from services.tenant_service import _ROLE_ARN_RE as write_re

    assert re.sub(r"\(\?P<name>([^)]*)\)", r"\1", seam_re.pattern) == write_re.pattern


@pytest.mark.parametrize("bad", _INJECTION_SHAPED_ROLE_ARNS)
def test_the_assume_seam_also_refuses_injection_shaped_paths(bad):
    """The reject boundary is shared too: a value the write rule refuses must not parse into a
    plausible role label downstream."""
    from services.tenant_credentials import _ROLE_ARN_RE, _safe_role_label

    assert _ROLE_ARN_RE.match(bad) is None
    assert _safe_role_label(bad) == "deploy role (malformed ARN)"


def test_create_persists_nested_stages():
    svc = _svc()
    t = svc.create(_create_body(), created_by="a@b.com")
    got = svc.get(t.id)
    assert got.stages["dev"].account_id == "111111111111"
    assert got.stages["prod"].account_id == "222222222222"


def test_create_rejects_duplicate_name():
    svc = _svc()
    svc.create(_create_body(), created_by="a@b.com")
    with pytest.raises(TenantError) as ei:
        svc.create(_create_body(), created_by="a@b.com")
    assert ei.value.kind == "name_taken"
    assert [t.id for t in svc.list()] == ["ten-aaaa0001"]


def test_get_unknown_raises_not_found():
    svc = _svc()
    with pytest.raises(TenantError) as ei:
        svc.get("nope")
    assert ei.value.kind == "not_found"


def test_update_partial_merges_and_bumps_updated_at():
    clock = iter([FIXED, LATER])
    svc = _svc(now=lambda: next(clock))
    t = svc.create(_create_body(), created_by="a@b.com")

    updated = svc.update(t.id, TenantUpdate(description="EU retail", line_of_business="EU"))

    assert updated.description == "EU retail"
    assert updated.line_of_business == "EU"
    assert updated.name == "Retail Banking"
    assert updated.entra_group_ids == ["grp-1"]
    assert updated.stages["dev"].account_id == "111111111111"
    assert updated.created_by == "a@b.com"
    assert updated.created_at == FIXED.isoformat()
    assert updated.updated_at == LATER.isoformat()
    assert svc.get(t.id).description == "EU retail"
    assert svc.get(t.id).updated_at == LATER.isoformat()


def test_update_unknown_raises_not_found():
    svc = _svc()
    with pytest.raises(TenantError) as ei:
        svc.update("ghost", TenantUpdate(name="x"))
    assert ei.value.kind == "not_found"


def test_update_explicit_none_leaves_field_unchanged():
    clock = iter([FIXED, LATER])
    svc = _svc(now=lambda: next(clock))
    t = svc.create(_create_body(), created_by="a@b.com")

    updated = svc.update(t.id, TenantUpdate(name=None, description="EU retail"))

    assert updated.name == "Retail Banking"
    assert updated.description == "EU retail"
    assert svc.get(t.id).name == "Retail Banking"
    assert svc.get(t.id).updated_at == LATER.isoformat()


def test_update_rejects_bad_account_id():
    svc = _svc()
    t = svc.create(_create_body(), created_by="a@b.com")
    with pytest.raises(TenantError) as ei:
        svc.update(
            t.id,
            TenantUpdate(
                stages={
                    "dev": TenantStageConfig(account_id="123"),
                    "prod": TenantStageConfig(account_id="222222222222"),
                }
            ),
        )
    assert ei.value.kind == "validation"
    assert svc.get(t.id).stages["dev"].account_id == "111111111111"


def test_update_rejects_malformed_deploy_role_arn():
    """``update`` re-validates the merged stage map, so a bad ARN on a submitted stage is refused
    and the stored record is left alone."""
    svc = _svc()
    t = svc.create(_create_body(), created_by="a@b.com")
    with pytest.raises(TenantError) as ei:
        svc.update(
            t.id,
            TenantUpdate(
                stages={
                    "dev": TenantStageConfig(account_id="111111111111", deploy_role_arn="agp-deployment-dev"),
                }
            ),
        )
    assert ei.value.kind == "validation"
    assert _as_aws(svc.get(t.id).stages["dev"]).deploy_role_arn == ""
    assert set(svc.get(t.id).stages) == {"dev", "prod"}


def test_update_can_still_clear_a_deploy_role_arn():
    """Clearing the ARN is how an operator moves a stage back to deploy-in-place
    (the Warnings callout in docs/tenant-account-onboarding.md tells them to), so the
    new rule must not turn "" into a validation error."""
    svc = _svc()
    t = svc.create(
        _create_body(
            stages={
                "dev": TenantStageConfig(
                    account_id="111111111111",
                    deploy_role_arn="arn:aws:iam::111111111111:role/agp-deployment-dev",
                )
            }
        ),
        created_by="a@b.com",
    )
    upd = svc.update(
        t.id,
        TenantUpdate(
            stages={
                "dev": TenantStageConfig(account_id="111111111111"),
            }
        ),
    )
    assert _as_aws(upd.stages["dev"]).deploy_role_arn == ""
    assert _as_aws(svc.get(t.id).stages["dev"]).deploy_role_arn == ""


def test_update_replaces_only_the_submitted_stages():
    svc = _svc()
    t = svc.create(
        _create_body(
            stages={
                "dev": TenantStageConfig(
                    account_id="111111111111", deploy_role_arn="arn:aws:iam::111111111111:role/old-deploy"
                ),
                "prod": TenantStageConfig(account_id="222222222222"),
            }
        ),
        created_by="a@b.com",
    )

    upd = svc.update(t.id, TenantUpdate(stages={"dev": TenantStageConfig(account_id="333333333333")}))

    assert upd.stages["dev"].account_id == "333333333333"
    assert _as_aws(upd.stages["dev"]).deploy_role_arn == ""
    assert upd.stages["prod"].account_id == "222222222222"
    assert svc.get(t.id).stages["dev"].account_id == "333333333333"
    assert _as_aws(svc.get(t.id).stages["dev"]).deploy_role_arn == ""


def test_update_with_single_stage_body_preserves_other_stages():
    svc = _svc()
    t = svc.create(_create_body(), created_by="a@b.com")
    new_dev = TenantStageConfig(account_id="333333333333", region="eu-west-1")

    updated = svc.update(t.id, TenantUpdate(stages={"dev": new_dev}))

    assert set(updated.stages) == {"dev", "prod"}
    assert updated.stages["dev"] == new_dev
    assert updated.stages["prod"].account_id == "222222222222"
    assert set(svc.get(t.id).stages) == {"dev", "prod"}


def test_update_rename_to_existing_name_raises_name_taken():
    svc = _svc()
    first = svc.create(_create_body(name="Retail Banking"), created_by="a@b.com")
    second = svc.create(_create_body(name="Wholesale Banking"), created_by="a@b.com")
    with pytest.raises(TenantError) as ei:
        svc.update(second.id, TenantUpdate(name="Retail Banking"))
    assert ei.value.kind == "name_taken"
    assert svc.get(second.id).name == "Wholesale Banking"
    assert svc.get(first.id).name == "Retail Banking"


def test_update_rename_to_own_name_is_allowed():
    clock = iter([FIXED, LATER])
    svc = _svc(now=lambda: next(clock))
    t = svc.create(_create_body(name="Retail Banking"), created_by="a@b.com")

    updated = svc.update(t.id, TenantUpdate(name="Retail Banking", description="x"))

    assert updated.name == "Retail Banking"
    assert updated.description == "x"
    assert updated.updated_at == LATER.isoformat()


def test_delete_removes_tenant():
    svc = _svc()
    t = svc.create(_create_body(), created_by="a@b.com")
    svc.delete(t.id)
    assert svc.list() == []
    with pytest.raises(TenantError) as ei:
        svc.get(t.id)
    assert ei.value.kind == "not_found"


def test_an_empty_table_name_selects_the_in_memory_store():
    assert _svc()._store.in_memory is True


def test_the_record_is_stored_under_the_tenant_partition_keyed_by_id():
    svc = _svc()
    t = svc.create(_create_body(), created_by="a@b.com")
    rows = svc._store.query("tenant")
    assert [(r["pk"], r["sk"]) for r in rows] == [("tenant", t.id)]
    assert svc._from_item(rows[0]) == t


def test_reads_degrade_on_a_store_fault_and_writes_propagate_it():
    svc = _svc()
    boom = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Query")
    table = MagicMock()
    table.get_item.side_effect = boom
    table.query.side_effect = boom
    table.put_item.side_effect = ClientError({"Error": {"Code": "AccessDeniedException"}}, "PutItem")
    svc._store = StateStore("tenants", table=table)

    assert svc._store.in_memory is False
    assert svc.list() == []
    with pytest.raises(TenantError) as ei:
        svc.get("ten-aaaa0001")
    assert ei.value.kind == "not_found"
    with pytest.raises(ClientError):
        svc.upsert_seed(_seed_tenant())


def _seed_tenant() -> Tenant:
    return Tenant(
        id="default",
        name="Default Tenant",
        line_of_business="Platform",
        entra_group_ids=["grp-default"],
        stages={
            "dev": TenantStageConfig(account_id="111111111111"),
            "prod": TenantStageConfig(account_id="222222222222"),
        },
        description="Seed tenant",
        created_by="seed",
        created_at=FIXED.isoformat(),
        updated_at=FIXED.isoformat(),
    )


def test_service_reads_legacy_flat_record_via_hydration():
    svc = TenantService(table_name="")
    legacy_item = {
        "pk": "tenant",
        "sk": "default",
        "id": "default",
        "name": "Default",
        "line_of_business": "Platform",
        "entra_group_ids": ["g1"],
        "aws_account_dev": "111111111111",
        "aws_account_prod": "222222222222",
        "aws_region": "us-east-1",
        "description": "",
        "created_by": "u",
        "created_at": "t",
        "updated_at": "t",
    }
    t = svc._from_item(legacy_item)
    assert t.stages["dev"].account_id == "111111111111"


def test_upsert_seed_is_idempotent():
    svc = _svc()
    first = svc.upsert_seed(_seed_tenant())
    second = svc.upsert_seed(_seed_tenant())

    assert first.id == "default" and second.id == "default"
    tenants = svc.list()
    assert len(tenants) == 1
    assert tenants[0].id == "default"
    assert svc.get("default").name == "Default Tenant"


def test_upsert_seed_creates_once_then_rewires_the_existing_row_versioned():
    svc = _svc()
    svc.upsert_seed(_seed_tenant())
    row = svc._store.get(("tenant", "default"))
    assert row is not None
    assert row["row_version"] == 1

    svc.upsert_seed(_seed_tenant().model_copy(update={"name": "Rewired"}))

    row = svc._store.get(("tenant", "default"))
    assert row is not None
    assert (row["row_version"], row["name"]) == (2, "Rewired")


def test_a_tenant_row_written_before_versioning_takes_one_versioned_update():
    svc = _svc()
    seed(svc._store, {"pk": "tenant", "sk": "default", **_seed_tenant().model_dump(mode="json")})

    svc.update("default", TenantUpdate(description="first"))
    row = svc._store.get(("tenant", "default"))
    assert row is not None
    assert row["row_version"] == 1
    svc.update("default", TenantUpdate(description="second"))

    row = svc._store.get(("tenant", "default"))
    assert row is not None
    assert (row["row_version"], row["description"]) == (2, "second")


def test_a_create_over_a_taken_id_answers_the_store_fault_and_leaves_the_stored_tenant_alone():
    svc = _svc(ids=["ten-aaaa0001", "ten-aaaa0001"])
    first = svc.create(_create_body(), created_by="a@b.com")

    with pytest.raises(TenantError) as ei:
        svc.create(_create_body(name="Wealth"), created_by="a@b.com")

    assert ei.value.kind == "secret_error"
    assert svc.get(first.id).name == "Retail Banking"
    row = svc._store.get(("tenant", first.id))
    assert row is not None
    assert row["row_version"] == 1


_CHANGED = {"detail": "Tenant changed while this request ran; reload and retry", "code": "tenant.changed"}
_ROLE_ARN = "arn:aws:iam::111111111111:role/deploy"


def _tenant_whose_reach_probe_runs(race: list) -> tuple[TenantService, Tenant]:
    def prober(role_arn, region):
        while race:
            race.pop()()
        return True, ""

    svc = _svc(aws_reach_prober=prober)
    stages = {"dev": TenantStageConfig(account_id="111111111111", deploy_role_arn=_ROLE_ARN)}
    return svc, svc.create(_create_body(stages=stages), created_by="a@b.com")


def test_two_updates_that_read_the_same_tenant_cannot_both_land():
    race: list = []
    svc, t = _tenant_whose_reach_probe_runs(race)
    race.append(lambda: svc.update(t.id, TenantUpdate(description="written by B")))

    with pytest.raises(TenantError) as ei:
        svc.update(t.id, TenantUpdate(description="written by A"))

    assert (ei.value.kind, ei.value.status_code, ei.value.body()) == ("changed", 409, _CHANGED)
    assert svc.get(t.id).description == "written by B"


def test_an_update_that_raced_a_delete_answers_changed_and_does_not_resurrect_the_tenant():
    race: list = []
    svc, t = _tenant_whose_reach_probe_runs(race)
    race.append(lambda: svc.delete(t.id))

    with pytest.raises(TenantError) as ei:
        svc.update(t.id, TenantUpdate(description="edited"))

    assert (ei.value.kind, ei.value.status_code, ei.value.body()) == ("changed", 409, _CHANGED)
    assert svc._store.get(("tenant", t.id)) is None
    assert svc.list() == []


def test_an_upsert_seed_that_lost_its_create_answers_changed_and_keeps_the_winner(monkeypatch):
    svc = _svc()
    read = svc._store.get

    def read_then_compete(key, **kwargs):
        row = read(key, **kwargs)
        monkeypatch.setattr(svc._store, "get", read)
        winner = _seed_tenant().model_copy(update={"name": "Winner"}).model_dump(mode="json")
        seed(svc._store, {"pk": "tenant", "sk": "default", "row_version": 1, **winner})
        return row

    monkeypatch.setattr(svc._store, "get", read_then_compete)

    with pytest.raises(TenantError) as ei:
        svc.upsert_seed(_seed_tenant())

    assert (ei.value.kind, ei.value.status_code, ei.value.body()) == ("changed", 409, _CHANGED)
    row = svc._store.get(("tenant", "default"))
    assert row is not None
    assert (row["row_version"], row["name"]) == (1, "Winner")


def test_the_reads_that_feed_a_versioned_save_are_strongly_consistent_and_a_get_is_not():
    svc = _svc()
    t = svc.create(_create_body(), created_by="a@b.com")
    svc.upsert_seed(_seed_tenant())
    table = MagicMock(wraps=svc._store._table)
    svc._store = StateStore("tenants", table=table)

    svc.get(t.id)
    svc.update(t.id, TenantUpdate(description="edited"))
    svc.reprobe(t.id)
    svc.upsert_seed(_seed_tenant())

    reads = [c.kwargs["ConsistentRead"] for c in table.get_item.call_args_list]
    assert reads == [False, True, True, True]


WS_URL = "https://dbc-test.cloud.databricks.com"


def _dbx_stage(**overrides) -> DatabricksStageConfig:
    body: dict[str, Any] = {
        "workspace_url": WS_URL,
        "workspace_id": "1234567890123456",
        "sp_client_id": "sp-abc",
    }
    body.update(overrides)
    return DatabricksStageConfig(**body)


def _dbx_body(**overrides) -> TenantCreate:
    body: dict[str, Any] = {
        "name": "Databricks LoB",
        "line_of_business": "Analytics",
        "entra_group_ids": ["grp-1"],
        "platform": TenantPlatform.DATABRICKS,
        "stages": {"dev": _dbx_stage()},
    }
    body.update(overrides)
    return TenantCreate(**body)


def test_create_defaults_to_aws_platform():
    """An AWS create body carries no ``platform``; the record reads "aws" and, with no deploy role
    to probe, the capability fields stay empty."""
    svc = _svc()
    t = svc.create(_create_body(), created_by="a@b.com")
    assert t.platform == TenantPlatform.AWS
    assert t.capabilities == {} and t.binding_mode == ""
    assert svc.get(t.id).platform == "aws"


def test_aws_tenant_still_rejects_databricks_stage_shape():
    """The 12-digit rule is scoped to AWS tenants — it must not become skippable by
    submitting a Databricks-shaped stage on an AWS tenant."""
    svc = _svc()
    with pytest.raises(TenantError) as ei:
        svc.create(_create_body(stages={"dev": _dbx_stage()}), created_by="a@b.com")
    assert ei.value.kind == "validation"
    assert svc.list() == []


def test_create_databricks_tenant_round_trip():
    svc = _svc()
    t = svc.create(_dbx_body(), created_by="admin@example.com")
    assert t.platform == TenantPlatform.DATABRICKS
    assert _as_dbx(t.stages["dev"]).workspace_url == WS_URL
    assert _as_dbx(t.stages["dev"]).sp_client_id == "sp-abc"
    assert _as_dbx(t.stages["dev"]).sp_client_secret_arn == ""
    got = svc.get(t.id)
    assert isinstance(got.stages["dev"], DatabricksStageConfig)
    assert got.platform == "databricks"


def test_create_databricks_accepts_workspace_id_zero():
    """``"0"`` is a REAL workspace id (the URL carries no ``o=``) — it must not be
    mistaken for "unset" and rejected."""
    svc = _svc()
    t = svc.create(_dbx_body(stages={"dev": _dbx_stage(workspace_id="0")}), created_by="a@b.com")
    assert _as_dbx(t.stages["dev"]).workspace_id == "0"


@pytest.mark.parametrize(
    "hostile_url",
    [
        "javascript:alert(1)",
        f"{WS_URL}/",
        "http://dbc-test.cloud.databricks.com",
        "https://dbc test.cloud.databricks.com",
        f"{WS_URL}\njavascript:x",
        f"{WS_URL}/api/2.0/apps",
        "HTTPS://DBC-TEST.CLOUD.DATABRICKS.COM",
        "https://user:pass@evil.example.com",
        "",
    ],
)
def test_create_databricks_rejects_hostile_workspace_url(hostile_url):
    svc = _svc()
    with pytest.raises(TenantError) as ei:
        svc.create(_dbx_body(stages={"dev": _dbx_stage(workspace_url=hostile_url)}), created_by="a@b.com")
    assert ei.value.kind == "validation"
    assert svc.list() == []


def test_create_databricks_requires_sp_client_id():
    svc = _svc()
    with pytest.raises(TenantError) as ei:
        svc.create(_dbx_body(stages={"dev": _dbx_stage(sp_client_id="")}), created_by="a@b.com")
    assert ei.value.kind == "validation"
    assert svc.list() == []


@pytest.mark.parametrize("bad_id", ["abc", "12a", "-1", "1.5", "", "12\n", "١٢٣"])
def test_create_databricks_rejects_non_digit_workspace_id(bad_id):
    svc = _svc()
    with pytest.raises(TenantError) as ei:
        svc.create(_dbx_body(stages={"dev": _dbx_stage(workspace_id=bad_id)}), created_by="a@b.com")
    assert ei.value.kind == "validation"
    assert svc.list() == []


def test_create_databricks_rejects_aws_stage_shape():
    svc = _svc()
    with pytest.raises(TenantError) as ei:
        svc.create(
            _dbx_body(stages={"dev": TenantStageConfig(account_id="111111111111")}), created_by="a@b.com"
        )
    assert ei.value.kind == "validation"
    assert svc.list() == []


def test_create_databricks_validates_every_stage_not_just_the_first():
    svc = _svc()
    with pytest.raises(TenantError) as ei:
        svc.create(
            _dbx_body(
                stages={"dev": _dbx_stage(), "prod": _dbx_stage(workspace_url="http://insecure.example.com")}
            ),
            created_by="a@b.com",
        )
    assert ei.value.kind == "validation"
    assert svc.list() == []


def test_create_databricks_still_requires_a_group_and_a_stage():
    svc = _svc()
    with pytest.raises(TenantError) as ei:
        svc.create(_dbx_body(entra_group_ids=[]), created_by="a@b.com")
    assert ei.value.kind == "validation"
    with pytest.raises(TenantError) as ei:
        svc.create(_dbx_body(stages={}), created_by="a@b.com")
    assert ei.value.kind == "validation"


def test_update_databricks_stages_succeeds_with_matching_shape():
    clock = iter([FIXED, LATER])
    svc = _svc(now=lambda: next(clock))
    t = svc.create(_dbx_body(), created_by="a@b.com")

    upd = svc.update(t.id, TenantUpdate(stages={"dev": _dbx_stage(workspace_id="7")}))

    assert _as_dbx(upd.stages["dev"]).workspace_id == "7"
    assert upd.platform == TenantPlatform.DATABRICKS
    assert _as_dbx(svc.get(t.id).stages["dev"]).workspace_id == "7"


def test_update_cannot_swap_an_aws_tenant_to_databricks_stages():
    """Platform immutability, enforced where it matters: an AWS tenant cannot be re-shaped
    into a Databricks one via a stages update (which would leave ``platform == "aws"``
    governing Databricks config)."""
    svc = _svc()
    t = svc.create(_create_body(), created_by="a@b.com")
    with pytest.raises(TenantError) as ei:
        svc.update(t.id, TenantUpdate(stages={"dev": _dbx_stage()}))
    assert ei.value.kind == "validation"
    stored = svc.get(t.id)
    assert stored.platform == TenantPlatform.AWS
    assert stored.stages["dev"].account_id == "111111111111"


def test_update_cannot_swap_a_databricks_tenant_to_aws_stages():
    svc = _svc()
    t = svc.create(_dbx_body(), created_by="a@b.com")
    with pytest.raises(TenantError) as ei:
        svc.update(t.id, TenantUpdate(stages={"dev": TenantStageConfig(account_id="111111111111")}))
    assert ei.value.kind == "validation"
    stored = svc.get(t.id)
    assert stored.platform == TenantPlatform.DATABRICKS
    assert _as_dbx(stored.stages["dev"]).workspace_url == WS_URL


def test_update_rejects_hostile_workspace_url():
    svc = _svc()
    t = svc.create(_dbx_body(), created_by="a@b.com")
    with pytest.raises(TenantError) as ei:
        svc.update(t.id, TenantUpdate(stages={"dev": _dbx_stage(workspace_url=f"{WS_URL}/")}))
    assert ei.value.kind == "validation"
    assert _as_dbx(svc.get(t.id).stages["dev"]).workspace_url == WS_URL


def test_update_ignores_a_platform_key_in_the_body():
    """``TenantUpdate`` has no ``platform`` field, so a client sending one is ignored
    (never obeyed, never a 500) — the stored platform survives."""
    clock = iter([FIXED, LATER])
    svc = _svc(now=lambda: next(clock))
    t = svc.create(_dbx_body(), created_by="a@b.com")

    upd = svc.update(t.id, TenantUpdate.model_validate({"platform": "aws", "description": "x"}))

    assert upd.platform == TenantPlatform.DATABRICKS
    assert upd.description == "x"
    assert svc.get(t.id).platform == "databricks"


def test_update_non_stage_fields_on_a_databricks_tenant_revalidates_stored_stages():
    """A metadata-only update re-runs validation over the STORED stages — so it must not
    trip the AWS 12-digit rule on a Databricks tenant."""
    clock = iter([FIXED, LATER])
    svc = _svc(now=lambda: next(clock))
    t = svc.create(_dbx_body(), created_by="a@b.com")
    upd = svc.update(t.id, TenantUpdate(line_of_business="Risk"))
    assert upd.line_of_business == "Risk"
    assert _as_dbx(upd.stages["dev"]).workspace_url == WS_URL


def test_service_reads_pre_e29_record_as_aws_platform():
    svc = TenantService(table_name="")
    pre_e29_item = {
        "pk": "tenant",
        "sk": "ten-old",
        "id": "ten-old",
        "name": "Retail",
        "line_of_business": "Consumer",
        "entra_group_ids": ["g1"],
        "stages": {"dev": {"account_id": "111111111111", "region": "us-east-1"}},
        "description": "",
        "created_by": "u",
        "created_at": "t",
        "updated_at": "t",
    }
    t = svc._from_item(pre_e29_item)
    assert t.platform == TenantPlatform.AWS
    assert t.capabilities == {} and t.binding_mode == ""
    assert isinstance(t.stages["dev"], TenantStageConfig)


def test_service_round_trips_a_databricks_item():
    svc = TenantService(table_name="")
    item = {
        "pk": "tenant",
        "sk": "ten-d",
        "id": "ten-d",
        "name": "DBX",
        "line_of_business": "Analytics",
        "entra_group_ids": ["g1"],
        "platform": "databricks",
        "stages": {
            "dev": {"workspace_url": WS_URL, "workspace_id": "0", "cloud": "aws", "sp_client_id": "sp-abc"}
        },
        "capabilities": {"can_discover": True, "account_admin": False},
        "binding_mode": "sp_secret",
        "description": "",
        "created_by": "u",
        "created_at": "t",
        "updated_at": "t",
    }
    t = svc._from_item(item)
    assert t.platform == TenantPlatform.DATABRICKS
    assert isinstance(t.stages["dev"], DatabricksStageConfig)
    assert t.capabilities == {"can_discover": True, "account_admin": False}
    assert t.binding_mode == "sp_secret"
    assert svc._to_item(t)["platform"] == "databricks"


DBX_ACCOUNT = "12345678-1234-1234-1234-123456789abc"
SECRET_PREFIX = "agp-test/databricks-tenants/"


class _FakeProbe:
    """A stand-in for ``DatabricksWorkspaceService.probe_capabilities`` that records every call.

    Strict like the real probe: the account-level flags are forced False unless both account-admin
    halves are supplied, since without them no account token can be minted. A fake more generous
    than reality makes tests that cannot fail."""

    def __init__(self, result=None, error=None):
        self.result = result or {"can_discover": True, "account_admin": False, "user_sync": False}
        self.error = error
        self.calls: list[dict] = []

    async def probe_capabilities(
        self,
        workspace_url,
        account_id,
        client_id,
        client_secret,
        account_admin_client_id=None,
        account_admin_secret=None,
    ):
        self.calls.append(
            {
                "workspace_url": workspace_url,
                "account_id": account_id,
                "client_id": client_id,
                "client_secret": client_secret,
                "account_admin_client_id": account_admin_client_id,
                "account_admin_secret": account_admin_secret,
            }
        )
        if self.error:
            raise self.error
        result = dict(self.result)
        if not (account_admin_client_id and account_admin_secret):
            result["account_admin"] = False
            result["user_sync"] = False
        return result


def _dbx_svc(probe=None, ids=None, now=None) -> TenantService:
    """A ``TenantService`` with a fake probe and a moto Secrets Manager client; persistence stays
    in memory."""
    ids = iter(ids or ["ten-dbx0001", "ten-dbx0002", "ten-dbx0003"])
    return TenantService(
        table_name="",
        region="us-east-1",
        secret_prefix=SECRET_PREFIX,
        workspace=probe or _FakeProbe(),
        secrets_client=aws_clients.client("secretsmanager", region="us-east-1"),
        new_id=lambda: next(ids),
        now=now or (lambda: FIXED),
    )


def _dbx_create(**overrides) -> TenantCreate:
    body: dict[str, Any] = {
        "name": "Databricks LoB",
        "line_of_business": "Analytics",
        "entra_group_ids": ["grp-1"],
        "platform": TenantPlatform.DATABRICKS,
        "stages": {"dev": _dbx_stage(account_id=DBX_ACCOUNT, sp_client_secret="s3cr3t")},
    }
    body.update(overrides)
    return TenantCreate(**body)


def _sm():
    return aws_clients.client("secretsmanager", region="us-east-1")


@mock_aws
def test_create_databricks_runs_the_probe_with_the_stage_credential():
    probe = _FakeProbe()
    svc = _dbx_svc(probe)
    svc.create(_dbx_create(), created_by="a@b.com")
    assert len(probe.calls) == 1
    call = probe.calls[0]
    assert call["workspace_url"] == WS_URL
    assert call["account_id"] == DBX_ACCOUNT
    assert (call["client_id"], call["client_secret"]) == ("sp-abc", "s3cr3t")
    assert call["account_admin_client_id"] in (None, "")


@mock_aws
def test_create_databricks_persists_the_probed_capabilities():
    probe = _FakeProbe({"can_discover": True, "account_admin": False, "user_sync": False})
    svc = _dbx_svc(probe)
    t = svc.create(_dbx_create(), created_by="a@b.com")
    assert t.capabilities == {"can_discover": True, "account_admin": False, "user_sync": False}
    assert svc.get(t.id).capabilities["can_discover"] is True


@mock_aws
def test_binding_mode_is_federation_only_when_account_admin_and_user_sync_both_hold():
    """The account-admin PAIR must be supplied for the account-level flags to be reachable at
    all — the strict ``_FakeProbe`` enforces that, so this test now has to pass a credential to
    get ``federation``. It previously asserted federation from a bare body, which the real probe
    could never have produced."""
    probe = _FakeProbe({"can_discover": True, "account_admin": True, "user_sync": True})
    t = _dbx_svc(probe).create(
        _dbx_create(account_admin_client_id="admin-id", account_admin_secret="admin-secret"),
        created_by="a@b.com",
    )
    assert t.binding_mode == "federation"


@pytest.mark.parametrize(
    "caps",
    [
        {"can_discover": True, "account_admin": True, "user_sync": False},
        {"can_discover": True, "account_admin": False, "user_sync": True},
        {"can_discover": True, "account_admin": False, "user_sync": False},
        {"can_discover": False, "account_admin": False, "user_sync": False},
    ],
)
@mock_aws
def test_binding_mode_is_invoke_unavailable_for_every_other_combination(caps):
    """``federation`` requires both legs; every other combination is ``invoke_unavailable``, never
    ``sp_secret``: a tenant that cannot federate is not invocable rather than silently downgraded
    to a shared service-principal identity."""
    t = _dbx_svc(_FakeProbe(caps)).create(_dbx_create(), created_by="a@b.com")
    assert t.binding_mode == "invoke_unavailable"


@mock_aws
def test_aws_tenant_never_probes_and_keeps_an_empty_binding_mode():
    """The AWS branch is the fence: no probe, no capabilities, no binding mode."""
    probe = _FakeProbe()
    svc = _dbx_svc(probe)
    t = svc.create(_create_body(), created_by="a@b.com")
    assert probe.calls == []
    assert t.capabilities == {} and t.binding_mode == ""


@mock_aws
def test_a_raising_probe_still_creates_the_tenant_with_all_flags_false():
    """The tenant is a record of an operator's intent and the capabilities of what AGP could verify;
    a failed probe badges the tenant, it does not block the create."""
    svc = _dbx_svc(_FakeProbe(error=RuntimeError("workspace unreachable")))
    t = svc.create(_dbx_create(), created_by="a@b.com")
    assert t.capabilities == {"can_discover": False, "account_admin": False, "user_sync": False}
    assert t.binding_mode == "invoke_unavailable"
    assert svc.get(t.id).id == t.id


@mock_aws
def test_a_failed_can_discover_does_not_block_the_create():
    probe = _FakeProbe({"can_discover": False, "account_admin": False, "user_sync": False})
    svc = _dbx_svc(probe)
    t = svc.create(_dbx_create(), created_by="a@b.com")
    assert t.capabilities["can_discover"] is False
    assert svc.list() == [t]


@mock_aws
def test_a_probe_failure_never_leaks_its_message_into_the_record():
    svc = _dbx_svc(_FakeProbe(error=RuntimeError("PERMISSION_DENIED on /Workspace/secret/path")))
    t = svc.create(_dbx_create(), created_by="a@b.com")
    assert "Workspace" not in t.model_dump_json()


@mock_aws
def test_create_stores_the_stage_sp_secret_in_secrets_manager():
    svc = _dbx_svc()
    t = svc.create(_dbx_create(), created_by="a@b.com")
    arn = _as_dbx(t.stages["dev"]).sp_client_secret_arn
    assert arn.startswith("arn:aws:secretsmanager:")
    body = _json.loads(_sm().get_secret_value(SecretId=arn)["SecretString"])
    assert body == {SP_SECRET_KEY: "s3cr3t"}


@mock_aws
def test_the_stored_record_never_carries_the_sp_secret():
    """Both the API response and the DDB item go through this serializer — asserted on the
    persist shape, which is the one that would keep a credential forever."""
    svc = _dbx_svc()
    t = svc.create(_dbx_create(), created_by="a@b.com")
    item = svc._to_item(t)
    assert "s3cr3t" not in _json.dumps(item)
    assert "sp_client_secret" not in item["stages"]["dev"]
    assert item["stages"]["dev"]["sp_client_secret_arn"].startswith("arn:aws:secretsmanager:")


@mock_aws
def test_the_returned_record_does_not_keep_the_secret_in_memory_either():
    svc = _dbx_svc()
    t = svc.create(_dbx_create(), created_by="a@b.com")
    assert _as_dbx(t.stages["dev"]).sp_client_secret == ""


@mock_aws
def test_a_stage_with_no_secret_supplied_stores_no_secret_and_still_creates():
    """A tenant may be registered before its credential exists (the ARN slot stays empty).
    That must not mint an empty secret — a secret whose body is "" reads as configured."""
    svc = _dbx_svc()
    t = svc.create(_dbx_create(stages={"dev": _dbx_stage()}), created_by="a@b.com")
    assert _as_dbx(t.stages["dev"]).sp_client_secret_arn == ""
    assert _sm().list_secrets()["SecretList"] == []


@mock_aws
def test_each_stage_gets_its_own_secret():
    """Two workspaces are two credentials. One secret per tenant would make a prod rotation
    silently re-point dev at the new workspace's SP."""
    svc = _dbx_svc()
    t = svc.create(
        _dbx_create(
            stages={
                "dev": _dbx_stage(sp_client_secret="dev-secret"),
                "prod": _dbx_stage(sp_client_secret="prod-secret"),
            }
        ),
        created_by="a@b.com",
    )
    dev_arn = _as_dbx(t.stages["dev"]).sp_client_secret_arn
    prod_arn = _as_dbx(t.stages["prod"]).sp_client_secret_arn
    assert dev_arn and prod_arn and dev_arn != prod_arn
    sm = _sm()
    assert _json.loads(sm.get_secret_value(SecretId=dev_arn)["SecretString"])[SP_SECRET_KEY] == "dev-secret"
    assert _json.loads(sm.get_secret_value(SecretId=prod_arn)["SecretString"])[SP_SECRET_KEY] == "prod-secret"


@mock_aws
def test_create_stores_the_account_admin_credential_and_probes_with_it():
    """The account-admin pair unlocks federation: it is stored for later re-probes and provisioning,
    and it reaches the probe so the tenant is badged federation on the first connect."""
    probe = _FakeProbe({"can_discover": True, "account_admin": True, "user_sync": True})
    svc = _dbx_svc(probe)
    t = svc.create(
        _dbx_create(account_admin_client_id="admin-id", account_admin_secret="admin-secret"),
        created_by="a@b.com",
    )
    assert probe.calls[0]["account_admin_client_id"] == "admin-id"
    assert probe.calls[0]["account_admin_secret"] == "admin-secret"

    assert t.account_admin_secret_arn.startswith("arn:aws:secretsmanager:")
    body = _json.loads(_sm().get_secret_value(SecretId=t.account_admin_secret_arn)["SecretString"])
    assert body == {ACCOUNT_ADMIN_ID_KEY: "admin-id", ACCOUNT_ADMIN_SECRET_KEY: "admin-secret"}
    assert t.binding_mode == "federation"


@mock_aws
def test_the_account_admin_credential_never_reaches_the_record():
    svc = _dbx_svc()
    t = svc.create(
        _dbx_create(account_admin_client_id="admin-id", account_admin_secret="admin-secret"),
        created_by="a@b.com",
    )
    dumped = _json.dumps(svc._to_item(t))
    assert "admin-secret" not in dumped
    assert 'account_admin_secret":' not in dumped.replace("account_admin_secret_arn", "X")


@mock_aws
def test_a_half_filled_account_admin_pair_is_ignored():
    """All-or-nothing: an id with no secret cannot mint a token, so storing half of it would
    create a credential that can only fail — and would badge the tenant as having one."""
    probe = _FakeProbe()
    svc = _dbx_svc(probe)
    t = svc.create(_dbx_create(account_admin_client_id="admin-id"), created_by="a@b.com")
    assert t.account_admin_secret_arn == ""
    assert probe.calls[0]["account_admin_client_id"] in (None, "")
    names = [s["Name"] for s in _sm().list_secrets()["SecretList"]]
    assert not any(n.endswith("/account-admin") for n in names)


@pytest.mark.parametrize(
    "hostile",
    [
        "a1/../..",
        "a1/../../accounts/other/v1/token",
        f"{DBX_ACCOUNT}/../evil",
        DBX_ACCOUNT.upper(),
        f"{DBX_ACCOUNT}\n../evil",
        "not-a-uuid",
        "%2e%2e%2f",
    ],
)
@mock_aws
def test_create_databricks_rejects_a_traversal_shaped_account_id(hostile):
    """``account_id`` aims the account-admin token-mint path. A rejected value must not be
    stored AND must not have been probed — the probe would have made the call this guard
    exists to prevent."""
    probe = _FakeProbe()
    svc = _dbx_svc(probe)
    with pytest.raises(TenantError) as ei:
        svc.create(_dbx_create(stages={"dev": _dbx_stage(account_id=hostile)}), created_by="a@b.com")
    assert ei.value.kind == "validation"
    assert svc.list() == []
    assert probe.calls == []


@mock_aws
def test_create_databricks_accepts_an_empty_account_id():
    """``account_id`` is required for federation only; rejecting an empty value would make the
    account-admin grant mandatory for every Databricks tenant."""
    svc = _dbx_svc()
    t = svc.create(_dbx_create(stages={"dev": _dbx_stage(sp_client_secret="s")}), created_by="a@b.com")
    assert t.stages["dev"].account_id == ""


@mock_aws
def test_update_rejects_a_traversal_shaped_account_id():
    clock = iter([FIXED, LATER])
    svc = _dbx_svc(now=lambda: next(clock))
    t = svc.create(_dbx_create(), created_by="a@b.com")
    with pytest.raises(TenantError) as ei:
        svc.update(t.id, TenantUpdate(stages={"dev": _dbx_stage(account_id="a1/../..")}))
    assert ei.value.kind == "validation"
    assert svc.get(t.id).stages["dev"].account_id == DBX_ACCOUNT


@mock_aws
def test_update_preserves_the_stored_sp_secret_arn_when_no_new_secret_is_sent():
    """The frontend echoes the ARN back, but the SERVER's stored value is what wins. An
    untouched secret box must leave the pointer exactly where it was."""
    clock = iter([FIXED, LATER])
    svc = _dbx_svc(now=lambda: next(clock))
    t = svc.create(_dbx_create(), created_by="a@b.com")
    original = _as_dbx(t.stages["dev"]).sp_client_secret_arn

    upd = svc.update(t.id, TenantUpdate(stages={"dev": _dbx_stage(account_id=DBX_ACCOUNT)}))
    assert _as_dbx(upd.stages["dev"]).sp_client_secret_arn == original


@mock_aws
def test_update_ignores_a_body_supplied_sp_secret_arn():
    """THE CROSS-TENANT READ THIS BLOCKS: a client that could set this ARN could point its own
    tenant at another tenant's secret and then have AGP read it on every discovery call."""
    clock = iter([FIXED, LATER])
    svc = _dbx_svc(now=lambda: next(clock))
    t = svc.create(_dbx_create(), created_by="a@b.com")
    original = _as_dbx(t.stages["dev"]).sp_client_secret_arn
    forged = "arn:aws:secretsmanager:us-east-1:111111111111:secret:other-tenants-secret"

    upd = svc.update(
        t.id,
        TenantUpdate(
            stages={
                "dev": _dbx_stage(account_id=DBX_ACCOUNT, sp_client_secret_arn=forged),
            }
        ),
    )
    assert _as_dbx(upd.stages["dev"]).sp_client_secret_arn == original
    assert _as_dbx(svc.get(t.id).stages["dev"]).sp_client_secret_arn == original


@mock_aws
def test_update_with_a_new_sp_secret_rotates_the_stored_secret_server_side():
    clock = iter([FIXED, LATER])
    svc = _dbx_svc(now=lambda: next(clock))
    t = svc.create(_dbx_create(), created_by="a@b.com")

    upd = svc.update(
        t.id,
        TenantUpdate(
            stages={
                "dev": _dbx_stage(account_id=DBX_ACCOUNT, sp_client_secret="rotated"),
            }
        ),
    )
    arn = _as_dbx(upd.stages["dev"]).sp_client_secret_arn
    assert arn
    body = _json.loads(_sm().get_secret_value(SecretId=arn)["SecretString"])
    assert body == {SP_SECRET_KEY: "rotated"}


@mock_aws
def test_update_preserves_the_account_admin_arn_it_was_never_sent():
    clock = iter([FIXED, LATER])
    svc = _dbx_svc(now=lambda: next(clock))
    t = svc.create(
        _dbx_create(account_admin_client_id="admin-id", account_admin_secret="admin-secret"),
        created_by="a@b.com",
    )
    original = t.account_admin_secret_arn
    assert original
    upd = svc.update(t.id, TenantUpdate(description="edited"))
    assert upd.account_admin_secret_arn == original


@mock_aws
def test_update_can_add_an_account_admin_credential_and_unlock_federation():
    """A tenant connects with workspace credentials only, the customer later grants account-admin
    access, and the update re-probes and re-badges."""
    clock = iter([FIXED, LATER])
    probe = _FakeProbe({"can_discover": True, "account_admin": False, "user_sync": False})
    svc = _dbx_svc(probe, now=lambda: next(clock))
    t = svc.create(_dbx_create(), created_by="a@b.com")
    assert t.binding_mode == "invoke_unavailable"

    probe.result = {"can_discover": True, "account_admin": True, "user_sync": True}
    upd = svc.update(
        t.id, TenantUpdate(account_admin_client_id="admin-id", account_admin_secret="admin-secret")
    )

    assert upd.binding_mode == "federation"
    assert upd.account_admin_secret_arn.startswith("arn:aws:secretsmanager:")
    assert probe.calls[-1]["account_admin_client_id"] == "admin-id"


@mock_aws
def test_update_reprobes_using_the_stored_secret_when_none_is_resent():
    """An edit that does not resend the SP secret must still probe with a real credential —
    read back from Secrets Manager, which is the only place it exists."""
    clock = iter([FIXED, LATER])
    probe = _FakeProbe()
    svc = _dbx_svc(probe, now=lambda: next(clock))
    t = svc.create(_dbx_create(), created_by="a@b.com")

    svc.update(t.id, TenantUpdate(description="edited"))
    assert probe.calls[-1]["client_secret"] == "s3cr3t"


@mock_aws
def test_update_preserves_capabilities_when_no_credential_can_be_resolved():
    """A tenant with no stored credential yet: there is nothing to probe with, so the stored
    capabilities are LEFT ALONE. Zeroing them would be a fabricated downgrade — "AGP could
    not look" is not evidence that a capability was lost (``read_repo``'s rule for a read)."""
    clock = iter([FIXED, LATER])
    probe = _FakeProbe()
    svc = _dbx_svc(probe, now=lambda: next(clock))
    t = svc.create(_dbx_create(stages={"dev": _dbx_stage(account_id=DBX_ACCOUNT)}), created_by="a@b.com")
    before_calls = len(probe.calls)

    upd = svc.update(t.id, TenantUpdate(description="edited"))
    assert len(probe.calls) == before_calls
    assert upd.capabilities == t.capabilities
    assert upd.binding_mode == t.binding_mode


@mock_aws
def test_update_on_an_aws_tenant_never_probes_or_touches_secrets():
    """The AWS fence on the update path too."""
    clock = iter([FIXED, LATER])
    probe = _FakeProbe()
    svc = _dbx_svc(probe, now=lambda: next(clock))
    t = svc.create(_create_body(), created_by="a@b.com")
    upd = svc.update(t.id, TenantUpdate(description="edited"))
    assert probe.calls == []
    assert upd.capabilities == {} and upd.binding_mode == ""
    assert _sm().list_secrets()["SecretList"] == []


@mock_aws
def test_a_bare_description_update_keeps_a_federation_tenant_federated():
    """THE REGRESSION. Create with account-admin credentials (federation), then edit only the
    description: the stored pair must be read back and probed WITH, so the mode survives."""
    clock = iter([FIXED, LATER])
    probe = _FakeProbe({"can_discover": True, "account_admin": True, "user_sync": True})
    svc = _dbx_svc(probe, now=lambda: next(clock))
    t = svc.create(
        _dbx_create(account_admin_client_id="admin-id", account_admin_secret="admin-secret"),
        created_by="a@b.com",
    )
    assert t.binding_mode == "federation"

    upd = svc.update(t.id, TenantUpdate(description="edited"))

    assert upd.binding_mode == "federation"
    assert upd.capabilities == {"can_discover": True, "account_admin": True, "user_sync": True}
    assert probe.calls[-1]["account_admin_client_id"] == "admin-id"
    assert probe.calls[-1]["account_admin_secret"] == "admin-secret"
    assert svc.get(t.id).binding_mode == "federation"


@mock_aws
def test_the_stored_account_admin_pair_is_read_back_for_a_stages_update_too():
    """Any update path, not just the metadata one — a stages edit must not demote either."""
    clock = iter([FIXED, LATER])
    probe = _FakeProbe({"can_discover": True, "account_admin": True, "user_sync": True})
    svc = _dbx_svc(probe, now=lambda: next(clock))
    t = svc.create(
        _dbx_create(account_admin_client_id="admin-id", account_admin_secret="admin-secret"),
        created_by="a@b.com",
    )
    upd = svc.update(t.id, TenantUpdate(stages={"dev": _dbx_stage(account_id=DBX_ACCOUNT)}))
    assert upd.binding_mode == "federation"
    assert probe.calls[-1]["account_admin_client_id"] == "admin-id"


@mock_aws
def test_a_newly_supplied_admin_pair_wins_over_the_stored_one():
    """Rotation must not be masked by the read-back: the body's credential is authoritative."""
    clock = iter([FIXED, LATER])
    probe = _FakeProbe({"can_discover": True, "account_admin": True, "user_sync": True})
    svc = _dbx_svc(probe, now=lambda: next(clock))
    t = svc.create(
        _dbx_create(account_admin_client_id="old-id", account_admin_secret="old-secret"),
        created_by="a@b.com",
    )
    svc.update(t.id, TenantUpdate(account_admin_client_id="new-id", account_admin_secret="new-secret"))
    assert probe.calls[-1]["account_admin_client_id"] == "new-id"
    assert probe.calls[-1]["account_admin_secret"] == "new-secret"


@mock_aws
def test_an_unreadable_admin_secret_preserves_the_stored_binding_mode():
    """FAIL-CLOSED MUST NOT MEAN FAIL-DOWNWARD. If the stored admin secret cannot be read, AGP
    has no evidence the grant was revoked — so the stored capabilities stand rather than the
    tenant being demoted by a Secrets Manager blip. Same rule as an unresolvable stage secret."""
    clock = iter([FIXED, LATER])
    probe = _FakeProbe({"can_discover": True, "account_admin": True, "user_sync": True})
    svc = _dbx_svc(probe, now=lambda: next(clock))
    t = svc.create(
        _dbx_create(account_admin_client_id="admin-id", account_admin_secret="admin-secret"),
        created_by="a@b.com",
    )
    _sm().delete_secret(SecretId=t.account_admin_secret_arn, ForceDeleteWithoutRecovery=True)

    upd = svc.update(t.id, TenantUpdate(description="edited"))
    assert upd.binding_mode == "federation"
    assert upd.capabilities == t.capabilities


@mock_aws
def test_a_non_federated_tenant_with_no_admin_credential_still_probes_without_one():
    """The fence on the other side: no stored pair ⇒ nothing read back, and the probe is still
    called with the stage credential (a tenant that never had federation is not broken by this)."""
    clock = iter([FIXED, LATER])
    probe = _FakeProbe({"can_discover": True, "account_admin": False, "user_sync": False})
    svc = _dbx_svc(probe, now=lambda: next(clock))
    t = svc.create(_dbx_create(), created_by="a@b.com")
    upd = svc.update(t.id, TenantUpdate(description="edited"))
    assert upd.binding_mode == "invoke_unavailable"
    assert probe.calls[-1]["client_secret"] == "s3cr3t"
    assert probe.calls[-1]["account_admin_client_id"] in (None, "")


_ALL_CAPABILITY_COMBOS = [
    {"can_discover": d, "account_admin": a, "user_sync": u}
    for d in (True, False)
    for a in (True, False)
    for u in (True, False)
]


@pytest.mark.parametrize("caps", _ALL_CAPABILITY_COMBOS)
@mock_aws
def test_the_connect_flow_never_yields_sp_secret_for_any_capability_combination(caps):
    """Every one of the eight combinations, on BOTH connect paths (create and update)."""
    probe = _FakeProbe(caps)
    svc = _dbx_svc(probe)
    t = svc.create(
        _dbx_create(account_admin_client_id="admin-id", account_admin_secret="admin-secret"),
        created_by="a@b.com",
    )
    upd = svc.update(t.id, TenantUpdate(description="edited"))
    expected = "federation" if (caps["account_admin"] and caps["user_sync"]) else "invoke_unavailable"
    assert t.binding_mode == expected
    assert upd.binding_mode == expected


@mock_aws
def test_a_not_probeable_create_yields_invoke_unavailable_not_sp_secret():
    """No stage carries a readable credential, so nothing was probed; absence of evidence yields
    ``invoke_unavailable``, never ``sp_secret``."""
    probe = _FakeProbe()
    svc = _dbx_svc(probe)
    t = svc.create(_dbx_create(stages={"dev": _dbx_stage(account_id=DBX_ACCOUNT)}), created_by="a@b.com")
    assert probe.calls == []
    assert t.capabilities == {"can_discover": False, "account_admin": False, "user_sync": False}
    assert t.binding_mode == "invoke_unavailable"


@mock_aws
def test_a_legacy_sp_secret_record_is_normalized_on_its_next_probe():
    """A record stored before the connect flow stopped assigning ``sp_secret`` re-maps to
    ``invoke_unavailable`` on its next probe; its capabilities still carry over, since "AGP could
    not look" is not evidence a capability was lost."""
    probe = _FakeProbe()
    svc = _dbx_svc(probe)
    t = svc.create(_dbx_create(stages={"dev": _dbx_stage(account_id=DBX_ACCOUNT)}), created_by="a@b.com")
    legacy = t.model_copy(
        update={
            "binding_mode": "sp_secret",
            "capabilities": {"can_discover": True, "account_admin": False, "user_sync": False},
        }
    )
    svc.upsert_seed(legacy)

    upd = svc.update(t.id, TenantUpdate(description="edited"))
    assert probe.calls == []
    assert upd.binding_mode == "invoke_unavailable"
    assert upd.capabilities == {"can_discover": True, "account_admin": False, "user_sync": False}


@mock_aws
def test_the_carry_over_preserves_federation_and_the_aws_empty_mode():
    """The normalization is surgical: only ``sp_secret`` is re-mapped. A federation tenant whose
    credential became unreadable keeps ``federation`` (demoting it would be the fabricated
    downgrade the carry-over exists to prevent), and an AWS tenant keeps ``""``."""
    svc = _dbx_svc(_FakeProbe())
    t = svc.create(_dbx_create(stages={"dev": _dbx_stage(account_id=DBX_ACCOUNT)}), created_by="a@b.com")
    svc.upsert_seed(t.model_copy(update={"binding_mode": "federation"}))
    assert svc.update(t.id, TenantUpdate(description="edited")).binding_mode == "federation"

    svc.upsert_seed(svc.get(t.id).model_copy(update={"binding_mode": ""}))
    assert svc.update(t.id, TenantUpdate(description="again")).binding_mode == ""


@mock_aws
def test_delete_removes_the_stage_and_account_admin_secrets():
    svc = _dbx_svc()
    t = svc.create(
        _dbx_create(account_admin_client_id="admin-id", account_admin_secret="admin-secret"),
        created_by="a@b.com",
    )
    stage_arn = _as_dbx(t.stages["dev"]).sp_client_secret_arn
    admin_arn = t.account_admin_secret_arn
    sm = _sm()
    assert sm.get_secret_value(SecretId=stage_arn)
    assert sm.get_secret_value(SecretId=admin_arn)

    svc.delete(t.id)

    for arn in (stage_arn, admin_arn):
        with pytest.raises(ClientError):
            sm.get_secret_value(SecretId=arn)


@mock_aws
def test_delete_removes_every_stages_secret():
    svc = _dbx_svc()
    t = svc.create(
        _dbx_create(
            stages={
                "dev": _dbx_stage(sp_client_secret="dev-secret"),
                "prod": _dbx_stage(sp_client_secret="prod-secret"),
            }
        ),
        created_by="a@b.com",
    )
    arns = [_as_dbx(s).sp_client_secret_arn for s in t.stages.values()]
    svc.delete(t.id)
    sm = _sm()
    for arn in arns:
        with pytest.raises(ClientError):
            sm.get_secret_value(SecretId=arn)


@mock_aws
def test_delete_still_removes_the_record_when_no_secret_exists():
    """An AWS tenant has no secrets at all — the cleanup must be a no-op, not a failure that
    blocks the delete (``ResourceNotFoundException`` is success, per the best-effort contract)."""
    svc = _dbx_svc()
    t = svc.create(_create_body(), created_by="a@b.com")
    svc.delete(t.id)
    assert svc.list() == []


@mock_aws
def test_delete_of_an_unknown_tenant_still_404s_before_touching_secrets():
    svc = _dbx_svc()
    with pytest.raises(TenantError) as ei:
        svc.delete("ten-nope")
    assert ei.value.kind == "not_found"


@mock_aws
def test_a_rotation_fault_surfaces_as_secret_error_not_a_raw_botocore_error():
    from unittest.mock import MagicMock as _MM

    from botocore.exceptions import ClientError as _CE

    real = aws_clients.client("secretsmanager", region="us-east-1")
    fake = _MM(wraps=real)
    fake.exceptions = real.exceptions
    fake.create_secret.side_effect = real.exceptions.ResourceExistsException(
        {"Error": {"Code": "ResourceExistsException", "Message": "exists"}}, "CreateSecret"
    )
    fake.put_secret_value.side_effect = _CE(
        {"Error": {"Code": "InternalServiceError", "Message": "kms key unavailable for arn:x"}},
        "PutSecretValue",
    )

    svc = TenantService(
        table_name="",
        region="us-east-1",
        secret_prefix=SECRET_PREFIX,
        workspace=_FakeProbe(),
        secrets_client=fake,
        new_id=lambda: "ten-rot0001",
        now=lambda: FIXED,
    )
    with pytest.raises(TenantError) as ei:
        svc.create(_dbx_create(), created_by="a@b.com")
    assert ei.value.kind == "secret_error"
    assert "kms" not in ei.value.message.lower()
    assert "arn:x" not in ei.value.message


@mock_aws
def test_delete_removes_the_secret_of_a_stage_that_was_removed_after_creation():
    """LEAK (a). ``prod`` is dropped by an update; its credential must still die with the tenant.
    Rebuilding names from the CURRENT stages can never see it."""
    clock = iter([FIXED, LATER])
    svc = _dbx_svc(now=lambda: next(clock))
    t = svc.create(
        _dbx_create(
            stages={
                "dev": _dbx_stage(sp_client_secret="dev-secret"),
                "prod": _dbx_stage(sp_client_secret="prod-secret"),
            }
        ),
        created_by="a@b.com",
    )
    prod_arn = _as_dbx(t.stages["prod"]).sp_client_secret_arn
    assert prod_arn

    svc.update(t.id, TenantUpdate(stages={"dev": _dbx_stage(account_id=DBX_ACCOUNT)}))
    svc.delete(t.id)

    with pytest.raises(ClientError):
        _sm().get_secret_value(SecretId=prod_arn)


@mock_aws
def test_delete_removes_secrets_even_when_the_configured_prefix_changed():
    """LEAK (b). The prefix is a SETTINGS value: a deploy that retunes it must not turn every
    subsequent tenant delete into a credential leak. Deleting by the stored ARN is immune."""
    svc_created = _dbx_svc()
    t = svc_created.create(
        _dbx_create(account_admin_client_id="admin-id", account_admin_secret="admin-secret"),
        created_by="a@b.com",
    )
    stage_arn = _as_dbx(t.stages["dev"]).sp_client_secret_arn
    admin_arn = t.account_admin_secret_arn
    assert stage_arn and admin_arn

    svc_later = TenantService(
        table_name="",
        region="us-east-1",
        secret_prefix="agp-prod/databricks-tenants/",
        workspace=_FakeProbe(),
        secrets_client=aws_clients.client("secretsmanager", region="us-east-1"),
        new_id=lambda: "ten-unused",
        now=lambda: LATER,
    )
    svc_later._store = svc_created._store
    svc_later.delete(t.id)

    sm = _sm()
    for arn in (stage_arn, admin_arn):
        with pytest.raises(ClientError):
            sm.get_secret_value(SecretId=arn)


@mock_aws
def test_delete_is_unaffected_by_a_stage_renamed_after_creation():
    """The same class as (a) from the other direction: a stage RENAMED (uat → staging) leaves a
    secret under the old name that no rebuilt name would match."""
    clock = iter([FIXED, LATER])
    svc = _dbx_svc(now=lambda: next(clock))
    t = svc.create(
        _dbx_create(stages={"uat": _dbx_stage(sp_client_secret="uat-secret")}), created_by="a@b.com"
    )
    old_arn = _as_dbx(t.stages["uat"]).sp_client_secret_arn

    svc.update(t.id, TenantUpdate(stages={"staging": _dbx_stage(sp_client_secret="new-secret")}))
    svc.delete(t.id)

    with pytest.raises(ClientError):
        _sm().get_secret_value(SecretId=old_arn)


@mock_aws
def test_the_create_rollback_still_deletes_by_name():
    """The rollback path CANNOT use ARNs — it runs when the record does not exist yet, so the
    name is the only identifier available. Pinned so a future refactor does not "unify" the two
    paths onto ARNs and silently make the rollback a no-op (re-leaking the orphan that rollback
    exists to prevent)."""
    real = aws_clients.client("secretsmanager", region="us-east-1")
    svc = TenantService(
        table_name="",
        region="us-east-1",
        secret_prefix=SECRET_PREFIX,
        workspace=_FakeProbe(),
        secrets_client=real,
        new_id=lambda: "ten-rb0001",
        now=lambda: FIXED,
    )

    def _boom(record):
        raise RuntimeError("ddb down")

    svc._create = _boom
    with pytest.raises(TenantError) as ei:
        svc.create(_dbx_create(account_admin_client_id="ai", account_admin_secret="as"), created_by="a@b.com")
    assert ei.value.kind == "secret_error"
    names = [s["Name"] for s in real.list_secrets()["SecretList"]]
    assert not any(n.startswith(f"{SECRET_PREFIX}ten-rb0001") for n in names), names


@mock_aws
def test_delete_of_an_aws_tenant_issues_no_secret_calls_at_all():
    """An AWS tenant has no credentials, so the delete fires no DeleteSecret call that would log an
    error on a normal delete."""
    from unittest.mock import MagicMock as _MM

    real = aws_clients.client("secretsmanager", region="us-east-1")
    spy = _MM(wraps=real)
    spy.exceptions = real.exceptions
    svc = TenantService(
        table_name="",
        region="us-east-1",
        secret_prefix=SECRET_PREFIX,
        workspace=_FakeProbe(),
        secrets_client=spy,
        new_id=lambda: "ten-aws0001",
        now=lambda: FIXED,
    )
    t = svc.create(_create_body(), created_by="a@b.com")
    svc.delete(t.id)
    assert svc.list() == []
    spy.delete_secret.assert_not_called()


@mock_aws
def test_delete_of_a_databricks_tenant_with_no_stored_arns_issues_no_secret_calls():
    """A Databricks tenant registered before its credential exists carries empty ARN slots —
    nothing to delete, so nothing should be attempted."""
    from unittest.mock import MagicMock as _MM

    real = aws_clients.client("secretsmanager", region="us-east-1")
    spy = _MM(wraps=real)
    spy.exceptions = real.exceptions
    svc = TenantService(
        table_name="",
        region="us-east-1",
        secret_prefix=SECRET_PREFIX,
        workspace=_FakeProbe(),
        secrets_client=spy,
        new_id=lambda: "ten-dbx0009",
        now=lambda: FIXED,
    )
    t = svc.create(_dbx_create(stages={"dev": _dbx_stage()}), created_by="a@b.com")
    assert _as_dbx(t.stages["dev"]).sp_client_secret_arn == ""
    svc.delete(t.id)
    spy.delete_secret.assert_not_called()


@mock_aws
def test_a_rotation_does_not_prune_the_secret_the_stage_still_uses():
    """THE PRUNE'S DANGEROUS EDGE. The prune deletes the secrets of stages an update removed, and
    it compares ARNs rather than stage keys precisely so a ROTATION is not mistaken for a
    removal: rotating reuses the same secret NAME, hence the same ARN, so the stage that still
    uses it must keep a readable credential. Comparing keys would have been fine here, but
    comparing anything coarser (or pruning "everything stored") would delete the live secret."""
    clock = iter([FIXED, LATER])
    svc = _dbx_svc(now=lambda: next(clock))
    t = svc.create(_dbx_create(), created_by="a@b.com")

    upd = svc.update(
        t.id,
        TenantUpdate(
            stages={
                "dev": _dbx_stage(account_id=DBX_ACCOUNT, sp_client_secret="rotated"),
            }
        ),
    )
    arn = _as_dbx(upd.stages["dev"]).sp_client_secret_arn
    assert arn
    body = _json.loads(_sm().get_secret_value(SecretId=arn)["SecretString"])
    assert body == {SP_SECRET_KEY: "rotated"}


@mock_aws
def test_adding_a_stage_prunes_nothing():
    """A pure addition removes no ARN, so the prune must be a no-op — the existing stage's
    credential stays readable."""
    clock = iter([FIXED, LATER])
    svc = _dbx_svc(now=lambda: next(clock))
    t = svc.create(_dbx_create(), created_by="a@b.com")
    dev_arn = _as_dbx(t.stages["dev"]).sp_client_secret_arn

    svc.update(
        t.id,
        TenantUpdate(
            stages={
                "dev": _dbx_stage(account_id=DBX_ACCOUNT),
                "prod": _dbx_stage(sp_client_secret="prod-secret"),
            }
        ),
    )
    assert _sm().get_secret_value(SecretId=dev_arn)


@mock_aws
def test_a_metadata_only_update_prunes_nothing():
    """``stages`` untouched ⇒ nothing removed ⇒ no DeleteSecret call at all."""
    from unittest.mock import MagicMock as _MM

    real = aws_clients.client("secretsmanager", region="us-east-1")
    spy = _MM(wraps=real)
    spy.exceptions = real.exceptions
    clock = iter([FIXED, LATER])
    svc = TenantService(
        table_name="",
        region="us-east-1",
        secret_prefix=SECRET_PREFIX,
        workspace=_FakeProbe(),
        secrets_client=spy,
        new_id=lambda: "ten-meta0001",
        now=lambda: next(clock),
    )
    t = svc.create(_dbx_create(), created_by="a@b.com")
    svc.update(t.id, TenantUpdate(description="edited"))
    spy.delete_secret.assert_not_called()
    assert _sm().get_secret_value(SecretId=_as_dbx(t.stages["dev"]).sp_client_secret_arn)


OTHER_PREFIX = "agp-test-v2/databricks-tenants/"


def _spy_sm():
    """A Secrets Manager client that records calls while really performing them."""
    from unittest.mock import MagicMock as _MM

    real = aws_clients.client("secretsmanager", region="us-east-1")
    spy = _MM(wraps=real)
    spy.exceptions = real.exceptions
    return spy


def _schedule_secret(arn: str) -> None:
    """Mark a secret for deletion out of band, as an operator in the console or an earlier prune
    would."""
    aws_clients.client("secretsmanager", region="us-east-1").delete_secret(SecretId=arn)


@mock_aws
def test_an_update_naming_one_stage_keeps_the_other_and_prunes_nothing():
    """A body naming only ``dev`` merges: ``prod`` survives, and because the record never stops
    naming prod's secret there is nothing to prune."""
    clock = iter([FIXED, LATER])
    spy = _spy_sm()
    svc = TenantService(
        table_name="",
        region="us-east-1",
        secret_prefix=SECRET_PREFIX,
        workspace=_FakeProbe(),
        secrets_client=spy,
        new_id=lambda: "ten-keep0001",
        now=lambda: next(clock),
    )
    t = svc.create(
        _dbx_create(
            stages={
                "dev": _dbx_stage(sp_client_secret="dev-secret"),
                "prod": _dbx_stage(sp_client_secret="prod-secret"),
            }
        ),
        created_by="a@b.com",
    )
    prod_arn = _as_dbx(t.stages["prod"]).sp_client_secret_arn
    spy.delete_secret.reset_mock()

    upd = svc.update(t.id, TenantUpdate(stages={"dev": _dbx_stage(account_id=DBX_ACCOUNT)}))

    assert set(upd.stages) == {"dev", "prod"}
    spy.delete_secret.assert_not_called()
    body = _json.loads(_sm().get_secret_value(SecretId=prod_arn)["SecretString"])
    assert body == {SP_SECRET_KEY: "prod-secret"}


@mock_aws
def test_a_failed_save_prunes_nothing_and_leaves_the_secret_readable():
    """The write fails, so the record still references the orphaned stage's secret, which must
    survive: deleting before the save would destroy a credential the record still points at."""
    clock = iter([FIXED, LATER])
    spy = _spy_sm()
    svc = TenantService(
        table_name="",
        region="us-east-1",
        secret_prefix=SECRET_PREFIX,
        workspace=_FakeProbe(),
        secrets_client=spy,
        new_id=lambda: "ten-fail0001",
        now=lambda: next(clock),
    )
    t = svc.create(
        _dbx_create(
            stages={
                "dev": _dbx_stage(sp_client_secret="dev-secret"),
            }
        ),
        created_by="a@b.com",
    )
    old_dev_arn = _as_dbx(t.stages["dev"]).sp_client_secret_arn
    spy.delete_secret.reset_mock()

    def _boom(record):
        raise RuntimeError("ProvisionedThroughputExceededException")

    svc.secret_prefix = OTHER_PREFIX
    svc._save = _boom
    with pytest.raises(RuntimeError):
        svc.update(
            t.id,
            TenantUpdate(
                stages={
                    "dev": _dbx_stage(account_id=DBX_ACCOUNT, sp_client_secret="rotated"),
                }
            ),
        )

    spy.delete_secret.assert_not_called()
    body = _json.loads(_sm().get_secret_value(SecretId=old_dev_arn)["SecretString"])
    assert body == {SP_SECRET_KEY: "dev-secret"}
    assert _as_dbx(svc.get(t.id).stages["dev"]).sp_client_secret_arn == old_dev_arn


@mock_aws
def test_the_prune_still_runs_after_a_successful_save():
    """On a good write the prune happens. The orphan comes from a prefix move, the one edit that
    can still produce one now that an update merges stages."""
    clock = iter([FIXED, LATER])
    svc = _dbx_svc(now=lambda: next(clock))
    t = svc.create(
        _dbx_create(
            stages={
                "dev": _dbx_stage(sp_client_secret="dev-secret"),
            }
        ),
        created_by="a@b.com",
    )
    old_dev_arn = _as_dbx(t.stages["dev"]).sp_client_secret_arn

    svc.secret_prefix = OTHER_PREFIX
    upd = svc.update(
        t.id,
        TenantUpdate(
            stages={
                "dev": _dbx_stage(account_id=DBX_ACCOUNT, sp_client_secret="rotated"),
            }
        ),
    )

    assert _as_dbx(upd.stages["dev"]).sp_client_secret_arn != old_dev_arn
    assert svc._resolve_stage_secret(_as_dbx(upd.stages["dev"])) == "rotated"
    with pytest.raises(ClientError):
        _sm().get_secret_value(SecretId=old_dev_arn)


@mock_aws
def test_an_omitted_stage_survives_the_update_and_keeps_its_secret():
    """An update merges stages: a stage the body never names survives, so omission cannot orphan its
    secret and the prune has nothing to do."""
    clock = iter([FIXED, LATER])
    svc = _dbx_svc(now=lambda: next(clock))
    t = svc.create(
        _dbx_create(
            stages={
                "dev": _dbx_stage(sp_client_secret="dev-secret"),
                "prod": _dbx_stage(sp_client_secret="prod-secret"),
            }
        ),
        created_by="a@b.com",
    )
    prod_arn = _as_dbx(t.stages["prod"]).sp_client_secret_arn

    upd = svc.update(t.id, TenantUpdate(stages={"dev": _dbx_stage(account_id=DBX_ACCOUNT)}))
    assert set(upd.stages) == {"dev", "prod"}
    assert _json.loads(_sm().get_secret_value(SecretId=prod_arn)["SecretString"]) == {
        SP_SECRET_KEY: "prod-secret"
    }


@mock_aws
def test_the_prune_uses_a_recoverable_delete_because_the_record_survives():
    """A pruned secret belongs to a live tenant whose record merely stopped naming it, so it gets a
    scheduled delete with a recovery window. Pinned at the helper, since no public write path
    other than a prefix move can orphan a stage secret any more."""
    spy = _spy_sm()
    svc = TenantService(
        table_name="",
        region="us-east-1",
        secret_prefix=SECRET_PREFIX,
        workspace=_FakeProbe(),
        secrets_client=spy,
        new_id=lambda: "ten-rec0001",
        now=lambda: FIXED,
    )
    t = svc.create(
        _dbx_create(
            stages={
                "dev": _dbx_stage(sp_client_secret="dev-secret"),
                "prod": _dbx_stage(sp_client_secret="prod-secret"),
            }
        ),
        created_by="a@b.com",
    )
    prod_arn = _as_dbx(t.stages["prod"]).sp_client_secret_arn
    spy.delete_secret.reset_mock()

    svc._prune_secrets([prod_arn], t.id)

    spy.delete_secret.assert_called_once()
    kwargs = spy.delete_secret.call_args.kwargs
    assert kwargs["SecretId"] == prod_arn
    assert "ForceDeleteWithoutRecovery" not in kwargs
    real = aws_clients.client("secretsmanager", region="us-east-1")
    real.restore_secret(SecretId=prod_arn)
    assert _json.loads(real.get_secret_value(SecretId=prod_arn)["SecretString"]) == {
        SP_SECRET_KEY: "prod-secret"
    }


@mock_aws
def test_the_tenant_delete_path_force_deletes_because_the_record_is_gone():
    """The contrast that makes the distinction meaningful: nothing will ever reference these
    secrets again, so they are removed outright rather than left lingering for 30 days."""
    spy = _spy_sm()
    svc = TenantService(
        table_name="",
        region="us-east-1",
        secret_prefix=SECRET_PREFIX,
        workspace=_FakeProbe(),
        secrets_client=spy,
        new_id=lambda: "ten-del0001",
        now=lambda: FIXED,
    )
    t = svc.create(
        _dbx_create(account_admin_client_id="admin-id", account_admin_secret="admin-secret"),
        created_by="a@b.com",
    )
    spy.delete_secret.reset_mock()
    svc.delete(t.id)

    assert spy.delete_secret.call_count == 2
    for call in spy.delete_secret.call_args_list:
        assert call.kwargs["ForceDeleteWithoutRecovery"] is True


@mock_aws
def test_the_create_rollback_force_deletes_too():
    """The record never existed, so there is nothing to recover the secret FOR."""
    spy = _spy_sm()
    svc = TenantService(
        table_name="",
        region="us-east-1",
        secret_prefix=SECRET_PREFIX,
        workspace=_FakeProbe(),
        secrets_client=spy,
        new_id=lambda: "ten-rb0002",
        now=lambda: FIXED,
    )

    def _boom(record):
        raise RuntimeError("ddb down")

    svc._create = _boom
    with pytest.raises(TenantError):
        svc.create(_dbx_create(), created_by="a@b.com")

    assert spy.delete_secret.call_count >= 1
    for call in spy.delete_secret.call_args_list:
        assert call.kwargs["ForceDeleteWithoutRecovery"] is True


@pytest.mark.parametrize(
    "hostile_key",
    [
        "account-admin",
        "ACCOUNT-ADMIN",
        "Account-Admin",
    ],
)
@mock_aws
def test_create_rejects_a_stage_named_account_admin(hostile_key):
    probe = _FakeProbe()
    svc = _dbx_svc(probe)
    with pytest.raises(TenantError) as ei:
        svc.create(_dbx_create(stages={hostile_key: _dbx_stage(sp_client_secret="x")}), created_by="a@b.com")
    assert ei.value.kind == "validation"
    assert svc.list() == []
    assert probe.calls == []
    assert _sm().list_secrets()["SecretList"] == []


@mock_aws
def test_update_rejects_renaming_a_stage_to_account_admin():
    clock = iter([FIXED, LATER])
    svc = _dbx_svc(now=lambda: next(clock))
    t = svc.create(_dbx_create(), created_by="a@b.com")
    with pytest.raises(TenantError) as ei:
        svc.update(t.id, TenantUpdate(stages={"account-admin": _dbx_stage(sp_client_secret="x")}))
    assert ei.value.kind == "validation"
    assert set(svc.get(t.id).stages) == {"dev"}


@mock_aws
def test_an_aws_tenant_may_still_use_any_stage_name():
    """The reservation is a Databricks concern: AWS tenants write no secrets, so their stage axis
    stays fully open."""
    svc = _dbx_svc()
    t = svc.create(
        _create_body(
            stages={
                "account-admin": TenantStageConfig(account_id="111111111111"),
            }
        ),
        created_by="a@b.com",
    )
    assert set(t.stages) == {"account-admin"}


@mock_aws
def test_rotating_a_stage_whose_secret_is_scheduled_yields_a_readable_secret():
    """End to end: ``prod``'s secret is marked for deletion, a rotation lands on it, and the
    credential comes back readable, unmarked and visible to the probe."""
    clock = iter([FIXED, LATER, LATER])
    probe = _FakeProbe()
    svc = _dbx_svc(probe, now=lambda: next(clock))
    t = svc.create(
        _dbx_create(
            stages={
                "dev": _dbx_stage(sp_client_secret="dev-secret"),
                "prod": _dbx_stage(sp_client_secret="prod-secret"),
            }
        ),
        created_by="a@b.com",
    )

    _schedule_secret(_as_dbx(t.stages["prod"]).sp_client_secret_arn)

    probe.calls.clear()
    upd = svc.update(
        t.id,
        TenantUpdate(
            stages={
                "prod": _dbx_stage(sp_client_secret="prod-again"),
            }
        ),
    )

    arn = _as_dbx(upd.stages["prod"]).sp_client_secret_arn
    assert arn
    sm = _sm()
    assert "DeletedDate" not in sm.describe_secret(SecretId=arn)
    assert _json.loads(sm.get_secret_value(SecretId=arn)["SecretString"]) == {SP_SECRET_KEY: "prod-again"}
    assert "prod-again" in [c["client_secret"] for c in probe.calls]


@mock_aws
def test_a_resurrected_stage_secret_is_readable_by_the_discovery_path_too():
    """The consequence that made the bug invisible: ``_resolve_stage_secret`` returns "" for a
    marked secret, so the stage vanishes from probing. Assert the RESOLVER, not just the store."""
    clock = iter([FIXED, LATER, LATER])
    svc = _dbx_svc(now=lambda: next(clock))
    t = svc.create(
        _dbx_create(
            stages={
                "dev": _dbx_stage(sp_client_secret="dev-secret"),
                "prod": _dbx_stage(sp_client_secret="prod-secret"),
            }
        ),
        created_by="a@b.com",
    )
    _schedule_secret(_as_dbx(t.stages["prod"]).sp_client_secret_arn)
    upd = svc.update(
        t.id,
        TenantUpdate(
            stages={
                "prod": _dbx_stage(sp_client_secret="prod-again"),
            }
        ),
    )
    assert svc._resolve_stage_secret(_as_dbx(upd.stages["prod"])) == "prod-again"


@mock_aws
def test_restoring_happens_only_when_the_secret_was_actually_marked():
    """A plain rotation of a LIVE secret must not gain a restore call — an unconditional
    ``restore_secret`` would be a pointless write on the common path (and would mask a genuinely
    scheduled secret elsewhere)."""
    clock = iter([FIXED, LATER])
    spy = _spy_sm()
    svc = TenantService(
        table_name="",
        region="us-east-1",
        secret_prefix=SECRET_PREFIX,
        workspace=_FakeProbe(),
        secrets_client=spy,
        new_id=lambda: "ten-rot0002",
        now=lambda: next(clock),
    )
    t = svc.create(_dbx_create(), created_by="a@b.com")
    spy.restore_secret.reset_mock()

    svc.update(
        t.id,
        TenantUpdate(
            stages={
                "dev": _dbx_stage(account_id=DBX_ACCOUNT, sp_client_secret="rotated"),
            }
        ),
    )
    spy.restore_secret.assert_not_called()
    arn = _as_dbx(svc.get(t.id).stages["dev"]).sp_client_secret_arn
    assert _json.loads(_sm().get_secret_value(SecretId=arn)["SecretString"]) == {SP_SECRET_KEY: "rotated"}


@mock_aws
def test_a_restore_failure_surfaces_as_secret_error_rather_than_a_silent_bad_write():
    """If the secret cannot be un-marked, the rotation must not write into it: that would be a
    success with an unreadable credential. The marked state arrives out of band here, through a
    console ``delete_secret`` with the default recovery window."""
    from unittest.mock import MagicMock as _MM

    from botocore.exceptions import ClientError as _CE

    real = aws_clients.client("secretsmanager", region="us-east-1")
    spy = _MM(wraps=real)
    spy.exceptions = real.exceptions
    clock = iter([FIXED, LATER])
    svc = TenantService(
        table_name="",
        region="us-east-1",
        secret_prefix=SECRET_PREFIX,
        workspace=_FakeProbe(),
        secrets_client=spy,
        new_id=lambda: "ten-res0001",
        now=lambda: next(clock),
    )
    t = svc.create(
        _dbx_create(
            stages={
                "dev": _dbx_stage(sp_client_secret="dev-secret"),
                "prod": _dbx_stage(sp_client_secret="prod-secret"),
            }
        ),
        created_by="a@b.com",
    )
    _schedule_secret(_as_dbx(t.stages["prod"]).sp_client_secret_arn)

    spy.restore_secret.side_effect = _CE(
        {"Error": {"Code": "InternalServiceError", "Message": "kms key arn:x unavailable"}}, "RestoreSecret"
    )
    with pytest.raises(TenantError) as ei:
        svc.update(
            t.id,
            TenantUpdate(
                stages={
                    "prod": _dbx_stage(sp_client_secret="prod-again"),
                }
            ),
        )
    assert ei.value.kind == "secret_error"
    assert "kms" not in ei.value.message.lower()
    assert "arn:x" not in ei.value.message
    spy.put_secret_value.assert_not_called()
