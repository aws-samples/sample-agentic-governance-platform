"""ProjectRoleService over the state store's memory double with an injected clock; the fault paths
inject a raising table through the store. No moto, no live AWS."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from seed import seed

from core.state_store import StateStore
from models.project_role import ProjectRole, ProjectRoleCreate, role_name
from services.project_role_service import ProjectRoleError, ProjectRoleService

FIXED_TS = "2026-07-27T00:00:00+00:00"


def _svc() -> ProjectRoleService:
    return ProjectRoleService(table_name="", now=lambda: datetime.fromisoformat(FIXED_TS))


def _create(principal_id: str = "oid-1", role: str = "owner", ptype: str = "user") -> ProjectRoleCreate:
    return ProjectRoleCreate(
        principal_id=principal_id, principal_type=ptype, principal_display="Alex", role=role
    )


def test_role_ordering_is_meaningful():
    assert ProjectRole.OWNER > ProjectRole.MAINTAINER > ProjectRole.VIEWER
    assert role_name(ProjectRole.OWNER) == "owner"


@pytest.mark.parametrize(
    "kind,status,detail",
    [
        ("not_found", 404, "project role not found"),
        ("validation", 400, "invalid project role"),
        ("last_owner", 409, "project must keep at least one owner"),
        ("ownership_unverified", 503, "could not verify project ownership"),
        ("changed", 409, "Project role changed while this request ran; reload and retry"),
    ],
)
def test_each_kind_renders_its_fixed_detail_and_keeps_the_message_for_the_log(kind, status, detail):
    err = ProjectRoleError("arn:aws:dynamodb:secret-table", kind)

    assert (err.status_code, err.detail, err.code) == (status, detail, f"project_role.{kind}")
    assert err.body() == {"detail": detail, "code": f"project_role.{kind}"}
    assert err.message == "arn:aws:dynamodb:secret-table" and err.kind == kind


def test_an_unknown_kind_is_refused_at_construction():
    with pytest.raises(TypeError, match="unknown kind 'nope'"):
        ProjectRoleError("boom", "nope")


def test_grant_then_list_for_project():
    svc = _svc()
    rec = svc.grant("proj-1", _create(), granted_by="admin-oid")
    assert rec.project_id == "proj-1"
    assert rec.role == "owner"
    assert rec.granted_by == "admin-oid"
    assert rec.granted_at == FIXED_TS
    assert [r.principal_id for r in svc.list_for_project("proj-1")] == ["oid-1"]


def test_grant_is_upsert_not_duplicate():
    svc = _svc()
    svc.grant("proj-1", _create(role="viewer"), granted_by="a")
    svc.grant("proj-1", _create(role="owner"), granted_by="a")
    rows = svc.list_for_project("proj-1")
    assert len(rows) == 1
    assert rows[0].role == "owner"


def test_rows_are_scoped_per_project():
    svc = _svc()
    svc.grant("proj-1", _create("oid-1"), granted_by="a")
    svc.grant("proj-2", _create("oid-2"), granted_by="a")
    assert [r.principal_id for r in svc.list_for_project("proj-1")] == ["oid-1"]
    assert len(svc.list_all()) == 2


def test_invalid_role_rejected():
    svc = _svc()
    with pytest.raises(ProjectRoleError) as ei:
        svc.grant("proj-1", _create(role="admin"), granted_by="a")
    assert ei.value.kind == "validation"


def test_invalid_principal_type_rejected():
    svc = _svc()
    with pytest.raises(ProjectRoleError) as ei:
        svc.grant("proj-1", _create(ptype="serviceprincipal"), granted_by="a")
    assert ei.value.kind == "validation"


def test_revoke_removes_row():
    svc = _svc()
    svc.grant("proj-1", _create("oid-1", role="owner"), granted_by="a")
    svc.grant("proj-1", _create("oid-2", role="maintainer"), granted_by="a")
    svc.revoke("proj-1", "oid-2")
    assert [r.principal_id for r in svc.list_for_project("proj-1")] == ["oid-1"]


def test_revoking_last_owner_is_blocked():
    svc = _svc()
    svc.grant("proj-1", _create("oid-1", role="owner"), granted_by="a")
    svc.grant("proj-1", _create("oid-2", role="maintainer"), granted_by="a")
    with pytest.raises(ProjectRoleError) as ei:
        svc.revoke("proj-1", "oid-1")
    assert ei.value.kind == "last_owner"
    assert svc.owner_count("proj-1") == 1


def test_revoking_a_non_last_owner_is_allowed():
    svc = _svc()
    svc.grant("proj-1", _create("oid-1", role="owner"), granted_by="a")
    svc.grant("proj-1", _create("oid-2", role="owner"), granted_by="a")
    svc.revoke("proj-1", "oid-1")
    assert svc.owner_count("proj-1") == 1


def test_downgrading_the_only_owner_is_blocked():
    """The upsert verb can strand a project with zero owners exactly like a revoke can, so the guard
    covers it and persists nothing on the refused path."""
    svc = _svc()
    svc.grant("proj-1", _create("oid-1", role="owner"), granted_by="a")
    with pytest.raises(ProjectRoleError) as ei:
        svc.grant("proj-1", _create("oid-1", role="viewer"), granted_by="a")
    assert ei.value.kind == "last_owner"
    assert svc.owner_count("proj-1") == 1
    assert [(r.principal_id, r.role) for r in svc.list_for_project("proj-1")] == [("oid-1", "owner")]


def test_downgrading_one_of_two_owners_is_allowed():
    svc = _svc()
    svc.grant("proj-1", _create("oid-1", role="owner"), granted_by="a")
    svc.grant("proj-1", _create("oid-2", role="owner"), granted_by="a")
    svc.grant("proj-1", _create("oid-1", role="maintainer"), granted_by="a")
    assert svc.owner_count("proj-1") == 1


def test_re_granting_the_only_owner_as_owner_is_allowed():
    """An idempotent re-grant never lowers the owner count, so the guard must not fire; otherwise the
    sole owner could not even refresh their own display name."""
    svc = _svc()
    svc.grant("proj-1", _create("oid-1", role="owner"), granted_by="a")
    rec = svc.grant("proj-1", _create("oid-1", role="owner"), granted_by="b")
    assert rec.granted_by == "b"
    assert svc.owner_count("proj-1") == 1


def test_granting_a_new_non_owner_beside_an_owner_is_allowed():
    """The guard keys on the existing row's role, not the incoming one: a brand-new viewer or
    maintainer leaves the owner count untouched."""
    svc = _svc()
    svc.grant("proj-1", _create("oid-1", role="owner"), granted_by="a")
    svc.grant("proj-1", _create("oid-2", role="viewer"), granted_by="a")
    svc.grant("proj-1", _create("oid-3", role="maintainer"), granted_by="a")
    assert svc.owner_count("proj-1") == 1
    assert len(svc.list_for_project("proj-1")) == 3


@pytest.mark.parametrize("role", ["viewer", "maintainer", "owner"])
def test_the_first_grant_on_an_empty_project_is_always_allowed(role):
    """Zero owners is the seed state, not a lockout; refusing here would make a fresh project
    impossible to govern at all."""
    svc = _svc()
    rec = svc.grant("proj-new", _create("oid-1", role=role), granted_by="a")
    assert rec.role == role


def test_revoke_unknown_principal_is_not_found():
    svc = _svc()
    with pytest.raises(ProjectRoleError) as ei:
        svc.revoke("proj-1", "ghost")
    assert ei.value.kind == "not_found"


def test_separator_in_principal_id_rejected():
    svc = _svc()
    with pytest.raises(ProjectRoleError) as ei:
        svc.grant("a", _create("b#c"), granted_by="a")
    assert ei.value.kind == "validation"


def test_separator_in_project_id_rejected():
    svc = _svc()
    with pytest.raises(ProjectRoleError) as ei:
        svc.grant("a#b", _create("c"), granted_by="a")
    assert ei.value.kind == "validation"


def test_aliasing_grants_cannot_collide():
    """``("a", "b#c")`` and ``("a#b", "c")`` both encode to the sort key ``a#b#c``; without the
    separator check the second grant overwrote the first and the survivor read back the wrong project."""
    svc = _svc()
    for project_id, principal_id in (("a", "b#c"), ("a#b", "c")):
        with pytest.raises(ProjectRoleError):
            svc.grant(project_id, _create(principal_id), granted_by="a")
    assert svc.list_all() == []


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.list_for_project("a#b"),
        lambda s: s.owner_count("a#b"),
        lambda s: s.revoke("a#b", "oid-1"),
        lambda s: s.revoke("a", "b#c"),
    ],
)
def test_every_entry_point_rejects_the_separator(call):
    svc = _svc()
    with pytest.raises(ProjectRoleError) as ei:
        call(svc)
    assert ei.value.kind == "validation"


def test_an_empty_table_name_selects_the_in_memory_store():
    assert _svc()._store.in_memory is True


def _svc_over(table: MagicMock) -> ProjectRoleService:
    svc = _svc()
    svc._store = StateStore("projects", table=table)
    assert svc._store.in_memory is False
    return svc


def _unreadable_table() -> MagicMock:
    table = MagicMock()
    table.query.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Query"
    )
    return table


def _rows(svc: ProjectRoleService) -> list[dict]:
    return svc._store.query("project_role")


def test_grant_stores_partition_and_composite_sk():
    svc = _svc()
    svc.grant("proj-1", _create("oid-1"), granted_by="admin-oid")
    assert [(i["pk"], i["sk"]) for i in _rows(svc)] == [("project_role", "proj-1#oid-1")]


def test_item_round_trips_through_to_item_and_from_item():
    """``pk`` and ``sk`` are key plumbing and never reach the model."""
    svc = _svc()
    rec = svc.grant("proj-1", _create("oid-1", role="maintainer"), granted_by="admin-oid")

    item = _rows(svc)[0]
    assert svc._from_item(item) == rec
    dumped = svc._from_item(item).model_dump()
    assert "pk" not in dumped and "sk" not in dumped
    assert set(dumped) == set(rec.model_dump())


def test_list_for_project_issues_a_begins_with_range_read():
    table = MagicMock()
    table.query.return_value = {"Items": []}
    svc = _svc_over(table)

    svc.list_for_project("proj-1")

    condition = table.query.call_args.kwargs["KeyConditionExpression"].get_expression()
    assert condition["operator"] == "AND"
    pk_cond, sk_cond = (part.get_expression() for part in condition["values"])
    assert (pk_cond["operator"], pk_cond["values"][1]) == ("=", "project_role")
    assert (sk_cond["operator"], sk_cond["values"][1]) == ("begins_with", "proj-1#")


def test_list_for_project_does_not_bleed_across_a_prefix():
    """The separator is part of the prefix, so ``proj-1`` never reads ``proj-1-extra``'s rows."""
    svc = _svc()
    svc.grant("proj-1", _create("oid-1"), granted_by="a")
    svc.grant("proj-1-extra", _create("oid-2"), granted_by="a")

    assert [r.principal_id for r in svc.list_for_project("proj-1")] == ["oid-1"]
    assert [r.principal_id for r in svc.list_for_project("proj-1-extra")] == ["oid-2"]
    assert len(svc.list_all()) == 2


