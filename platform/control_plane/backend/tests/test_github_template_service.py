"""GitHubTemplateService unit tests (E22/T2, re-based on the registry in E28B/T2).

The catalog is connection-scoped and now READS AGP'S OWN STORE — the ``is_template`` query
it used to make has no meaning on three of four providers. These tests inject a FAKE
GitHubRepoService (records calls), a FAKE ConnectionService (resolves org/base_url/token) and
a REAL ``TemplateRegistry`` in local-fallback mode (no table name ⇒ no boto3), so nothing
touches live GitHub or AWS.

Using the real registry rather than a fake store is deliberate: the upsert-on-derived-id
behaviour that makes a re-upload a version bump is the registry's, and a fake would be free
to duplicate where the real store cannot.
"""

import io
import zipfile
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from services.github_repo_service import GitHubRepoError
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


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr("app.py", "print('hi')\n")
    return buf.getvalue()


Z = _zip_bytes()


class FakeGitHubRepoService:
    """Records the calls the template service makes against the GitHub write client.

    Deliberately has NO ``list_template_repos`` and NO ``set_repo_metadata``: discovery is a
    store read now, and nothing flips ``is_template`` or writes topics. If the service
    regresses to calling either, these tests fail with AttributeError rather than passing
    against a fake more generous than the new design.
    """

    def __init__(self):
        self.created = None
        self.deleted = None

    def create_repo_from_zip(self, org, repo_name, zip_bytes, token, base_url=None):
        self.created = (org, repo_name, zip_bytes, base_url)
        return f"https://github.com/{org}/{repo_name}"

    def delete_repo(self, org, repo, token, base_url=None):
        self.deleted = (org, repo)


