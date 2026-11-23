"""The connection-scoped template catalog over a fake GitHub write client, a fake connection
service and a real ``TemplateRegistry`` over the in-memory store (no table name, so no AWS).

The real registry rather than a fake store is deliberate: the upsert-on-derived-id behaviour that
makes a re-upload a version bump is the registry's, and a fake would be free to duplicate where the
real store cannot."""

import inspect
import io
import zipfile
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from seed import seed

from core.state_store import StateStore
from services.connection_service import ConnectionError
from services.github_repo_errors import GitHubRepoError
from services.github_template_service import (
    GitHubTemplateError,
    GitHubTemplateService,
    TemplateView,
)
from services.template_registry import (
    TemplateRecord,
    TemplateRegistry,
    TemplateRegistryError,
    template_id_for,
)

TOKEN = "tok"


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr("app.py", "print('hi')\n")
    return buf.getvalue()


Z = _zip_bytes()


class FakeGitHubRepoService:
    """Records the calls the template service makes against the GitHub write client. It has no
    ``list_template_repos`` and no ``set_repo_metadata``, so a service that regressed to calling
    either fails with ``AttributeError`` rather than passing against a generous fake."""

    def __init__(self):
        self.created = None
        self.deleted = None

    def create_repo_from_zip(self, org, repo_name, zip_bytes, token, base_url=None):
        self.created = (org, repo_name, zip_bytes, base_url)
        return f"https://github.com/{org}/{repo_name}"

    def delete_repo(self, org, repo, token, base_url=None):
        self.deleted = (org, repo)


class FakeConnectionService:
    def __init__(self, org="acme", base_url=None, token=TOKEN):
        self._conn = SimpleNamespace(org=org, base_url=base_url)
        self._token = token
        self.got = None

    def get_connection(self, id):
        self.got = id
        return self._conn

    def get_bearer_token(self, id):
        return self._token


class _Fixed:
    def isoformat(self):
        return "2026-08-02T00:00:00+00:00"


@pytest.fixture
def fake_gh():
    return FakeGitHubRepoService()


@pytest.fixture
def registry():
    return TemplateRegistry(now=lambda: _Fixed())


@pytest.fixture
def svc(fake_gh, registry):
    return GitHubTemplateService(
        github_repo_service=fake_gh,
        connection_service=FakeConnectionService(),
        template_registry=registry,
        now=lambda: "2026-07-13T00:00:00+00:00",
    )


def _seed(registry, name="strands-agentcore", connection_id="conn1", **over):
    """Register a template directly, bypassing upload: the operator's catalog as it stands."""
    base: dict[str, Any] = dict(
        id=template_id_for(name),
        name=name,
        description="d",
        source_url=f"https://github.com/acme/{name}",
        version="1",
        connection_id=connection_id,
        created_at="2026-07-01T00:00:00+00:00",
        created_by="op@x.com",
        framework="strands",
        aws_services=["lambda"],
        tags=["fsi"],
    )
    base.update(over)
    registry.create(TemplateRecord(**base))


def _upload(svc, connection_id="conn1", **over):
    kwargs: dict[str, Any] = dict(
        zip_bytes=Z, name="my-tpl", description="", framework="strands", aws_services=[], tags=[]
    )
    kwargs.update(over)
    return svc.upload_template(connection_id, **kwargs)


class _RacedRegistry(TemplateRegistry):
    def __init__(self):
        super().__init__(now=lambda: _Fixed())
        self.rival: Callable[[], object] | None = None

    def get(self, connection_id, template_id, *, consistent=False):
        record = super().get(connection_id, template_id, consistent=consistent)
        rival, self.rival = self.rival, None
        if rival is not None:
            rival()
        return record


_CHANGED_BODY = {
    "detail": "GitHub template changed while this request ran; reload and retry",
    "code": "github_template.changed",
}


