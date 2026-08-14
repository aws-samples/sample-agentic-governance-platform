# tests/test_project_role_service.py — table_name="" (local fallback), injected clock. No moto.
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from models.project_role import ProjectRole, ProjectRoleCreate, role_name
from services.project_role_service import ProjectRoleError, ProjectRoleService

FIXED_TS = "2026-07-27T00:00:00+00:00"


def _svc() -> ProjectRoleService:
    return ProjectRoleService(table_name="", now=lambda: __import__("datetime").datetime.fromisoformat(FIXED_TS))


def _create(principal_id: str = "oid-1", role: str = "owner", ptype: str = "user") -> ProjectRoleCreate:
    return ProjectRoleCreate(principal_id=principal_id, principal_type=ptype, principal_display="Alex", role=role)


def test_role_ordering_is_meaningful():
    assert ProjectRole.OWNER > ProjectRole.MAINTAINER > ProjectRole.VIEWER
    assert role_name(ProjectRole.OWNER) == "owner"


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
    assert svc.owner_count("proj-1") == 1  # nothing persisted on failure


def test_revoking_a_non_last_owner_is_allowed():
    svc = _svc()
    svc.grant("proj-1", _create("oid-1", role="owner"), granted_by="a")
    svc.grant("proj-1", _create("oid-2", role="owner"), granted_by="a")
    svc.revoke("proj-1", "oid-1")
    assert svc.owner_count("proj-1") == 1


# --------------------------------------------------------------------------- #
# the last-owner guard covers the UPSERT verb too — a downgrade of the only owner
# is the same zero-owner lockout as revoking them, reachable via PUT instead of
# DELETE. Only an owner→lesser change on the LAST owner may be refused.
# --------------------------------------------------------------------------- #


def test_downgrading_the_only_owner_is_blocked():
    svc = _svc()
    svc.grant("proj-1", _create("oid-1", role="owner"), granted_by="a")
    with pytest.raises(ProjectRoleError) as ei:
        svc.grant("proj-1", _create("oid-1", role="viewer"), granted_by="a")
    assert ei.value.kind == "last_owner"
    # nothing persisted on the blocked path — the owner still holds the role
    assert svc.owner_count("proj-1") == 1
    assert [(r.principal_id, r.role) for r in svc.list_for_project("proj-1")] == [
        ("oid-1", "owner")
    ]


def test_downgrading_one_of_two_owners_is_allowed():
    svc = _svc()
    svc.grant("proj-1", _create("oid-1", role="owner"), granted_by="a")
    svc.grant("proj-1", _create("oid-2", role="owner"), granted_by="a")
    svc.grant("proj-1", _create("oid-1", role="maintainer"), granted_by="a")
    assert svc.owner_count("proj-1") == 1


def test_re_granting_the_only_owner_as_owner_is_allowed():
    """An idempotent re-grant never lowers the owner count, so the guard must not fire —
    otherwise the sole owner could not even refresh their own display name."""
    svc = _svc()
    svc.grant("proj-1", _create("oid-1", role="owner"), granted_by="a")
    rec = svc.grant("proj-1", _create("oid-1", role="owner"), granted_by="b")
    assert rec.granted_by == "b"
    assert svc.owner_count("proj-1") == 1


def test_granting_a_new_non_owner_beside_an_owner_is_allowed():
    """The guard keys on the EXISTING row's role, not the incoming one — a brand-new
    viewer/maintainer leaves the owner count untouched."""
    svc = _svc()
    svc.grant("proj-1", _create("oid-1", role="owner"), granted_by="a")
    svc.grant("proj-1", _create("oid-2", role="viewer"), granted_by="a")
    svc.grant("proj-1", _create("oid-3", role="maintainer"), granted_by="a")
    assert svc.owner_count("proj-1") == 1
    assert len(svc.list_for_project("proj-1")) == 3


