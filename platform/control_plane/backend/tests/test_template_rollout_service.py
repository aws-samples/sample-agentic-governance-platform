"""RolloutService unit tests (E22/T5 → rebuilt as the RECONCILE service in E28C/T3).

WHAT MOVED, AND WHY THE OLD TESTS COULD NOT SURVIVE
---------------------------------------------------
E28B/T2 answered "is this base template already there?" from AGP's CATALOG. That was a lie with
a name: a DDB row is evidence about AGP's own store, never about the ORG, so a registered
template whose repo had been deleted read as present, and a repo that already existed under a
seed's name read as absent and then 422'd inside the push. E28C/T3 replaces the boolean with a
THREE-STATE reconcile (design D-C3): the registry and the org are compared, each row gets the
one action that is honest for its state, and ``read_repo`` — not the catalog — answers "present".

``RolloutItem.exists_in_org`` is therefore DELETED rather than aliased, and destructive overwrite
(delete_repo + create_repo_from_zip) is deleted with it: a re-push is an idempotent
``commit_files`` of the seed bytes onto the existing repo.

THE FAKE PROVIDER IS NO MORE GENEROUS THAN GITHUB (E28B C-2)
------------------------------------------------------------
:class:`FakeRepoProvider` seeds REAL branch/tree state: ``create_repo`` auto-inits a branch (the
live one does, and a fake that left a repo branchless would let a broken push pass), ``read_repo``
answers ``None`` only for not-found, ``commit_files`` refuses an empty mapping and refuses a repo
that does not exist. It deliberately has NO ``delete_repo`` and NO ``create_repo_from_zip``, so a
regression to the destructive path fails with ``AttributeError`` instead of passing against an
over-generous double.

Everything else is injected too: a fake ConnectionService, a fake S3 client (returns the runtime
module zip), a fake ``known_repo_names`` callable, and a REAL ``TemplateRegistry`` in
local-fallback mode (no table name ⇒ no boto3). Nothing here touches live GitHub or AWS.
"""

import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from services.github_repo_service import GitHubRepoError
from services.repo_provider import RepoView
from services.template_registry import (
    TemplateRecord,
    TemplateRegistry,
    TemplateRegistryError,
    TemplateRegistryValidationError,
    template_id_for,
)
from services.template_rollout_service import (
    INFRA_REPO_NAME,
    ReconcileItem,
    RolloutError,
    RolloutService,
)

SEED_NAME = "strands-agentcore"


class FakeRepoProvider:
    """The three T1 reads + the two writes the seed push uses — with real repo state.

    ``repos`` maps repo name → ``{"default_branch", "head_sha", "files"}``. A name absent from
    the mapping does not exist in the org, which is what makes ``read_repo``'s ``None`` and
    ``list_repos``'s omission agree with each other.
    """

    def __init__(self, repos=None):
        self.repos = {name: dict(state) for name, state in (repos or {}).items()}
        self.created = []                 # repos this call sequence brought into existence
        self.commits = []                 # (repo, branch, files, message)
        self.read_repo_calls = []
        self.list_repos_calls = []

    # --- writes ---------------------------------------------------------- #

    def create_repo(self, org, name, *, private, token, base_url=None):
        if name not in self.repos:
            # auto_init: the live provider seeds ONE commit so the git database exists, and it
            # names that branch from the ORG's default-branch setting (not from AGP).
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
            raise GitHubRepoError(
                f"refusing to commit an empty file set to '{org}/{repo}'"
            )
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

    # --- reads (T1) ------------------------------------------------------ #

    def read_repo(self, org, repo, *, token, base_url=None):
        self.read_repo_calls.append(repo)
        state = self.repos.get(repo)
        if state is None:
            return None
        return RepoView(
            default_branch=state["default_branch"], head_sha=state["head_sha"]
        )

    def list_repos(self, org, *, token, base_url=None):
        self.list_repos_calls.append(org)
        return sorted(self.repos)


class FakeConnectionService:
    def __init__(self, org="acme", base_url=None, token="tok"):
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


class FakeS3Client:
    def __init__(self, body=None, error=None):
        self._body = _module_zip() if body is None else body
        self._error = error
        self.calls = []

    def get_object(self, *, Bucket, Key):
        self.calls.append((Bucket, Key))
        if self._error is not None:
            raise self._error
        return {"Body": SimpleNamespace(read=lambda: self._body)}


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
            json.dumps(template_json)
            if not isinstance(template_json, str)
            else template_json
        )
    return templates