def test_reads_degrade_on_client_error():
    table = _unreadable_table()
    table.get_item.side_effect = table.query.side_effect
    svc = _svc_over(table)

    assert svc.list_for_project("proj-1") == []
    assert svc.list_all() == []
    assert svc._get("proj-1", "oid-1") is None
    assert svc.owner_count("proj-1") == 0


def test_writes_propagate_client_error():
    boom = ClientError({"Error": {"Code": "AccessDeniedException"}}, "PutItem")
    table = MagicMock()
    table.put_item.side_effect = boom
    table.delete_item.side_effect = boom
    table.get_item.return_value = {}
    table.query.side_effect = lambda **kw: {"Items": [_ROW_OWNER_1, _ROW_OWNER_2]}
    svc = _svc_over(table)

    with pytest.raises(ClientError):
        svc.grant("proj-1", _create("oid-1"), granted_by="a")
    with pytest.raises(ClientError):
        svc.revoke("proj-1", "oid-1")


def test_strict_reads_propagate_as_ownership_unverified():
    """``has_role_rows`` and ``list_all_strict`` answer an authorization question, so an unreadable
    partition must not read as "ungoverned"; their degrading siblings still return ``[]``."""
    svc = _svc_over(_unreadable_table())

    with pytest.raises(ProjectRoleError) as ei:
        svc.has_role_rows("proj-1")
    assert ei.value.kind == "ownership_unverified"
    with pytest.raises(ProjectRoleError) as ei:
        svc.list_all_strict()
    assert ei.value.kind == "ownership_unverified"
    assert svc.list_for_project("proj-1") == []
    assert svc.list_all() == []
    assert svc.owner_count("proj-1") == 0


