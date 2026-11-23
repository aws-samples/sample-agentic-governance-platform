"""TemplateRegistry unit tests.

The registry is the store that replaces GitHub's ``is_template`` discovery. It writes through
``core.state_store.StateStore``: with no table name the store is in memory and nothing touches
AWS; a fault path injects a raising table into the store, so a genuine botocore ``ClientError``
runs through the registry's strict-read translation.

Covers: the connection-scoped range read, the derived-id create and save, deregistration,
``#``-rejection in both key halves, the pk/sk item shape, and the STRICT read posture (a
store fault raises rather than reading as an empty catalog).
"""

import json
import pathlib
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from seed import seed

from core.state_store import StateStore, VersionMismatch
from services.template_registry import (
    TemplateRecord,
    TemplateRegistry,
    TemplateRegistryError,
    TemplateRegistryValidationError,
    template_id_for,
)

_CLOCK = "2026-08-02T00:00:00+00:00"


def _registry(**kwargs):
    """A registry over the in-memory store (no table name, so nothing touches AWS)."""
    return TemplateRegistry(now=lambda: _Fixed(), **kwargs)


class _Fixed:
    """A fixed clock whose ``.isoformat()`` is stable, matching the ``now()`` contract."""

    def isoformat(self):
        return _CLOCK


def _record(name="strands-agentcore", connection_id="conn1", **over):
    base: dict[str, Any] = dict(
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


def _faulting_registry():
    """A registry whose store's every table call raises a real botocore ``ClientError``. The
    table NAME is a test fixture value, never a real resource."""
    table = MagicMock()
    for method in ("get_item", "query", "put_item", "delete_item"):
        getattr(table, method).side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Query"
        )
    reg = _registry()
    reg._store = StateStore("test-projects-table", table=table)
    return reg


def _stored(reg, sk):
    item = reg._store.get(("template", sk))
    assert item is not None
    return item


def test_record_stores_a_pointer_not_contents():
    """A record is a pointer: there is nowhere on it to put template contents.

    The structural ``source_org``/``source_repo`` pair is still a pointer, the one the materialize
    path dereferences; it cannot be derived from ``source_url``, which is any git URL."""
    fields = set(TemplateRecord.model_fields)
    assert fields == {
        "id",
        "name",
        "description",
        "source_url",
        "version",
        "connection_id",
        "created_at",
        "created_by",
        "updated_at",
        "framework",
        "aws_services",
        "tags",
        "source_org",
        "source_repo",
        "infra_template_id",
        "preview",
        "artifact_kind",
    }
    assert not {f for f in fields if f in {"files", "contents", "zip_bytes", "tree"}}


def test_source_url_may_point_outside_the_connected_org():
    """The thing ``is_template`` could not express: a template hosted anywhere."""
    reg = _registry()
    reg.create(_record(source_url="https://gitlab.com/someone-else/public-template"))
    (got,) = reg.list_for_connection("conn1")
    assert got.source_url == "https://gitlab.com/someone-else/public-template"


def test_the_source_pair_round_trips_through_the_store():
    reg = _registry()
    reg.create(_record(source_org="acme", source_repo="strands-agentcore"))
    (got,) = reg.list_for_connection("conn1")
    assert got.source_org == "acme"
    assert got.source_repo == "strands-agentcore"


def test_the_source_pair_survives_the_stored_item():
    """The pair must survive the item encode/decode, or a materialize would read the seed
    when the operator's iterated template repo was right there."""
    reg = _registry()
    reg.create(_record(source_org="acme", source_repo="my-tpl"))

    item = _stored(reg, "conn1#strands-agentcore")
    assert item["source_org"] == "acme"
    assert item["source_repo"] == "my-tpl"

    (read_back,) = reg.list_for_connection("conn1")
    assert read_back.source_org == "acme"
    assert read_back.source_repo == "my-tpl"