class _Fixed:
    def isoformat(self):
        return "2026-08-05T00:00:00+00:00"


def _registry():
    """A real registry in local-fallback mode — no table name, so no boto3, no AWS."""
    return TemplateRegistry(now=lambda: _Fixed())


def _register(
    registry,
    name,
    connection_id="conn1",
    *,
    version="7",
    created_at="2026-07-01T00:00:00+00:00",
    created_by="alice@x.com",
):
    """Put a template in the catalog — the "already registered" half of the matrix."""
    registry.put(
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
        )
    )


def _svc(
    tmp_path,
    *,
    gh=None,
    conn=None,
    s3=None,
    registry=None,
    known_repo_names=lambda connection_id: set(),
    template_json=SEED_TEMPLATE_JSON,
):
    templates = _seed_templates_dir(tmp_path, template_json=template_json)
    return RolloutService(
        gh or FakeRepoProvider(),
        conn or FakeConnectionService(),
        agent_templates_dir=str(templates),
        s3_client=s3 or FakeS3Client(),
        runtime_module_bucket="agp-tf-state",
        runtime_module_key="runtime-module/agentcore_runtime.zip",
        template_registry=registry if registry is not None else _registry(),
        known_repo_names=known_repo_names,
        now=lambda: "2026-08-05T00:00:00+00:00",
    )


def _row(view, name) -> ReconcileItem:
    return next(i for i in view.templates if i.name == name)


# ===================================================================== #
# 1. The three-state matrix — the whole point of the rebuild
# ===================================================================== #


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
    """(registered × present) → exactly one state, and PRESENT comes from ``read_repo``.

    MUTATION GUARD: revert the probe to the DDB-only ``name in existing`` and two of these four
    rows invert — ``(True, False)`` would claim ``registered_present`` (offering nothing for a
    repo that is gone) and ``(False, True)`` would claim ``seed_absent`` (offering a create that
    422s on the existing repo). Deleting the ``read_repo`` call reddens this test by name.
    """
    gh = FakeRepoProvider(
        repos=(
            {SEED_NAME: {"default_branch": "trunk", "head_sha": "abc123", "files": {}}}
            if present
            else {}
        )
    )
    registry = _registry()
    if registered:
        _register(registry, SEED_NAME)
    svc = _svc(tmp_path, gh=gh, registry=registry)

    row = _row(svc.reconcile("conn1"), SEED_NAME)

    assert row.state == expected_state
    # The probe genuinely ran for this row.
    assert SEED_NAME in gh.read_repo_calls
    if present:
        assert row.default_branch == "trunk"
        assert row.head_sha == "abc123"
    else:
        assert row.default_branch is None
        assert row.head_sha is None


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
    """T1 pins ``head_sha=""`` for an existing-but-EMPTY repo: present, not ``None``, no raise.

    It must read as PRESENT (offering "create" would 422 on a repo that is already there) while
    reporting NO head — so a later materialize cannot be handed ``""`` as a ref."""
    gh = FakeRepoProvider(
        repos={SEED_NAME: {"default_branch": "main", "head_sha": "", "files": {}}}
    )
    svc = _svc(tmp_path, gh=gh)

    row = _row(svc.reconcile("conn1"), SEED_NAME)
    assert row.state == "unregistered_present"
    assert row.default_branch == "main"
    assert row.head_sha is None


# ===================================================================== #
# 2. The adopt-row universe (org repos AGP does not account for)
# ===================================================================== #