@pytest.mark.parametrize("role", ["viewer", "maintainer", "owner"])
def test_the_first_grant_on_an_empty_project_is_always_allowed(role):
    """Zero owners is the SEED state, not a lockout — refusing here would make a fresh
    project impossible to govern at all."""
    svc = _svc()
    rec = svc.grant("proj-new", _create("oid-1", role=role), granted_by="a")
    assert rec.role == role


def test_revoke_unknown_principal_is_not_found():
    svc = _svc()
    with pytest.raises(ProjectRoleError) as ei:
        svc.revoke("proj-1", "ghost")
    assert ei.value.kind == "not_found"


# --------------------------------------------------------------------------- #
# composite-key injectivity — a '#' in either key half would alias two grants
# onto ONE sk (("a","b#c") and ("a#b","c") both encode to "a#b#c"), silently
# overwriting a grant. Rejected at every entry point that takes the ids.
# --------------------------------------------------------------------------- #


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
    """The pair that used to collapse onto "a#b#c": the second grant overwrote the first,
    list_all() returned 1 row, and the survivor reported project_id="a#b"."""
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


# --------------------------------------------------------------------------- #
# DDB mode — the composite-sk begins_with range read, _to_item/_from_item, and
# the read-degrades / write-propagates split. Fake table, no moto, no live AWS
# (mirrors test_project_service.py's _FakeTable idiom).
# --------------------------------------------------------------------------- #


class _FakeTable:
    """A minimal DDB Table double keyed on ("pk","sk"). ``query`` interprets the REAL
    boto3 condition the service builds — ``Key("pk").eq(...)`` alone, or ANDed with
    ``Key("sk").begins_with(...)`` — so prefix semantics are exercised, not simulated."""

    def __init__(self, items=None):
        self.items = list(items or [])
        self.queries = []  # every KeyConditionExpression the service issued

    # -- boto3 surface ------------------------------------------------------
    def query(self, **kwargs):
        expr = kwargs["KeyConditionExpression"]
        self.queries.append(expr)
        pk, prefix = _condition_parts(expr)
        rows = [i for i in self.items if i.get("pk") == pk]
        if prefix is not None:
            rows = [i for i in rows if str(i.get("sk", "")).startswith(prefix)]
        return {"Items": rows}

    def get_item(self, Key):  # noqa: N803 — boto3 param name
        for i in self.items:
            if i.get("pk") == Key["pk"] and i.get("sk") == Key["sk"]:
                return {"Item": i}
        return {}

    def put_item(self, Item):  # noqa: N803 — boto3 param name
        self.items = [
            i
            for i in self.items
            if not (i.get("pk") == Item["pk"] and i.get("sk") == Item["sk"])
        ]
        self.items.append(dict(Item))

    def delete_item(self, Key):  # noqa: N803 — boto3 param name
        self.items = [
            i
            for i in self.items
            if not (i.get("pk") == Key["pk"] and i.get("sk") == Key["sk"])
        ]


def _condition_parts(expr):
    """Unpack the service's KeyConditionExpression → (pk value, sk prefix or None)."""
    if expr.expression_operator == "AND":
        pk_cond, sk_cond = expr._values
        assert sk_cond.expression_operator == "begins_with"
        return pk_cond._values[1], sk_cond._values[1]
    return expr._values[1], None


def _ddb_svc(items=None):
    """Flip a service into DDB mode with a fake table (non-empty table_name + swapped table)."""
    svc = _svc()
    svc.table_name = "projects"
    svc._table = _FakeTable(items)
    assert svc._has_ddb is True
    return svc


def test_ddb_grant_stores_partition_and_composite_sk():
    svc = _ddb_svc()
    svc.grant("proj-1", _create("oid-1"), granted_by="admin-oid")
    keys = [(i["pk"], i["sk"]) for i in svc._table.items]
    assert keys == [("project_role", "proj-1#oid-1")]


