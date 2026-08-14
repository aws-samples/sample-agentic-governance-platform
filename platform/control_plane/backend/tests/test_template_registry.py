"""TemplateRegistry unit tests (E28B/T2).

The registry is the store that replaces GitHub's ``is_template`` discovery. These tests run
against the LOCAL fallback (no table name → no boto3, nothing touches AWS) except where a
DDB behaviour is specifically under test, in which case a fake table records the calls.

Covers: the connection-scoped range read, the derived-id upsert, deregistration,
``#``-rejection in both key halves, the pk/sk item shape, and the STRICT read posture (a
store fault raises rather than reading as an empty catalog).
"""

import pytest
from botocore.exceptions import ClientError

from services.template_registry import (
    TemplateRecord,
    TemplateRegistry,
    TemplateRegistryError,
    TemplateRegistryValidationError,
    template_id_for,
)

_CLOCK = "2026-08-02T00:00:00+00:00"


def _registry(**kwargs):
    """A local-fallback registry (no table name ⇒ no boto3, no AWS)."""
    return TemplateRegistry(now=lambda: _Fixed(), **kwargs)


class _Fixed:
    """A fixed clock whose ``.isoformat()`` is stable, matching the ``now()`` contract."""

    def isoformat(self):
        return _CLOCK


def _record(name="strands-agentcore", connection_id="conn1", **over):
    base = dict(
        id=template_id_for(name),
        name=name,
        description="A scaffold",
        source_url=f"https://github.com/acme/{name}",
        version="1",
        connection_id=connection_id,
        created_at=_CLOCK,
        created_by="op@x.com",
        framework="strands",
        aws_services=["lambda"],
        tags=["fsi"],
    )
    base.update(over)
    return TemplateRecord(**base)


# --- the pointer contract ----------------------------------------------------

def test_record_stores_a_pointer_not_contents():
    """D-B4: a record is a pointer. There is nowhere on it to put template contents.

    Amended in E28C/T2 (design D-C1) to add ``source_org``/``source_repo``. That is still a
    POINTER — a STRUCTURED one — and it is what makes the record *dereferenceable*: E28C reads
    the template's bytes from its repo at use-time, which needs an (org, repo) pair the seam's
    ``read_tree``/``read_repo`` can take positionally. It could not be derived from
    ``source_url``, which is contractually "any git URL" and not safely decomposable."""
    fields = set(TemplateRecord.model_fields)
    assert fields == {
        "id", "name", "description", "source_url", "version", "connection_id",
        "created_at", "created_by", "updated_at",
        # Catalog metadata the console renders (was GitHub topics, as unportable as
        # is_template) — describes the entry, not the template's contents.
        "framework", "aws_services", "tags",
        # The STRUCTURAL source (E28C/T2) — dereferenceable, unlike source_url.
        "source_org", "source_repo",
    }
    # No field carries file bytes, a zip, a tree or a file list.
    assert not {f for f in fields if f in {"files", "contents", "zip_bytes", "tree"}}


def test_source_url_may_point_outside_the_connected_org():
    """The thing ``is_template`` could not express: a template hosted anywhere."""
    reg = _registry()
    reg.put(_record(source_url="https://gitlab.com/someone-else/public-template"))
    (got,) = reg.list_for_connection("conn1")
    assert got.source_url == "https://gitlab.com/someone-else/public-template"


# --- the STRUCTURAL source pair (E28C/T2, design D-C1) -----------------------
# source_url is display-only and NOT decomposable ("any git URL"), so the pair AGP
# dereferences at materialize-time is stored, never parsed back out of the string.

def test_the_source_pair_round_trips_through_the_local_store():
    reg = _registry()
    reg.put(_record(source_org="acme", source_repo="strands-agentcore"))
    (got,) = reg.list_for_connection("conn1")
    assert got.source_org == "acme"
    assert got.source_repo == "strands-agentcore"


def test_the_source_pair_round_trips_through_ddb():
    """The pair must survive the item encode/decode, or a materialize would read the seed
    when the operator's iterated template repo was right there."""
    table = _FakeTable()
    reg = _ddb_registry(table)
    reg.put(_record(source_org="acme", source_repo="my-tpl"))

    (item,) = table.put_items
    assert item["source_org"] == "acme"
    assert item["source_repo"] == "my-tpl"

    table._items = table.put_items
    (read_back,) = reg.list_for_connection("conn1")
    assert read_back.source_org == "acme"
    assert read_back.source_repo == "my-tpl"


