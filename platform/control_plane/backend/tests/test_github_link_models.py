"""Model tests for the per-user GitHub account link (E27B/T2).

Pure Pydantic — no I/O, no AWS. Pins the two design invariants later tasks depend on:

  1. SECRET SEAM — ``GitHubUserLink`` is a read model and NEVER carries a token. It holds
     ``secret_arn`` only; the ``{"access_token", "refresh_token"}`` body lives in Secrets
     Manager (the ``connection``/``Connection.secret_arn`` idiom).
  2. The STABLE join key on GitHub's side is the numeric ``github_id`` (an ``int``), not the
     login — logins get renamed. ``github_login`` is a denormalized display label only.
"""

from __future__ import annotations

import json

from models.github_link import (
    GitHubLinkStatus,
    GitHubLinkView,
    GitHubUserLink,
    LinkCallbackRequest,
    LinkStartRequest,
    LinkStartResponse,
    LinkStatus,
    LinkableConnection,
)


def _link_kwargs(**over) -> dict:
    """The required-only ``GitHubUserLink`` kwargs."""
    base = dict(
        id="l-1",
        principal_oid="oid-1",
        connection_id="c-1",
        github_id=583231,
        github_login="octocat",
        status=LinkStatus.LINKED,
        secret_arn="arn:aws:secretsmanager:::x",
        created_at="2026-07-29T00:00:00+00:00",
        updated_at="2026-07-29T00:00:00+00:00",
    )
    base.update(over)
    return base


def test_link_has_no_token_field():
    assert "access_token" not in GitHubUserLink.model_fields
    assert "refresh_token" not in GitHubUserLink.model_fields


def test_link_round_trips_and_defaults():
    link = GitHubUserLink(id="l-1", principal_oid="oid-1", connection_id="c-1",
                          github_id=583231, github_login="octocat",
                          status=LinkStatus.LINKED, secret_arn="arn:aws:secretsmanager:::x",
                          created_at="2026-07-29T00:00:00+00:00",
                          updated_at="2026-07-29T00:00:00+00:00")
    assert link.token_version == 0
    assert link.access_token_expires_at is None
    assert GitHubUserLink(**link.model_dump()).github_id == 583231
    # JSON round-trip too — the wire shape is what T7/T8 persist and T10 renders.
    assert GitHubUserLink.model_validate(json.loads(link.model_dump_json())) == link


def test_github_id_is_an_int_not_a_string():
    # the STABLE join key; a login rename must not move it
    assert GitHubUserLink.model_fields["github_id"].annotation is int


def test_link_status_wire_values():
    assert [s.value for s in LinkStatus] == ["linked", "refreshing", "unlinked"]


def test_link_status_serializes_to_its_string_value():
    dumped = json.loads(GitHubUserLink(**_link_kwargs(status=LinkStatus.REFRESHING)).model_dump_json())
    assert dumped["status"] == "refreshing"


def test_link_optional_timestamps_round_trip():
    # every expiry/claim/verify timestamp is ADDITIVE — a row written without them validates
    # and reads as "unknown", and a row written with them survives a dump→load cycle.
    link = GitHubUserLink(**_link_kwargs(
        access_token_expires_at="2026-07-29T08:00:00+00:00",
        refresh_token_expires_at="2026-12-29T00:00:00+00:00",
        refresh_claimed_at="2026-07-29T07:59:00+00:00",
        last_verified_at="2026-07-29T07:00:00+00:00",
        token_version=3,
    ))
    reloaded = GitHubUserLink(**link.model_dump())
    assert reloaded.refresh_claimed_at == "2026-07-29T07:59:00+00:00"
    assert reloaded.refresh_token_expires_at == "2026-12-29T00:00:00+00:00"
    assert reloaded.token_version == 3


def test_legacy_row_without_optional_fields_validates():
    # a row missing every optional field must still load (additive-field contract)
    bare = GitHubUserLink(**_link_kwargs())
    assert bare.refresh_token_expires_at is None
    assert bare.refresh_claimed_at is None
    assert bare.last_verified_at is None


def test_required_identity_fields_have_no_defaults():
    for name in ("id", "principal_oid", "connection_id", "github_id", "github_login",
                 "status", "secret_arn", "created_at", "updated_at"):
        assert GitHubUserLink.model_fields[name].is_required(), name


def test_start_and_callback_request_shapes():
    start = LinkStartRequest(connection_id="c-1", redirect_uri="https://app.example/ops/github-link/callback")
    assert start.connection_id == "c-1"
    assert LinkStartResponse(authorize_url="https://github.com/login/oauth/authorize?x=1",
                             state="s-1").state == "s-1"
    cb = LinkCallbackRequest(code="abc", state="s-1")
    assert (cb.code, cb.state) == ("abc", "s-1")


def test_view_models_default_to_empty():
    view = GitHubLinkView()
    assert view.links == []
    assert view.connections == []
    # Pin the DECLARATION, not the isolation: pydantic v2 deep-copies a bare `= []` default too,
    # so appending to one instance and re-checking another passes either way and proves nothing.
    for name in ("links", "connections"):
        assert GitHubLinkView.model_fields[name].default_factory is list, name


def test_status_row_login_optional():
    # an unlinked row needs no login
    row = GitHubLinkStatus(connection_id="c-1", org="acme", linked=False, status="unlinked")
    assert row.github_login is None
    assert row.last_verified_at is None
    linked = GitHubLinkStatus(connection_id="c-1", org="acme", linked=True, status="linked",
                              github_login="octocat", last_verified_at="2026-07-29T07:00:00+00:00")
    assert GitHubLinkStatus.model_validate(json.loads(linked.model_dump_json())) == linked


def test_status_row_status_is_a_plain_str_on_the_wire():
    # GitHubLinkStatus.status is a plain str (the UI's three literals) — not the enum, so a
    # future status value can't 500 the read path.
    assert GitHubLinkStatus.model_fields["status"].annotation is str


def test_linkable_connection_carries_the_oauth_readiness_flag():
    c = LinkableConnection(connection_id="c-1", org="acme", oauth_client_ready=False)
    assert c.oauth_client_ready is False
    view = GitHubLinkView(connections=[c])
    assert GitHubLinkView.model_validate(json.loads(view.model_dump_json())) == view


_SECRET_FREE_MODELS = (GitHubUserLink, GitHubLinkStatus, LinkableConnection, GitHubLinkView,
                       LinkStartRequest, LinkStartResponse)


def test_models_carry_no_secret_material_anywhere():
    forbidden = {"access_token", "refresh_token", "client_secret", "code_verifier", "token"}
    for model in _SECRET_FREE_MODELS:
        assert not (forbidden & set(model.model_fields)), model.__name__


def test_no_model_permits_extra_fields():
    # The `model_fields` sweep above is bypassable on its own: under `extra="allow"` pydantic
    # RETAINS and SERIALIZES an undeclared `access_token` while `"access_token" not in
    # model_fields` still passes. `extra="ignore"` (the inherited default) DISCARDS it, which is
    # what actually makes these read models safe — so pin the config, not just the field list.
    for model in _SECRET_FREE_MODELS:
        assert model.model_config.get("extra", "ignore") != "allow", model.__name__