def test_ddb_item_round_trips_through_to_item_and_from_item():
    svc = _ddb_svc()
    rec = svc.grant("proj-1", _create("oid-1", role="maintainer"), granted_by="admin-oid")

    item = svc._table.items[0]
    assert svc._from_item(item) == rec
    # pk/sk are key-only plumbing — they never leak into the model.
    dumped = svc._from_item(item).model_dump()
    assert "pk" not in dumped and "sk" not in dumped
    assert set(dumped) == set(rec.model_dump())


def test_ddb_list_for_project_issues_a_begins_with_range_read():
    svc = _ddb_svc()
    svc.grant("proj-1", _create("oid-1"), granted_by="a")
    svc._table.queries.clear()

    svc.list_for_project("proj-1")

    pk, prefix = _condition_parts(svc._table.queries[-1])
    assert pk == "project_role"
    assert prefix == "proj-1#"  # the separator is PART of the prefix — see next test


def test_ddb_list_for_project_does_not_bleed_across_a_prefix():
    svc = _ddb_svc()
    svc.grant("proj-1", _create("oid-1"), granted_by="a")
    svc.grant("proj-1-extra", _create("oid-2"), granted_by="a")

    assert [r.principal_id for r in svc.list_for_project("proj-1")] == ["oid-1"]
    assert [r.principal_id for r in svc.list_for_project("proj-1-extra")] == ["oid-2"]
    assert len(svc.list_all()) == 2  # list_all keeps the bare-pk whole-partition query


def test_ddb_reads_degrade_on_client_error():
    svc = _ddb_svc()
    boom = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Query")
    svc._table = MagicMock()
    svc._table.query.side_effect = boom
    svc._table.get_item.side_effect = boom

    assert svc.list_for_project("proj-1") == []
    assert svc.list_all() == []
    assert svc._get("proj-1", "oid-1") is None
    # owner_count is a READ too — the routes depend on it degrading rather than raising.
    assert svc.owner_count("proj-1") == 0


def test_ddb_writes_propagate_client_error():
    svc = _ddb_svc([_ROW_OWNER_1, _ROW_OWNER_2])
    boom = ClientError({"Error": {"Code": "AccessDeniedException"}}, "PutItem")
    table = MagicMock()
    table.put_item.side_effect = boom
    table.delete_item.side_effect = boom
    table.query.side_effect = lambda **kw: {"Items": [_ROW_OWNER_1, _ROW_OWNER_2]}
    svc._table = table

    with pytest.raises(ClientError):
        svc.grant("proj-1", _create("oid-1"), granted_by="a")
    with pytest.raises(ClientError):
        svc.revoke("proj-1", "oid-1")  # two owners, so the guard passes → delete raises


# --------------------------------------------------------------------------- #
# The public STRICT reads (E27/T4). ``has_role_rows`` / ``list_all_strict`` answer an
# AUTHORIZATION question, not a data one: "the partition is empty" and "the partition is
# unreadable" are the same value to a degrading read but OPPOSITE answers to "is this
# project governed?" — "ungoverned" hands out the design-§3 MAINTAINER fallback. So these
# two do NOT degrade, while their ``list_for_project`` / ``list_all`` siblings still must.
# --------------------------------------------------------------------------- #


def test_strict_reads_propagate_as_ownership_unverified():
    svc = _ddb_svc()
    boom = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Query")
    svc._table = MagicMock()
    svc._table.query.side_effect = boom

    with pytest.raises(ProjectRoleError) as ei:
        svc.has_role_rows("proj-1")
    assert ei.value.kind == "ownership_unverified"
    with pytest.raises(ProjectRoleError) as ei:
        svc.list_all_strict()
    assert ei.value.kind == "ownership_unverified"
    # ...while the DEGRADING siblings the read routes + the resolver depend on are unchanged.
    assert svc.list_for_project("proj-1") == []
    assert svc.list_all() == []
    assert svc.owner_count("proj-1") == 0


