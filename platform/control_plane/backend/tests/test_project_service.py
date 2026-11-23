"""Service tests for ProjectService: the project and repository reads, the delivery limits, the
deployment history and the persistence rules.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

from conftest import TEMPLATE_NAME, VALID_AGENT_CONFIG, _make_project, _principal
from seed import seed

from models.deployment import DeploymentOutcome
from models.repository import StepStatus
from services import project_materialize
from services.project_delivery import _PROMOTE_IN_FLIGHT_MINUTES
from services.project_errors import ProjectError
from services.project_service import ProjectService
from services.project_store import _REPOSITORY_PK, _ROLLBACK_HISTORY_LIMIT


def test_create_project_persists_empty_container(project_service_fakes):
    svc = project_service_fakes
    p = _make_project(svc)

    assert p.id == "proj-1"
    assert p.name == "p1"
    assert p.connection_id == "c1"
    assert p.tenant_id == "default"
    assert p.description == "the first project"
    assert p.created_by == "op@x"
    svc._registry.create.assert_not_called()
    svc._identity.provision_identity.assert_not_called()
    svc._rollout.create_repo.assert_not_called()
    assert svc.list_repositories() == []
    detail = svc.get_project("proj-1")
    assert detail is not None
    assert detail.project.id == "proj-1"
    assert detail.repositories == []


def test_get_project_returns_detail_with_repos(project_service_fakes):
    svc = project_service_fakes
    _make_project(svc)
    r1 = svc.add_repo(
        project_id="proj-1",
        name="p1",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )

    detail = svc.get_project("proj-1")
    assert detail is not None
    assert detail.project.id == "proj-1"
    assert [r.id for r in detail.repositories] == [r1.id]


def test_get_project_unknown_returns_none(project_service_fakes):
    assert project_service_fakes.get_project("ghost") is None


def test_list_repositories_flat(project_service_fakes):
    svc = project_service_fakes
    _make_project(svc)
    r1 = svc.add_repo(
        project_id="proj-1",
        name="p1",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )
    r2 = svc.add_repo(
        project_id="proj-1",
        name="p2",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )
    ids = {r.id for r in svc.list_repositories()}
    assert ids == {r1.id, r2.id}


def test_create_project_persists_tenant_id(project_service_fakes):
    svc = project_service_fakes
    p = _make_project(svc, tenant_id="ten-9")
    assert p.tenant_id == "ten-9"
    detail = svc.get_project("proj-1")
    assert detail.project.tenant_id == "ten-9"
    assert svc.list_projects()[0].tenant_id == "ten-9"


_LEGACY_PROJECT_ROW = {
    "pk": "project",
    "sk": "legacy-1",
    "id": "legacy-1",
    "name": "legacy",
    "connection_id": "c1",
    "description": "",
    "created_by": "op@x",
    "created_at": "2026-01-01T00:00:00+00:00",
    "updated_at": "2026-01-01T00:00:00+00:00",
}


def test_legacy_stored_project_hydrates_tenant_id_default(project_service_fakes):
    """An older stored record WITHOUT tenant_id hydrates as tenant_id="default" in the service read path
    — get AND list."""
    svc = project_service_fakes
    seed(svc._store, dict(_LEGACY_PROJECT_ROW))

    detail = svc.get_project("legacy-1")
    assert detail is not None
    assert detail.project.tenant_id == "default"

    projects = svc.list_projects()
    assert [p.id for p in projects] == ["legacy-1"]
    assert projects[0].tenant_id == "default"


def test_stored_project_with_tenant_id_keeps_it(project_service_fakes):
    """Hydration only fills the MISSING key — a stored tenant_id is never overwritten."""
    svc = project_service_fakes
    seed(svc._store, {**_LEGACY_PROJECT_ROW, "tenant_id": "ten-2"})

    detail = svc.get_project("legacy-1")
    assert detail.project.tenant_id == "ten-2"
    assert svc.list_projects()[0].tenant_id == "ten-2"


def _stored(project_service_fakes, items):
    """The fixture's service over hand-crafted (incl. malformed) raw rows."""
    svc = project_service_fakes
    for item in items:
        seed(svc._store, dict(item))
    return svc


_VALID_REPO_ROW = {
    "pk": "repository",
    "sk": "repo-ok",
    "id": "repo-ok",
    "project_id": "proj-1",
    "name": "good",
    "repo_url": "https://github.com/acme/good",
    "agent_id": "agent-1",
    "template_name": "strands-agentcore",
    "cicd_status": "provisioning",
    "status": "provisioning",
    "created_by": "op@x",
    "created_at": "2026-07-02T00:00:00+00:00",
    "updated_at": "2026-07-02T00:00:00+00:00",
}


_BAD_REPO_ROW = {
    **{k: v for k, v in _VALID_REPO_ROW.items() if k != "template_name"},
    "sk": "repo-bad",
    "id": "repo-bad",
    "name": "bad",
}


def test_list_repositories_skips_malformed_row(project_service_fakes):
    svc = _stored(project_service_fakes, [_VALID_REPO_ROW, _BAD_REPO_ROW])

    repos = svc.list_repositories()

    assert [r.id for r in repos] == ["repo-ok"]


def test_get_project_repositories_skips_malformed_row(project_service_fakes):
    project_row = {
        "pk": "project",
        "sk": "proj-1",
        "id": "proj-1",
        "name": "p1",
        "connection_id": "c1",
        "description": "the first project",
        "created_by": "op@x",
        "created_at": "2026-07-02T00:00:00+00:00",
        "updated_at": "2026-07-02T00:00:00+00:00",
    }
    svc = _stored(project_service_fakes, [project_row, _VALID_REPO_ROW, _BAD_REPO_ROW])

    detail = svc.get_project("proj-1")

    assert detail is not None
    assert [r.id for r in detail.repositories] == ["repo-ok"]


