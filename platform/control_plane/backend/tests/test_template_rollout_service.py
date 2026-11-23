"""RolloutService unit tests.

The registry alone cannot say whether a repository exists, so ``read_repo`` is the probe and every
verb decides on the three-state comparison (see the service's module docstring); a re-push is an
idempotent ``commit_files`` of the seed bytes onto the existing repo, never a delete.

:class:`FakeRepoProvider` is no more generous than GitHub: ``create_repo`` auto-inits a branch named
from the org's default-branch setting, ``read_repo`` answers ``None`` only for not-found,
``commit_files`` refuses an empty mapping and a repo that does not exist, and there is deliberately
no ``delete_repo`` and no ``create_repo_from_zip``, so a regression to the destructive path fails with
``AttributeError`` instead of passing against an over-generous double.

Everything else is injected too: a fake ConnectionService, a keyed fake S3 client (one module zip
per key), a fake ``known_repo_names`` callable and real ``TemplateRegistry`` and
``InfraTemplateRegistry`` instances in local-fallback mode (no table name, so no boto3). The infra
repos a rollout ensures are derived, so ``RUNTIME_REPO`` and ``HARNESS_REPO`` are the seeds' repo
names read off :data:`SEED_INFRA_TEMPLATES`, never re-typed. Nothing here touches live GitHub or AWS.
"""

import io
import json
import zipfile
from collections.abc import Callable
from types import SimpleNamespace
from typing import get_args
from unittest.mock import MagicMock

import pytest

from core.state_store import StateStore
from services.github_repo_errors import GitHubRepoError
from services.infra_template_registry import (
    SEED_INFRA_TEMPLATES,
    InfraTemplateRecord,
    InfraTemplateRegistry,
    InfraTemplateRegistryError,
)
from services.repo_provider import RepoView
from services.template_registry import (
    TemplateRecord,
    TemplateRegistry,
    TemplateRegistryError,
    TemplateRegistryValidationError,
    template_id_for,
)
from services.template_rollout_service import (
    DEFAULT_INFRA_TEMPLATE_ID,
    ReconcileItem,
    ReconcileOrigin,
    ReconcileState,
    RolloutError,
    RolloutService,
)

SEED_NAME = "strands-agentcore"

_SEEDS_BY_ID = {s["id"]: s for s in SEED_INFRA_TEMPLATES}
RUNTIME_REPO = _SEEDS_BY_ID[DEFAULT_INFRA_TEMPLATE_ID]["repo_name"]
RUNTIME_KEY = _SEEDS_BY_ID[DEFAULT_INFRA_TEMPLATE_ID]["module_source"]
HARNESS_ID = "agentcore-harness"
HARNESS_REPO = _SEEDS_BY_ID[HARNESS_ID]["repo_name"]
HARNESS_KEY = _SEEDS_BY_ID[HARNESS_ID]["module_source"]


class FakeRepoProvider:
    """The three reads and the two writes the seed push uses, with real repo state.

    ``repos`` maps repo name → ``{"default_branch", "head_sha", "files"}``. A name absent from
    the mapping does not exist in the org, which is what makes ``read_repo``'s ``None`` and
    ``list_repos``'s omission agree with each other.
    """

    def __init__(self, repos=None):
        self.repos = {name: dict(state) for name, state in (repos or {}).items()}
        self.created = []  # repos this call sequence brought into existence
        self.commits = []  # (repo, branch, files, message)
        self.read_repo_calls = []
        self.list_repos_calls = []

    def create_repo(self, org, name, *, private, token, base_url=None):
        if name not in self.repos:
            self.repos[name] = {
                "default_branch": "main",
                "head_sha": "auto-init-sha",
                "files": {"README.md": b"# init\n"},
            }
            self.created.append(name)
        return f"https://github.com/{org}/{name}"

    def commit_files(self, org, repo, files, *, branch, message, token, base_url=None):
        """IDEMPOTENT BY CONTENT, mirroring the real client's tree-sha gate
        (``github_repo_service.py`` "THE IDEMPOTENCE GATE"): if the branch already carries this
        exact file set, NO commit is written, the head sha does not move, and no push event is
        recorded. A fake that always appended would report success for an implementation that
        re-pushes identical bytes — i.e. fires a build for nothing."""
        if not files:
            raise GitHubRepoError(f"refusing to commit an empty file set to '{org}/{repo}'")
        state = self.repos.get(repo)
        if state is None:
            raise GitHubRepoError(f"repo '{org}/{repo}' does not exist")
        if state.get("default_branch") == branch and state.get("files") == dict(files):
            return state["head_sha"]  # same content ⇒ same tree sha ⇒ nothing to commit
        state["files"] = dict(files)
        state["default_branch"] = state.get("default_branch") or branch
        state["head_sha"] = f"sha-{len(self.commits) + 1}"
        self.commits.append((repo, branch, dict(files), message))
        return state["head_sha"]

    def read_repo(self, org, repo, *, token, base_url=None):
        self.read_repo_calls.append(repo)
        state = self.repos.get(repo)
        if state is None:
            return None
        return RepoView(default_branch=state["default_branch"], head_sha=state["head_sha"])

    def list_repos(self, org, *, token, base_url=None):
        self.list_repos_calls.append(org)
        return sorted(self.repos)


class FakeConnectionService:
    def __init__(self, org="acme", base_url=None, token="tok"):  # noqa: S107 — a fake connection's placeholder bearer, not a credential; expires 2027-10-23; added E49A-50A/T14
        self._conn = SimpleNamespace(org=org, base_url=base_url)
        self._token = token

    def get_connection(self, connection_id):
        return self._conn

    def get_bearer_token(self, connection_id):
        return self._token


