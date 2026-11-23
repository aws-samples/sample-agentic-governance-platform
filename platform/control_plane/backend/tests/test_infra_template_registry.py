"""InfraTemplateRegistry unit tests.

The infra-template registry is the FIFTH partition of the projects table and the thing that
makes "how is this template deployed?" a stored fact instead of a pattern-match on the
template name. It writes through ``core.state_store.StateStore``: with no table name the store
is in memory and nothing touches AWS; a fault path injects a raising table into the store — the
fixtures mirror ``tests/test_template_registry.py``.

Covers: the pinned record shape, the closed-set validation (``platform``/``build_kind``) and
the shape validation (``id``/``iac_type``), create/save/get/list/delete round-trips, the
connection-scoped range read, the pk/sk item shape, the STRICT read posture (a store fault
raises rather than reading as an empty catalog), and ``SEED_INFRA_TEMPLATES``.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from seed import seed

from core.state_store import StateStore, VersionMismatch
from services.infra_template_registry import (
    SEED_INFRA_TEMPLATES,
    InfraTemplateRecord,
    InfraTemplateRegistry,
    InfraTemplateRegistryError,
    InfraTemplateRegistryValidationError,
)

_CLOCK = "2026-08-06T00:00:00+00:00"


class _Fixed:
    """A fixed clock whose ``.isoformat()`` is stable, matching the ``now()`` contract."""

    def isoformat(self):
        return _CLOCK


def _registry(**kwargs):
    """A registry over the in-memory store (no table name, so nothing touches AWS)."""
    return InfraTemplateRegistry(now=lambda: _Fixed(), **kwargs)


def _record(id="agentcore-runtime", connection_id="conn1", **over):
    base: dict[str, Any] = dict(
        id=id,
        name="AgentCore Runtime",
        description="Container runtime on Bedrock AgentCore",
        platform="aws",
        iac_type="agentcore_runtime",
        build_kind="container",
        repo_name="agp-runtime-infra",
        module_source="runtime-module/agentcore_runtime.zip",
        version="1",
        connection_id=connection_id,
        created_at=_CLOCK,
        created_by="op@x.com",
    )
    base.update(over)
    return InfraTemplateRecord(**base)


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


def test_record_carries_the_pinned_fields_and_nothing_else():
    """The shape is a contract the rollout, the build service and the templates route read, so it
    is pinned by test.

    Like the agent-template record this is a POINTER: ``repo_name`` + ``module_source`` name
    where the IaC lives, they never carry it."""
    assert set(InfraTemplateRecord.model_fields) == {
        "id",
        "name",
        "description",
        "platform",
        "iac_type",
        "build_kind",
        "repo_name",
        "module_source",
        "version",
        "connection_id",
        "created_at",
        "created_by",
        "updated_at",
        "artifact_kinds",
    }


def test_record_defaults_match_the_contract():
    record = InfraTemplateRecord(
        id="agentcore-harness",
        name="AgentCore Harness",
        platform="aws",
        iac_type="agentcore_harness",
        build_kind="config",
        repo_name="agp-harness-infra",
        module_source="harness-module/agentcore_harness.zip",
        connection_id="conn1",
        created_at=_CLOCK,
    )
    assert record.description == ""
    assert record.version == "1"
    assert record.created_by == ""
    assert record.updated_at == ""


@pytest.mark.parametrize("platform", ["aws", "databricks"])
def test_platform_accepts_the_pinned_values(platform):
    assert _record(platform=platform).platform == platform


@pytest.mark.parametrize("platform", ["gcp", "AWS", "", "azure"])
def test_an_unknown_platform_is_refused(platform):
    """Bad platform → ValueError (pydantic's ValidationError is one). The compatibility join
    is ``infra.platform == tenant.platform``, so an unpinned string would make a template
    invisible to every tenant with no error anywhere."""
    with pytest.raises(ValueError):
        _record(platform=platform)


@pytest.mark.parametrize("build_kind", ["container", "config"])
def test_build_kind_accepts_the_pinned_values(build_kind):
    assert _record(build_kind=build_kind).build_kind == build_kind


@pytest.mark.parametrize("build_kind", ["Container", "image", "", "zip"])
def test_an_unknown_build_kind_is_refused(build_kind):
    """``build_kind`` decides whether materialize sets ECR vars at all — a typo would give a
    config build ECR vars it cannot use, or strip them from a container build."""
    with pytest.raises(ValueError):
        _record(build_kind=build_kind)


@pytest.mark.parametrize("id", ["agentcore-runtime", "agentcore-harness", "a", "a" * 64])
def test_id_accepts_kebab_case(id):
    assert _record(id=id).id == id


@pytest.mark.parametrize(
    "id",
    [
        "",  # empty
        "AgentCore",  # uppercase
        "1runtime",  # leading digit
        "-runtime",  # leading separator
        "agentcore_runtime",  # snake_case is the iac_type shape, not the id shape
        "foo#bar",  # would break the composite sk
        "a" * 65,  # too long
    ],
)
def test_a_malformed_id_is_refused(id):
    """``^[a-z][a-z0-9-]{0,63}$`` — the same shape as a template name, which is what lets the
    id be an ``sk`` half safely (it cannot contain the ``#`` separator)."""
    with pytest.raises(ValueError):
        _record(id=id)


@pytest.mark.parametrize("id", ["agentcore-runtime\n", "agentcore-runtime\n\n"])
def test_a_trailing_newline_in_the_id_is_refused(id):
    """``$`` matches before a final newline, so a ``re.match`` guard accepts ``"foo\\n"`` —
    which would become an ``sk`` half carrying an invisible character and read back as a
    different template than the one the operator seeded."""
    with pytest.raises(ValueError):
        _record(id=id)


@pytest.mark.parametrize("iac_type", ["agentcore_runtime\n", "agentcore_harness\n"])
def test_a_trailing_newline_in_the_iac_type_is_refused(iac_type):
    """Same ``$`` trap, worse consequence: ``iac_type`` is a buildspec branch key, so an
    invisible newline matches no branch and the build fails with nothing to point at."""
    with pytest.raises(ValueError):
        _record(iac_type=iac_type)


@pytest.mark.parametrize("field", ["name", "repo_name", "module_source"])
def test_padded_pointer_fields_are_stored_stripped(field):
    """The emptiness check tests ``.strip()``, so the STORED value must be stripped too —
    otherwise ``" agp-runtime-infra "`` passes validation and the rollout ensures a repo whose
    name has spaces in it, or stages a zip at an S3 key that does not match ``module_source``."""
    padded = _record(**{field: f"  {getattr(_record(), field)}  "})
    assert getattr(padded, field) == getattr(_record(), field)


@pytest.mark.parametrize("iac_type", ["agentcore_runtime", "agentcore_harness", "databricks_job"])
def test_iac_type_accepts_snake_case_branch_keys(iac_type):
    """``iac_type`` is an OPEN set, deliberately: it is a buildspec branch key, and every new
    deploy mechanism adds one. Only the shape is checked, so a third branch needs no edit
    here."""
    assert _record(iac_type=iac_type).iac_type == iac_type


@pytest.mark.parametrize("iac_type", ["", "AgentCore", "agentcore harness", "agentcore-harness"])
def test_a_malformed_iac_type_is_refused(iac_type):
    with pytest.raises(ValueError):
        _record(iac_type=iac_type)


@pytest.mark.parametrize("field", ["name", "repo_name", "module_source"])
def test_the_pointer_fields_must_be_non_empty(field):
    """An infra template with no repo or no module zip is not deployable by anything — the
    rollout would ensure a repo named "" and stage a zip at key ""."""
    with pytest.raises(ValueError):
        _record(**{field: ""})


def test_create_get_round_trip():
    reg = _registry()
    reg.create(_record())
    got = reg.get("conn1", "agentcore-runtime")
    assert got is not None
    assert got.iac_type == "agentcore_runtime"
    assert got.module_source == "runtime-module/agentcore_runtime.zip"


def test_get_returns_none_for_an_unregistered_id():
    reg = _registry()
    reg.create(_record())
    assert reg.get("conn1", "agentcore-harness") is None


def test_list_is_scoped_to_one_connection():
    reg = _registry()
    reg.create(_record(connection_id="conn1"))
    reg.create(_record(id="agentcore-harness", connection_id="conn2"))

    assert [r.id for r in reg.list("conn1")] == ["agentcore-runtime"]
    assert [r.id for r in reg.list("conn2")] == ["agentcore-harness"]


def test_list_of_an_empty_partition_is_empty():
    assert _registry().list("conn1") == []


def test_a_save_replaces_rather_than_duplicating():
    """The id is the operator-chosen catalog key, so re-seeding a connection must VERSION the
    one entry — a rollout that ran twice must not leave two ``agentcore-runtime`` rows."""
    reg = _registry()
    reg.create(_record(description="first"))
    first = reg.get("conn1", "agentcore-runtime")
    assert first is not None
    reg.save(first.model_copy(update={"description": "second", "version": "2"}))

    rows = reg.list("conn1")
    assert len(rows) == 1
    assert (rows[0].description, rows[0].version) == ("second", "2")


def _stored(reg, sk="conn1#agentcore-runtime"):
    item = reg._store.get(("infra_template", sk))
    assert item is not None
    return item


def test_create_on_an_absent_row_writes_it_at_row_version_1():
    reg = _registry()
    record = _record()

    assert reg.create(record) is True

    assert record._row_version == 1
    assert _stored(reg)["row_version"] == 1
    got = reg.get("conn1", "agentcore-runtime")
    assert got is not None
    read = got._row_version
    assert (read, type(read)) == (1, int)
    assert "row_version" not in record.model_dump()


def test_save_on_an_absent_row_raises_and_writes_nothing():
    reg = _registry()

    with pytest.raises(VersionMismatch):
        reg.save(_record())

    assert reg.get("conn1", "agentcore-runtime") is None


def test_a_create_over_a_registered_row_returns_false_and_writes_nothing():
    reg = _registry()
    reg.create(_record(description="registered"))

    assert reg.create(_record(description="blind")) is False

    assert (_stored(reg)["row_version"], _stored(reg)["description"]) == (1, "registered")


def test_a_row_written_before_the_version_takes_one_versioned_save():
    reg = _registry()
    seed(reg._store, {"pk": "infra_template", "sk": "conn1#agentcore-runtime", **_record().model_dump()})
    legacy = reg.get("conn1", "agentcore-runtime")
    assert legacy is not None
    assert legacy._row_version is None

    reg.save(legacy.model_copy(update={"description": "healed"}))

    assert (_stored(reg)["row_version"], _stored(reg)["description"]) == (1, "healed")
    with pytest.raises(VersionMismatch):
        reg.save(legacy.model_copy(update={"description": "stale"}))
    assert _stored(reg)["description"] == "healed"


def test_of_two_readers_the_second_save_raises_and_the_first_stands():
    reg = _registry()
    reg.create(_record())
    a = reg.get("conn1", "agentcore-runtime")
    assert a is not None
    b = reg.get("conn1", "agentcore-runtime")
    assert b is not None

    reg.save(a.model_copy(update={"description": "from a"}))
    with pytest.raises(VersionMismatch):
        reg.save(b.model_copy(update={"description": "from b"}))

    assert (_stored(reg)["row_version"], _stored(reg)["description"]) == (2, "from a")


def test_a_save_over_a_deleted_row_raises_and_recreates_nothing():
    reg = _registry()
    reg.create(_record())
    read = reg.get("conn1", "agentcore-runtime")
    assert read is not None
    reg.delete("conn1", "agentcore-runtime")

    with pytest.raises(VersionMismatch):
        reg.save(read.model_copy(update={"description": "resurrected"}))

    assert reg.get("conn1", "agentcore-runtime") is None


def test_a_save_of_an_unversioned_read_whose_row_was_deleted_raises_and_recreates_nothing():
    reg = _registry()
    seed(reg._store, {"pk": "infra_template", "sk": "conn1#agentcore-runtime", **_record().model_dump()})
    read = reg.get("conn1", "agentcore-runtime")
    assert read is not None
    reg.delete("conn1", "agentcore-runtime")

    with pytest.raises(VersionMismatch):
        reg.save(read.model_copy(update={"description": "resurrected"}))

    assert reg.get("conn1", "agentcore-runtime") is None


def test_create_and_save_stamp_updated_at():
    reg = _registry()
    record = _record()
    reg.create(record)
    assert record.updated_at == _CLOCK
    assert reg.save(record.model_copy(update={"updated_at": ""})).updated_at == _CLOCK


def test_delete_reports_whether_it_removed_anything():
    """``delete`` returns ``bool`` (pinned) — the rollout logs "deregistered" vs "already
    gone", but neither is an error, so a retried delete is safe."""
    reg = _registry()
    reg.create(_record())
    assert reg.delete("conn1", "agentcore-runtime") is True
    assert reg.delete("conn1", "agentcore-runtime") is False
    assert reg.list("conn1") == []


def test_delete_removes_only_the_named_entry():
    reg = _registry()
    reg.create(_record())
    reg.create(_record(id="agentcore-harness"))

    reg.delete("conn1", "agentcore-harness")

    assert [r.id for r in reg.list("conn1")] == ["agentcore-runtime"]


def test_a_hash_in_the_connection_id_is_rejected():
    """``("a","b#c")`` and ``("a#b","c")`` both encode to ``"a#b#c"``, so a ``#`` would let one
    write silently OVERWRITE another connection's entry. The id shape already excludes ``#``;
    the connection_id is caller-supplied, so it is checked here."""
    reg = _registry()
    with pytest.raises(InfraTemplateRegistryValidationError):
        reg.get("conn#1", "agentcore-runtime")
    with pytest.raises(InfraTemplateRegistryValidationError):
        reg.delete("conn#1", "agentcore-runtime")
    with pytest.raises(InfraTemplateRegistryValidationError):
        reg.list("conn#1")


def test_an_empty_connection_id_is_rejected():
    with pytest.raises(InfraTemplateRegistryValidationError):
        _registry().list("")


def test_the_validation_error_is_catchable_as_the_base_type():
    """Same subclass relationship as the template registry: callers that only care "the
    registry refused" catch the parent, callers that map to HTTP tell 422 from 503."""
    assert issubclass(InfraTemplateRegistryValidationError, InfraTemplateRegistryError)
    with pytest.raises(InfraTemplateRegistryError):
        _registry().list("conn#1")


def test_an_empty_table_name_selects_the_in_memory_store():
    assert _registry()._store.in_memory is True


def test_the_item_uses_the_infra_template_partition_and_composite_sk():
    """The FIFTH partition of the EXISTING projects table — no new table, no new env var, no
    GSI (the same idiom the template registry's fourth partition uses)."""
    reg = _registry()
    reg.create(_record())

    (item,) = reg._store.query("infra_template")
    assert item["pk"] == "infra_template"
    assert item["sk"] == "conn1#agentcore-runtime"
    assert item["iac_type"] == "agentcore_runtime"
    assert "pk" not in InfraTemplateRecord.model_fields


def test_delete_removes_the_row_under_the_composite_key():
    reg = _registry()
    reg.create(_record())
    reg.create(_record(id="agentcore-harness"))

    reg.delete("conn1", "agentcore-runtime")

    assert [row["sk"] for row in reg._store.query("infra_template")] == ["conn1#agentcore-harness"]


def test_the_round_trip_drops_the_keys_from_the_model():
    reg = _registry()
    reg.create(_record())
    (read_back,) = reg.list("conn1")
    assert read_back.id == "agentcore-runtime"
    assert read_back.repo_name == "agp-runtime-infra"


@pytest.mark.parametrize(
    "call",
    [
        lambda reg: reg.list("conn1"),
        lambda reg: reg.get("conn1", "agentcore-runtime"),
        lambda reg: reg.create(_record()),
        lambda reg: reg.save(_record()),
        lambda reg: reg.delete("conn1", "agentcore-runtime"),
    ],
)
def test_a_store_fault_raises_rather_than_degrading(call):
    """STRICT: "no infra templates" and "the partition is unreadable" are opposite answers —
    the first invites a re-seed of templates that already exist, or tells the console every
    agent template is incompatible with every tenant."""
    reg = _faulting_registry()
    with pytest.raises(InfraTemplateRegistryError) as exc:
        call(reg)
    assert not isinstance(exc.value, InfraTemplateRegistryValidationError)


def test_store_errors_carry_a_safe_message():
    """The store's error value (which can name the table) must never reach the message."""
    reg = _faulting_registry()
    with pytest.raises(InfraTemplateRegistryError) as exc:
        reg.list("conn1")
    assert "test-projects-table" not in str(exc.value)
    assert "ProvisionedThroughputExceededException" not in str(exc.value)


def test_the_seed_covers_every_infra_template_id():
    assert [t["id"] for t in SEED_INFRA_TEMPLATES] == [
        "agentcore-runtime",
        "agentcore-harness",
        "agentcore-gateway",
    ]


def test_every_seed_entry_validates_as_a_record():
    """The seed is connection-AGNOSTIC — ``connection_id``/``created_at`` are supplied by the
    rollout, per connection. Everything else must already be a legal record, or the failure
    surfaces during a rollout instead of here."""
    for entry in SEED_INFRA_TEMPLATES:
        assert "connection_id" not in entry
        record = InfraTemplateRecord(**entry, connection_id="conn1", created_at=_CLOCK)
        assert record.platform == "aws"


def test_the_seed_pins_the_runtime_and_harness_wiring():
    """These values are consumed by the rollout (repo to ensure) and the staged-zip S3 key.
    They are pinned by test because a drifted zip key fails LIVE, silently, by deploying the
    previous module."""
    runtime, harness, _gateway = SEED_INFRA_TEMPLATES

    assert runtime["iac_type"] == "agentcore_runtime"
    assert runtime["build_kind"] == "container"
    assert runtime["repo_name"] == "agp-runtime-infra"
    assert runtime["module_source"] == "runtime-module/agentcore_runtime.zip"

    assert harness["iac_type"] == "agentcore_harness"
    assert harness["build_kind"] == "config"
    assert harness["repo_name"] == "agp-harness-infra"
    assert harness["module_source"] == "harness-module/agentcore_harness.zip"


def test_the_seed_entries_are_not_shared_mutable_state():
    """``SEED_INFRA_TEMPLATES`` is module-level, so a caller that mutates an entry (the
    rollout adds ``connection_id``) must not corrupt it for the next connection."""
    reg = _registry()
    for entry in SEED_INFRA_TEMPLATES:
        reg.create(InfraTemplateRecord(**entry, connection_id="conn1", created_at=_CLOCK))
    assert all("connection_id" not in entry for entry in SEED_INFRA_TEMPLATES)
    assert len(reg.list("conn1")) == len(SEED_INFRA_TEMPLATES)


def test_artifact_kinds_default_to_agent_only():
    """Every infra template before MCP servers existed deployed agents, so an omitted attribute
    (an older item, or the two agent seeds) folds to ``["agent"]`` with no migration."""
    assert _record().artifact_kinds == ["agent"]
    runtime, harness, _gateway = SEED_INFRA_TEMPLATES
    for entry in (runtime, harness):
        record = InfraTemplateRecord(**entry, connection_id="conn1", created_at=_CLOCK)
        assert record.artifact_kinds == ["agent"]


def test_an_unknown_artifact_kind_is_refused():
    with pytest.raises(ValueError):
        _record(artifact_kinds=["workflow"])


def test_the_gateway_seed_pins_its_wiring_and_deploys_only_mcp_servers():
    """The build's gateway arm keys on ``iac_type``; the rollout stages ``module_source`` and
    ensures ``repo_name`` — a drift in any fails live, not here."""
    gateway = next(t for t in SEED_INFRA_TEMPLATES if t["id"] == "agentcore-gateway")
    record = InfraTemplateRecord(**gateway, connection_id="conn1", created_at=_CLOCK)
    assert record.name == "AgentCore Gateway"
    assert record.platform == "aws"
    assert record.iac_type == "agentcore_gateway"
    assert record.build_kind == "config"
    assert record.repo_name == "agp-gateway-infra"
    assert record.module_source == "gateway-module/agentcore_gateway.zip"
    assert record.artifact_kinds == ["mcp_server"]


def test_artifact_kinds_round_trip_through_the_store():
    reg = _registry()
    reg.create(_record(artifact_kinds=["mcp_server"]))
    (read_back,) = reg.list("conn1")
    assert read_back.artifact_kinds == ["mcp_server"]


def test_a_store_fault_renders_the_retryable_503_with_a_fixed_detail():
    reg = _faulting_registry()
    with pytest.raises(InfraTemplateRegistryError) as exc:
        reg.list("conn1")
    assert (exc.value.status_code, exc.value.kind) == (503, "store_error")
    assert exc.value.body() == {
        "detail": "Infra template catalog is temporarily unavailable",
        "code": "infra_template.store_error",
    }


def test_a_malformed_key_renders_the_permanent_422_with_a_fixed_detail():
    with pytest.raises(InfraTemplateRegistryValidationError) as exc:
        _registry().list("conn#1")
    assert (exc.value.status_code, exc.value.kind) == (422, "validation")
    assert exc.value.body() == {
        "detail": "Invalid infra template request",
        "code": "infra_template.validation",
    }