def test_a_pre_28c_record_has_no_source_pair_and_needs_no_migration():
    """NO migration: a record written before E28C simply lacks the pair, and both halves
    default to ``None``. ``None`` means "not dereferenceable" → the materialize path falls
    back to the on-disk seed (D-C2), which is why the default must not be ``""``: an empty
    string would read as a real org named nothing."""
    old_item = {
        "pk": "template",
        "sk": "conn1#legacy",
        "id": "legacy",
        "name": "legacy",
        "description": "registered before E28C",
        "source_url": "https://github.com/acme/legacy",
        "version": "1",
        "connection_id": "conn1",
        "created_at": _CLOCK,
    }
    record = TemplateRegistry._from_item(old_item)
    assert record.source_org is None
    assert record.source_repo is None


def test_the_source_pair_is_independent_of_source_url():
    """Recorded on purpose: nothing backfills the pair from the URL. A record may carry a
    display URL pointing at a mirror while the pair names the repo AGP actually reads."""
    reg = _registry()
    reg.put(
        _record(
            source_url="https://gitlab.com/mirror/tpl",
            source_org="acme",
            source_repo="tpl",
        )
    )
    (got,) = reg.list_for_connection("conn1")
    assert got.source_url == "https://gitlab.com/mirror/tpl"
    assert (got.source_org, got.source_repo) == ("acme", "tpl")


# --- discovery is a store read, scoped to one connection --------------------

def test_list_is_scoped_to_one_connection():
    reg = _registry()
    reg.put(_record(name="mine", connection_id="conn1"))
    reg.put(_record(name="theirs", connection_id="conn2"))

    assert [r.name for r in reg.list_for_connection("conn1")] == ["mine"]
    assert [r.name for r in reg.list_for_connection("conn2")] == ["theirs"]


def test_list_of_an_empty_catalog_is_empty():
    assert _registry().list_for_connection("conn1") == []


def test_get_returns_none_for_an_unregistered_name():
    reg = _registry()
    reg.put(_record(name="a"))
    assert reg.get("conn1", template_id_for("b")) is None


# --- the derived id makes put an upsert -------------------------------------

def test_put_is_an_upsert_keyed_on_the_derived_id():
    """Re-registering a name REPLACES its entry — a catalog must not grow duplicates of
    one template. The id is derived from the name, which is what guarantees this."""
    reg = _registry()
    reg.put(_record(description="first"))
    reg.put(_record(description="second", version="2"))

    rows = reg.list_for_connection("conn1")
    assert len(rows) == 1
    assert rows[0].description == "second"
    assert rows[0].version == "2"


def test_put_stamps_updated_at():
    reg = _registry()
    stored = reg.put(_record(updated_at=""))
    assert stored.updated_at == _CLOCK


# --- deregistration ---------------------------------------------------------

def test_delete_removes_only_the_named_entry():
    reg = _registry()
    reg.put(_record(name="keep"))
    reg.put(_record(name="drop"))

    reg.delete("conn1", template_id_for("drop"))

    assert [r.name for r in reg.list_for_connection("conn1")] == ["keep"]


def test_delete_is_idempotent():
    """E23 cascade idiom: already gone = success, so a retried delete is safe."""
    reg = _registry()
    reg.delete("conn1", template_id_for("never-existed"))  # must not raise


# --- the composite key must stay injective ---------------------------------

@pytest.mark.parametrize(
    "connection_id,template_id",
    [
        ("conn#1", "t"),   # separator in the left half
        ("conn1", "t#2"),  # separator in the right half
    ],
)
def test_hash_in_either_key_half_is_rejected(connection_id, template_id):
    """``("a","b#c")`` and ``("a#b","c")`` both encode to ``"a#b#c"``, so a ``#`` would let
    one write silently OVERWRITE another entry and read back the wrong connection_id."""
    reg = _registry()
    with pytest.raises(TemplateRegistryError):
        reg.get(connection_id, template_id)
    with pytest.raises(TemplateRegistryError):
        reg.delete(connection_id, template_id)
    with pytest.raises(TemplateRegistryError):
        reg.put(_record(connection_id=connection_id, id=template_id))


def test_empty_connection_id_is_rejected():
    with pytest.raises(TemplateRegistryError):
        _registry().list_for_connection("")


# --- validation and store faults are DIFFERENT exception types ---------------
# They map to opposite HTTP semantics: malformed input is permanent (422), a store fault is
# transient (503). One type for both told the console to retry the unretryable.

@pytest.mark.parametrize(
    "connection_id,template_id",
    [("", "t"), ("conn#1", "t"), ("conn1", ""), ("conn1", "t#2")],
)
def test_bad_keys_raise_the_VALIDATION_subclass(connection_id, template_id):
    reg = _registry()
    with pytest.raises(TemplateRegistryValidationError):
        reg.get(connection_id, template_id)


def test_the_validation_error_is_catchable_as_the_base_type():
    """The subclass relationship is deliberate: a caller that only cares "the registry
    refused" still catches it with the parent."""
    assert issubclass(TemplateRegistryValidationError, TemplateRegistryError)
    with pytest.raises(TemplateRegistryError):
        _registry().list_for_connection("conn#1")