@pytest.mark.parametrize("prior_uploads,rival_version", [(0, "1"), (1, "2")])
def test_two_uploads_of_one_name_from_the_same_previous_answer_changed_for_one(
    fake_gh, prior_uploads, rival_version
):
    registry = _RacedRegistry()
    svc = GitHubTemplateService(
        github_repo_service=fake_gh, connection_service=FakeConnectionService(), template_registry=registry
    )
    for _ in range(prior_uploads):
        _upload(svc, description="earlier")
    registry.rival = lambda: _upload(svc, description="rival")

    with pytest.raises(GitHubTemplateError) as exc:
        _upload(svc, description="loser")

    assert (exc.value.status_code, exc.value.body()) == (409, _CHANGED_BODY)
    (row,) = registry.list_for_connection("conn1")
    assert (row.description, row.version) == ("rival", rival_version)


def test_a_patch_that_raced_another_patch_answers_changed_and_keeps_the_other(fake_gh):
    registry = _RacedRegistry()
    svc = GitHubTemplateService(
        github_repo_service=fake_gh, connection_service=FakeConnectionService(), template_registry=registry
    )
    _seed(registry)
    registry.rival = lambda: svc.patch_template("conn1", "strands-agentcore", description="rival")

    with pytest.raises(GitHubTemplateError) as exc:
        svc.patch_template("conn1", "strands-agentcore", tags=["loser"])

    assert (exc.value.status_code, exc.value.body()) == (409, _CHANGED_BODY)
    (row,) = registry.list_for_connection("conn1")
    assert (row.description, row.tags) == ("rival", ["fsi"])


def test_a_patch_that_raced_a_delete_answers_changed_and_recreates_nothing(fake_gh):
    registry = _RacedRegistry()
    svc = GitHubTemplateService(
        github_repo_service=fake_gh, connection_service=FakeConnectionService(), template_registry=registry
    )
    _seed(registry)
    registry.rival = lambda: registry.delete("conn1", template_id_for("strands-agentcore"))

    with pytest.raises(GitHubTemplateError) as exc:
        svc.patch_template("conn1", "strands-agentcore", description="resurrected")

    assert exc.value.code == "github_template.changed"
    assert registry.list_for_connection("conn1") == []


def test_a_reupload_over_a_row_written_before_the_version_saves_once(svc, registry):
    legacy = TemplateRecord(
        id="my-tpl", name="my-tpl", connection_id="conn1", created_at="2026-07-01T00:00:00+00:00"
    )
    seed(registry._store, {"pk": "template", "sk": "conn1#my-tpl", **legacy.model_dump()})

    view = _upload(svc, description="healed")

    assert view.description == "healed"
    row = registry._store.get(("template", "conn1#my-tpl"))
    assert (row["row_version"], row["version"]) == (1, "2")


def test_the_reads_before_an_upload_or_a_patch_are_strongly_consistent_and_a_delete_read_is_not(
    svc, registry
):
    table = MagicMock(wraps=registry._store._table)
    registry._store = StateStore("projects", table=table)

    _upload(svc, description="first")
    _upload(svc, description="second")
    svc.patch_template("conn1", "my-tpl", description="third")
    svc.delete_template("conn1", "my-tpl")

    assert [c.kwargs["ConsistentRead"] for c in table.get_item.call_args_list] == [True, True, True, False]


def test_list_templates_returns_the_registered_catalog(svc, registry):
    _seed(registry)
    views = svc.list_templates("conn1")
    assert views[0].name == "strands-agentcore"
    assert views[0].framework == "strands"
    assert views[0].aws_services == ["lambda"]
    assert views[0].tags == ["fsi"]


def test_list_templates_makes_no_provider_call(svc, registry, fake_gh):
    _seed(registry)
    svc.list_templates("conn1")
    assert fake_gh.created is None
    assert fake_gh.deleted is None


def test_list_is_scoped_to_the_connection(svc, registry):
    _seed(registry, name="mine", connection_id="conn1")
    _seed(registry, name="theirs", connection_id="conn2")
    assert [v.name for v in svc.list_templates("conn1")] == ["mine"]