def test_strict_reads_answer_normally_when_the_partition_is_readable():
    """A readable and empty partition is a real answer: ungoverned, not unverifiable."""
    svc = _svc()
    assert svc.has_role_rows("proj-1") is False
    assert svc.list_all_strict() == []

    svc.grant("proj-1", _create("oid-1"), granted_by="a")
    assert svc.has_role_rows("proj-1") is True
    assert svc.has_role_rows("proj-2") is False
    assert [r.project_id for r in svc.list_all_strict()] == ["proj-1"]


def test_list_for_project_strict_propagates_as_ownership_unverified():
    """The roster read: the console turns the list's emptiness into a decision, so a swallowed store
    fault would let a Viewer grant demote a principal already holding Owner. Strict here, the
    degrading sibling kept for the resolver and the migration inventory."""
    svc = _svc_over(_unreadable_table())

    with pytest.raises(ProjectRoleError) as ei:
        svc.list_for_project_strict("proj-1")
    assert ei.value.kind == "ownership_unverified"
    assert svc.list_for_project("proj-1") == []


def test_list_for_project_strict_answers_normally_when_readable():
    svc = _svc()
    assert svc.list_for_project_strict("proj-1") == []

    svc.grant("proj-1", _create("oid-1"), granted_by="a")
    svc.grant("proj-2", _create("oid-2"), granted_by="a")
    assert [r.principal_id for r in svc.list_for_project_strict("proj-1")] == ["oid-1"]
    assert [r.principal_id for r in svc.list_for_project_strict("proj-2")] == ["oid-2"]