def test_a_store_fault_is_NOT_the_validation_subclass():
    """The load-bearing half: a DDB fault must not be mistakable for bad input, or the
    strict-read guard silently becomes a 422 and the console stops retrying."""
    reg = _ddb_registry(_FakeTable(error=True))
    with pytest.raises(TemplateRegistryError) as exc:
        reg.list_for_connection("conn1")
    assert not isinstance(exc.value, TemplateRegistryValidationError)


def test_template_id_for_enforces_a_legal_key_component():
    """``template_id_for`` is an identity transform, so its REASON to exist is this check —
    the one place guaranteeing a name can be an ``sk`` half. Matters most on the rollout
    path, where names are on-disk dirnames that never meet the service's stricter regex."""
    assert template_id_for("strands-agentcore") == "strands-agentcore"
    with pytest.raises(TemplateRegistryValidationError):
        template_id_for("foo#bar")
    with pytest.raises(TemplateRegistryValidationError):
        template_id_for("")


# --- DDB item shape + the STRICT read posture -------------------------------

class _FakeTable:
    """Records DDB calls; optionally raises the ClientError the strict reads must surface."""

    def __init__(self, *, error=None, items=None):
        self._error = error
        self._items = items or []
        self.put_items = []
        self.deleted_keys = []

    def _boom(self):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Query")

    def query(self, **kwargs):
        if self._error:
            self._boom()
        return {"Items": self._items}

    def get_item(self, **kwargs):
        if self._error:
            self._boom()
        return {"Item": self._items[0]} if self._items else {}

    def put_item(self, *, Item):
        if self._error:
            self._boom()
        self.put_items.append(Item)

    def delete_item(self, *, Key):
        if self._error:
            self._boom()
        self.deleted_keys.append(Key)


def _ddb_registry(table):
    """A registry in DDB mode with the table swapped for a fake. The table NAME is a test
    fixture value, never a real resource — and no account id appears anywhere."""
    reg = TemplateRegistry(now=lambda: _Fixed())
    reg.table_name = "test-projects-table"
    reg._table = table
    return reg


def test_ddb_item_uses_the_template_partition_and_composite_sk():
    """pk="template" in the EXISTING projects table, sk="<connection_id>#<template_id>" —
    the partition idiom project_role_service uses (no new table, no GSI)."""
    table = _FakeTable()
    _ddb_registry(table).put(_record(name="strands-agentcore", connection_id="conn1"))

    (item,) = table.put_items
    assert item["pk"] == "template"
    assert item["sk"] == "conn1#strands-agentcore"
    assert item["name"] == "strands-agentcore"
    # pk/sk are keys, not payload — they are not duplicated back onto the model.
    assert "pk" not in TemplateRecord.model_fields


def test_delete_targets_the_composite_key():
    table = _FakeTable()
    _ddb_registry(table).delete("conn1", template_id_for("t"))
    assert table.deleted_keys == [{"pk": "template", "sk": "conn1#t"}]


def test_list_raises_rather_than_reporting_an_empty_catalog():
    """STRICT read: "no templates" and "the catalog is unreadable" are opposite answers to
    the operator — the first invites a re-upload of templates that already exist."""
    reg = _ddb_registry(_FakeTable(error=True))
    with pytest.raises(TemplateRegistryError):
        reg.list_for_connection("conn1")


def test_get_raises_rather_than_reporting_absent():
    reg = _ddb_registry(_FakeTable(error=True))
    with pytest.raises(TemplateRegistryError):
        reg.get("conn1", template_id_for("t"))


def test_put_raises_on_a_store_failure():
    reg = _ddb_registry(_FakeTable(error=True))
    with pytest.raises(TemplateRegistryError):
        reg.put(_record())


def test_store_errors_carry_a_safe_message():
    """The DDB error value (which can name the table) must never reach the message."""
    reg = _ddb_registry(_FakeTable(error=True))
    with pytest.raises(TemplateRegistryError) as exc:
        reg.list_for_connection("conn1")
    assert "test-projects-table" not in str(exc.value)
    assert "ProvisionedThroughputExceededException" not in str(exc.value)


def test_ddb_roundtrip_drops_the_keys_from_the_model():
    stored = _record()
    table = _FakeTable()
    reg = _ddb_registry(table)
    reg.put(stored)
    # Feed the written item straight back as a query result.
    table._items = table.put_items
    (read_back,) = reg.list_for_connection("conn1")
    assert read_back.name == stored.name
    assert read_back.framework == "strands"
    assert read_back.aws_services == ["lambda"]
    assert read_back.tags == ["fsi"]
