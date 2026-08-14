"""Model tests for the E25 Tenant reshape (Task 1) + the E29 platform typing (E29/T1).

The ``Tenant`` model moved from flat ``aws_account_dev/aws_account_prod/aws_region``
fields to a nested ``stages: Dict[str, TenantStageConfig]`` map. ``hydrate_tenant_item``
folds a legacy flat stored record into the nested shape on read so pre-E25 data still
validates. Pure models — no I/O here.

E29 adds an immutable ``platform`` discriminator (``aws``/``databricks``) and a second
stage shape, ``DatabricksStageConfig``. The security-boundary regexes
(:data:`WORKSPACE_URL_RE`, :data:`WORKSPACE_ID_RE`) are verified by EXECUTION over hostile
inputs here — a regex asserted only by inspection is a regex nobody tested.
"""

from __future__ import annotations

import pytest

from models.tenant import (
    WORKSPACE_ID_RE,
    WORKSPACE_URL_RE,
    DatabricksStageConfig,
    Tenant,
    TenantCreate,
    TenantPlatform,
    TenantStageConfig,
    TenantUpdate,
    hydrate_tenant_item,
)


def test_tenant_holds_nested_stages():
    t = Tenant(
        id="ten-1", name="N", line_of_business="LoB", entra_group_ids=["g1"],
        stages={
            "dev": TenantStageConfig(account_id="111111111111", region="us-east-1",
                                     ecr_repo_uri="u-dev", push_role_arn="p-dev", deploy_role_arn="agp-deployment-dev"),
            "prod": TenantStageConfig(account_id="222222222222"),
        },
        created_by="u", created_at="t", updated_at="t",
    )
    assert t.stages["dev"].account_id == "111111111111"
    assert t.stages["prod"].deploy_role_arn == ""   # defaults empty


def test_hydrate_legacy_flat_item_folds_into_stages():
    legacy = {"id": "default", "name": "Default", "line_of_business": "Platform",
              "entra_group_ids": ["g1"], "aws_account_dev": "111111111111",
              "aws_account_prod": "222222222222", "aws_region": "eu-west-1",
              "description": "", "created_by": "u", "created_at": "t", "updated_at": "t"}
    clean = hydrate_tenant_item(legacy)
    assert "aws_account_dev" not in clean and "stages" in clean
    t = Tenant.model_validate(clean)
    assert t.stages["dev"].account_id == "111111111111"
    assert t.stages["prod"].account_id == "222222222222"
    assert t.stages["dev"].region == "eu-west-1"


def test_hydrate_is_noop_when_already_nested():
    already = {"id": "x", "stages": {"dev": {"account_id": "111111111111"}}}
    assert hydrate_tenant_item(already) is already or hydrate_tenant_item(already).get("stages")


# --------------------------------------------------------------------------- #
# E29/T1 — platform typing
# --------------------------------------------------------------------------- #


def _aws_tenant(**overrides) -> Tenant:
    body = {
        "id": "ten-1", "name": "N", "line_of_business": "LoB", "entra_group_ids": ["g1"],
        "stages": {"dev": TenantStageConfig(account_id="111111111111")},
        "created_by": "u", "created_at": "t", "updated_at": "t",
    }
    body.update(overrides)
    return Tenant(**body)


def test_platform_defaults_to_aws_with_empty_capability_fields():
    t = _aws_tenant()
    assert t.platform == TenantPlatform.AWS
    assert t.platform == "aws"  # str-enum: serializes/compares as the wire value
    assert t.capabilities == {}
    assert t.binding_mode == ""


def test_databricks_stage_config_defaults():
    s = DatabricksStageConfig(workspace_url="https://dbc-test.cloud.databricks.com")
    assert s.workspace_id == "0"  # "0" is legal — a URL with no o= parameter
    assert s.cloud == "aws"
    assert (s.region, s.account_id, s.sp_client_id, s.sp_client_secret_arn) == ("", "", "", "")


def test_databricks_tenant_holds_databricks_stages():
    t = _aws_tenant(
        platform=TenantPlatform.DATABRICKS,
        stages={
            "dev": DatabricksStageConfig(
                workspace_url="https://dbc-test.cloud.databricks.com",
                workspace_id="1234567890123456",
                sp_client_id="sp-abc",
                sp_client_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:x",
            )
        },
    )
    assert t.platform == "databricks"
    assert isinstance(t.stages["dev"], DatabricksStageConfig)
    assert t.stages["dev"].workspace_id == "1234567890123456"