def test_reconcile_offers_unknown_org_repos_as_adopt_rows(tmp_path):
    gh = FakeRepoProvider(
        repos={
            "someones-own-template": {
                "default_branch": "main", "head_sha": "zzz", "files": {}
            }
        }
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
            INFRA_REPO_NAME: {"default_branch": "main", "head_sha": "c", "files": {}},
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
    # The seed name is present in the org, but it already has its own (seed-origin) row —
    # it must not be listed twice.
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
    gh = FakeRepoProvider(
        repos={INFRA_REPO_NAME: {"default_branch": "main", "head_sha": "i1", "files": {}}}
    )
    svc = _svc(tmp_path, gh=gh)

    view = svc.reconcile("conn1")
    assert view.infra_repo.name == INFRA_REPO_NAME
    assert view.infra_repo.state == "unregistered_present"
    assert view.infra_repo.head_sha == "i1"
    # It is never an adopt row.
    assert INFRA_REPO_NAME not in [i.name for i in view.templates]


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


# ===================================================================== #
# 3. Destructive overwrite is GONE — a re-push is a commit on top
# ===================================================================== #


def test_rollout_creates_and_registers_a_seed_template(tmp_path):
    gh = FakeRepoProvider()
    registry = _registry()
    svc = _svc(tmp_path, gh=gh, registry=registry)

    r = svc.rollout("conn1", template_names=[SEED_NAME], overwrite=False)

    assert SEED_NAME in gh.created
    # ONE commit, carrying the seed bytes, onto the repo's OWN default branch.
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
    # The repo was NOT re-created — it already existed and stayed. (The forced infra repo IS
    # created here; it was absent, and rollout always ensures it.)
    assert SEED_NAME not in gh.created
    # The push went to the repo's OWN trunk, not a guessed "main".
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
        repos={
            SEED_NAME: {
                "default_branch": "main", "head_sha": "pre", "files": dict(their_files)
            }
        }
    )
    svc = _svc(tmp_path, gh=gh)

    assert _row(svc.reconcile("conn1"), SEED_NAME).state == "unregistered_present"

    r = svc.rollout("conn1", template_names=[SEED_NAME], overwrite=overwrite)

    item = next(i for i in r.items if i.name == SEED_NAME)
    assert item.action == "skipped"
    assert "adopt" in (item.reason or "").lower()
    # THE TREE IS UNTOUCHED — the whole point.
    assert gh.repos[SEED_NAME]["files"] == their_files
    assert [c for c in gh.commits if c[0] == SEED_NAME] == []
    assert SEED_NAME not in gh.created
    # And nothing was registered on the back of a refusal.
    assert [r_.name for r_ in svc._registry.list_for_connection("conn1")] == []


@pytest.mark.parametrize("overwrite", [False, True])
def test_a_registered_template_whose_repo_is_gone_is_recreated_from_seed(tmp_path, overwrite):
    """``registered_missing`` — D-C3 offers "Re-create from seed", and it must not need the
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
            INFRA_REPO_NAME: {"default_branch": "main", "head_sha": "b", "files": {}},
        }
    )
    registry = _registry()
    _register(registry, SEED_NAME)
    svc = _svc(tmp_path, gh=gh, registry=registry)

    r = svc.rollout("conn1", template_names=[SEED_NAME], overwrite=False)

    assert next(i for i in r.items if i.name == SEED_NAME).action == "skipped"
    assert next(i for i in r.items if i.name == INFRA_REPO_NAME).action == "skipped"
    assert gh.commits == []
    # A skip touches nothing: an iterated template repo is not quietly reset to seed bytes.
    assert gh.repos[SEED_NAME]["files"] == iterated


def test_re_push_reregisters_instead_of_duplicating(tmp_path):
    """RE-PINNED (E28C/T3 review F4): the registry write is an upsert — the id is derived from the
    name — so re-rolling out a template must not leave the operator with two catalog entries. The
    old test asserted this against the DELETE+recreate path; the contract outlived that path, so it
    is re-expressed here against the re-push."""
    gh = FakeRepoProvider(
        repos={SEED_NAME: {"default_branch": "main", "head_sha": "old", "files": {}}}
    )
    registry = _registry()
    _register(registry, SEED_NAME)
    svc = _svc(tmp_path, gh=gh, registry=registry)

    svc.rollout("conn1", template_names=[SEED_NAME], overwrite=True)
    svc.rollout("conn1", template_names=[SEED_NAME], overwrite=True)

    assert [r.name for r in registry.list_for_connection("conn1")] == [SEED_NAME]


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

    # ONE commit for the whole sequence, and the ref never moved on the second run.
    assert [c[0] for c in gh.commits].count(SEED_NAME) == 1
    assert gh.repos[SEED_NAME]["head_sha"] == first_sha
    # Still REPORTED as a re-push: convergence is not an error, and the row's state is unchanged.
    assert next(i for i in r.items if i.name == SEED_NAME).action == "overwritten"


def test_rollout_then_reconcile_reports_it_registered_and_present(tmp_path):
    """RE-PINNED (E28C/T3 review F4): the round trip — what rollout registers and pushes is what
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


# ===================================================================== #
# 4. The forced infra repo — same idempotent push, no delete
# ===================================================================== #


