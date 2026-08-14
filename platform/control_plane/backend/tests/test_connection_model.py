import pytest
from pydantic import ValidationError

from models.connection import (
    Provider, AuthType, ConnStatus, Connection, ConnectionCreate, ConnectionOAuthClient,
    ConnectionTokenReplace,
)


def _conn(**over):
    base = dict(
        id="c1", provider=Provider.GITHUB, org="acme", base_url=None,
        auth_type=AuthType.PAT, status=ConnStatus.CONNECTED, status_detail=None,
        account_login="octocat", secret_arn="arn:aws:secretsmanager:...:c1",
        has_secret=True, last_verified_at="2026-06-30T00:00:00Z",
        created_by="admin@example.com", created_at="2026-06-30T00:00:00Z",
        updated_at="2026-06-30T00:00:00Z",
    )
    base.update(over)
    return Connection(**base)


def test_connection_round_trips_via_model_dump_json():
    import json
    c = _conn()
    clean = json.loads(c.model_dump_json())
    assert Connection.model_validate(clean) == c


def test_connection_read_model_has_no_token_field():
    assert "token" not in Connection.model_fields


def test_enums_serialize_to_their_string_values():
    c = _conn(provider=Provider.GITLAB, status=ConnStatus.ERROR)
    import json
    d = json.loads(c.model_dump_json())
    assert d["provider"] == "gitlab" and d["status"] == "error" and d["auth_type"] == "pat"


def test_create_and_replace_carry_the_write_only_token():
    cc = ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x")
    assert cc.token == "ghp_x" and cc.base_url is None
    assert ConnectionTokenReplace(token="ghp_y").token == "ghp_y"


# --------------------------------------------------------------------------- #
# E27B — the OAuth client on a GitHub App connection (per-user link flow).
# --------------------------------------------------------------------------- #


def test_connection_client_id_defaults_none_and_has_oauth_client_false():
    c = _conn()
    assert c.client_id is None and c.has_oauth_client is False


def test_legacy_connection_item_without_the_new_fields_validates():
    # a pre-E27B DDB item must still load (additive-field contract)
    assert _conn().has_oauth_client is False


def test_connection_carries_the_non_secret_client_id_beside_app_id():
    c = _conn(auth_type=AuthType.GITHUB_APP, app_id="12345", client_id="Iv1.abc",
              has_oauth_client=True)
    import json
    d = json.loads(c.model_dump_json())
    assert d["app_id"] == "12345" and d["client_id"] == "Iv1.abc"
    assert d["has_oauth_client"] is True


def test_connection_has_no_client_secret_field():
    assert "client_secret" not in Connection.model_fields   # write-only, never on the read model


def test_read_model_does_not_permit_extra_fields():
    # The `model_fields` guard above is bypassable on its own: under `extra="allow"` pydantic
    # RETAINS and SERIALIZES an undeclared `client_secret` while `"client_secret" not in
    # model_fields` still passes. `extra="ignore"` (the inherited default) DISCARDS it, which is
    # what actually makes the read model safe — so pin the config, not just the field list.
    assert Connection.model_config.get("extra", "ignore") != "allow"


def test_oauth_client_input_carries_both_write_only_values():
    oc = ConnectionOAuthClient(client_id="Iv1.abc", client_secret="cs_x")
    assert oc.client_id == "Iv1.abc" and oc.client_secret == "cs_x"


def test_oauth_client_input_requires_both_fields():
    with pytest.raises(ValidationError):
        ConnectionOAuthClient(client_id="Iv1.abc")
    with pytest.raises(ValidationError):
        ConnectionOAuthClient(client_secret="cs_x")


def test_pat_and_app_create_shapes_are_unchanged():
    # regression: no third auth type was introduced
    assert [a.value for a in AuthType] == ["pat", "github_app"]


def test_pat_create_still_validates_unchanged():
    # regression: the validator's `elif not self.token` catch-all is untouched
    cc = ConnectionCreate(provider=Provider.GITHUB, org="acme", token="ghp_x")
    assert cc.auth_type == AuthType.PAT
    with pytest.raises(ValidationError):
        ConnectionCreate(provider=Provider.GITHUB, org="acme")


def test_github_app_create_still_validates_unchanged():
    # regression: app_id + installation_id + private_key remain the required triple
    cc = ConnectionCreate(
        provider=Provider.GITHUB, org="acme", auth_type=AuthType.GITHUB_APP,
        app_id="12345", installation_id="678", private_key="-----BEGIN...",
    )
    assert cc.private_key == "-----BEGIN..." and cc.token is None
    with pytest.raises(ValidationError):
        ConnectionCreate(
            provider=Provider.GITHUB, org="acme", auth_type=AuthType.GITHUB_APP,
            app_id="12345",
        )


def test_create_input_takes_no_oauth_client_fields():
    # the OAuth client is supplied out-of-band (PUT .../oauth-client), never on create
    assert "client_secret" not in ConnectionCreate.model_fields
    assert "client_id" not in ConnectionCreate.model_fields