def test_stage_union_resolves_each_shape_to_its_own_model():
    """A raw stored item must rehydrate to the RIGHT stage model per platform — the union
    is what T3/T6/T7 branch on, so a mis-resolved shape would silently mis-govern."""
    aws = Tenant.model_validate({
        "id": "ten-a", "name": "N", "line_of_business": "L", "entra_group_ids": ["g"],
        "stages": {"dev": {"account_id": "111111111111", "region": "eu-west-1"}},
        "created_by": "u", "created_at": "t", "updated_at": "t",
    })
    assert isinstance(aws.stages["dev"], TenantStageConfig)

    dbx = Tenant.model_validate({
        "id": "ten-d", "name": "N", "line_of_business": "L", "entra_group_ids": ["g"],
        "platform": "databricks",
        "stages": {"dev": {"workspace_url": "https://dbc-test.cloud.databricks.com",
                           "sp_client_id": "sp-abc"}},
        "created_by": "u", "created_at": "t", "updated_at": "t",
    })
    assert isinstance(dbx.stages["dev"], DatabricksStageConfig)


def test_tenant_create_carries_platform_but_update_cannot():
    """``platform`` is immutable after create: ``TenantUpdate`` has no such field, and a
    body carrying one is DROPPED (pydantic's default ``extra="ignore"``) rather than
    quietly re-typing a live tenant."""
    created = TenantCreate(
        name="N", line_of_business="L", entra_group_ids=["g"],
        platform=TenantPlatform.DATABRICKS,
        stages={"dev": DatabricksStageConfig(
            workspace_url="https://dbc-test.cloud.databricks.com", sp_client_id="sp-abc")},
    )
    assert created.platform == "databricks"

    assert "platform" not in TenantUpdate.model_fields
    upd = TenantUpdate.model_validate({"name": "x", "platform": "aws"})
    assert "platform" not in upd.model_dump(exclude_unset=True)


def test_tenant_create_defaults_to_aws_platform():
    created = TenantCreate(
        name="N", line_of_business="L", entra_group_ids=["g"],
        stages={"dev": TenantStageConfig(account_id="111111111111")},
    )
    assert created.platform == TenantPlatform.AWS


def test_hydrate_defaults_platform_to_aws_for_pre_e29_record():
    """A raw pre-E29 DDB item has no ``platform``/``capabilities``/``binding_mode`` keys.
    It must validate untouched and read ``platform == "aws"`` — zero migration."""
    pre_e29 = {"id": "ten-old", "name": "Retail", "line_of_business": "Consumer",
               "entra_group_ids": ["g1"],
               "stages": {"dev": {"account_id": "111111111111", "region": "us-east-1"}},
               "description": "", "created_by": "u", "created_at": "t", "updated_at": "t"}
    clean = hydrate_tenant_item(pre_e29)
    assert clean["platform"] == "aws"
    t = Tenant.model_validate(clean)
    assert t.platform == TenantPlatform.AWS
    assert t.capabilities == {} and t.binding_mode == ""
    assert isinstance(t.stages["dev"], TenantStageConfig)


def test_hydrate_defaults_platform_on_legacy_flat_record_too():
    legacy = {"id": "default", "name": "Default", "line_of_business": "Platform",
              "entra_group_ids": ["g1"], "aws_account_dev": "111111111111",
              "aws_account_prod": "222222222222", "aws_region": "eu-west-1",
              "description": "", "created_by": "u", "created_at": "t", "updated_at": "t"}
    t = Tenant.model_validate(hydrate_tenant_item(legacy))
    assert t.platform == TenantPlatform.AWS
    assert t.stages["dev"].account_id == "111111111111"


def test_hydrate_preserves_an_explicit_platform():
    stored = {"id": "ten-d", "platform": "databricks",
              "stages": {"dev": {"workspace_url": "https://dbc-test.cloud.databricks.com"}}}
    assert hydrate_tenant_item(stored)["platform"] == "databricks"


# --- security-boundary regexes: EXECUTED over hostile inputs -----------------

@pytest.mark.parametrize("url", [
    "https://dbc-test.cloud.databricks.com",
    "https://adb-1234567890123456.16.azuredatabricks.net",
    "https://dbc-a1b2c3d4-e5f6.cloud.databricks.com",
])
def test_workspace_url_regex_accepts_real_workspace_origins(url):
    assert WORKSPACE_URL_RE.fullmatch(url)