def test_list_for_project_strict_rejects_a_separator_bearing_id():
    with pytest.raises(ProjectRoleError) as ei:
        _svc().list_for_project_strict("proj#1")
    assert ei.value.kind == "validation"


def test_has_role_rows_rejects_a_separator_bearing_id():
    with pytest.raises(ProjectRoleError) as ei:
        _svc().has_role_rows("proj#1")
    assert ei.value.kind == "validation"


def _row(principal_id: str, role: str = "owner", project_id: str = "proj-1") -> dict:
    return {
        "pk": "project_role",
        "sk": f"{project_id}#{principal_id}",
        "project_id": project_id,
        "principal_id": principal_id,
        "principal_type": "user",
        "principal_display": "Alex",
        "role": role,
        "granted_by": "a",
        "granted_at": FIXED_TS,
    }


_ROW_OWNER_1 = _row("oid-1")
_ROW_OWNER_2 = _row("oid-2")


def test_an_unreadable_partition_refuses_a_sole_owner_downgrade():
    """The last-owner guard is a write decision, so its read fails closed: ``[]`` is also the legitimate
    first-grant state, and a swallowed fault would read as "no owners to protect"."""
    table = _unreadable_table()
    svc = _svc_over(table)

    with pytest.raises(ProjectRoleError) as ei:
        svc.grant("proj-1", _create("oid-1", role="viewer"), granted_by="a")
    assert ei.value.kind == "ownership_unverified"
    table.put_item.assert_not_called()