def test_rollout_always_pushes_infra_from_the_s3_module_zip_when_absent(tmp_path):
    gh = FakeRepoProvider()
    s3 = FakeS3Client()
    svc = _svc(tmp_path, gh=gh, s3=s3)

    r = svc.rollout("conn1", template_names=[], overwrite=False)

    assert next(i for i in r.items if i.name == INFRA_REPO_NAME).action == "created"
    assert INFRA_REPO_NAME in gh.created
    # The zip was UNPACKED and committed as files — the dict→zip→dict round trip is gone.
    pushed = next(c for c in gh.commits if c[0] == INFRA_REPO_NAME)[2]
    assert pushed == RUNTIME_MODULE_FILES
    assert s3.calls == [("agp-tf-state", "runtime-module/agentcore_runtime.zip")]


def test_a_template_re_push_does_not_authorize_an_infra_re_push(tmp_path):
    """E28D: the infra repo has its OWN consent. ``overwrite`` is a TEMPLATE flag from here on.

    Before this split, ticking a re-push on any template handed the same boolean to
    ``_ensure_infra``, so AGP's Terraform module was pushed over the org's existing
    ``agp-runtime-infra`` without anyone asking for it. With ``overwrite_infra`` defaulted, an
    existing infra repo is LEFT ALONE — even on a run that re-pushes a template."""
    gh = FakeRepoProvider(
        repos={
            SEED_NAME: {"default_branch": "main", "head_sha": "old", "files": {"x": b"1"}},
            INFRA_REPO_NAME: {"default_branch": "main", "head_sha": "old", "files": {"a": b"1"}},
        }
    )
    registry = _registry()
    _register(registry, SEED_NAME)
    svc = _svc(tmp_path, gh=gh, registry=registry)

    r = svc.rollout("conn1", template_names=[SEED_NAME], overwrite=True)

    infra = next(i for i in r.items if i.name == INFRA_REPO_NAME)
    assert infra.action == "skipped"
    assert infra.reason == "already exists in org"
    # The template DID get its re-push — the two consents are independent, not both off.
    assert next(i for i in r.items if i.name == SEED_NAME).action == "overwritten"
    assert [c[0] for c in gh.commits] == [SEED_NAME]


def test_infra_overwrite_commits_on_top_without_deleting(tmp_path):
    gh = FakeRepoProvider(
        repos={
            INFRA_REPO_NAME: {"default_branch": "main", "head_sha": "old", "files": {"a": b"1"}}
        }
    )
    svc = _svc(tmp_path, gh=gh)

    r = svc.rollout("conn1", template_names=[], overwrite=True, overwrite_infra=True)

    assert next(i for i in r.items if i.name == INFRA_REPO_NAME).action == "overwritten"
    assert gh.created == []
    assert next(c for c in gh.commits if c[0] == INFRA_REPO_NAME)[2] == RUNTIME_MODULE_FILES


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
        known_repo_names=lambda connection_id: set(),
    )

    with pytest.raises(RolloutError) as exc:
        svc.rollout("conn1", template_names=[], overwrite=False)
    assert exc.value.kind == "rollout_error"
    assert gh.created == [] and gh.commits == []


# ===================================================================== #
# 5. _register — seed template.json metadata + the STRUCTURAL source pair
# ===================================================================== #


def test_register_writes_the_seeds_template_json_metadata(tmp_path):
    """Requirement 4 / design 4e: a rolled-out card must render a description and all three pill
    groups exactly like an uploaded one, from the seed's own ``template.json``."""
    registry = _registry()
    svc = _svc(tmp_path, registry=registry)

    svc.rollout("conn1", template_names=[SEED_NAME], overwrite=False)

    record = registry.get("conn1", template_id_for(SEED_NAME))
    assert record.description == SEED_TEMPLATE_JSON["description"]
    assert record.framework == "strands"
    assert record.aws_services == ["bedrock", "ecr", "agentcore"]
    assert record.tags == ["starter", "python", "strands"]
    assert record.version == "1.0.0"


def test_register_writes_both_halves_of_the_source_pair(tmp_path):
    """T2 carried note N-1: a HALF-SET pair (org without repo) is type-permitted but unpinned,
    and this is the first NEW writer of it. Both or neither — never one."""
    registry = _registry()
    svc = _svc(tmp_path, registry=registry)

    svc.rollout("conn1", template_names=[SEED_NAME], overwrite=False)

    record = registry.get("conn1", template_id_for(SEED_NAME))
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

    record = registry.get("conn1", template_id_for(SEED_NAME))
    assert record.version == "1.0.0"          # the seed's DECLARED version, not "1"
    assert record.created_by == "alice@x.com"
    assert record.created_at == "2026-07-01T00:00:00+00:00"