def test_list_serves_html_url_from_source_url(svc, registry):
    """The pointer may name a repo outside the connected org."""
    _seed(registry, source_url="https://gitlab.com/other/tpl")
    assert svc.list_templates("conn1")[0].html_url == "https://gitlab.com/other/tpl"


def test_list_of_an_empty_catalog_is_empty(svc):
    assert svc.list_templates("conn1") == []


def test_upload_creates_repo_and_registers_it(svc, fake_gh, registry):
    view = _upload(svc, description="d", aws_services=["lambda"], tags=["x"])
    assert fake_gh.created is not None
    record = registry.get("conn1", template_id_for("my-tpl"))
    assert record is not None
    assert record.source_url == "https://github.com/acme/my-tpl"
    assert record.framework == "strands"
    assert record.aws_services == ["lambda"]
    assert record.tags == ["x"]
    assert isinstance(view, TemplateView)
    assert view.framework == "strands"
    assert view.html_url == "https://github.com/acme/my-tpl"


def test_upload_then_list_shows_the_template(svc):
    _upload(svc, description="d")
    assert [v.name for v in svc.list_templates("conn1")] == ["my-tpl"]


def test_reupload_versions_one_entry_instead_of_duplicating(svc, registry):
    _upload(svc, description="v1")
    _upload(svc, description="v2")
    views = svc.list_templates("conn1")
    assert len(views) == 1
    assert views[0].description == "v2"
    assert registry.get("conn1", template_id_for("my-tpl")).version == "2"


def test_reupload_preserves_the_original_registrant_and_created_at(svc, registry):
    _seed(registry, name="my-tpl", created_by="first@x.com", created_at="2026-01-01T00:00:00+00:00")
    _upload(svc, description="d")
    record = registry.get("conn1", template_id_for("my-tpl"))
    assert record.created_by == "first@x.com"
    assert record.created_at == "2026-01-01T00:00:00+00:00"


def test_upload_registers_nothing_when_the_provider_write_fails(registry):
    """An entry pointing at a repo that does not exist would materialize into a 404 later."""

    class Raiser(FakeGitHubRepoService):
        def create_repo_from_zip(self, org, repo_name, zip_bytes, token, base_url=None):
            raise GitHubRepoError("boom (HTTP 422)")

    svc = GitHubTemplateService(
        github_repo_service=Raiser(),
        connection_service=FakeConnectionService(),
        template_registry=registry,
    )
    with pytest.raises(GitHubTemplateError) as e:
        _upload(svc)
    assert e.value.kind == "github_error"
    assert registry.list_for_connection("conn1") == []


def test_upload_ACCEPTS_any_non_empty_framework(svc, registry):
    """The catalog is the authority on which frameworks exist, so it is not also their gate; the
    recorded value is asserted because ``validate_agent_config`` compares against it."""
    for framework in ("langgraph", "crewai", "harness", "some-future-framework"):
        view = _upload(svc, name=f"t-{framework}", framework=framework)
        assert view.framework == framework
        stored = registry.get("conn1", template_id_for(f"t-{framework}"))
        assert stored.framework == framework


def test_upload_rejects_an_EMPTY_framework(svc, registry):
    """A blank recorded framework folds the governance gate for every repo materialized from the
    row, so it is refused while there is an operator to ask; whitespace counts as empty, and the
    refusal lands before the provider call."""
    for blank in ("", "   "):
        with pytest.raises(GitHubTemplateError) as e:
            _upload(svc, name="t", framework=blank)
        assert e.value.kind == "invalid_input"
    assert registry.list_for_connection("conn1") == []


def test_upload_STORES_the_framework_STRIPPED(svc, registry):
    """What is validated and what is stored must be the same string, or ``' strands '``
    registers and every add_repo against it fails comparing two strings that look identical."""
    view = _upload(svc, name="padded-tpl", framework="  strands  ")

    assert view.framework == "strands"
    assert registry.get("conn1", template_id_for("padded-tpl")).framework == "strands"