class FakeConnectionService:
    def __init__(self, org="acme", base_url=None, token="tok"):
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
    """A real registry in local-fallback mode — no table name, so no boto3, no AWS."""
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
    """Register a template directly, bypassing upload — the operator's catalog as it stands."""
    base = dict(
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
    return registry.put(TemplateRecord(**base))


# --- list: THE SURVIVING CONTRACT — the catalog lists what the operator registered ---
# (the mechanism moved from a GitHub is_template query to a store read; the contract did not)

def test_list_templates_returns_the_registered_catalog(svc, registry):
    _seed(registry)
    views = svc.list_templates("conn1")
    assert views[0].name == "strands-agentcore"
    assert views[0].framework == "strands"
    assert views[0].aws_services == ["lambda"]
    assert views[0].tags == ["fsi"]


def test_list_templates_makes_no_provider_call(svc, registry, fake_gh):
    """Discovery is portable now: listing the catalog touches no git provider at all."""
    _seed(registry)
    svc.list_templates("conn1")
    assert fake_gh.created is None
    assert fake_gh.deleted is None


def test_list_is_scoped_to_the_connection(svc, registry):
    _seed(registry, name="mine", connection_id="conn1")
    _seed(registry, name="theirs", connection_id="conn2")
    assert [v.name for v in svc.list_templates("conn1")] == ["mine"]


def test_list_serves_html_url_from_source_url(svc, registry):
    """The view shape is unchanged, so ``html_url`` is now fed by the record's pointer —
    which may name a repo outside the connected org."""
    _seed(registry, source_url="https://gitlab.com/other/tpl")
    assert svc.list_templates("conn1")[0].html_url == "https://gitlab.com/other/tpl"


def test_list_of_an_empty_catalog_is_empty(svc):
    assert svc.list_templates("conn1") == []


# --- upload: create the repo + REGISTER it (no is_template flip) -------------

def test_upload_creates_repo_and_registers_it(svc, fake_gh, registry):
    view = svc.upload_template(
        "conn1",
        zip_bytes=Z,
        name="my-tpl",
        description="d",
        framework="strands",
        aws_services=["lambda"],
        tags=["x"],
    )
    # The provider write still happens — the scaffold has to land somewhere.
    assert fake_gh.created is not None
    # …and the catalog entry is what now makes it a template.
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
    """The end-to-end contract that survives the mechanism change."""
    svc.upload_template(
        "conn1", zip_bytes=Z, name="my-tpl", description="d", framework="strands",
        aws_services=[], tags=[],
    )
    assert [v.name for v in svc.list_templates("conn1")] == ["my-tpl"]


def test_reupload_versions_one_entry_instead_of_duplicating(svc, registry):
    svc.upload_template(
        "conn1", zip_bytes=Z, name="my-tpl", description="v1", framework="strands",
        aws_services=[], tags=[],
    )
    svc.upload_template(
        "conn1", zip_bytes=Z, name="my-tpl", description="v2", framework="strands",
        aws_services=[], tags=[],
    )
    views = svc.list_templates("conn1")
    assert len(views) == 1
    assert views[0].description == "v2"
    assert registry.get("conn1", template_id_for("my-tpl")).version == "2"


def test_reupload_preserves_the_original_registrant_and_created_at(svc, registry):
    _seed(
        registry, name="my-tpl", created_by="first@x.com",
        created_at="2026-01-01T00:00:00+00:00",
    )
    svc.upload_template(
        "conn1", zip_bytes=Z, name="my-tpl", description="d", framework="strands",
        aws_services=[], tags=[],
    )
    record = registry.get("conn1", template_id_for("my-tpl"))
    assert record.created_by == "first@x.com"
    assert record.created_at == "2026-01-01T00:00:00+00:00"


def test_upload_registers_nothing_when_the_provider_write_fails(registry):
    """A failed push must leave NO catalog entry — an entry pointing at a repo that does not
    exist would materialize into a 404 later."""
    class Raiser(FakeGitHubRepoService):
        def create_repo_from_zip(self, org, repo_name, zip_bytes, token, base_url=None):
            raise GitHubRepoError("boom (HTTP 422)")

    svc = GitHubTemplateService(
        github_repo_service=Raiser(),
        connection_service=FakeConnectionService(),
        template_registry=registry,
    )
    with pytest.raises(GitHubTemplateError) as e:
        svc.upload_template(
            "conn1", zip_bytes=Z, name="my-tpl", description="", framework="strands",
            aws_services=[], tags=[],
        )
    assert e.value.kind == "github_error"
    assert registry.list_for_connection("conn1") == []


def test_upload_rejects_bad_framework(svc):
    with pytest.raises(GitHubTemplateError) as e:
        svc.upload_template(
            "conn1", zip_bytes=Z, name="t", description="", framework="langgraph",
            aws_services=[], tags=[],
        )
    assert e.value.kind == "invalid_input"


def test_upload_rejects_bad_name(svc):
    with pytest.raises(GitHubTemplateError) as e:
        svc.upload_template(
            "conn1", zip_bytes=Z, name="bad name!", description="", framework="strands",
            aws_services=[], tags=[],
        )
    assert e.value.kind == "invalid_input"


def test_upload_rejects_bad_zip(svc):
    with pytest.raises(GitHubTemplateError) as e:
        svc.upload_template(
            "conn1", zip_bytes=b"not a zip", name="t", description="", framework="strands",
            aws_services=[], tags=[],
        )
    assert e.value.kind == "invalid_zip"


def test_upload_validates_before_touching_the_provider(svc, fake_gh, registry):
    """Validation is up front, so a rejected upload creates neither repo nor entry."""
    with pytest.raises(GitHubTemplateError):
        svc.upload_template(
            "conn1", zip_bytes=b"nope", name="t", description="", framework="strands",
            aws_services=[], tags=[],
        )
    assert fake_gh.created is None
    assert registry.list_for_connection("conn1") == []


# --- patch: edit the record's metadata, merge unspecified fields -------------

def test_patch_updates_metadata_and_merges_unspecified(svc, registry):
    _seed(registry, name="my-tpl")
    view = svc.patch_template("conn1", "my-tpl", tags=["new"])
    # framework + aws untouched (merged from the record), tags replaced.
    assert view.framework == "strands"
    assert view.aws_services == ["lambda"]
    assert view.tags == ["new"]
    assert registry.get("conn1", template_id_for("my-tpl")).tags == ["new"]


def test_patch_makes_no_provider_call(svc, registry, fake_gh):
    """Metadata lives on the record now — patching it is not a repo mutation."""
    _seed(registry, name="my-tpl")
    svc.patch_template("conn1", "my-tpl", description="new")
    assert fake_gh.created is None
    assert fake_gh.deleted is None


def test_patch_does_not_create_a_second_entry(svc, registry):
    _seed(registry, name="my-tpl")
    svc.patch_template("conn1", "my-tpl", description="new")
    assert len(registry.list_for_connection("conn1")) == 1


def test_patch_rejects_a_bad_framework(svc, registry):
    _seed(registry, name="my-tpl")
    with pytest.raises(GitHubTemplateError) as e:
        svc.patch_template("conn1", "my-tpl", framework="langgraph")
    assert e.value.kind == "invalid_input"


def test_patch_unknown_not_found(svc):
    with pytest.raises(GitHubTemplateError) as e:
        svc.patch_template("conn1", "nope", description="x")
    assert e.value.kind == "not_found"


# --- the STRUCTURAL source pair (E28C/T2, design D-C1) -----------------------

def test_upload_records_the_structural_source_pair(svc, registry):
    """``upload_template`` creates the repo IN the connection's org, so it knows the exact
    (org, repo) pair — it records it instead of leaving a later reader to parse ``source_url``
    (contractually "any git URL", not safely decomposable)."""
    svc.upload_template(
        "conn1", zip_bytes=Z, name="my-tpl", description="d", framework="strands",
        aws_services=[], tags=[],
    )
    record = registry.get("conn1", template_id_for("my-tpl"))
    # "acme" is FakeConnectionService's org — the same org create_repo_from_zip wrote into.
    assert record.source_org == "acme"
    assert record.source_repo == "my-tpl"


def test_the_recorded_pair_names_the_org_the_repo_was_created_in(svc, fake_gh, registry):
    """The pair and the provider write must agree, or materialize would read a different repo
    than the upload created."""
    svc.upload_template(
        "conn1", zip_bytes=Z, name="my-tpl", description="", framework="strands",
        aws_services=[], tags=[],
    )
    created_org, created_repo = fake_gh.created[0], fake_gh.created[1]
    record = registry.get("conn1", template_id_for("my-tpl"))
    assert (record.source_org, record.source_repo) == (created_org, created_repo)


def test_reupload_keeps_the_same_source_pair(svc, registry):
    """A re-upload targets the same org + name, so the pair is stable across versions (unlike
    ``version``, and unlike ``created_by``/``created_at`` which are preserved from the first)."""
    for _ in range(2):
        svc.upload_template(
            "conn1", zip_bytes=Z, name="my-tpl", description="", framework="strands",
            aws_services=[], tags=[],
        )
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
    """A MOVED template is a NEW registration, not a patch. ``patch_template`` edits catalog
    metadata only — there is deliberately no parameter that can repoint the record at another
    repo, because repointing changes which bytes every future materialize ships and must be an
    explicit re-registration the operator (and an auditor) can see."""
    _seed(registry, name="my-tpl", source_org="acme", source_repo="my-tpl")
    svc.patch_template("conn1", "my-tpl", **patch_kwargs)
    record = registry.get("conn1", template_id_for("my-tpl"))
    assert (record.source_org, record.source_repo) == ("acme", "my-tpl")


def test_patch_has_no_parameter_that_could_repoint_a_record(svc):
    """The structural guarantee behind the test above: the signature itself refuses. If a
    ``source_org``/``source_repo`` parameter ever appears here, the immutability is gone even
    if every merge test still passes."""
    import inspect

    params = set(inspect.signature(svc.patch_template).parameters)
    assert not params & {"source_org", "source_repo", "source_url"}


def test_patch_preserves_a_missing_pair_as_missing(svc, registry):
    """A pre-28C record patched today does NOT acquire a pair — patch has no way to know one,
    and inventing "the connection's org" would claim a repo that may not exist."""
    _seed(registry, name="legacy")  # no source pair
    svc.patch_template("conn1", "legacy", description="edited")
    record = registry.get("conn1", template_id_for("legacy"))
    assert record.source_org is None
    assert record.source_repo is None


# --- delete: DEREGISTER — the entry goes, the repository stays ---------------

def test_delete_removes_the_catalog_entry(svc, registry):
    _seed(registry, name="t")
    svc.delete_template("conn1", "t")
    assert registry.get("conn1", template_id_for("t")) is None
    assert svc.list_templates("conn1") == []


def test_delete_does_not_delete_the_repository(svc, registry, fake_gh):
    """``source_url`` may name a public repo or a mirror AGP does not own, so deleting the
    repo behind a pointer is unsafe and provider-specific. Deregistering is the honest verb.
    Accepted consequence: a repo created by upload_template outlives its entry."""
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


# --- store failures wrapped as store_error ----------------------------------

class _BrokenRegistry:
    """Every operation faults — the store-fault posture under test."""

    def list_for_connection(self, connection_id):
        raise TemplateRegistryError("Could not read the template catalog")

    def get(self, connection_id, template_id):
        raise TemplateRegistryError("Could not read the template catalog")

    def put(self, record):
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
    """An unreadable catalog must not render as "you have no templates" — that invites a
    re-upload of templates that already exist."""
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
        _broken_svc().upload_template(
            "conn1", zip_bytes=Z, name="t", description="", framework="strands",
            aws_services=[], tags=[],
        )
    assert e.value.kind == "store_error"


# --- the token never leaks --------------------------------------------------

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
        svc.upload_template(
            "conn1", zip_bytes=Z, name="t", description="", framework="strands",
            aws_services=[], tags=[],
        )
    assert "super-secret-token" not in e.value.message


def test_unknown_connection_maps_not_found(registry):
    class Missing(FakeConnectionService):
        def get_connection(self, id):
            err = RuntimeError("no such connection")
            err.kind = "not_found"
            raise err

    svc = GitHubTemplateService(
        github_repo_service=FakeGitHubRepoService(),
        connection_service=Missing(),
        template_registry=registry,
    )
    with pytest.raises(GitHubTemplateError) as e:
        svc.upload_template(
            "conn1", zip_bytes=Z, name="t", description="", framework="strands",
            aws_services=[], tags=[],
        )
    assert e.value.kind == "not_found"


# --- 422 vs 503: malformed input is NOT a retryable store fault --------------
# The two halves of the same defect. Before this split every case below came back as
# store_error → 503, telling the console to retry a request that can never succeed.
#
# Half 1 — patch/delete never validated `name` at all (only upload did), so a raw path
#          segment reached the registry's key guard. Fixed at the service boundary.
# Half 2 — `connection_id` is unvalidated on every route, and `list` has no name to
#          validate, so that one genuinely needs the exception split.

@pytest.mark.parametrize("bad_name", ["a#b", "", "Bad Name!", "-leading-hyphen"])
def test_patch_rejects_a_malformed_name_as_invalid_input(svc, bad_name):
    """422, not 503: no retry will ever make 'a#b' a legal template name."""
    with pytest.raises(GitHubTemplateError) as e:
        svc.patch_template("conn1", bad_name, description="x")
    assert e.value.kind == "invalid_input"


@pytest.mark.parametrize("bad_name", ["a#b", "", "Bad Name!", "-leading-hyphen"])
def test_delete_rejects_a_malformed_name_as_invalid_input(svc, bad_name):
    with pytest.raises(GitHubTemplateError) as e:
        svc.delete_template("conn1", bad_name)
    assert e.value.kind == "invalid_input"


def test_malformed_name_is_refused_before_any_store_write(svc, registry, fake_gh):
    """A rejected name must not reach the store or the provider."""
    with pytest.raises(GitHubTemplateError):
        svc.delete_template("conn1", "a#b")
    assert registry.list_for_connection("conn1") == []
    assert fake_gh.deleted is None


@pytest.mark.parametrize("bad_connection", ["", "conn#1"])
def test_list_rejects_a_malformed_connection_id_as_invalid_input(svc, bad_connection):
    """The half that needs the exception split: ``list`` has no name to validate, so the
    registry's key guard is the only boundary — and its refusal must read as 422."""
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
        svc.upload_template(
            bad_connection, zip_bytes=Z, name="t", description="", framework="strands",
            aws_services=[], tags=[],
        )
    assert e.value.kind == "invalid_input"


# --- THE GUARD THAT MUST NOT BE TRADED AWAY ---------------------------------

class _FaultingTable:
    """A DDB table whose every call raises a REAL botocore ClientError."""

    def _boom(self):
        raise ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Query"
        )

    def query(self, **kwargs):
        self._boom()

    def get_item(self, **kwargs):
        self._boom()

    def put_item(self, **kwargs):
        self._boom()

    def delete_item(self, **kwargs):
        self._boom()