def test_strict_reads_answer_normally_when_the_partition_is_readable():
    """A readable-and-EMPTY partition is a real answer: ungoverned, not unverifiable."""
    svc = _ddb_svc()
    assert svc.has_role_rows("proj-1") is False
    assert svc.list_all_strict() == []

    svc.grant("proj-1", _create("oid-1"), granted_by="a")
    assert svc.has_role_rows("proj-1") is True
    assert svc.has_role_rows("proj-2") is False  # scoped per project, not partition-wide
    assert [r.project_id for r in svc.list_all_strict()] == ["proj-1"]


def test_list_for_project_strict_propagates_as_ownership_unverified():
    """E27/T11 — the ROSTER read has the same hazard one layer up: the console turns the
    list's EMPTINESS into a decision (``existingIds`` is what makes its Grant an ADD rather
    than an upsert-downgrade), so a swallowed ``ClientError`` would let "grant Viewer"
    silently demote a principal already holding Owner. Strict here, degrading sibling kept."""
    svc = _ddb_svc()
    boom = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Query")
    svc._table = MagicMock()
    svc._table.query.side_effect = boom

    with pytest.raises(ProjectRoleError) as ei:
        svc.list_for_project_strict("proj-1")
    assert ei.value.kind == "ownership_unverified"
    # The degrading sibling is untouched — the resolver + the migration inventory want it.
    assert svc.list_for_project("proj-1") == []


def test_list_for_project_strict_answers_normally_when_readable():
    """A readable-and-EMPTY roster is a real answer, and a populated one round-trips."""
    svc = _ddb_svc()
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
    """Same validation as every other entry point — a '#' makes the composite sk
    non-injective, so the store cannot answer for that id at all."""
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


class _PagedFakeTable(_FakeTable):
    """``query`` hands back one page at a time with a ``LastEvaluatedKey`` until the last —
    the real DDB continuation shape a >1MB partition produces."""

    def __init__(self, pages):
        super().__init__()
        self._pages = list(pages)

    def query(self, **kwargs):
        self.queries.append(kwargs)
        page = self._pages.pop(0)
        resp = {"Items": page}
        if self._pages:
            resp["LastEvaluatedKey"] = {"pk": page[-1]["pk"], "sk": page[-1]["sk"]}
        return resp


# --------------------------------------------------------------------------- #
# the last-owner guard is a WRITE decision, so its read must FAIL CLOSED. Reads
# elsewhere degrade to []/0, and [] is also the legitimate FIRST-grant state — so a
# swallowed ClientError would read as "no owners to protect" and let the very
# downgrade the guard exists to refuse through, stranding the project at zero owners.
# --------------------------------------------------------------------------- #


def test_an_unreadable_partition_refuses_a_sole_owner_downgrade():
    svc = _ddb_svc()
    boom = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Query")
    table = MagicMock()
    table.query.side_effect = boom
    svc._table = table

    with pytest.raises(ProjectRoleError) as ei:
        svc.grant("proj-1", _create("oid-1", role="viewer"), granted_by="a")
    assert ei.value.kind == "ownership_unverified"
    # persists NOTHING — the whole point: a read blip must not authorize a demotion.
    table.put_item.assert_not_called()


def test_a_genuinely_empty_partition_still_allows_the_first_grant():
    """The strict read must only fail closed on a FAILURE — a real empty partition is the
    seed state and the first grant (any role) still has to land."""
    svc = _ddb_svc()  # empty _FakeTable: the query succeeds and returns no rows
    rec = svc.grant("proj-new", _create("oid-1", role="viewer"), granted_by="a")
    assert rec.role == "viewer"
    assert [(i["pk"], i["sk"]) for i in svc._table.items] == [
        ("project_role", "proj-new#oid-1")
    ]