def test_a_pre_28c_record_has_no_source_pair_and_needs_no_migration():
    """No migration: a record written before the source pair existed lacks it, and both halves
    default to ``None``, which means not dereferenceable, so the materialize path reads the
    on-disk seed. The default must not be ``""``: an empty string would read as a real org
    named nothing."""
    old_item = {
        "pk": "template",
        "sk": "conn1#legacy",
        "id": "legacy",
        "name": "legacy",
        "description": "registered before the source pair",
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
    reg.create(
        _record(
            source_url="https://gitlab.com/mirror/tpl",
            source_org="acme",
            source_repo="tpl",
        )
    )
    (got,) = reg.list_for_connection("conn1")
    assert got.source_url == "https://gitlab.com/mirror/tpl"
    assert (got.source_org, got.source_repo) == ("acme", "tpl")


def test_the_chain_fields_default_to_the_pinned_values():
    record = _record()
    assert record.infra_template_id == "agentcore-runtime"
    assert record.preview is False


def test_explicit_chain_values_round_trip_through_the_store():
    reg = _registry()
    reg.create(_record(infra_template_id="agentcore-harness", preview=True))
    (got,) = reg.list_for_connection("conn1")
    assert got.infra_template_id == "agentcore-harness"
    assert got.preview is True


def test_explicit_chain_values_survive_the_stored_item():
    """The pair must survive the item encode/decode: a harness template read back as an
    ``agentcore-runtime`` one would be deployed by the wrong module."""
    reg = _registry()
    reg.create(_record(infra_template_id="agentcore-harness", preview=True))

    item = _stored(reg, "conn1#strands-agentcore")
    assert item["infra_template_id"] == "agentcore-harness"
    assert item["preview"] is True

    (read_back,) = reg.list_for_connection("conn1")
    assert read_back.infra_template_id == "agentcore-harness"
    assert read_back.preview is True


def test_a_pre_e30_record_folds_to_the_pinned_defaults():
    """No migration and no backfill: a record written before the infra chain existed lacks
    both attributes and hydrates to the pinned defaults. ``agentcore-runtime`` is the only
    thing such a record could ever have been, so unlike ``source_org`` there is nothing
    truthful about ``None`` here; a null would make an existing catalog entry
    un-materializable."""
    old_item = {
        "pk": "template",
        "sk": "conn1#legacy",
        "id": "legacy",
        "name": "legacy",
        "description": "registered before the infra chain",
        "source_url": "https://github.com/acme/legacy",
        "version": "1",
        "connection_id": "conn1",
        "created_at": _CLOCK,
    }
    record = TemplateRegistry._from_item(old_item)
    assert record.infra_template_id == "agentcore-runtime"
    assert record.preview is False


def test_artifact_kind_defaults_to_agent():
    assert _record().artifact_kind == "agent"


def test_a_pre_e40_record_folds_to_the_agent_kind():
    old_item = {
        "pk": "template",
        "sk": "conn1#legacy",
        "id": "legacy",
        "name": "legacy",
        "connection_id": "conn1",
        "created_at": _CLOCK,
    }
    assert TemplateRegistry._from_item(old_item).artifact_kind == "agent"


def test_the_mcp_server_kind_survives_the_stored_item():
    reg = _registry()
    reg.create(_record(artifact_kind="mcp_server"))

    item = _stored(reg, "conn1#strands-agentcore")
    assert item["artifact_kind"] == "mcp_server"

    (read_back,) = reg.list_for_connection("conn1")
    assert read_back.artifact_kind == "mcp_server"


def test_an_unknown_artifact_kind_is_refused():
    with pytest.raises(ValueError):
        _record(artifact_kind="workflow")


def test_the_strands_seed_declares_the_agentcore_runtime_infra_template():
    """The scaffold AGP ships must name its infra template in ``template.json``, because that
    file is what the rollout reads to build the catalog record. A seed that declared nothing
    would only be materializable by the model default — correct today, and silently wrong the
    moment a second infra template exists."""
    path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "agent-templates"
        / "strands-agentcore"
        / "template.json"
    )
    metadata = json.loads(path.read_text())
    assert metadata["infra_template_id"] == "agentcore-runtime"


def test_list_is_scoped_to_one_connection():
    reg = _registry()
    reg.create(_record(name="mine", connection_id="conn1"))
    reg.create(_record(name="theirs", connection_id="conn2"))

    assert [r.name for r in reg.list_for_connection("conn1")] == ["mine"]
    assert [r.name for r in reg.list_for_connection("conn2")] == ["theirs"]


def test_list_of_an_empty_catalog_is_empty():
    assert _registry().list_for_connection("conn1") == []


def test_get_returns_none_for_an_unregistered_name():
    reg = _registry()
    reg.create(_record(name="a"))
    assert reg.get("conn1", template_id_for("b")) is None