def test_re_push_without_seed_metadata_keeps_the_recorded_version(tmp_path):
    """No ``template.json`` on disk ⇒ nothing to declare, so the recorded version stands. Still
    not reset to "1"."""
    registry = _registry()
    _register(registry, SEED_NAME, version="7")
    svc = _svc(tmp_path, registry=registry, template_json=None)

    svc.rollout("conn1", template_names=[SEED_NAME], overwrite=True)

    assert registry.get("conn1", template_id_for(SEED_NAME)).version == "7"


def test_unreadable_seed_metadata_fails_loudly(tmp_path):
    """A PRESENT but malformed ``template.json`` is a defect in the seed, not an absence. Falling
    back to defaults would ship a blank card and call it success."""
    registry = _registry()
    svc = _svc(tmp_path, registry=registry, template_json="{not json")

    with pytest.raises(RolloutError) as exc:
        svc.rollout("conn1", template_names=[SEED_NAME], overwrite=False)
    assert exc.value.kind == "rollout_error"


# ===================================================================== #
# 6. adopt — register-as-is, no content inspection, no push
# ===================================================================== #


def test_adopt_registers_an_existing_org_repo_as_is(tmp_path):
    gh = FakeRepoProvider(
        repos={"their-template": {"default_branch": "trunk", "head_sha": "ddd", "files": {}}}
    )
    registry = _registry()
    svc = _svc(tmp_path, gh=gh, registry=registry)

    view = svc.adopt(
        "conn1", repo_name="their-template", description="ours now", created_by="bob@x.com"
    )

    assert view.name == "their-template"
    assert view.description == "ours now"
    record = registry.get("conn1", template_id_for("their-template"))
    assert record.source_org == "acme"
    assert record.source_repo == "their-template"
    assert record.created_by == "bob@x.com"
    # Register-as-is: NO push, NO repo creation, NO content read.
    assert gh.commits == [] and gh.created == []


def test_adopt_of_an_absent_repo_is_repo_not_found(tmp_path):
    svc = _svc(tmp_path, gh=FakeRepoProvider())
    with pytest.raises(RolloutError) as exc:
        svc.adopt("conn1", repo_name="ghost", description=None)
    assert exc.value.kind == "repo_not_found"


def test_adopt_of_an_already_registered_name_is_a_conflict(tmp_path):
    gh = FakeRepoProvider(
        repos={SEED_NAME: {"default_branch": "main", "head_sha": "a", "files": {}}}
    )
    registry = _registry()
    _register(registry, SEED_NAME)
    svc = _svc(tmp_path, gh=gh, registry=registry)

    with pytest.raises(RolloutError) as exc:
        svc.adopt("conn1", repo_name=SEED_NAME, description=None)
    assert exc.value.kind == "conflict"


def test_adopt_refuses_the_forced_infra_repo(tmp_path):
    """The infra repo is NOT a template and never was (D-B4). Adopting it produced a registry row
    for a name that also occupies ``infra_repo``, so reconcile showed it TWICE — and a materialize
    could then ship Terraform as an agent.

    The subtraction has to live in the VERB, not only in the picker: a picker that omits a row is a
    UI courtesy, and this endpoint is reachable with a hand-made POST."""
    gh = FakeRepoProvider(
        repos={INFRA_REPO_NAME: {"default_branch": "main", "head_sha": "i", "files": {}}}
    )
    registry = _registry()
    svc = _svc(tmp_path, gh=gh, registry=registry)

    with pytest.raises(RolloutError) as exc:
        svc.adopt("conn1", repo_name=INFRA_REPO_NAME, description=None)

    assert exc.value.kind == "conflict"
    assert "infra" in exc.value.message.lower()
    assert registry.list_for_connection("conn1") == []


def test_adopt_refuses_a_materialized_agent_repo(tmp_path):
    """``_materialized_repo_names``' docstring says the subtraction exists so an operator cannot
    "register a live agent's repository as a template — after which every materialize from it
    would ship that agent's code". That was true of the picker and false of the verb."""
    gh = FakeRepoProvider(
        repos={"my-live-agent": {"default_branch": "main", "head_sha": "a", "files": {}}}
    )
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
    gh = FakeRepoProvider(
        repos={"their-template": {"default_branch": "main", "head_sha": "d", "files": {}}}
    )
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