@pytest.mark.parametrize("hostile", [
    "javascript:alert(1)",
    "javascript:https://dbc-test.cloud.databricks.com",
    "https://dbc-test.cloud.databricks.com/",            # trailing slash
    "https://dbc-test.cloud.databricks.com/api/2.0/apps",  # path
    "http://dbc-test.cloud.databricks.com",               # plaintext
    "HTTPS://DBC-TEST.CLOUD.DATABRICKS.COM",              # uppercase host
    "https://dbc test.cloud.databricks.com",              # embedded space
    "https://dbc-test.cloud.databricks.com ",             # trailing space
    " https://dbc-test.cloud.databricks.com",             # leading space
    "https://dbc-test.cloud.databricks.com\n",            # trailing newline
    "https://dbc-test.cloud.databricks.com\njavascript:x",
    "https://user:pass@evil.example.com",                 # userinfo
    "https://dbc-test.cloud.databricks.com:8443",         # explicit port
    "https://dbc-test.cloud.databricks.com?x=1",          # query
    "https://dbc-test.cloud.databricks.com#f",            # fragment
    "https://",
    "https://x",                                          # too short for the 3-part pattern
    "",
    "//dbc-test.cloud.databricks.com",
    "dbc-test.cloud.databricks.com",
    "https://dbc-test.cloud.databricks.com/../evil",
])
def test_workspace_url_regex_rejects_hostile_inputs(hostile):
    assert WORKSPACE_URL_RE.fullmatch(hostile) is None


@pytest.mark.parametrize("wid", ["0", "1234567890123456", "42"])
def test_workspace_id_regex_accepts_digit_strings(wid):
    assert WORKSPACE_ID_RE.fullmatch(wid)


@pytest.mark.parametrize("hostile", [
    "", "abc", "12a", "-1", "1.5", "1 2", "12\n", " 12", "12 ", "1e6", "٣", "١٢٣",
])
def test_workspace_id_regex_rejects_non_digit_strings(hostile):
    assert WORKSPACE_ID_RE.fullmatch(hostile) is None


# --------------------------------------------------------------------------- #
# E29/T3 — OB-7: the write-only credential fields the frontend actually sends
#
# The backend was DROPPING them. ``TenantCreate`` had no ``account_admin_*`` fields and
# ``DatabricksStageConfig`` had no ``sp_client_secret``, so pydantic's ``extra="ignore"``
# swallowed all three silently — the admin UI collected credentials, the API answered 201,
# and nothing was ever stored. These tests pin the exact wire names T4 ships and, more
# importantly, the ASYMMETRY: settable on the way in, absent on the way out.
# --------------------------------------------------------------------------- #

WS = "https://dbc-test.cloud.databricks.com"


def test_stage_accepts_sp_client_secret_on_the_way_in():
    s = DatabricksStageConfig(workspace_url=WS, sp_client_id="sp-abc", sp_client_secret="s3cr3t")
    assert s.sp_client_secret == "s3cr3t"


def test_stage_sp_client_secret_defaults_empty_so_a_read_shape_still_validates():
    """A stored record carries no ``sp_client_secret`` key at all — the read path must not
    require one (it is the write half of the split, exactly like ``ConnectionCreate.token``)."""
    s = DatabricksStageConfig(workspace_url=WS, sp_client_id="sp-abc")
    assert s.sp_client_secret == ""


def test_stage_sp_client_secret_never_serializes():
    """The whole point of the field: it goes in and it does NOT come out. Both dump paths
    are asserted because they are two different callers — ``model_dump`` is what FastAPI's
    response_model serializes through, ``model_dump_json`` is what the DDB persist writes."""
    s = DatabricksStageConfig(workspace_url=WS, sp_client_id="sp-abc", sp_client_secret="s3cr3t")
    assert "sp_client_secret" not in s.model_dump()
    assert "s3cr3t" not in s.model_dump_json()
    # The ARN — the pointer that replaces it — is still a read field.
    assert "sp_client_secret_arn" in s.model_dump()


def test_tenant_read_model_never_echoes_a_stage_secret():
    """The exclusion must survive nesting: a stage secret set in memory must not reach the
    tenant's own dump either (that dump is BOTH the API response and the DDB item)."""
    t = Tenant(
        id="ten-1", name="N", line_of_business="L", entra_group_ids=["g"],
        platform=TenantPlatform.DATABRICKS,
        stages={"dev": DatabricksStageConfig(
            workspace_url=WS, sp_client_id="sp-abc", sp_client_secret="s3cr3t")},
        created_by="u", created_at="t", updated_at="t",
    )
    assert "s3cr3t" not in t.model_dump_json()
    assert "sp_client_secret" not in t.model_dump()["stages"]["dev"]