def test_patch_STORES_the_framework_STRIPPED(svc, registry):
    _seed(registry, name="my-tpl")

    view = svc.patch_template("conn1", "my-tpl", framework="\tlanggraph\n")

    assert view.framework == "langgraph"
    assert registry.get("conn1", template_id_for("my-tpl")).framework == "langgraph"


def test_upload_rejects_bad_name(svc):
    with pytest.raises(GitHubTemplateError) as e:
        _upload(svc, name="bad name!")
    assert e.value.kind == "invalid_input"


def test_upload_rejects_bad_zip(svc):
    with pytest.raises(GitHubTemplateError) as e:
        _upload(svc, zip_bytes=b"not a zip", name="t")
    assert e.value.kind == "invalid_zip"


def test_upload_validates_before_touching_the_provider(svc, fake_gh, registry):
    with pytest.raises(GitHubTemplateError):
        _upload(svc, zip_bytes=b"nope", name="t")
    assert fake_gh.created is None
    assert registry.list_for_connection("conn1") == []


def test_patch_updates_metadata_and_merges_unspecified(svc, registry):
    _seed(registry, name="my-tpl")
    view = svc.patch_template("conn1", "my-tpl", tags=["new"])
    assert view.framework == "strands"
    assert view.aws_services == ["lambda"]
    assert view.tags == ["new"]
    assert registry.get("conn1", template_id_for("my-tpl")).tags == ["new"]


def test_patch_makes_no_provider_call(svc, registry, fake_gh):
    _seed(registry, name="my-tpl")
    svc.patch_template("conn1", "my-tpl", description="new")
    assert fake_gh.created is None
    assert fake_gh.deleted is None


def test_patch_does_not_create_a_second_entry(svc, registry):
    _seed(registry, name="my-tpl")
    svc.patch_template("conn1", "my-tpl", description="new")
    assert len(registry.list_for_connection("conn1")) == 1


def test_patch_ACCEPTS_any_non_empty_framework(svc, registry):
    """A template repo ported from one framework to another must be able to say so without
    losing its version history and registrant to a deregister-and-register."""
    _seed(registry, name="my-tpl")
    view = svc.patch_template("conn1", "my-tpl", framework="crewai")
    assert view.framework == "crewai"
    assert registry.get("conn1", template_id_for("my-tpl")).framework == "crewai"


def test_patch_rejects_an_EMPTY_framework_but_leaves_an_UNSET_one_alone(svc, registry):
    """An explicit ``""`` blanks a load-bearing field and is refused; ``None`` means "not
    patching it", so a legacy row with a blank framework stays editable in its tags."""
    _seed(registry, name="my-tpl")
    with pytest.raises(GitHubTemplateError) as e:
        svc.patch_template("conn1", "my-tpl", framework="")
    assert e.value.kind == "invalid_input"

    _seed(registry, name="legacy-tpl", framework="")
    view = svc.patch_template("conn1", "legacy-tpl", tags=["fsi"])
    assert view.tags == ["fsi"]
    assert view.framework == ""


def test_patch_unknown_not_found(svc):
    with pytest.raises(GitHubTemplateError) as e:
        svc.patch_template("conn1", "nope", description="x")
    assert e.value.kind == "not_found"


def test_upload_records_the_structural_source_pair(svc, registry):
    """``upload_template`` creates the repo in the connection's org, so it records the exact pair
    instead of leaving a later reader to parse ``source_url``."""
    _upload(svc, description="d")
    record = registry.get("conn1", template_id_for("my-tpl"))
    assert record.source_org == "acme"
    assert record.source_repo == "my-tpl"


def test_the_recorded_pair_names_the_org_the_repo_was_created_in(svc, fake_gh, registry):
    _upload(svc)
    created_org, created_repo = fake_gh.created[0], fake_gh.created[1]
    record = registry.get("conn1", template_id_for("my-tpl"))
    assert (record.source_org, record.source_repo) == (created_org, created_repo)