def test_adopt_writes_both_halves_of_the_source_pair(tmp_path):
    """N-1 again, on the other new writer."""
    gh = FakeRepoProvider(
        repos={"their-template": {"default_branch": "main", "head_sha": "d", "files": {}}}
    )
    registry = _registry()
    svc = _svc(tmp_path, gh=gh, registry=registry)

    svc.adopt("conn1", repo_name="their-template", description=None)

    record = registry.get("conn1", template_id_for("their-template"))
    assert record.source_org and record.source_repo
    assert (record.source_org is None) == (record.source_repo is None)


def test_adopt_source_url_defaults_to_github_com_without_a_base_url(tmp_path):
    """No base URL on the connection ⇒ github.com. The BASELINE for the GHE cases below."""
    gh = FakeRepoProvider(
        repos={"their-template": {"default_branch": "main", "head_sha": "d", "files": {}}}
    )
    registry = _registry()
    svc = _svc(tmp_path, gh=gh, registry=registry)

    svc.adopt("conn1", repo_name="their-template", description=None)

    record = registry.get("conn1", template_id_for("their-template"))
    assert record.source_url == "https://github.com/acme/their-template"


@pytest.mark.parametrize(
    "base_url,expected_host",
    [
        # The GHE default: the API lives under /api/v3, the browsable repo does not.
        ("https://ghe.example.com/api/v3", "https://ghe.example.com"),
        # A trailing slash must not survive into the display URL.
        ("https://ghe.example.com/api/v3/", "https://ghe.example.com"),
        # The bare /api suffix — the loop's second prefix.
        ("https://ghe.example.com/api", "https://ghe.example.com"),
        # Neither suffix: the loop never fires and the host is used as-is.
        ("https://ghe.example.com", "https://ghe.example.com"),
        # Only the SUFFIX is stripped — an /api segment mid-path is part of the host path.
        ("https://ghe.example.com/api/v3/extra", "https://ghe.example.com/api/v3/extra"),
    ],
)
def test_adopt_source_url_is_derived_from_a_ghe_base_url(tmp_path, base_url, expected_host):
    """E28C/T3 F6: an on-prem repo must NOT be linked to github.com. ``source_url`` is display
    only, so the whole point of it is that a human clicking it lands on the right host."""
    gh = FakeRepoProvider(
        repos={"their-template": {"default_branch": "main", "head_sha": "d", "files": {}}}
    )
    registry = _registry()
    svc = _svc(
        tmp_path, gh=gh, registry=registry, conn=FakeConnectionService(base_url=base_url)
    )

    svc.adopt("conn1", repo_name="their-template", description=None)

    record = registry.get("conn1", template_id_for("their-template"))
    assert record.source_url == f"{expected_host}/acme/their-template"


def test_adopt_then_reconcile_reports_it_registered_and_present(tmp_path):
    """The round trip: what adopt registers is what reconcile then sees — and it stops being an
    adopt row."""
    gh = FakeRepoProvider(
        repos={"their-template": {"default_branch": "main", "head_sha": "d", "files": {}}}
    )
    svc = _svc(tmp_path, gh=gh)

    svc.adopt("conn1", repo_name="their-template", description=None)

    row = _row(svc.reconcile("conn1"), "their-template")
    assert row.state == "registered_present"
    assert row.origin == "registry"


# ===================================================================== #
# 7. The catalog read stays STRICT; validation stays distinct from a store fault
# ===================================================================== #


class _BrokenRegistry:
    def list_for_connection(self, connection_id):
        raise TemplateRegistryError("Could not read the template catalog")

    def get(self, connection_id, template_id):
        raise TemplateRegistryError("Could not read the template catalog")

    def put(self, record):
        raise TemplateRegistryError("Could not write the template catalog")


class _ValidationRejectingRegistry:
    def list_for_connection(self, connection_id):
        raise TemplateRegistryValidationError("connection_id must not contain '#'")

    def get(self, connection_id, template_id):
        raise TemplateRegistryValidationError("connection_id must not contain '#'")

    def put(self, record):
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
    gh = FakeRepoProvider(
        repos={"their-template": {"default_branch": "main", "head_sha": "d", "files": {}}}
    )
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