def test_tenant_create_carries_the_account_admin_credential():
    body = TenantCreate(
        name="N", line_of_business="L", entra_group_ids=["g"],
        platform=TenantPlatform.DATABRICKS,
        stages={"dev": DatabricksStageConfig(workspace_url=WS, sp_client_id="sp-abc")},
        account_admin_client_id="admin-id",
        account_admin_secret="admin-secret",
    )
    assert body.account_admin_client_id == "admin-id"
    assert body.account_admin_secret == "admin-secret"


def test_tenant_update_carries_the_account_admin_credential_too():
    """Rotation happens on the UPDATE path — an admin who supplies account-admin credentials
    on an existing tenant is unlocking federation on a tenant that connected without them."""
    upd = TenantUpdate.model_validate(
        {"account_admin_client_id": "admin-id", "account_admin_secret": "admin-secret"}
    )
    assert upd.account_admin_client_id == "admin-id"
    assert upd.account_admin_secret == "admin-secret"


def test_account_admin_credentials_default_absent_on_the_update_path():
    """``exclude_unset`` is how the service tells "rotate" from "leave alone" — so an
    untouched credential must not appear in the dump at all."""
    upd = TenantUpdate.model_validate({"description": "x"})
    dumped = upd.model_dump(exclude_unset=True)
    assert "account_admin_client_id" not in dumped
    assert "account_admin_secret" not in dumped


def test_tenant_read_model_exposes_only_the_account_admin_arn():
    t = Tenant(
        id="ten-1", name="N", line_of_business="L", entra_group_ids=["g"],
        platform=TenantPlatform.DATABRICKS,
        stages={"dev": DatabricksStageConfig(workspace_url=WS, sp_client_id="sp-abc")},
        account_admin_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:x",
        created_by="u", created_at="t", updated_at="t",
    )
    dumped = t.model_dump()
    assert dumped["account_admin_secret_arn"].startswith("arn:aws:secretsmanager:")
    # The credential halves themselves are NOT read fields — only the pointer is.
    assert "account_admin_secret" not in dumped
    assert "account_admin_client_id" not in dumped


def test_account_admin_secret_arn_defaults_empty_for_a_pre_e29_record():
    t = Tenant.model_validate({
        "id": "ten-old", "name": "N", "line_of_business": "L", "entra_group_ids": ["g"],
        "stages": {"dev": {"account_id": "111111111111"}},
        "created_by": "u", "created_at": "t", "updated_at": "t",
    })
    assert t.account_admin_secret_arn == ""


# --------------------------------------------------------------------------- #
# E29/T3 — OB-11: the Databricks ACCOUNT id is a UUID, and it aims a path
#
# ``account_id`` is interpolated into the account-admin token-mint path
# (``/oidc/accounts/{account_id}/v1/token`` — the highest-privilege call in the Databricks
# client). ``databricks_workspace_service`` quotes it, which stops traversal at the URL
# layer; this regex stops a traversal-shaped value from ever being STORED. Two independent
# guards, because the one that fails silently is the one that was never executed — so this
# is EXECUTED over hostile inputs, per the epic's Global Constraints.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("account_id", [
    "12345678-1234-1234-1234-123456789abc",
    "00000000-0000-0000-0000-000000000000",
    "deadbeef-dead-beef-dead-beefdeadbeef",
])
def test_databricks_account_id_regex_accepts_uuids(account_id):
    from models.tenant import DATABRICKS_ACCOUNT_ID_RE

    assert DATABRICKS_ACCOUNT_ID_RE.fullmatch(account_id)


@pytest.mark.parametrize("hostile", [
    "a1/../..",                                       # path traversal — the OB-11 headline
    "a1/../../accounts/other/v1/token",               # re-aiming the account-admin mint
    "12345678-1234-1234-1234-123456789abc/../evil",
    "12345678-1234-1234-1234-123456789ABC",           # uppercase (Databricks emits lowercase)
    "12345678-1234-1234-1234-123456789abcd",          # too long
    "12345678-1234-1234-1234-123456789ab",            # too short
    "12345678_1234_1234_1234_123456789abc",           # underscores, not hyphens
    "12345678-1234-1234-1234-123456789abz",           # non-hex
    "12345678-1234-1234-1234-123456789abc\n",         # newline smuggling past a "$" anchor
    "12345678-1234-1234-1234-123456789abc\n../evil",
    " 12345678-1234-1234-1234-123456789abc",
    "12345678-1234-1234-1234-123456789abc ",
    "not-a-uuid",
    "%2e%2e%2f",
    "../",
])
def test_databricks_account_id_regex_rejects_hostile_inputs(hostile):
    from models.tenant import DATABRICKS_ACCOUNT_ID_RE

    assert DATABRICKS_ACCOUNT_ID_RE.fullmatch(hostile) is None