def test_an_unreadable_partition_does_not_block_an_owner_grant():
    """The guard early-exits on ``role == owner`` BEFORE it reads, so a store blip cannot
    block the one write that can never lower the owner count."""
    svc = _ddb_svc()
    svc._table = MagicMock()
    svc._table.query.side_effect = ClientError({"Error": {"Code": "Throttled"}}, "Query")

    rec = svc.grant("proj-1", _create("oid-1", role="owner"), granted_by="a")
    assert rec.role == "owner"
    svc._table.put_item.assert_called_once()


def test_ddb_scan_partition_follows_the_last_evaluated_key():
    """A paginated partition must yield BOTH pages. Dropping the continuation would
    silently UNDER-count rows — an owner could read back as absent (owner_count 0 →
    the last-owner guard passes) or as unprivileged in the resolver's role map."""
    svc = _ddb_svc()
    svc._table = _PagedFakeTable([[_ROW_OWNER_1], [_ROW_OWNER_2]])

    assert [r.principal_id for r in svc.list_all()] == ["oid-1", "oid-2"]
    assert svc._table.queries[1]["ExclusiveStartKey"] == {
        "pk": "project_role",
        "sk": "proj-1#oid-1",
    }


# --------------------------------------------------------------------------- #
# revoke_all — the project-deletion counterpart (E27 fix pass, review I1)
# --------------------------------------------------------------------------- #


def test_revoke_all_deletes_every_row_on_the_project_only():
    """Scoped: a project delete must not touch another project's governance."""
    svc = _svc()
    svc.grant("proj-1", _create("oid-1", role="owner"), granted_by="a")
    svc.grant("proj-1", _create("oid-2", role="viewer"), granted_by="a")
    svc.grant("proj-2", _create("oid-3", role="owner"), granted_by="a")

    assert svc.revoke_all("proj-1") == 2
    assert svc.list_for_project("proj-1") == []
    assert [r.principal_id for r in svc.list_for_project("proj-2")] == ["oid-3"]


def test_revoke_all_ignores_the_last_owner_guard():
    """It MUST bypass it — the guard protects a LIVE project from becoming unadministerable,
    but the project is gone, and applying it here would make the final owner row undeletable."""
    svc = _svc()
    svc.grant("proj-1", _create("oid-1", role="owner"), granted_by="a")
    assert svc.owner_count("proj-1") == 1  # the guard would refuse a plain revoke here
    assert svc.revoke_all("proj-1") == 1
    assert svc.list_for_project("proj-1") == []


def test_revoke_all_is_idempotent_and_already_gone_is_success():
    """E23 cascade idiom — a retried or partially-completed delete must be safe to re-run."""
    svc = _svc()
    svc.grant("proj-1", _create("oid-1"), granted_by="a")
    assert svc.revoke_all("proj-1") == 1
    assert svc.revoke_all("proj-1") == 0        # already gone
    assert svc.revoke_all("never-existed") == 0


def test_revoke_all_deletes_the_ddb_items():
    svc = _ddb_svc()
    svc.grant("proj-1", _create("oid-1"), granted_by="a")
    svc.grant("proj-1", _create("oid-2"), granted_by="a")
    assert svc.revoke_all("proj-1") == 2
    assert svc._table.items == []


def test_revoke_all_propagates_an_unreadable_partition():
    """Strict: deleting nothing because the partition was briefly unreadable is exactly the
    silent orphaning this method exists to prevent. The route logs and continues."""
    svc = _ddb_svc()
    svc._table = MagicMock()
    svc._table.query.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Query"
    )
    with pytest.raises(ProjectRoleError) as ei:
        svc.revoke_all("proj-1")
    assert ei.value.kind == "ownership_unverified"
    svc._table.delete_item.assert_not_called()


def test_revoke_all_rejects_a_separator_bearing_id():
    with pytest.raises(ProjectRoleError) as ei:
        _svc().revoke_all("proj#1")
    assert ei.value.kind == "validation"