def test_a_save_replaces_the_one_entry_keyed_on_the_derived_id():
    """Re-registering a name REPLACES its entry — a catalog must not grow duplicates of
    one template. The id is derived from the name, which is what guarantees this."""
    reg = _registry()
    reg.create(_record(description="first"))
    first = reg.get("conn1", template_id_for("strands-agentcore"))
    assert first is not None
    reg.save(first.model_copy(update={"description": "second", "version": "2"}))

    rows = reg.list_for_connection("conn1")
    assert len(rows) == 1
    assert rows[0].description == "second"
    assert rows[0].version == "2"


def test_create_on_an_absent_row_writes_it_at_row_version_1():
    reg = _registry()
    record = _record()

    assert reg.create(record) is True

    assert record._row_version == 1
    assert _stored(reg, "conn1#strands-agentcore")["row_version"] == 1
    got = reg.get("conn1", template_id_for("strands-agentcore"))
    assert got is not None
    read = got._row_version
    assert (read, type(read)) == (1, int)


def test_save_on_an_absent_row_raises_and_writes_nothing():
    reg = _registry()

    with pytest.raises(VersionMismatch):
        reg.save(_record())

    assert reg.get("conn1", template_id_for("strands-agentcore")) is None


def test_row_version_is_storage_only_and_never_the_content_version():
    reg = _registry()
    saved = _record(version="7")
    reg.create(saved)

    assert "row_version" not in saved.model_dump()
    assert "row_version" not in TemplateRecord.model_json_schema()["properties"]
    assert (_stored(reg, "conn1#strands-agentcore")["version"], saved.version) == ("7", "7")


def test_a_row_written_before_the_version_takes_one_versioned_save():
    reg = _registry()
    seed(reg._store, {"pk": "template", "sk": "conn1#strands-agentcore", **_record().model_dump()})
    legacy = reg.get("conn1", template_id_for("strands-agentcore"))
    assert legacy is not None
    assert legacy._row_version is None

    reg.save(legacy.model_copy(update={"description": "healed"}))

    row = _stored(reg, "conn1#strands-agentcore")
    assert (row["row_version"], row["description"]) == (1, "healed")
    with pytest.raises(VersionMismatch):
        reg.save(legacy.model_copy(update={"description": "stale"}))
    assert _stored(reg, "conn1#strands-agentcore")["description"] == "healed"


def test_of_two_readers_the_second_save_raises_and_the_first_stands():
    reg = _registry()
    reg.create(_record())
    a = reg.get("conn1", template_id_for("strands-agentcore"))
    assert a is not None
    b = reg.get("conn1", template_id_for("strands-agentcore"))
    assert b is not None

    reg.save(a.model_copy(update={"description": "from a"}))
    with pytest.raises(VersionMismatch):
        reg.save(b.model_copy(update={"description": "from b"}))

    row = _stored(reg, "conn1#strands-agentcore")
    assert (row["row_version"], row["description"]) == (2, "from a")


def test_a_create_over_a_registered_row_returns_false_and_writes_nothing():
    reg = _registry()
    reg.create(_record(description="registered"))

    assert reg.create(_record(description="blind")) is False

    row = _stored(reg, "conn1#strands-agentcore")
    assert (row["row_version"], row["description"]) == (1, "registered")


def test_a_save_over_a_deleted_row_raises_and_recreates_nothing():
    reg = _registry()
    reg.create(_record())
    read = reg.get("conn1", template_id_for("strands-agentcore"))
    assert read is not None
    reg.delete("conn1", template_id_for("strands-agentcore"))

    with pytest.raises(VersionMismatch):
        reg.save(read.model_copy(update={"description": "resurrected"}))

    assert reg.get("conn1", template_id_for("strands-agentcore")) is None


def test_a_save_of_an_unversioned_read_whose_row_was_deleted_raises_and_recreates_nothing():
    reg = _registry()
    seed(reg._store, {"pk": "template", "sk": "conn1#strands-agentcore", **_record().model_dump()})
    read = reg.get("conn1", template_id_for("strands-agentcore"))
    assert read is not None
    reg.delete("conn1", template_id_for("strands-agentcore"))

    with pytest.raises(VersionMismatch):
        reg.save(read.model_copy(update={"description": "resurrected"}))

    assert reg.get("conn1", template_id_for("strands-agentcore")) is None


def test_create_and_save_stamp_updated_at():
    reg = _registry()
    record = _record(updated_at="")
    reg.create(record)
    assert record.updated_at == _CLOCK
    assert reg.save(record.model_copy(update={"updated_at": ""})).updated_at == _CLOCK