def _codebuild_wrote(svc, repo_id, tag="agent7-abc123", status="deployed"):
    """The buildspec's out-of-band targeted UpdateItem on the live row."""
    svc._store.set_owned((_REPOSITORY_PK, repo_id), {"last_dev_image_tag": tag, "cicd_status": status})


def _svc_with_live_repo(project_service_fakes):
    svc = project_service_fakes
    seed(svc._store, dict(_VALID_REPO_ROW))
    return svc


def test_save_repo_after_codebuild_write_keeps_the_tag_and_status(project_service_fakes):
    """The exact interleaving: backend READS the repo → CodeBuild writes
    last_dev_image_tag + cicd_status → backend SAVES the stale record. Both
    CodeBuild-owned attributes must survive."""
    svc = _svc_with_live_repo(project_service_fakes)

    stale = svc._get_repo("repo-ok")
    assert stale.last_dev_image_tag is None
    _codebuild_wrote(svc, "repo-ok")

    stale.repo_url = "https://github.com/acme/renamed"
    svc._save_repo(stale)

    after = svc._get_repo("repo-ok")
    assert after.last_dev_image_tag == "agent7-abc123"
    assert after.cicd_status == "deployed"
    assert after.repo_url == "https://github.com/acme/renamed"


def test_save_repo_step_after_codebuild_write_keeps_the_tag(project_service_fakes):
    """`_save_repo_step` (the long-lived materialize orchestration — one of the two
    plausible clobber windows) must not destroy a concurrently-written tag."""
    svc = _svc_with_live_repo(project_service_fakes)
    svc._get_repo("repo-ok")
    _codebuild_wrote(svc, "repo-ok")

    project_materialize._save_repo_step(svc, "repo-ok", "finalize", StepStatus.DONE)

    after = svc._get_repo("repo-ok")
    assert after.last_dev_image_tag == "agent7-abc123"
    assert after.cicd_status == "deployed"
    assert [s.status for s in after.steps if s.key == "finalize"] == [StepStatus.DONE]


def test_retry_materialize_keeps_the_tag_but_does_reset_cicd_status(project_service_fakes):
    """`retry_materialize` is the other window. It legitimately OWNS the cicd_status
    reset (opt-in), but must still never touch the CodeBuild-exclusive tag."""
    svc = _svc_with_live_repo(project_service_fakes)
    seed(
        svc._store,
        {
            "pk": "project",
            "sk": "proj-1",
            "id": "proj-1",
            "name": "p1",
            "connection_id": "c1",
            "tenant_id": "default",
            "description": "d",
            "created_by": "op@x",
            "created_at": "2026-07-02T00:00:00+00:00",
            "updated_at": "2026-07-02T00:00:00+00:00",
        },
    )
    _codebuild_wrote(svc, "repo-ok", status="deployed")

    svc.retry_materialize("repo-ok")

    after = svc._get_repo("repo-ok")
    assert after.last_dev_image_tag == "agent7-abc123"
    assert after.cicd_status == "provisioning"


def test_finalize_does_not_rewrite_the_codebuild_owned_dev_tag(project_service_fakes):
    """The skip-set regression the new write could have introduced. `status` is in NO skip set
    (so no signature change was needed), but `_finalize_repo` saves the whole record —
    so a tag CodeBuild wrote out-of-band during the materialize must still survive the save
    that now also advances `status`."""
    svc = _svc_with_live_repo(project_service_fakes)
    svc._get_repo("repo-ok")
    _codebuild_wrote(svc, "repo-ok")

    project_materialize._finalize_repo(svc, "repo-ok", "https://github.com/acme/good")

    after = svc._get_repo("repo-ok")
    assert after.status == "ready"
    assert after.last_dev_image_tag == "agent7-abc123"


def test_the_in_flight_window_is_the_codebuild_projects_build_timeout():
    main_tf = Path(__file__).resolve().parents[2] / "infrastructure" / "modules" / "codebuild" / "main.tf"

    timeouts = re.findall(r"^\s*build_timeout\s*=\s*(\d+)\s*$", main_tf.read_text(), re.M)

    assert [int(t) for t in timeouts] == [_PROMOTE_IN_FLIGHT_MINUTES]


def test_a_succeeded_tag_beyond_the_rollback_history_limit_is_refused():
    clock = iter(datetime(2026, 7, 2, tzinfo=timezone.utc) + timedelta(seconds=i) for i in range(10_000))
    svc = ProjectService(
        registry=MagicMock(), identity=MagicMock(), connection_service=MagicMock(), now=lambda: next(clock)
    )
    for tag in ["old", *(f"t{i}" for i in range(_ROLLBACK_HISTORY_LIMIT))]:
        svc.append_deployment(
            repo_id="r-1", agent_id="a-1", stage="prod", image_tag=tag, outcome=DeploymentOutcome.SUCCEEDED
        )

    assert svc._has_succeeded("r-1", "prod", "t0") is True
    assert svc._has_succeeded("r-1", "prod", "old") is False


def test_project_error_is_built_by_its_own_constructor_with_a_positional_kind():
    """The class keeps its own ``__init__(message, kind)``; a helper misplaced inside the class body
    once swallowed it, and only ``AppError``'s positional kind kept the behaviour alive."""
    from core.errors import AppError

    assert ProjectError.__init__ is not AppError.__init__
    err = ProjectError("m", "not_found")
    assert (err.kind, err.message, err.status_code, err.detail) == (
        "not_found",
        "m",
        404,
        "Project not found",
    )