def _module_zip(files=None):
    """A REAL zip — the infra path unpacks the S3 object into ``{path: bytes}`` now that
    ``create_repo_from_zip`` is gone."""
    payload = files or {"main.tf": b'variable "x" {}\n', "versions.tf": b"# tf\n"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for path, content in payload.items():
            zf.writestr(path, content)
    return buf.getvalue()


RUNTIME_MODULE_FILES = {"main.tf": b'variable "x" {}\n', "versions.tf": b"# tf\n"}
HARNESS_MODULE_FILES = {"harness.tf": b'variable "harness_config_path" {}\n'}


class FakeS3Client:
    """Keyed: one archive per module key.

    The old double answered the SAME zip to every ``get_object``, which would let an
    implementation that pushed the RUNTIME module into ``agp-harness-infra`` pass — the exact
    defect a per-``module_source`` fetch exists to prevent. So each key holds its own archive and
    an unstaged key raises the way S3 does.
    """

    def __init__(self, body=None, error=None, bodies=None):
        self._bodies = (
            {k: _module_zip(v) for k, v in bodies.items()}
            if bodies is not None
            else {
                RUNTIME_KEY: _module_zip(),
                HARNESS_KEY: _module_zip(HARNESS_MODULE_FILES),
            }
        )
        self._body = body
        self._error = error
        self.calls = []

    def get_object(self, *, Bucket, Key):
        self.calls.append((Bucket, Key))
        if self._error is not None:
            raise self._error
        body = self._body if self._body is not None else self._bodies.get(Key)
        if body is None:
            raise RuntimeError(f"NoSuchKey: s3://{Bucket}/{Key}")
        return {"Body": SimpleNamespace(read=lambda: body)}


SEED_TEMPLATE_JSON = {
    "id": SEED_NAME,
    "name": "Strands + AgentCore",
    "framework": "strands",
    "description": "Barebones Strands agent on Bedrock AgentCore.",
    "aws_services": ["bedrock", "ecr", "agentcore"],
    "tags": ["starter", "python", "strands"],
    "version": "1.0.0",
}


def _seed_templates_dir(tmp_path, *, template_json=SEED_TEMPLATE_JSON):
    """Build an on-disk seed dir (one base template), including its ``template.json`` — the
    catalog metadata ``_register`` now reads from disk (allowed: it IS the seed)."""
    templates = tmp_path / "agent-templates"
    (templates / SEED_NAME / "src").mkdir(parents=True)
    (templates / SEED_NAME / "src" / "app.py").write_text("print('hi')\n")
    if template_json is not None:
        (templates / SEED_NAME / "template.json").write_text(
            json.dumps(template_json) if not isinstance(template_json, str) else template_json
        )
    return templates


class _Fixed:
    def isoformat(self):
        return "2026-08-05T00:00:00+00:00"


def _registry():
    """A real registry in local-fallback mode — no table name, so no boto3, no AWS."""
    return TemplateRegistry(now=lambda: _Fixed())


def _infra_registry():
    """The infra catalog, same local-fallback mode. A REAL registry rather than a mock, so
    the seed dicts are validated by the real model on every rollout."""
    return InfraTemplateRegistry(now=lambda: _Fixed())


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


class _RacedInfraRegistry(InfraTemplateRegistry):
    def __init__(self):
        super().__init__(now=lambda: _Fixed())
        self.rival: Callable[[], object] | None = None

    def get(self, connection_id, infra_template_id, *, consistent=False):
        record = super().get(connection_id, infra_template_id, consistent=consistent)
        rival, self.rival = self.rival, None
        if rival is not None:
            rival()
        return record


def _register(
    registry,
    name,
    connection_id="conn1",
    *,
    version="7",
    created_at="2026-07-01T00:00:00+00:00",
    created_by="alice@x.com",
    infra_template_id=DEFAULT_INFRA_TEMPLATE_ID,
):
    """Put a template in the catalog — the "already registered" half of the matrix."""
    registry.create(
        TemplateRecord(
            id=template_id_for(name),
            name=name,
            source_url=f"https://github.com/acme/{name}",
            version=version,
            connection_id=connection_id,
            created_at=created_at,
            created_by=created_by,
            framework="strands",
            source_org="acme",
            source_repo=name,
            infra_template_id=infra_template_id,
        )
    )


def _svc(
    tmp_path,
    *,
    gh=None,
    conn=None,
    s3=None,
    registry=None,
    infra_registry=None,
    known_repo_names=lambda connection_id: set(),
    template_json=SEED_TEMPLATE_JSON,
    now=lambda: "2026-08-05T00:00:00+00:00",
):
    templates = _seed_templates_dir(tmp_path, template_json=template_json)
    return RolloutService(
        gh or FakeRepoProvider(),
        conn or FakeConnectionService(),
        agent_templates_dir=str(templates),
        s3_client=s3 or FakeS3Client(),
        runtime_module_bucket="agp-tf-state",
        runtime_module_key=RUNTIME_KEY,
        template_registry=registry if registry is not None else _registry(),
        infra_template_registry=(infra_registry if infra_registry is not None else _infra_registry()),
        known_repo_names=known_repo_names,
        now=now,
    )


def _row(view, name) -> ReconcileItem:
    return next(i for i in view.templates if i.name == name)


def _template(registry: TemplateRegistry, name: str) -> TemplateRecord:
    record = registry.get("conn1", template_id_for(name))
    assert record is not None
    return record


@pytest.mark.parametrize(
    "registered,present,expected_state",
    [
        (True, True, "registered_present"),
        (True, False, "registered_missing"),
        (False, True, "unregistered_present"),
        (False, False, "seed_absent"),
    ],
)
def test_reconcile_three_state_matrix(tmp_path, registered, present, expected_state):
    """(registered, present) → exactly one state, and PRESENT comes from ``read_repo``.

    MUTATION GUARD: revert the probe to the DDB-only ``name in existing`` and two of these four
    rows invert — ``(True, False)`` would claim ``registered_present`` (offering nothing for a
    repo that is gone) and ``(False, True)`` would claim ``seed_absent`` (offering a create that
    422s on the existing repo). Deleting the ``read_repo`` call reddens this test by name.
    """
    gh = FakeRepoProvider(
        repos=({SEED_NAME: {"default_branch": "trunk", "head_sha": "abc123", "files": {}}} if present else {})
    )
    registry = _registry()
    if registered:
        _register(registry, SEED_NAME)
    svc = _svc(tmp_path, gh=gh, registry=registry)

    row = _row(svc.reconcile("conn1"), SEED_NAME)

    assert row.state == expected_state
    assert SEED_NAME in gh.read_repo_calls
    if present:
        assert row.default_branch == "trunk"
        assert row.head_sha == "abc123"
    else:
        assert row.default_branch is None
        assert row.head_sha is None


def test_reconcile_answers_every_closed_origin_and_state_and_nothing_else(tmp_path):
    gh = FakeRepoProvider(
        repos={
            SEED_NAME: {"default_branch": "main", "head_sha": "a", "files": {}},
            "someones-own-template": {"default_branch": "main", "head_sha": "z", "files": {}},
        }
    )
    registry = _registry()
    _register(registry, SEED_NAME)
    _register(registry, "uploaded-one")
    view = _svc(tmp_path, gh=gh, registry=registry).reconcile("conn1")
    rows = [*view.templates, view.infra_repo]

    assert {row.origin for row in rows} == set(get_args(ReconcileOrigin))
    assert {row.state for row in rows} == set(get_args(ReconcileState))


def test_reconcile_row_for_a_seed_name_is_origin_seed(tmp_path):
    """``origin`` says WHY the row exists. A seed name keeps ``origin="seed"`` even once it is
    registered, because seed-ness is the capability signal (re-push / re-create from seed)."""
    registry = _registry()
    _register(registry, SEED_NAME)
    svc = _svc(tmp_path, gh=FakeRepoProvider(), registry=registry)

    assert _row(svc.reconcile("conn1"), SEED_NAME).origin == "seed"


def test_reconcile_row_for_a_registry_only_name_is_origin_registry(tmp_path):
    """A registered template with no seed on disk (an uploaded or adopted one) still gets a row —
    the registry is part of the row universe, not just the seed dir."""
    registry = _registry()
    _register(registry, "uploaded-one")
    svc = _svc(tmp_path, gh=FakeRepoProvider(), registry=registry)

    row = _row(svc.reconcile("conn1"), "uploaded-one")
    assert row.origin == "registry"
    assert row.state == "registered_missing"  # not in the org


def test_reconcile_present_but_empty_repo_is_adoptable_with_no_head(tmp_path):
    """``read_repo`` reports ``head_sha=""`` for an existing-but-empty repo: present, not ``None``, no raise.

    It must read as PRESENT (offering "create" would 422 on a repo that is already there) while
    reporting NO head — so a later materialize cannot be handed ``""`` as a ref."""
    gh = FakeRepoProvider(repos={SEED_NAME: {"default_branch": "main", "head_sha": "", "files": {}}})
    svc = _svc(tmp_path, gh=gh)

    row = _row(svc.reconcile("conn1"), SEED_NAME)
    assert row.state == "unregistered_present"
    assert row.default_branch == "main"
    assert row.head_sha is None


def test_reconcile_offers_unknown_org_repos_as_adopt_rows(tmp_path):
    gh = FakeRepoProvider(
        repos={"someones-own-template": {"default_branch": "main", "head_sha": "zzz", "files": {}}}
    )
    svc = _svc(tmp_path, gh=gh)

    row = _row(svc.reconcile("conn1"), "someones-own-template")
    assert row.origin == "org"
    assert row.state == "unregistered_present"
    assert gh.list_repos_calls == ["acme"]


def test_reconcile_subtracts_agp_known_non_templates(tmp_path):
    """Registered templates, this connection's materialized AGENT repos and the forced infra
    repo are all things AGP already accounts for — none may be offered for adoption."""
    gh = FakeRepoProvider(
        repos={
            SEED_NAME: {"default_branch": "main", "head_sha": "a", "files": {}},
            "my-agent-repo": {"default_branch": "main", "head_sha": "b", "files": {}},
            RUNTIME_REPO: {"default_branch": "main", "head_sha": "c", "files": {}},
            "adoptable-one": {"default_branch": "main", "head_sha": "d", "files": {}},
        }
    )
    registry = _registry()
    _register(registry, SEED_NAME)
    svc = _svc(
        tmp_path,
        gh=gh,
        registry=registry,
        known_repo_names=lambda connection_id: {"my-agent-repo"},
    )

    view = svc.reconcile("conn1")

    org_rows = [i.name for i in view.templates if i.origin == "org"]
    assert org_rows == ["adoptable-one"]
    assert [i.name for i in view.templates].count(SEED_NAME) == 1


def test_reconcile_without_a_repo_inventory_fails_closed(tmp_path):
    """No ``known_repo_names`` ⇒ AGP cannot subtract its own agent repos, and a picker that
    offered one for adoption would invite an operator to register a live agent repo as a
    template. Refuse rather than under-subtract."""
    svc = _svc(tmp_path, known_repo_names=None)
    with pytest.raises(RolloutError) as exc:
        svc.reconcile("conn1")
    assert exc.value.kind == "rollout_error"


def test_reconcile_reports_the_infra_repo_from_the_probe(tmp_path):
    """The infra repo is its OWN view field (that is how "forced, never a choice" is expressed
    structurally now that ``selectable`` is gone), and its state comes from ``read_repo``."""
    gh = FakeRepoProvider(repos={RUNTIME_REPO: {"default_branch": "main", "head_sha": "i1", "files": {}}})
    svc = _svc(tmp_path, gh=gh)

    view = svc.reconcile("conn1")
    assert view.infra_repo.name == RUNTIME_REPO
    assert view.infra_repo.state == "unregistered_present"
    assert view.infra_repo.head_sha == "i1"
    assert RUNTIME_REPO not in [i.name for i in view.templates]


def test_reconcile_infra_absent_is_seed_absent(tmp_path):
    svc = _svc(tmp_path, gh=FakeRepoProvider())
    assert svc.reconcile("conn1").infra_repo.state == "seed_absent"


def test_reconcile_provider_failure_is_a_rollout_error_not_an_empty_org(tmp_path):
    """A failed ``list_repos`` must not read as "the org has no repos" — that would offer to
    create seeds over repos AGP simply could not see."""

    class Blind(FakeRepoProvider):
        def list_repos(self, org, *, token, base_url=None):
            raise GitHubRepoError("boom (HTTP 500)")

    svc = _svc(tmp_path, gh=Blind())
    with pytest.raises(RolloutError) as exc:
        svc.reconcile("conn1")
    assert exc.value.kind == "rollout_error"
    assert "boom" not in exc.value.message


def test_reconcile_does_not_call_the_deleted_ddb_only_lie(tmp_path):
    """``exists_in_org`` is DELETED, not aliased: nothing may read a boolean off a row."""
    svc = _svc(tmp_path)
    row = _row(svc.reconcile("conn1"), SEED_NAME)
    assert not hasattr(row, "exists_in_org")
    assert not hasattr(row, "selectable")


def test_rollout_creates_and_registers_a_seed_template(tmp_path):
    gh = FakeRepoProvider()
    registry = _registry()
    svc = _svc(tmp_path, gh=gh, registry=registry)

    r = svc.rollout("conn1", template_names=[SEED_NAME], overwrite=False)

    assert SEED_NAME in gh.created
    pushes = [c for c in gh.commits if c[0] == SEED_NAME]
    assert len(pushes) == 1
    assert pushes[0][1] == "main"
    assert "src/app.py" in pushes[0][2]
    assert next(i for i in r.items if i.name == SEED_NAME).action == "created"


def test_re_push_commits_on_top_and_never_deletes_the_repo(tmp_path):
    """The destructive path (delete_repo + create_repo_from_zip) is deleted. A re-push keeps the
    repository — and its history — and lands as a normal commit on top.

    The fake has NO ``delete_repo``, so a regression fails with AttributeError rather than
    passing; this asserts the absence explicitly too."""
    gh = FakeRepoProvider(
        repos={SEED_NAME: {"default_branch": "trunk", "head_sha": "old", "files": {"x": b"1"}}}
    )
    registry = _registry()
    _register(registry, SEED_NAME)
    svc = _svc(tmp_path, gh=gh, registry=registry)

    r = svc.rollout("conn1", template_names=[SEED_NAME], overwrite=True)

    assert not hasattr(gh, "delete_repo")
    assert not hasattr(gh, "create_repo_from_zip")
    assert SEED_NAME not in gh.created
    assert [c[1] for c in gh.commits if c[0] == SEED_NAME] == ["trunk"]
    assert next(i for i in r.items if i.name == SEED_NAME).action == "overwritten"


@pytest.mark.parametrize("overwrite", [False, True])
def test_a_pre_existing_unregistered_repo_is_refused_never_overwritten(tmp_path, overwrite):
    """Requirement 3, BOTH halves: a repo that already carries a seed's name is not a 422 — and
    it is not a push target either. It reconciles as an ADOPT row, and rollout REFUSES it.

    THIS TEST PREVIOUSLY PINNED THE BUG. Its fake repo held ``files: {}``, so it could not see
    that ``commit_files`` builds its tree with NO ``base_tree`` — the push REPLACED the whole
    tree. The reviewer executed it: a repo holding ``their_work.py`` came back as ``['app.py']``,
    reported ``action="created"``. So the fake now holds REAL files and the assertion is that they
    SURVIVE.

    ``overwrite=True`` is parametrized deliberately: overwrite governs whether an ALREADY
    REGISTERED template is re-pushed. An unregistered repo has no record consenting to anything,
    so no flag on a rollout call may authorize replacing a stranger's tree — adopt it first, which
    is an explicit human statement about that specific repo.
    """
    their_files = {"their_work.py": b"# months of work\n", "README.md": b"# theirs\n"}
    gh = FakeRepoProvider(
        repos={SEED_NAME: {"default_branch": "main", "head_sha": "pre", "files": dict(their_files)}}
    )
    svc = _svc(tmp_path, gh=gh)

    assert _row(svc.reconcile("conn1"), SEED_NAME).state == "unregistered_present"

    r = svc.rollout("conn1", template_names=[SEED_NAME], overwrite=overwrite)

    item = next(i for i in r.items if i.name == SEED_NAME)
    assert item.action == "skipped"
    assert "adopt" in (item.reason or "").lower()
    assert gh.repos[SEED_NAME]["files"] == their_files
    assert [c for c in gh.commits if c[0] == SEED_NAME] == []
    assert SEED_NAME not in gh.created
    assert [r_.name for r_ in svc._registry.list_for_connection("conn1")] == []


@pytest.mark.parametrize("overwrite", [False, True])
def test_a_registered_template_whose_repo_is_gone_is_recreated_from_seed(tmp_path, overwrite):
    """``registered_missing`` — the reconcile view offers "Re-create from seed", and it must not need the
    overwrite flag: there is nothing to overwrite. The repo is GONE, so the create cannot collide
    with anything and cannot destroy anything.

    Before the fix this returned ``skipped, reason="already in the template catalog"`` — a
    catalog-shaped answer about a repository that did not exist."""
    gh = FakeRepoProvider()  # the org has nothing
    registry = _registry()
    _register(registry, SEED_NAME)  # …but AGP has a record
    svc = _svc(tmp_path, gh=gh, registry=registry)

    assert _row(svc.reconcile("conn1"), SEED_NAME).state == "registered_missing"

    r = svc.rollout("conn1", template_names=[SEED_NAME], overwrite=overwrite)

    item = next(i for i in r.items if i.name == SEED_NAME)
    assert item.action == "recreated"
    assert SEED_NAME in gh.created
    assert "src/app.py" in gh.repos[SEED_NAME]["files"]


def test_rollout_skips_a_registered_present_template_without_overwrite(tmp_path):
    """``registered_present`` + no overwrite = in sync, nothing to do. The repo must be PRESENT
    for this to be the state under test — a registered name whose repo is gone is
    ``registered_missing`` and gets re-created (the test above)."""
    iterated = {"iterated.py": b"# the customer's own improvements\n"}
    gh = FakeRepoProvider(
        repos={
            SEED_NAME: {"default_branch": "main", "head_sha": "a", "files": dict(iterated)},
            RUNTIME_REPO: {"default_branch": "main", "head_sha": "b", "files": {}},
        }
    )
    registry = _registry()
    _register(registry, SEED_NAME)
    svc = _svc(tmp_path, gh=gh, registry=registry)

    r = svc.rollout("conn1", template_names=[SEED_NAME], overwrite=False)

    assert next(i for i in r.items if i.name == SEED_NAME).action == "skipped"
    assert next(i for i in r.items if i.name == RUNTIME_REPO).action == "skipped"
    assert gh.commits == []
    assert gh.repos[SEED_NAME]["files"] == iterated


def test_re_push_reregisters_instead_of_duplicating(tmp_path):
    """The registry write is an upsert — the id is derived from the
    name — so re-rolling out a template must not leave the operator with two catalog entries. The
    old test asserted this against the DELETE+recreate path; the contract outlived that path, so it
    is re-expressed here against the re-push."""
    gh = FakeRepoProvider(repos={SEED_NAME: {"default_branch": "main", "head_sha": "old", "files": {}}})
    registry = _registry()
    _register(registry, SEED_NAME)
    svc = _svc(tmp_path, gh=gh, registry=registry)

    svc.rollout("conn1", template_names=[SEED_NAME], overwrite=True)
    svc.rollout("conn1", template_names=[SEED_NAME], overwrite=True)

    assert [r.name for r in registry.list_for_connection("conn1")] == [SEED_NAME]


def _rival_first_register(registry):
    _register(registry, SEED_NAME, created_by="rival@x.com")


def _rival_re_register(registry):
    current = registry.get("conn1", template_id_for(SEED_NAME))
    registry.save(current.model_copy(update={"created_by": "rival@x.com"}))


@pytest.mark.parametrize(
    "registered,rival", [(False, _rival_first_register), (True, _rival_re_register)], ids=["first", "re-push"]
)
def test_a_register_that_lost_to_a_concurrent_write_answers_changed(tmp_path, registered, rival):
    repos = {SEED_NAME: {"default_branch": "main", "head_sha": "old", "files": {}}} if registered else {}
    registry = _RacedRegistry()
    if registered:
        _register(registry, SEED_NAME)
    registry.rival = lambda: rival(registry)
    svc = _svc(tmp_path, gh=FakeRepoProvider(repos=repos), registry=registry)

    with pytest.raises(RolloutError) as exc:
        svc.rollout("conn1", template_names=[SEED_NAME], overwrite=True)

    assert (exc.value.status_code, exc.value.body()) == (
        409,
        {"detail": "Rollout changed while this request ran; reload and retry", "code": "rollout.changed"},
    )
    (row,) = registry.list_for_connection("conn1")
    assert row.created_by == "rival@x.com"


def test_a_re_push_of_unchanged_bytes_writes_no_second_commit(tmp_path):
    """``_push``'s docstring claims ``commit_files`` is idempotent BY CONTENT, so a re-push of
    unchanged seed bytes "writes no commit, moves no ref and fires no build". The fake now models
    that gate (as the real client does), which makes the claim testable rather than asserted."""
    gh = FakeRepoProvider(
        repos={SEED_NAME: {"default_branch": "main", "head_sha": "old", "files": {"x": b"1"}}}
    )
    registry = _registry()
    _register(registry, SEED_NAME)
    svc = _svc(tmp_path, gh=gh, registry=registry)

    svc.rollout("conn1", template_names=[SEED_NAME], overwrite=True)
    first_sha = gh.repos[SEED_NAME]["head_sha"]
    r = svc.rollout("conn1", template_names=[SEED_NAME], overwrite=True)

    assert [c[0] for c in gh.commits].count(SEED_NAME) == 1
    assert gh.repos[SEED_NAME]["head_sha"] == first_sha
    assert next(i for i in r.items if i.name == SEED_NAME).action == "unchanged"


def test_rollout_then_reconcile_reports_it_registered_and_present(tmp_path):
    """The round trip — what rollout registers and pushes is what
    reconcile then sees. Only the ADOPT round trip was pinned after the rewrite."""
    gh = FakeRepoProvider()
    svc = _svc(tmp_path, gh=gh)

    svc.rollout("conn1", template_names=[SEED_NAME], overwrite=False)

    row = _row(svc.reconcile("conn1"), SEED_NAME)
    assert row.state == "registered_present"
    assert row.origin == "seed"
    assert row.head_sha  # a real sha from the push, not None


def test_rollout_registers_nothing_when_the_push_fails(tmp_path):
    """A failed push must leave NO catalog entry — a record pointing at unpushed bytes would
    materialize the wrong thing later."""

    class Raiser(FakeRepoProvider):
        def commit_files(self, *a, **k):
            raise GitHubRepoError("boom (HTTP 422)")

    registry = _registry()
    svc = _svc(tmp_path, gh=Raiser(), registry=registry)

    with pytest.raises(RolloutError) as exc:
        svc.rollout("conn1", template_names=[SEED_NAME], overwrite=False)
    assert exc.value.kind == "rollout_error"
    assert registry.list_for_connection("conn1") == []


def test_rollout_refuses_an_illegal_template_name_as_validation(tmp_path):
    """Requirement 4: the rollout path applies the same ``_NAME_RE`` authority the catalog does.
    A traversal segment is refused BEFORE any disk read or provider write."""
    gh = FakeRepoProvider()
    svc = _svc(tmp_path, gh=gh)

    with pytest.raises(RolloutError) as exc:
        svc.rollout("conn1", template_names=["../../etc"], overwrite=False)
    assert exc.value.kind == "validation"
    assert gh.created == [] and gh.commits == []


def test_rollout_unknown_seed_is_not_found(tmp_path):
    svc = _svc(tmp_path)
    with pytest.raises(RolloutError) as exc:
        svc.rollout("conn1", template_names=["no-such-seed"], overwrite=False)
    assert exc.value.kind == "not_found"


def test_rollout_always_pushes_infra_from_the_s3_module_zip_when_absent(tmp_path):
    gh = FakeRepoProvider()
    s3 = FakeS3Client()
    svc = _svc(tmp_path, gh=gh, s3=s3)

    r = svc.rollout("conn1", template_names=[], overwrite=False)

    assert next(i for i in r.items if i.name == RUNTIME_REPO).action == "created"
    assert RUNTIME_REPO in gh.created
    pushed = next(c for c in gh.commits if c[0] == RUNTIME_REPO)[2]
    assert pushed == RUNTIME_MODULE_FILES
    assert s3.calls == [("agp-tf-state", "runtime-module/agentcore_runtime.zip")]


def test_a_template_re_push_does_not_authorize_an_infra_re_push(tmp_path):
    """The infra repo has its OWN consent. ``overwrite`` is a TEMPLATE flag from here on.

    Before this split, ticking a re-push on any template handed the same boolean to
    ``_ensure_infra``, so AGP's Terraform module was pushed over the org's existing
    ``agp-runtime-infra`` without anyone asking for it. With ``overwrite_infra`` defaulted, an
    existing infra repo is LEFT ALONE — even on a run that re-pushes a template."""
    gh = FakeRepoProvider(
        repos={
            SEED_NAME: {"default_branch": "main", "head_sha": "old", "files": {"x": b"1"}},
            RUNTIME_REPO: {"default_branch": "main", "head_sha": "old", "files": {"a": b"1"}},
        }
    )
    registry = _registry()
    _register(registry, SEED_NAME)
    svc = _svc(tmp_path, gh=gh, registry=registry)

    r = svc.rollout("conn1", template_names=[SEED_NAME], overwrite=True)

    infra = next(i for i in r.items if i.name == RUNTIME_REPO)
    assert infra.action == "skipped"
    assert infra.reason == "already exists in org"
    assert next(i for i in r.items if i.name == SEED_NAME).action == "overwritten"
    assert [c[0] for c in gh.commits] == [SEED_NAME]


def test_infra_overwrite_commits_on_top_without_deleting(tmp_path):
    gh = FakeRepoProvider(
        repos={RUNTIME_REPO: {"default_branch": "main", "head_sha": "old", "files": {"a": b"1"}}}
    )
    svc = _svc(tmp_path, gh=gh)

    r = svc.rollout("conn1", template_names=[], overwrite=True, overwrite_infra=True)

    assert next(i for i in r.items if i.name == RUNTIME_REPO).action == "overwritten"
    assert gh.created == []
    assert next(c for c in gh.commits if c[0] == RUNTIME_REPO)[2] == RUNTIME_MODULE_FILES


def test_infra_re_push_with_identical_module_reports_unchanged(tmp_path):
    """Live test A1: an infra repo already carrying the module got NO commit yet read "Re-pushed".
    The second run's push is a no-op, so its receipt must say "unchanged"."""
    gh = FakeRepoProvider(
        repos={RUNTIME_REPO: {"default_branch": "main", "head_sha": "old", "files": {"a": b"1"}}}
    )
    svc = _svc(tmp_path, gh=gh)

    svc.rollout("conn1", template_names=[], overwrite=True, overwrite_infra=True)
    r = svc.rollout("conn1", template_names=[], overwrite=True, overwrite_infra=True)

    assert next(i for i in r.items if i.name == RUNTIME_REPO).action == "unchanged"
    assert [c[0] for c in gh.commits].count(RUNTIME_REPO) == 1


def test_infra_repo_is_never_registered_as_a_template(tmp_path):
    registry = _registry()
    svc = _svc(tmp_path, registry=registry)

    svc.rollout("conn1", template_names=[], overwrite=False)

    assert registry.list_for_connection("conn1") == []


def test_rollout_infra_s3_failure_raises_before_any_write(tmp_path):
    gh = FakeRepoProvider()
    svc = _svc(tmp_path, gh=gh, s3=FakeS3Client(error=RuntimeError("boom")))

    with pytest.raises(RolloutError) as exc:
        svc.rollout("conn1", template_names=[], overwrite=False)

    assert exc.value.kind == "rollout_error"
    assert "boom" not in exc.value.message
    assert gh.created == [] and gh.commits == []


def test_rollout_infra_unreadable_zip_raises_before_any_write(tmp_path):
    """A corrupt S3 object must not half-create the infra repo (and must not be pushed as one
    opaque file, which is what a missing unzip guard would do)."""
    gh = FakeRepoProvider()
    svc = _svc(tmp_path, gh=gh, s3=FakeS3Client(body=b"not-a-zip"))

    with pytest.raises(RolloutError) as exc:
        svc.rollout("conn1", template_names=[], overwrite=False)
    assert exc.value.kind == "rollout_error"
    assert gh.created == [] and gh.commits == []


def test_rollout_infra_missing_config_raises(tmp_path):
    gh = FakeRepoProvider()
    templates = _seed_templates_dir(tmp_path)
    svc = RolloutService(
        gh,
        FakeConnectionService(),
        agent_templates_dir=str(templates),
        s3_client=FakeS3Client(),
        runtime_module_bucket="",
        runtime_module_key="",
        template_registry=_registry(),
        infra_template_registry=_infra_registry(),
        known_repo_names=lambda connection_id: set(),
    )

    with pytest.raises(RolloutError) as exc:
        svc.rollout("conn1", template_names=[], overwrite=False)
    assert exc.value.kind == "rollout_error"
    assert gh.created == [] and gh.commits == []


def test_rollout_seeds_both_infra_template_records_for_the_connection(tmp_path):
    """The two seeds land in the connection's infra catalog on every rollout.

    :data:`SEED_INFRA_TEMPLATES` is connection-AGNOSTIC by construction (the entries omit
    ``connection_id``/``created_at``), so seeding is the rollout's job and the per-connection
    halves are supplied HERE. A record with no ``connection_id`` cannot be written at all — the
    composite sk needs both halves — so this is the write that makes the catalog exist."""
    infra = _infra_registry()
    svc = _svc(tmp_path, infra_registry=infra)

    svc.rollout("conn1", template_names=[], overwrite=False)

    seeded = {r.id: r for r in infra.list("conn1")}
    assert set(seeded) == {DEFAULT_INFRA_TEMPLATE_ID, HARNESS_ID, "agentcore-gateway"}
    runtime = seeded[DEFAULT_INFRA_TEMPLATE_ID]
    assert runtime.connection_id == "conn1"
    assert runtime.created_at == "2026-08-05T00:00:00+00:00"
    assert (runtime.platform, runtime.build_kind, runtime.iac_type) == (
        "aws",
        "container",
        "agentcore_runtime",
    )
    assert seeded[HARNESS_ID].build_kind == "config"
    assert seeded[HARNESS_ID].repo_name == HARNESS_REPO


def test_seeding_infra_records_is_an_upsert_that_keeps_the_first_created_at(tmp_path):
    """Re-seeding must not leave two ``agentcore-runtime`` rows and must not
    re-stamp ``created_at`` — the same rule ``_register`` follows for agent templates, so the two
    catalogs cannot disagree about when a connection's entry was first written.

    The clock ADVANCES between the two runs — with a frozen one the created_at assertion would
    pass against an implementation that re-stamps it every time."""
    infra = _infra_registry()
    clock = iter(["2026-08-05T00:00:00+00:00"] + ["2026-09-09T09:09:09+00:00"] * 8)
    svc = _svc(tmp_path, infra_registry=infra, now=lambda: next(clock))

    svc.rollout("conn1", template_names=[], overwrite=False)
    seeded = infra.get("conn1", DEFAULT_INFRA_TEMPLATE_ID)
    assert seeded is not None
    assert seeded.created_at == ("2026-08-05T00:00:00+00:00")

    svc.rollout("conn1", template_names=[], overwrite=False)  # a later run, later clock

    assert len(infra.list("conn1")) == len(SEED_INFRA_TEMPLATES)
    seeded = infra.get("conn1", DEFAULT_INFRA_TEMPLATE_ID)
    assert seeded is not None
    assert seeded.created_at == ("2026-08-05T00:00:00+00:00")


_RACED_SEED = SEED_INFRA_TEMPLATES[0]
_RACED_SK = f"conn1#{_RACED_SEED['id']}"


def test_a_lost_infra_seed_whose_row_equals_the_seed_is_done_without_a_second_write(tmp_path):
    infra = _RacedInfraRegistry()
    infra.rival = lambda: infra.create(
        InfraTemplateRecord(**_RACED_SEED, connection_id="conn1", created_at="2026-08-04T00:00:00+00:00")
    )
    svc = _svc(tmp_path, infra_registry=infra)

    svc.rollout("conn1", template_names=[], overwrite=False)

    row = infra._store.get(("infra_template", _RACED_SK))
    assert row is not None
    assert (row["row_version"], row["created_at"]) == (1, "2026-08-04T00:00:00+00:00")
    assert len(infra.list("conn1")) == len(SEED_INFRA_TEMPLATES)


@pytest.mark.parametrize("rival", ["edit", "delete"])
def test_a_lost_infra_seed_whose_row_differs_from_the_seed_answers_changed(tmp_path, rival):
    infra = _RacedInfraRegistry()
    gh = FakeRepoProvider()
    svc = _svc(tmp_path, gh=gh, infra_registry=infra)
    svc.rollout("conn1", template_names=[], overwrite=False)
    commits_before = list(gh.commits)

    def edit():
        current = infra.get("conn1", _RACED_SEED["id"])
        assert current is not None
        infra.save(current.model_copy(update={"description": "edited meanwhile"}))

    infra.rival = {"edit": edit, "delete": lambda: infra.delete("conn1", _RACED_SEED["id"])}[rival]

    with pytest.raises(RolloutError) as exc:
        svc.rollout("conn1", template_names=[SEED_NAME], overwrite=False)

    assert (exc.value.status_code, exc.value.body()) == (
        409,
        {"detail": "Rollout changed while this request ran; reload and retry", "code": "rollout.changed"},
    )
    assert gh.commits == commits_before


def test_the_reads_before_a_register_and_a_seed_write_are_strongly_consistent(tmp_path):
    registry, infra = _registry(), _RacedInfraRegistry()
    infra.rival = lambda: infra.create(
        InfraTemplateRecord(**_RACED_SEED, connection_id="conn1", created_at="2026-08-04T00:00:00+00:00")
    )
    templates = MagicMock(wraps=registry._store._table)
    registry._store = StateStore("projects", table=templates)
    infra_table = MagicMock(wraps=infra._store._table)
    infra._store = StateStore("projects", table=infra_table)
    svc = _svc(tmp_path, registry=registry, infra_registry=infra)

    svc.rollout("conn1", template_names=[SEED_NAME], overwrite=False)

    assert [c.kwargs["ConsistentRead"] for c in templates.get_item.call_args_list] == [True]
    assert [c.kwargs["ConsistentRead"] for c in infra_table.get_item.call_args_list] == [
        True,
        True,
        True,
        True,
        False,
    ]


def test_infra_records_are_scoped_to_the_rolled_out_connection(tmp_path):
    infra = _infra_registry()
    svc = _svc(tmp_path, infra_registry=infra)

    svc.rollout("conn1", template_names=[], overwrite=False)

    assert infra.list("conn2") == []


def test_rollout_ensures_only_the_infra_repos_its_templates_reference(tmp_path):
    """ONE ORG REPO PER REFERENCED INFRA TEMPLATE — and ``referenced`` is the whole point.

    Seeding the harness CATALOG row costs nothing (it is a pointer), but pushing
    ``agp-harness-infra`` into a customer's org means a repository they never asked for, holding
    preview-status Terraform. So the repos follow the agent templates that are actually
    registered: this connection has only the strands seed, whose chain is the runtime, so exactly
    one infra repo appears."""
    gh = FakeRepoProvider()
    svc = _svc(tmp_path, gh=gh)

    r = svc.rollout("conn1", template_names=[SEED_NAME], overwrite=False)

    assert next(i for i in r.items if i.name == RUNTIME_REPO).action == "created"
    assert HARNESS_REPO not in [i.name for i in r.items]
    assert HARNESS_REPO not in gh.repos


def test_a_config_chain_template_pulls_in_its_own_infra_repo(tmp_path):
    """A registered template pointing at the harness gets the HARNESS repo, filled from the
    harness ``module_source`` — not the runtime zip under a different name."""
    gh = FakeRepoProvider()
    s3 = FakeS3Client()
    registry = _registry()
    _register(registry, "harness-agentcore", infra_template_id=HARNESS_ID)
    svc = _svc(tmp_path, gh=gh, s3=s3, registry=registry)

    r = svc.rollout("conn1", template_names=[], overwrite=False)

    actions = {i.name: i.action for i in r.items}
    assert actions[HARNESS_REPO] == "created"
    assert actions[RUNTIME_REPO] == "created"
    pushed = next(c for c in gh.commits if c[0] == HARNESS_REPO)[2]
    assert pushed == HARNESS_MODULE_FILES
    assert ("agp-tf-state", HARNESS_KEY) in s3.calls


def test_the_default_infra_repo_is_ensured_even_with_no_registered_templates(tmp_path):
    """FORCEDNESS SURVIVES THE GENERALIZATION. Before the catalog the runtime repo was ensured
    unconditionally; a purely reference-driven rule would stop ensuring it on a fresh connection
    (no templates ⇒ no references ⇒ no infra), and a tenant runtime cannot deploy without it."""
    gh = FakeRepoProvider()
    svc = _svc(tmp_path, gh=gh)

    r = svc.rollout("conn1", template_names=[], overwrite=False)

    assert next(i for i in r.items if i.name == RUNTIME_REPO).action == "created"


def test_a_dangling_infra_template_id_does_not_ensure_a_repo(tmp_path):
    """A template referencing an infra id with no catalog row names no repo, so there is nothing
    to ensure — and inventing a name from the id would create a repository whose Terraform AGP
    cannot supply. The rollout continues (the default repo still lands)."""
    gh = FakeRepoProvider()
    registry = _registry()
    _register(registry, "exotic-agent", infra_template_id="no-such-infra")
    svc = _svc(tmp_path, gh=gh, registry=registry)

    r = svc.rollout("conn1", template_names=[], overwrite=False)

    assert [i.name for i in r.items] == [RUNTIME_REPO]


def test_infra_overwrite_consent_covers_every_referenced_infra_repo(tmp_path):
    """``overwrite_infra`` is still the infra repos' OWN consent — now for all of them.
    Defaulted off, an existing infra repo is left alone; asked for, each is re-pushed."""
    gh = FakeRepoProvider(
        repos={
            RUNTIME_REPO: {"default_branch": "main", "head_sha": "r", "files": {"a": b"1"}},
            HARNESS_REPO: {"default_branch": "main", "head_sha": "h", "files": {"b": b"2"}},
        }
    )
    registry = _registry()
    _register(registry, "harness-agentcore", infra_template_id=HARNESS_ID)
    svc = _svc(tmp_path, gh=gh, registry=registry)

    skipped = svc.rollout("conn1", template_names=[], overwrite=True)
    assert {i.action for i in skipped.items} == {"skipped"}
    assert gh.commits == []

    overwritten = svc.rollout("conn1", template_names=[], overwrite=False, overwrite_infra=True)
    assert {i.action for i in overwritten.items} == {"overwritten"}
    assert sorted(c[0] for c in gh.commits) == sorted([HARNESS_REPO, RUNTIME_REPO])


def test_reconcile_still_subtracts_every_infra_repo_from_the_adopt_rows(tmp_path):
    """The adopt-picker subtraction generalizes with the repos. An infra repo offered as an adopt
    row invites an operator to register Terraform as an agent template — that was
    true of one hardcoded name and must stay true of every catalog-named one."""
    gh = FakeRepoProvider(
        repos={
            RUNTIME_REPO: {"default_branch": "main", "head_sha": "r", "files": {}},
            HARNESS_REPO: {"default_branch": "main", "head_sha": "h", "files": {}},
        }
    )
    registry = _registry()
    _register(registry, "harness-agentcore", infra_template_id=HARNESS_ID)
    svc = _svc(tmp_path, gh=gh, registry=registry)

    view = svc.reconcile("conn1")

    names = [i.name for i in view.templates]
    assert RUNTIME_REPO not in names and HARNESS_REPO not in names
    assert view.infra_repo.name == RUNTIME_REPO


def test_adopt_refuses_any_infra_repo_not_just_the_runtime_one(tmp_path):
    gh = FakeRepoProvider(repos={HARNESS_REPO: {"default_branch": "main", "head_sha": "h", "files": {}}})
    registry = _registry()
    _register(registry, "harness-agentcore", infra_template_id=HARNESS_ID)
    svc = _svc(tmp_path, gh=gh, registry=registry)

    with pytest.raises(RolloutError) as exc:
        svc.adopt("conn1", repo_name=HARNESS_REPO, description=None)

    assert exc.value.kind == "conflict"
    assert "infra" in exc.value.message.lower()


def test_adopt_refuses_the_runtime_repo_on_an_unseeded_connection(tmp_path):
    """FAILS CLOSED on a connection whose catalog is empty. The guard now reads the catalog, so a
    connection that has never been rolled out has no infra ROWS — and if the refusal came only
    from those rows, the very first thing an operator could do to a fresh connection is adopt
    ``agp-runtime-infra`` as a template. The SEED repo names are part of the guard for that
    reason."""
    gh = FakeRepoProvider(repos={RUNTIME_REPO: {"default_branch": "main", "head_sha": "r", "files": {}}})
    svc = _svc(tmp_path, gh=gh, infra_registry=_infra_registry())

    with pytest.raises(RolloutError) as exc:
        svc.adopt("conn1", repo_name=RUNTIME_REPO, description=None)
    assert exc.value.kind == "conflict"


def test_a_failing_harness_module_still_leaves_the_runtime_repo_ensured(tmp_path):
    """ATTEMPT ALL, THEN RAISE.

    The targets are sorted by id, so ``agentcore-harness`` is attempted BEFORE
    ``agentcore-runtime``. A bare loop therefore made one unstaged harness zip abort the rollout
    before ``agp-runtime-infra`` was ensured at all — a repo the pre-catalog code attempted
    unconditionally, and the one every existing tenant runtime deploys through. A preview
    mechanism's staging problem must not cost the org its production infra repo.

    Both halves are the contract: the runtime repo IS created, and the run still RAISES naming the
    failed infra template (a partial rollout must never read as success)."""
    gh = FakeRepoProvider()
    s3 = FakeS3Client(bodies={RUNTIME_KEY: RUNTIME_MODULE_FILES})
    registry = _registry()
    _register(registry, "harness-agentcore", infra_template_id=HARNESS_ID)
    svc = _svc(tmp_path, gh=gh, s3=s3, registry=registry)

    with pytest.raises(RolloutError) as exc:
        svc.rollout("conn1", template_names=[], overwrite=False)

    assert exc.value.kind == "rollout_error"
    assert HARNESS_ID in exc.value.message
    assert RUNTIME_REPO in gh.created
    assert next(c for c in gh.commits if c[0] == RUNTIME_REPO)[2] == RUNTIME_MODULE_FILES
    assert "s3://" not in exc.value.message and "agp-tf-state" not in exc.value.message


def test_an_unreadable_infra_catalog_raises_before_any_write(tmp_path):
    """STRICT, like every other registry read here: "AGP could not read its infra catalog" is not
    evidence that a connection references no infra, and degrading would skip ensuring the repo a
    tenant runtime deploys through while reporting success."""

    class _Exploding:
        def list(self, connection_id):
            raise InfraTemplateRegistryError("store down")

        def create(self, record):
            raise InfraTemplateRegistryError("store down")

        def save(self, record):
            raise InfraTemplateRegistryError("store down")

        def get(self, connection_id, infra_template_id, *, consistent=False):
            raise InfraTemplateRegistryError("store down")

    gh = FakeRepoProvider()
    svc = _svc(tmp_path, gh=gh, infra_registry=_Exploding())

    with pytest.raises(RolloutError) as exc:
        svc.rollout("conn1", template_names=[], overwrite=False)

    assert exc.value.kind == "rollout_error"
    assert "store down" not in exc.value.message
    assert gh.created == [] and gh.commits == []


def test_the_module_constant_is_gone(tmp_path):
    """``INFRA_REPO_NAME`` was the one hardcoded deploy mechanism (design §2). It must be DELETED
    rather than left beside the catalog: a surviving constant is what a later edit reaches for,
    and it can only ever name the runtime."""
    import services.template_rollout_service as mod

    assert not hasattr(mod, "INFRA_REPO_NAME")


def test_register_writes_the_seeds_template_json_metadata(tmp_path):
    """Requirement 4 / design 4e: a rolled-out card must render a description and all three pill
    groups exactly like an uploaded one, from the seed's own ``template.json``."""
    registry = _registry()
    svc = _svc(tmp_path, registry=registry)

    svc.rollout("conn1", template_names=[SEED_NAME], overwrite=False)

    record = _template(registry, SEED_NAME)
    assert record.description == SEED_TEMPLATE_JSON["description"]
    assert record.framework == "strands"
    assert record.aws_services == ["bedrock", "ecr", "agentcore"]
    assert record.tags == ["starter", "python", "strands"]
    assert record.version == "1.0.0"


def test_register_writes_both_halves_of_the_source_pair(tmp_path):
    """A HALF-SET pair (org without repo) is type-permitted; both or neither — never one."""
    registry = _registry()
    svc = _svc(tmp_path, registry=registry)

    svc.rollout("conn1", template_names=[SEED_NAME], overwrite=False)

    record = _template(registry, SEED_NAME)
    assert record.source_org == "acme"
    assert record.source_repo == SEED_NAME
    assert (record.source_org is None) == (record.source_repo is None)


def test_re_push_stops_stamping_version_1_and_keeps_the_first_registrant(tmp_path):
    """Requirement 4: ``version="1"`` was stamped unconditionally, so a re-push silently reset a
    versioned catalog entry. ``created_at``/``created_by`` belong to the FIRST registration."""
    registry = _registry()
    _register(registry, SEED_NAME, version="7", created_by="alice@x.com")
    svc = _svc(tmp_path, registry=registry)

    svc.rollout("conn1", template_names=[SEED_NAME], overwrite=True)

    record = _template(registry, SEED_NAME)
    assert record.version == "1.0.0"  # the seed's DECLARED version, not "1"
    assert record.created_by == "alice@x.com"
    assert record.created_at == "2026-07-01T00:00:00+00:00"


def test_re_push_without_seed_metadata_keeps_the_recorded_version(tmp_path):
    """No ``template.json`` on disk ⇒ nothing to declare, so the recorded version stands. Still
    not reset to "1"."""
    registry = _registry()
    _register(registry, SEED_NAME, version="7")
    svc = _svc(tmp_path, registry=registry, template_json=None)

    svc.rollout("conn1", template_names=[SEED_NAME], overwrite=True)

    assert _template(registry, SEED_NAME).version == "7"


def test_register_threads_the_seeds_declared_infra_chain(tmp_path):
    """``template.json`` declares ``infra_template_id`` + ``preview``, and ``_register`` once
    DROPPED both — so a rolled-out harness template registered as a
    CONTAINER one, materialize would set ECR vars for a repo with no Dockerfile, and the console
    would show no Preview chip on a preview template."""
    registry = _registry()
    svc = _svc(
        tmp_path,
        registry=registry,
        template_json={
            **SEED_TEMPLATE_JSON,
            "infra_template_id": HARNESS_ID,
            "preview": True,
        },
    )

    svc.rollout("conn1", template_names=[SEED_NAME], overwrite=False)

    record = _template(registry, SEED_NAME)
    assert record.infra_template_id == HARNESS_ID
    assert record.preview is True


def test_a_seed_that_declares_no_chain_gets_the_default_not_an_empty_string(tmp_path):
    """AN OMITTED KEY MUST YIELD THE DEFAULT. ``metadata.get("infra_template_id", "")`` would
    write ``""`` — which is not a chain, dereferences to nothing, and would make every chainless
    seed's materialize take the no-infra branch. The model's default only applies when the field
    is not passed, so the READ has to supply it."""
    registry = _registry()
    svc = _svc(tmp_path, registry=registry, template_json=SEED_TEMPLATE_JSON)

    svc.rollout("conn1", template_names=[SEED_NAME], overwrite=False)

    record = _template(registry, SEED_NAME)
    assert record.infra_template_id == DEFAULT_INFRA_TEMPLATE_ID
    assert record.preview is False


def test_register_reads_the_seeds_declared_artifact_kind(tmp_path):
    """An MCP-server template says so in ``template.json``; dropping it (the
    ``infra_template_id`` trap again) would register it as an agent template."""
    registry = _registry()
    svc = _svc(
        tmp_path,
        registry=registry,
        template_json={**SEED_TEMPLATE_JSON, "artifact_kind": "mcp_server"},
    )

    svc.rollout("conn1", template_names=[SEED_NAME], overwrite=False)

    assert _template(registry, SEED_NAME).artifact_kind == "mcp_server"


def test_a_seed_that_declares_no_artifact_kind_registers_as_an_agent(tmp_path):
    registry = _registry()
    svc = _svc(tmp_path, registry=registry, template_json=SEED_TEMPLATE_JSON)

    svc.rollout("conn1", template_names=[SEED_NAME], overwrite=False)

    assert _template(registry, SEED_NAME).artifact_kind == "agent"


def test_unreadable_seed_metadata_fails_loudly(tmp_path):
    """A PRESENT but malformed ``template.json`` is a defect in the seed, not an absence. Falling
    back to defaults would ship a blank card and call it success."""
    registry = _registry()
    svc = _svc(tmp_path, registry=registry, template_json="{not json")

    with pytest.raises(RolloutError) as exc:
        svc.rollout("conn1", template_names=[SEED_NAME], overwrite=False)
    assert exc.value.kind == "rollout_error"


def test_adopt_registers_an_existing_org_repo_as_is(tmp_path):
    gh = FakeRepoProvider(
        repos={"their-template": {"default_branch": "trunk", "head_sha": "ddd", "files": {}}}
    )
    registry = _registry()
    svc = _svc(tmp_path, gh=gh, registry=registry)

    view = svc.adopt("conn1", repo_name="their-template", description="ours now", created_by="bob@x.com")

    assert view.name == "their-template"
    assert view.description == "ours now"
    record = _template(registry, "their-template")
    assert record.source_org == "acme"
    assert record.source_repo == "their-template"
    assert record.created_by == "bob@x.com"
    assert gh.commits == [] and gh.created == []


def test_adopt_of_an_absent_repo_is_repo_not_found(tmp_path):
    svc = _svc(tmp_path, gh=FakeRepoProvider())
    with pytest.raises(RolloutError) as exc:
        svc.adopt("conn1", repo_name="ghost", description=None)
    assert exc.value.kind == "repo_not_found"


def test_adopt_of_an_already_registered_name_is_a_conflict(tmp_path):
    gh = FakeRepoProvider(repos={SEED_NAME: {"default_branch": "main", "head_sha": "a", "files": {}}})
    registry = _registry()
    _register(registry, SEED_NAME)
    svc = _svc(tmp_path, gh=gh, registry=registry)

    with pytest.raises(RolloutError) as exc:
        svc.adopt("conn1", repo_name=SEED_NAME, description=None)
    assert exc.value.kind == "conflict"


def test_two_adopts_of_one_repository_answer_conflict_for_one(tmp_path):
    gh = FakeRepoProvider(repos={"their-template": {"default_branch": "main", "head_sha": "d", "files": {}}})
    registry = _RacedRegistry()
    svc = _svc(tmp_path, gh=gh, registry=registry)
    registry.rival = lambda: svc.adopt(
        "conn1", repo_name="their-template", description="rival", created_by="rival@x.com"
    )

    with pytest.raises(RolloutError) as exc:
        svc.adopt("conn1", repo_name="their-template", description="loser", created_by="loser@x.com")

    assert (exc.value.status_code, exc.value.body()) == (
        409,
        {"detail": "Template already registered", "code": "rollout.conflict"},
    )
    (row,) = registry.list_for_connection("conn1")
    assert (row.description, row.created_by) == ("rival", "rival@x.com")


def test_adopt_refuses_the_forced_infra_repo(tmp_path):
    """The infra repo is NOT a template and never was (D-B4). Adopting it produced a registry row
    for a name that also occupies ``infra_repo``, so reconcile showed it TWICE — and a materialize
    could then ship Terraform as an agent.

    The subtraction has to live in the VERB, not only in the picker: a picker that omits a row is a
    UI courtesy, and this endpoint is reachable with a hand-made POST."""
    gh = FakeRepoProvider(repos={RUNTIME_REPO: {"default_branch": "main", "head_sha": "i", "files": {}}})
    registry = _registry()
    svc = _svc(tmp_path, gh=gh, registry=registry)

    with pytest.raises(RolloutError) as exc:
        svc.adopt("conn1", repo_name=RUNTIME_REPO, description=None)

    assert exc.value.kind == "conflict"
    assert "infra" in exc.value.message.lower()
    assert registry.list_for_connection("conn1") == []


def test_adopt_refuses_a_materialized_agent_repo(tmp_path):
    """``_materialized_repo_names``' docstring says the subtraction exists so an operator cannot
    "register a live agent's repository as a template — after which every materialize from it
    would ship that agent's code". That was true of the picker and false of the verb."""
    gh = FakeRepoProvider(repos={"my-live-agent": {"default_branch": "main", "head_sha": "a", "files": {}}})
    registry = _registry()
    svc = _svc(
        tmp_path,
        gh=gh,
        registry=registry,
        known_repo_names=lambda connection_id: {"my-live-agent"},
    )

    with pytest.raises(RolloutError) as exc:
        svc.adopt("conn1", repo_name="my-live-agent", description=None)

    assert exc.value.kind == "conflict"
    assert "agent" in exc.value.message.lower()
    assert registry.list_for_connection("conn1") == []


def test_adopt_fails_closed_without_a_repo_inventory(tmp_path):
    """Same posture as reconcile: if AGP cannot enumerate its own agent repos it cannot prove the
    target is not one, and guessing would be the exact mistake the guard prevents."""
    gh = FakeRepoProvider(repos={"their-template": {"default_branch": "main", "head_sha": "d", "files": {}}})
    svc = _svc(tmp_path, gh=gh, known_repo_names=None)

    with pytest.raises(RolloutError) as exc:
        svc.adopt("conn1", repo_name="their-template", description=None)
    assert exc.value.kind == "rollout_error"


def test_adopt_refuses_an_illegal_name_before_touching_the_provider(tmp_path):
    gh = FakeRepoProvider()
    svc = _svc(tmp_path, gh=gh)
    with pytest.raises(RolloutError) as exc:
        svc.adopt("conn1", repo_name="Bad Name", description=None)
    assert exc.value.kind == "validation"
    assert gh.read_repo_calls == []


def test_adopt_view_copies_the_infra_chain_from_the_record(tmp_path):
    """The projection must COPY ``infra_template_id``/``preview``, not let pydantic default them.

    Omitting them was invisible: adopt writes neither, so the record carries the same defaults the
    view would fill in, and the two agreed by coincidence. Pinned against a record whose chain is
    NON-default — write one directly through the registry and re-project it — so the projection
    breaks loudly if it ever goes back to guessing.
    """
    from services.template_rollout_service import _view_from_record

    registry = _registry()
    record = TemplateRecord(
        id="harness-agentcore",
        name="harness-agentcore",
        connection_id="conn1",
        created_at="2026-08-06T00:00:00+00:00",
        infra_template_id="agentcore-harness",
        preview=True,
    )
    registry.create(record)

    view = _view_from_record(_template(registry, "harness-agentcore"))

    assert view.infra_template_id == "agentcore-harness"
    assert view.preview is True
    assert view.platform == "aws"
    assert view.build_kind == "container"


def test_the_rollout_view_copies_the_artifact_kind_from_the_record():
    """The SAME drift trap as the infra chain above — omitted, pydantic would default it
    and an MCP-server template would render as an agent one."""
    from services.template_rollout_service import _view_from_record

    record = TemplateRecord(
        id="gateway-agentcore",
        name="gateway-agentcore",
        connection_id="conn1",
        created_at="2026-10-05T00:00:00+00:00",
        artifact_kind="mcp_server",
    )

    assert _view_from_record(record).artifact_kind == "mcp_server"


def test_adopt_writes_both_halves_of_the_source_pair(tmp_path):
    """N-1 again, on the other new writer."""
    gh = FakeRepoProvider(repos={"their-template": {"default_branch": "main", "head_sha": "d", "files": {}}})
    registry = _registry()
    svc = _svc(tmp_path, gh=gh, registry=registry)

    svc.adopt("conn1", repo_name="their-template", description=None)

    record = _template(registry, "their-template")
    assert record.source_org and record.source_repo
    assert (record.source_org is None) == (record.source_repo is None)


def test_adopt_source_url_defaults_to_github_com_without_a_base_url(tmp_path):
    """No base URL on the connection ⇒ github.com. The BASELINE for the GHE cases below."""
    gh = FakeRepoProvider(repos={"their-template": {"default_branch": "main", "head_sha": "d", "files": {}}})
    registry = _registry()
    svc = _svc(tmp_path, gh=gh, registry=registry)

    svc.adopt("conn1", repo_name="their-template", description=None)

    record = _template(registry, "their-template")
    assert record.source_url == "https://github.com/acme/their-template"


@pytest.mark.parametrize(
    "base_url,expected_host",
    [
        pytest.param("https://ghe.example.com/api/v3", "https://ghe.example.com", id="api_v3_suffix"),
        pytest.param("https://ghe.example.com/api/v3/", "https://ghe.example.com", id="trailing_slash"),
        pytest.param("https://ghe.example.com/api", "https://ghe.example.com", id="bare_api_suffix"),
        pytest.param("https://ghe.example.com", "https://ghe.example.com", id="no_suffix"),
        pytest.param(
            "https://ghe.example.com/api/v3/extra", "https://ghe.example.com/api/v3/extra", id="mid_path_api"
        ),
    ],
)
def test_adopt_source_url_is_derived_from_a_ghe_base_url(tmp_path, base_url, expected_host):
    """An on-prem repo must NOT be linked to github.com. ``source_url`` is display
    only, so the whole point of it is that a human clicking it lands on the right host."""
    gh = FakeRepoProvider(repos={"their-template": {"default_branch": "main", "head_sha": "d", "files": {}}})
    registry = _registry()
    svc = _svc(tmp_path, gh=gh, registry=registry, conn=FakeConnectionService(base_url=base_url))

    svc.adopt("conn1", repo_name="their-template", description=None)

    record = _template(registry, "their-template")
    assert record.source_url == f"{expected_host}/acme/their-template"


def test_adopt_then_reconcile_reports_it_registered_and_present(tmp_path):
    """The round trip: what adopt registers is what reconcile then sees — and it stops being an
    adopt row."""
    gh = FakeRepoProvider(repos={"their-template": {"default_branch": "main", "head_sha": "d", "files": {}}})
    svc = _svc(tmp_path, gh=gh)

    svc.adopt("conn1", repo_name="their-template", description=None)

    row = _row(svc.reconcile("conn1"), "their-template")
    assert row.state == "registered_present"
    assert row.origin == "registry"


class _BrokenRegistry:
    def list_for_connection(self, connection_id):
        raise TemplateRegistryError("Could not read the template catalog")

    def get(self, connection_id, template_id, *, consistent=False):
        raise TemplateRegistryError("Could not read the template catalog")

    def save(self, record):
        raise TemplateRegistryError("Could not write the template catalog")


class _ValidationRejectingRegistry:
    def list_for_connection(self, connection_id):
        raise TemplateRegistryValidationError("connection_id must not contain '#'")

    def get(self, connection_id, template_id, *, consistent=False):
        raise TemplateRegistryValidationError("connection_id must not contain '#'")

    def save(self, record):
        raise TemplateRegistryValidationError("template name must not contain '#'")


def test_reconcile_raises_rather_than_calling_everything_absent(tmp_path):
    svc = _svc(tmp_path, registry=_BrokenRegistry())
    with pytest.raises(RolloutError) as exc:
        svc.reconcile("conn1")
    assert exc.value.kind == "rollout_error"


def test_rollout_raises_when_the_catalog_is_unreadable(tmp_path):
    gh = FakeRepoProvider()
    svc = _svc(tmp_path, gh=gh, registry=_BrokenRegistry())
    with pytest.raises(RolloutError) as exc:
        svc.rollout("conn1", template_names=[SEED_NAME], overwrite=False)
    assert exc.value.kind == "rollout_error"
    assert gh.created == [] and gh.commits == []


def test_rollout_errors_carry_a_safe_message(tmp_path):
    svc = _svc(tmp_path, registry=_BrokenRegistry())
    with pytest.raises(RolloutError) as exc:
        svc.reconcile("conn1")
    assert "TemplateRegistryError" not in exc.value.message


@pytest.mark.parametrize("bad_connection", ["", "conn#1"])
def test_reconcile_reports_a_malformed_connection_as_validation(tmp_path, bad_connection):
    svc = _svc(tmp_path, registry=_ValidationRejectingRegistry())
    with pytest.raises(RolloutError) as exc:
        svc.reconcile(bad_connection)
    assert exc.value.kind == "validation"


def test_rollout_reports_a_malformed_connection_as_validation(tmp_path):
    svc = _svc(tmp_path, registry=_ValidationRejectingRegistry())
    with pytest.raises(RolloutError) as exc:
        svc.rollout("conn#1", template_names=[SEED_NAME], overwrite=False)
    assert exc.value.kind == "validation"


def test_adopt_reports_a_malformed_connection_as_validation(tmp_path):
    gh = FakeRepoProvider(repos={"their-template": {"default_branch": "main", "head_sha": "d", "files": {}}})
    svc = _svc(tmp_path, gh=gh, registry=_ValidationRejectingRegistry())
    with pytest.raises(RolloutError) as exc:
        svc.adopt("conn#1", repo_name="their-template", description=None)
    assert exc.value.kind == "validation"


def test_a_store_fault_stays_rollout_error_not_validation(tmp_path):
    """The guard: collapsing both kinds into "validation" would pass the three tests above while
    telling the console NOT to retry a transient store fault."""
    svc = _svc(tmp_path, registry=_BrokenRegistry())
    with pytest.raises(RolloutError) as exc:
        svc.reconcile("conn1")
    assert exc.value.kind == "rollout_error"


def test_reconcile_seed_row_carries_the_infra_template_its_template_json_declares(tmp_path):
    """A queued seed is REGISTERED before ``_referenced_infra_records`` runs, so its declared
    chain makes an infra repo load-bearing in the same roll-out. The console can only show that if
    the row says which infra the seed deploys through."""
    svc = _svc(
        tmp_path,
        template_json={**SEED_TEMPLATE_JSON, "infra_template_id": "agentcore-gateway"},
    )
    assert _row(svc.reconcile("conn1"), SEED_NAME).infra_template_id == "agentcore-gateway"


def test_reconcile_seed_row_without_a_declared_chain_folds_to_the_default(tmp_path):
    """Same ``or DEFAULT`` ``_register`` applies, so the two cannot disagree."""
    svc = _svc(tmp_path)
    assert _row(svc.reconcile("conn1"), SEED_NAME).infra_template_id == DEFAULT_INFRA_TEMPLATE_ID


def test_reconcile_seed_row_with_unreadable_metadata_does_not_fail_the_read(tmp_path):
    """The compare view is a READ; a malformed seed fails loudly at roll-out time already."""
    svc = _svc(tmp_path, template_json="{not json")
    assert _row(svc.reconcile("conn1"), SEED_NAME).infra_template_id is None


def test_reconcile_registry_only_row_has_no_infra_template_id(tmp_path):
    registry = _registry()
    _register(registry, "uploaded-one")
    svc = _svc(tmp_path, registry=registry)
    assert _row(svc.reconcile("conn1"), "uploaded-one").infra_template_id is None


def test_reconcile_lists_the_infra_seeds_the_roll_out_upserts_first(tmp_path):
    """``rollout()`` seeds the catalog BEFORE it reads it, so a seed missing from the catalog
    today is still a target. The view carries the seeds so the console can show it."""
    view = _svc(tmp_path).reconcile("conn1")
    assert [(s["id"], s["repo_name"]) for s in view.infra_seeds] == [
        (s["id"], s["repo_name"]) for s in SEED_INFRA_TEMPLATES
    ]
    assert all("module_source" not in s for s in view.infra_seeds)


def test_seed_and_infra_pushes_carry_the_skip_ci_marker(tmp_path):
    """One commit per push and ``[skip ci]`` on it: the marker is what keeps a seed push from firing
    the repo's own build, on every provider, with no branch filter to keep correct."""
    gh = FakeRepoProvider()
    svc = _svc(tmp_path, gh=gh)

    svc.rollout("conn1", template_names=[SEED_NAME], overwrite=False)

    messages = {repo: message for repo, _branch, _files, message in gh.commits}
    assert set(messages) == {SEED_NAME, RUNTIME_REPO}
    assert all("[skip ci]" in message for message in messages.values())


@pytest.mark.parametrize(
    "kind,status,detail",
    [
        ("not_found", 404, "Unknown base template"),
        ("repo_not_found", 404, "Repository not found in the org"),
        ("conflict", 409, "Template already registered"),
        ("validation", 422, "Invalid template name or connection id"),
        ("rollout_error", 502, "Template rollout failed"),
        ("changed", 409, "Rollout changed while this request ran; reload and retry"),
    ],
)
def test_every_rollout_error_kind_renders_a_fixed_detail(kind, status, detail):
    err = RolloutError("the provider said: secret internals", kind)

    assert (err.status_code, err.kind) == (status, kind)
    assert err.body() == {"detail": detail, "code": f"rollout.{kind}"}