def test_reupload_keeps_the_same_source_pair(svc, registry):
    for _ in range(2):
        _upload(svc)
    record = registry.get("conn1", template_id_for("my-tpl"))
    assert (record.source_org, record.source_repo) == ("acme", "my-tpl")
    assert record.version == "2"


@pytest.mark.parametrize(
    "patch_kwargs",
    [
        {"description": "edited"},
        {"tags": ["edited"]},
        {"aws_services": ["edited"]},
        {"framework": "strands"},
    ],
)
def test_patch_never_moves_the_source_pair(svc, registry, patch_kwargs):
    """A moved template is a new registration, not a patch: repointing changes which bytes every
    future materialize ships."""
    _seed(registry, name="my-tpl", source_org="acme", source_repo="my-tpl")
    svc.patch_template("conn1", "my-tpl", **patch_kwargs)
    record = registry.get("conn1", template_id_for("my-tpl"))
    assert (record.source_org, record.source_repo) == ("acme", "my-tpl")


def test_patch_has_no_parameter_that_could_repoint_a_record(svc):
    params = set(inspect.signature(svc.patch_template).parameters)
    assert not params & {"source_org", "source_repo", "source_url"}


def test_patch_preserves_a_missing_pair_as_missing(svc, registry):
    """A patch has no way to know a pair, and inventing the connection's org would claim a repo
    that may not exist."""
    _seed(registry, name="legacy")
    svc.patch_template("conn1", "legacy", description="edited")
    record = registry.get("conn1", template_id_for("legacy"))
    assert record.source_org is None
    assert record.source_repo is None


def test_delete_removes_the_catalog_entry(svc, registry):
    _seed(registry, name="t")
    svc.delete_template("conn1", "t")
    assert registry.get("conn1", template_id_for("t")) is None
    assert svc.list_templates("conn1") == []


def test_delete_does_not_delete_the_repository(svc, registry, fake_gh):
    _seed(registry, name="t")
    svc.delete_template("conn1", "t")
    assert fake_gh.deleted is None


def test_delete_unknown_maps_not_found(svc, registry, fake_gh):
    with pytest.raises(GitHubTemplateError) as e:
        svc.delete_template("conn1", "gone")
    assert e.value.kind == "not_found"
    assert fake_gh.deleted is None


def test_delete_leaves_other_entries_alone(svc, registry):
    _seed(registry, name="keep")
    _seed(registry, name="drop")
    svc.delete_template("conn1", "drop")
    assert [v.name for v in svc.list_templates("conn1")] == ["keep"]


class _BrokenRegistry:
    """Every operation faults."""

    def list_for_connection(self, connection_id):
        raise TemplateRegistryError("Could not read the template catalog")

    def get(self, connection_id, template_id, *, consistent=False):
        raise TemplateRegistryError("Could not read the template catalog")

    def save(self, record):
        raise TemplateRegistryError("Could not write the template catalog")

    def delete(self, connection_id, template_id):
        raise TemplateRegistryError("Could not update the template catalog")


def _broken_svc(fake_gh=None):
    return GitHubTemplateService(
        github_repo_service=fake_gh or FakeGitHubRepoService(),
        connection_service=FakeConnectionService(),
        template_registry=_BrokenRegistry(),
    )


def test_list_raises_store_error_rather_than_an_empty_catalog():
    with pytest.raises(GitHubTemplateError) as e:
        _broken_svc().list_templates("conn1")
    assert e.value.kind == "store_error"


def test_patch_wraps_a_store_failure():
    with pytest.raises(GitHubTemplateError) as e:
        _broken_svc().patch_template("conn1", "t", description="x")
    assert e.value.kind == "store_error"


def test_delete_wraps_a_store_failure():
    with pytest.raises(GitHubTemplateError) as e:
        _broken_svc().delete_template("conn1", "t")
    assert e.value.kind == "store_error"


def test_upload_wraps_a_store_failure():
    with pytest.raises(GitHubTemplateError) as e:
        _upload(_broken_svc(), name="t")
    assert e.value.kind == "store_error"