def test_a_genuinely_empty_partition_still_allows_the_first_grant():
    svc = _svc()
    rec = svc.grant("proj-new", _create("oid-1", role="viewer"), granted_by="a")
    assert rec.role == "viewer"
    assert [(i["pk"], i["sk"]) for i in _rows(svc)] == [("project_role", "proj-new#oid-1")]


def test_an_unreadable_partition_does_not_block_an_owner_grant():
    """The guard exits on ``role == owner`` before it reads, so a store fault cannot block the one
    write that can never lower the owner count."""
    table = MagicMock()
    table.query.side_effect = ClientError({"Error": {"Code": "Throttled"}}, "Query")
    table.get_item.return_value = {}
    svc = _svc_over(table)

    rec = svc.grant("proj-1", _create("oid-1", role="owner"), granted_by="a")
    assert rec.role == "owner"
    table.put_item.assert_called_once()


_CHANGED = {
    "detail": "Project role changed while this request ran; reload and retry",
    "code": "project_role.changed",
}


def _after_the_read(monkeypatch, svc: ProjectRoleService, competitor) -> None:
    read = svc._store.get

    def read_then_compete(key, **kwargs):
        row = read(key, **kwargs)
        monkeypatch.setattr(svc._store, "get", read)
        competitor()
        return row

    monkeypatch.setattr(svc._store, "get", read_then_compete)


def test_a_first_grant_writes_version_one_and_each_re_grant_the_next():
    svc = _svc()
    svc.grant("proj-1", _create("oid-1"), granted_by="a")
    assert _rows(svc)[0]["row_version"] == 1

    svc.grant("proj-1", _create("oid-1"), granted_by="b")

    assert _rows(svc)[0]["row_version"] == 2


def test_two_re_grants_that_read_the_same_row_cannot_both_land(monkeypatch):
    svc = _svc()
    svc.grant("proj-1", _create("oid-1"), granted_by="a")
    svc.grant("proj-1", _create("oid-2", role="viewer"), granted_by="a")
    _after_the_read(
        monkeypatch, svc, lambda: svc.grant("proj-1", _create("oid-2", role="maintainer"), granted_by="b")
    )

    with pytest.raises(ProjectRoleError) as ei:
        svc.grant("proj-1", _create("oid-2", role="owner"), granted_by="c")

    assert (ei.value.kind, ei.value.status_code, ei.value.body()) == ("changed", 409, _CHANGED)
    assert [(r.role, r.granted_by) for r in svc.list_for_project("proj-1") if r.principal_id == "oid-2"] == [
        ("maintainer", "b")
    ]


def test_two_first_grants_for_one_principal_cannot_both_land(monkeypatch):
    svc = _svc()
    _after_the_read(monkeypatch, svc, lambda: svc.grant("proj-1", _create("oid-1"), granted_by="b"))

    with pytest.raises(ProjectRoleError) as ei:
        svc.grant("proj-1", _create("oid-1", role="maintainer"), granted_by="c")

    assert (ei.value.kind, ei.value.status_code, ei.value.body()) == ("changed", 409, _CHANGED)
    assert [(r.role, r.granted_by) for r in svc.list_for_project("proj-1")] == [("owner", "b")]


def test_a_re_grant_that_raced_a_revoke_answers_changed_and_does_not_resurrect_the_row(monkeypatch):
    svc = _svc()
    svc.grant("proj-1", _create("oid-1"), granted_by="a")
    svc.grant("proj-1", _create("oid-2", role="viewer"), granted_by="a")
    _after_the_read(monkeypatch, svc, lambda: svc.revoke("proj-1", "oid-2"))

    with pytest.raises(ProjectRoleError) as ei:
        svc.grant("proj-1", _create("oid-2", role="maintainer"), granted_by="c")

    assert (ei.value.kind, ei.value.status_code, ei.value.body()) == ("changed", 409, _CHANGED)
    assert [r.principal_id for r in svc.list_for_project("proj-1")] == ["oid-1"]


