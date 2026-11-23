"""Connection models: the read model carries no credential, the write models carry the right one per
auth type, and the org login is a trust-policy boundary."""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from models.connection import (
    ORG_LOGIN_ERROR,
    Connection,
    ConnectionAuthType,
    ConnectionCreate,
    ConnectionOAuthClient,
    ConnectionTokenReplace,
    ConnStatus,
    ManifestStart,
    Provider,
    validate_org_login,
)


def _conn(**over):
    base: dict[str, Any] = dict(
        id="c1",
        provider=Provider.GITHUB,
        org="acme",
        base_url=None,
        auth_type=ConnectionAuthType.PAT,
        status=ConnStatus.CONNECTED,
        status_detail=None,
        account_login="octocat",
        secret_arn="arn:aws:secretsmanager:...:c1",
        has_secret=True,
        last_verified_at="2026-06-30T00:00:00Z",
        created_by="admin@example.com",
        created_at="2026-06-30T00:00:00Z",
        updated_at="2026-06-30T00:00:00Z",
    )
    base.update(over)
    return Connection(**base)


def test_connection_round_trips_via_model_dump_json():
    c = _conn()
    clean = json.loads(c.model_dump_json())
    assert Connection.model_validate(clean) == c


def test_connection_read_model_has_no_token_field():
    assert "token" not in Connection.model_fields


def test_enums_serialize_to_their_string_values():
    c = _conn(provider=Provider.GITLAB, status=ConnStatus.ERROR)
    d = json.loads(c.model_dump_json())
    assert d["provider"] == "gitlab" and d["status"] == "error" and d["auth_type"] == "pat"


def test_create_and_replace_carry_the_write_only_token():
    cc = ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x")
    assert cc.token == "ghp_x" and cc.base_url is None
    assert ConnectionTokenReplace(token="ghp_y").token == "ghp_y"


def test_connection_client_id_defaults_none_and_has_oauth_client_false():
    c = _conn()
    assert c.client_id is None and c.has_oauth_client is False


def test_legacy_connection_item_without_the_new_fields_validates():
    """A stored item written before the OAuth client fields existed must still load."""
    assert _conn().has_oauth_client is False


def test_connection_carries_the_non_secret_client_id_beside_app_id():
    c = _conn(
        auth_type=ConnectionAuthType.GITHUB_APP, app_id="12345", client_id="Iv1.abc", has_oauth_client=True
    )
    d = json.loads(c.model_dump_json())
    assert d["app_id"] == "12345" and d["client_id"] == "Iv1.abc"
    assert d["has_oauth_client"] is True


def test_connection_has_no_client_secret_field():
    assert "client_secret" not in Connection.model_fields


def test_read_model_does_not_permit_extra_fields():
    """The `model_fields` guard is bypassable on its own: under `extra="allow"` pydantic RETAINS and
    SERIALIZES an undeclared `client_secret` while `"client_secret" not in model_fields` still passes.
    `extra="ignore"` (the inherited default) DISCARDS it, which is what makes the read model safe, so
    pin the config, not just the field list."""
    assert Connection.model_config.get("extra", "ignore") != "allow"


def test_oauth_client_input_carries_both_write_only_values():
    oc = ConnectionOAuthClient(client_id="Iv1.abc", client_secret="cs_x")
    assert oc.client_id == "Iv1.abc" and oc.client_secret == "cs_x"


def test_oauth_client_input_requires_both_fields():
    with pytest.raises(ValidationError):
        ConnectionOAuthClient.model_validate({"client_id": "Iv1.abc"})
    with pytest.raises(ValidationError):
        ConnectionOAuthClient.model_validate({"client_secret": "cs_x"})


def test_pat_and_app_create_shapes_are_unchanged():
    assert [a.value for a in ConnectionAuthType] == ["pat", "github_app"]


def test_the_auth_type_reaches_the_document_under_its_own_name():
    """Two enums named alike in one app made Pydantic qualify both by module; the connection one
    carries the frontend's name for it."""
    from main import app

    schemas = app.openapi()["components"]["schemas"]
    assert "models__connection__AuthType" not in schemas
    assert schemas["ConnectionAuthType"]["enum"] == ["pat", "github_app"]


def test_pat_create_still_validates_unchanged():
    cc = ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x")
    assert cc.auth_type == ConnectionAuthType.PAT
    with pytest.raises(ValidationError):
        ConnectionCreate(provider=Provider.GITHUB, org="acme")


def test_github_app_create_still_validates_unchanged():
    cc = ConnectionCreate(
        provider=Provider.GITHUB,
        org="acme",
        auth_type=ConnectionAuthType.GITHUB_APP,
        app_id="12345",
        installation_id="678",
        private_key="-----BEGIN...",
    )
    assert cc.private_key == "-----BEGIN..." and cc.token is None
    with pytest.raises(ValidationError):
        ConnectionCreate(
            provider=Provider.GITHUB,
            org="acme",
            auth_type=ConnectionAuthType.GITHUB_APP,
            app_id="12345",
        )


def test_create_input_takes_no_oauth_client_fields():
    """The OAuth client is supplied out-of-band (PUT .../oauth-client), never on create."""
    assert "client_secret" not in ConnectionCreate.model_fields
    assert "client_id" not in ConnectionCreate.model_fields


@pytest.mark.parametrize("org", ["acme", "AgenticOps-Platform", "a", "a1-b2-c3", "x" * 39])
def test_org_login_accepts_real_github_logins(org):
    assert validate_org_login(org) == org


@pytest.mark.parametrize(
    "hostile",
    [
        "*",
        "acme*",
        "?",
        "acme/other",
        "repo:acme",
        "a--b",
        "-acme",
        "acme-",
        "acme\n",
        "acme ",
        "acme.io",
        "acme_org",
        "",
        "x" * 40,
    ],
)
def test_org_login_refuses_every_character_that_could_widen_an_iam_trust_pattern(hostile):
    """`org` lands inside an IAM `StringLike` sub pattern where `*` and `?` are wildcards; the regex
    structurally excludes them, `/` and `:`, and `fullmatch` refuses the trailing newline `$` would
    tolerate."""
    with pytest.raises(ValueError, match="org must be"):
        validate_org_login(hostile)
    assert ORG_LOGIN_ERROR.startswith("org must be")


def test_both_doors_to_the_trust_policy_validate_the_org():
    """`ConnectionCreate` and `ManifestStart` both carry an org that reaches the trust document."""
    with pytest.raises(ValidationError, match="org must be"):
        ConnectionCreate(provider=Provider.GITHUB, org="*", token="ghp_x")
    with pytest.raises(ValidationError, match="org must be"):
        ManifestStart(org="*", redirect_url="https://app.example/callback")
    assert ManifestStart(org="acme", redirect_url="https://app.example/callback").org == "acme"