def test_errors_never_carry_the_connection_token(registry):
    class Raiser(FakeGitHubRepoService):
        def create_repo_from_zip(self, org, repo_name, zip_bytes, token, base_url=None):
            raise GitHubRepoError("boom (HTTP 500)")

    svc = GitHubTemplateService(
        github_repo_service=Raiser(),
        connection_service=FakeConnectionService(token="super-secret-token"),
        template_registry=registry,
    )
    with pytest.raises(GitHubTemplateError) as e:
        _upload(svc, name="t")
    assert "super-secret-token" not in e.value.message


def test_unknown_connection_maps_not_found(registry):
    class Missing(FakeConnectionService):
        def get_connection(self, id):
            raise ConnectionError("Unknown connection", kind="not_found")

    svc = GitHubTemplateService(
        github_repo_service=FakeGitHubRepoService(),
        connection_service=Missing(),
        template_registry=registry,
    )
    with pytest.raises(GitHubTemplateError) as e:
        _upload(svc, name="t")
    assert e.value.kind == "not_found"


def test_any_other_connection_failure_maps_github_error(registry):
    class NoToken(FakeConnectionService):
        def get_bearer_token(self, id):
            raise ConnectionError("Secret store operation failed", kind="secret_error")

    svc = GitHubTemplateService(
        github_repo_service=FakeGitHubRepoService(),
        connection_service=NoToken(),
        template_registry=registry,
    )
    with pytest.raises(GitHubTemplateError) as e:
        _upload(svc, name="t")
    assert e.value.kind == "github_error"


@pytest.mark.parametrize("bad_name", ["a#b", "", "Bad Name!", "-leading-hyphen"])
def test_patch_rejects_a_malformed_name_as_invalid_input(svc, bad_name):
    """422, not 503: no retry will ever make ``a#b`` a legal template name."""
    with pytest.raises(GitHubTemplateError) as e:
        svc.patch_template("conn1", bad_name, description="x")
    assert e.value.kind == "invalid_input"


@pytest.mark.parametrize("bad_name", ["a#b", "", "Bad Name!", "-leading-hyphen"])
def test_delete_rejects_a_malformed_name_as_invalid_input(svc, bad_name):
    with pytest.raises(GitHubTemplateError) as e:
        svc.delete_template("conn1", bad_name)
    assert e.value.kind == "invalid_input"


def test_malformed_name_is_refused_before_any_store_write(svc, registry, fake_gh):
    with pytest.raises(GitHubTemplateError):
        svc.delete_template("conn1", "a#b")
    assert registry.list_for_connection("conn1") == []
    assert fake_gh.deleted is None


@pytest.mark.parametrize("bad_connection", ["", "conn#1"])
def test_list_rejects_a_malformed_connection_id_as_invalid_input(svc, bad_connection):
    """``list`` has no name to validate, so the registry's key guard is the only boundary and its
    refusal must read as ``invalid_input``, not as a retryable store fault."""
    with pytest.raises(GitHubTemplateError) as e:
        svc.list_templates(bad_connection)
    assert e.value.kind == "invalid_input"


@pytest.mark.parametrize("bad_connection", ["", "conn#1"])
def test_upload_rejects_a_malformed_connection_id_as_invalid_input(registry, bad_connection):
    svc = GitHubTemplateService(
        github_repo_service=FakeGitHubRepoService(),
        connection_service=FakeConnectionService(),
        template_registry=registry,
    )
    with pytest.raises(GitHubTemplateError) as e:
        _upload(svc, connection_id=bad_connection, name="t")
    assert e.value.kind == "invalid_input"


class _FaultingTable:
    """A DDB table whose every call raises a real botocore ``ClientError``."""

    def _boom(self):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Query")

    def query(self, **kwargs):
        self._boom()

    def get_item(self, **kwargs):
        self._boom()

    def put_item(self, **kwargs):
        self._boom()

    def delete_item(self, **kwargs):
        self._boom()