def _svc_over_a_faulting_store():
    """A service over a REAL TemplateRegistry in DDB mode whose table faults.

    Deliberately NOT the hand-rolled ``_BrokenRegistry`` above: that one raises the
    exception this test is about, so it could not detect the registry losing its
    ClientError→TemplateRegistryError translation. This drives a genuine botocore
    ``ClientError`` through the real strict-read path. The table NAME is a test fixture
    string — no real resource, no account id.
    """
    registry = TemplateRegistry(now=lambda: _Fixed())
    registry.table_name = "test-projects-table"
    registry._table = _FaultingTable()
    return GitHubTemplateService(
        github_repo_service=FakeGitHubRepoService(),
        connection_service=FakeConnectionService(),
        template_registry=registry,
    )


def test_a_real_dynamodb_fault_is_still_store_error_503():
    """THE regression fence for the 422 fix.

    Mapping everything to 422 would satisfy every test above while silently retiring the
    strict-read guard — the catalog would answer a store outage with a client error and the
    console would stop retrying. This test fails if that happens, so the suite can tell a
    correct fix from one that threw the guard away."""
    with pytest.raises(GitHubTemplateError) as e:
        _svc_over_a_faulting_store().list_templates("conn1")
    assert e.value.kind == "store_error"


def test_a_real_dynamodb_fault_on_patch_is_store_error():
    """Same guard on a write path, where ``name`` IS validated — proving the new validation
    did not swallow the store fault that follows it."""
    with pytest.raises(GitHubTemplateError) as e:
        _svc_over_a_faulting_store().patch_template("conn1", "valid-name", description="x")
    assert e.value.kind == "store_error"