def test_delete_removes_only_the_named_entry():
    reg = _registry()
    reg.create(_record(name="keep"))
    reg.create(_record(name="drop"))

    reg.delete("conn1", template_id_for("drop"))

    assert [r.name for r in reg.list_for_connection("conn1")] == ["keep"]


def test_delete_is_idempotent():
    """Already gone is success, so a retried delete is safe."""
    reg = _registry()
    reg.delete("conn1", template_id_for("never-existed"))  # must not raise


@pytest.mark.parametrize(
    "connection_id,template_id",
    [
        ("conn#1", "t"),  # separator in the left half
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
        reg.create(_record(connection_id=connection_id, id=template_id))
    with pytest.raises(TemplateRegistryError):
        reg.save(_record(connection_id=connection_id, id=template_id))


def test_empty_connection_id_is_rejected():
    with pytest.raises(TemplateRegistryError):
        _registry().list_for_connection("")


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
    """The load-bearing half: a store fault must not be mistakable for bad input, or the
    strict-read guard silently becomes a 422 and the console stops retrying."""
    reg = _faulting_registry()
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


def test_an_empty_table_name_selects_the_in_memory_store():
    assert _registry()._store.in_memory is True


def test_the_item_uses_the_template_partition_and_composite_sk():
    """pk="template" in the EXISTING projects table, sk="<connection_id>#<template_id>" —
    the partition idiom project_role_service uses (no new table, no GSI)."""
    reg = _registry()
    reg.create(_record(name="strands-agentcore", connection_id="conn1"))

    (item,) = reg._store.query("template")
    assert item["pk"] == "template"
    assert item["sk"] == "conn1#strands-agentcore"
    assert item["name"] == "strands-agentcore"
    assert "pk" not in TemplateRecord.model_fields


def test_delete_removes_the_row_under_the_composite_key():
    reg = _registry()
    reg.create(_record(name="t"))
    reg.create(_record(name="u"))

    reg.delete("conn1", template_id_for("t"))

    assert [row["sk"] for row in reg._store.query("template")] == ["conn1#u"]


def test_list_raises_rather_than_reporting_an_empty_catalog():
    """STRICT read: "no templates" and "the catalog is unreadable" are opposite answers to
    the operator — the first invites a re-upload of templates that already exist."""
    reg = _faulting_registry()
    with pytest.raises(TemplateRegistryError):
        reg.list_for_connection("conn1")


def test_get_raises_rather_than_reporting_absent():
    reg = _faulting_registry()
    with pytest.raises(TemplateRegistryError):
        reg.get("conn1", template_id_for("t"))


def test_create_and_save_raise_on_a_store_failure():
    reg = _faulting_registry()
    with pytest.raises(TemplateRegistryError):
        reg.create(_record())
    with pytest.raises(TemplateRegistryError):
        reg.save(_record())


def test_delete_raises_on_a_store_failure():
    reg = _faulting_registry()
    with pytest.raises(TemplateRegistryError):
        reg.delete("conn1", template_id_for("t"))


def test_store_errors_carry_a_safe_message():
    """The store's error value (which can name the table) must never reach the message."""
    reg = _faulting_registry()
    with pytest.raises(TemplateRegistryError) as exc:
        reg.list_for_connection("conn1")
    assert "test-projects-table" not in str(exc.value)
    assert "ProvisionedThroughputExceededException" not in str(exc.value)


def test_the_round_trip_drops_the_keys_from_the_model():
    stored = _record()
    reg = _registry()
    reg.create(stored)
    (read_back,) = reg.list_for_connection("conn1")
    assert read_back.name == stored.name
    assert read_back.framework == "strands"
    assert read_back.aws_services == ["lambda"]
    assert read_back.tags == ["fsi"]


def test_a_store_fault_renders_the_retryable_503_with_a_fixed_detail():
    reg = _faulting_registry()
    with pytest.raises(TemplateRegistryError) as exc:
        reg.list_for_connection("conn1")
    assert (exc.value.status_code, exc.value.kind) == (503, "store_error")
    assert exc.value.body() == {
        "detail": "could not read the template catalog",
        "code": "template.store_error",
    }


def test_a_malformed_key_renders_the_permanent_422_with_a_fixed_detail():
    with pytest.raises(TemplateRegistryValidationError) as exc:
        _registry().get("conn#1", "t")
    assert (exc.value.status_code, exc.value.kind) == (422, "validation")
    assert exc.value.body() == {
        "detail": "invalid template or connection identifier",
        "code": "template.validation",
    }