def test_a_re_grant_reads_its_row_strongly_consistent():
    svc = _svc()
    svc.grant("proj-1", _create("oid-1"), granted_by="a")
    table = MagicMock(wraps=svc._store._table)
    svc._store = StateStore("projects", table=table)

    svc.grant("proj-1", _create("oid-1"), granted_by="b")

    assert [c.kwargs["ConsistentRead"] for c in table.get_item.call_args_list] == [True]


def test_a_role_row_written_before_versioning_takes_one_versioned_re_grant():
    svc = _svc()
    seed(svc._store, _row("oid-1", role="viewer"))

    svc.grant("proj-1", _create("oid-1", role="maintainer"), granted_by="b")
    assert _rows(svc)[0]["row_version"] == 1
    svc.grant("proj-1", _create("oid-1"), granted_by="c")

    row = _rows(svc)[0]
    assert (row["row_version"], row["role"]) == (2, "owner")


def test_list_all_follows_the_last_evaluated_key():
    """Dropping the continuation would under-count rows: an owner could read back as absent and pass
    the last-owner guard, or as unprivileged in the resolver's role map."""
    table = MagicMock()
    table.query.side_effect = [
        {"Items": [_ROW_OWNER_1], "LastEvaluatedKey": {"pk": "project_role", "sk": "proj-1#oid-1"}},
        {"Items": [_ROW_OWNER_2]},
    ]
    svc = _svc_over(table)

    assert [r.principal_id for r in svc.list_all()] == ["oid-1", "oid-2"]
    assert table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {
        "pk": "project_role",
        "sk": "proj-1#oid-1",
    }


def test_revoke_all_deletes_every_row_on_the_project_only():
    svc = _svc()
    svc.grant("proj-1", _create("oid-1", role="owner"), granted_by="a")
    svc.grant("proj-1", _create("oid-2", role="viewer"), granted_by="a")
    svc.grant("proj-2", _create("oid-3", role="owner"), granted_by="a")

    assert svc.revoke_all("proj-1") == 2
    assert svc.list_for_project("proj-1") == []
    assert [r.principal_id for r in svc.list_for_project("proj-2")] == ["oid-3"]


def test_revoke_all_ignores_the_last_owner_guard():
    """The guard protects a live project from becoming unadministerable; the project is gone, and
    applying it here would make the final owner row undeletable."""
    svc = _svc()
    svc.grant("proj-1", _create("oid-1", role="owner"), granted_by="a")
    assert svc.owner_count("proj-1") == 1
    assert svc.revoke_all("proj-1") == 1
    assert svc.list_for_project("proj-1") == []


def test_revoke_all_is_idempotent_and_already_gone_is_success():
    svc = _svc()
    svc.grant("proj-1", _create("oid-1"), granted_by="a")
    assert svc.revoke_all("proj-1") == 1
    assert svc.revoke_all("proj-1") == 0
    assert svc.revoke_all("never-existed") == 0


def test_revoke_all_deletes_the_stored_rows():
    svc = _svc()
    svc.grant("proj-1", _create("oid-1"), granted_by="a")
    svc.grant("proj-1", _create("oid-2"), granted_by="a")
    assert svc.revoke_all("proj-1") == 2
    assert _rows(svc) == []


def test_revoke_all_propagates_an_unreadable_partition():
    """Deleting nothing because the partition was briefly unreadable is the silent orphaning this
    method exists to prevent; the route logs and continues."""
    table = _unreadable_table()
    svc = _svc_over(table)
    with pytest.raises(ProjectRoleError) as ei:
        svc.revoke_all("proj-1")
    assert ei.value.kind == "ownership_unverified"
    table.delete_item.assert_not_called()


def test_revoke_all_rejects_a_separator_bearing_id():
    with pytest.raises(ProjectRoleError) as ei:
        _svc().revoke_all("proj#1")
    assert ei.value.kind == "validation"