def test_a_real_dynamodb_fault_on_delete_is_store_error():
    with pytest.raises(GitHubTemplateError) as e:
        _svc_over_a_faulting_store().delete_template("conn1", "valid-name")
    assert e.value.kind == "store_error"


def test_the_two_kinds_are_not_the_same_kind():
    """The whole point of the split, asserted as one fact: identical shape of call, one
    malformed and one faulting, must NOT produce the same kind."""
    faulting = _svc_over_a_faulting_store()
    with pytest.raises(GitHubTemplateError) as store_fault:
        faulting.list_templates("conn1")
    with pytest.raises(GitHubTemplateError) as bad_input:
        faulting.list_templates("conn#1")
    assert store_fault.value.kind == "store_error"
    assert bad_input.value.kind == "invalid_input"
    assert store_fault.value.kind != bad_input.value.kind


# --- created_by comes from the validated principal --------------------------

def test_upload_records_the_registrant(svc, registry):
    """An audit field that is always empty is worse than absent — it looks populated."""
    svc.upload_template(
        "conn1", zip_bytes=Z, name="my-tpl", description="", framework="strands",
        aws_services=[], tags=[], created_by="op@x.com",
    )
    assert registry.get("conn1", template_id_for("my-tpl")).created_by == "op@x.com"


# --- the read-model shape must not move in this task ------------------------

def test_template_view_shape_is_unchanged():
    """T2 keeps the frontend contract still — a separate task owns the frontend."""
    assert set(TemplateView.model_fields) == {
        "name", "description", "framework", "aws_services", "tags", "html_url", "updated_at",
    }