def _svc_over_a_faulting_store():
    """A service over a real ``TemplateRegistry`` whose store's table faults, so a genuine
    botocore ``ClientError`` runs through the real strict-read path; ``_BrokenRegistry`` raises the
    translated exception and could not detect the registry losing its translation."""
    registry = TemplateRegistry(now=lambda: _Fixed())
    registry._store = StateStore("test-projects-table", table=_FaultingTable())
    return GitHubTemplateService(
        github_repo_service=FakeGitHubRepoService(),
        connection_service=FakeConnectionService(),
        template_registry=registry,
    )


def test_a_real_dynamodb_fault_is_still_store_error_503():
    """Mapping everything to 422 would satisfy the malformed-input tests while retiring the
    strict-read guard; a store outage must stay a retry, never a client error."""
    with pytest.raises(GitHubTemplateError) as e:
        _svc_over_a_faulting_store().list_templates("conn1")
    assert e.value.kind == "store_error"


def test_a_real_dynamodb_fault_on_patch_is_store_error():
    with pytest.raises(GitHubTemplateError) as e:
        _svc_over_a_faulting_store().patch_template("conn1", "valid-name", description="x")
    assert e.value.kind == "store_error"


def test_a_real_dynamodb_fault_on_delete_is_store_error():
    with pytest.raises(GitHubTemplateError) as e:
        _svc_over_a_faulting_store().delete_template("conn1", "valid-name")
    assert e.value.kind == "store_error"


def test_the_two_kinds_are_not_the_same_kind():
    faulting = _svc_over_a_faulting_store()
    with pytest.raises(GitHubTemplateError) as store_fault:
        faulting.list_templates("conn1")
    with pytest.raises(GitHubTemplateError) as bad_input:
        faulting.list_templates("conn#1")
    assert store_fault.value.kind == "store_error"
    assert bad_input.value.kind == "invalid_input"
    assert store_fault.value.kind != bad_input.value.kind


def test_upload_records_the_registrant(svc, registry):
    _upload(svc, created_by="op@x.com")
    assert registry.get("conn1", template_id_for("my-tpl")).created_by == "op@x.com"


def test_template_view_shape_is_unchanged():
    """The read-model shape is pinned exhaustively so the next accidental field cannot ride in."""
    assert set(TemplateView.model_fields) == {
        "name",
        "description",
        "framework",
        "aws_services",
        "tags",
        "html_url",
        "updated_at",
        "infra_template_id",
        "platform",
        "build_kind",
        "preview",
        "artifact_kind",
    }


def test_the_view_copies_the_artifact_kind_from_the_record(svc, registry):
    """Omitted, pydantic would default it and an MCP-server template would list as an agent one."""
    _seed(registry, name="gateway-agentcore", artifact_kind="mcp_server")
    _seed(registry, name="strands-agentcore")

    kinds = {v.name: v.artifact_kind for v in svc.list_templates("conn1")}

    assert kinds == {"gateway-agentcore": "mcp_server", "strands-agentcore": "agent"}


def test_upload_stays_agent_only(svc, registry):
    view = _upload(svc)
    assert view.artifact_kind == "agent"
    assert registry.get("conn1", template_id_for("my-tpl")).artifact_kind == "agent"


@pytest.mark.parametrize(
    "kind,status,detail",
    [
        ("invalid_zip", 422, "Invalid template zip"),
        ("invalid_input", 422, "Invalid template metadata"),
        ("not_found", 404, "Template not found"),
        ("github_error", 502, "GitHub template operation failed"),
        ("store_error", 503, "Template catalog is temporarily unavailable"),
        ("changed", 409, "GitHub template changed while this request ran; reload and retry"),
    ],
)
def test_each_kind_renders_its_fixed_detail(kind, status, detail):
    err = GitHubTemplateError("Table agp-projects-xyz throughput exceeded", kind=kind)
    assert (err.status_code, err.detail, err.code) == (status, detail, f"github_template.{kind}")
    assert "agp-projects-xyz" not in str(err.detail)
