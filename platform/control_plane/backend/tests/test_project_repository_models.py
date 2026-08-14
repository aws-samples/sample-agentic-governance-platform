"""Model tests for the reshaped Project + new Repository surface (E20/T8).

Pure Pydantic — no I/O, no AWS. Pins the 1:N project→repos design: ``Project`` is an
empty container (no ``template_id``/``repo_url``/``agent_id``/``status``), those fields
now live on ``Repository``. ``ProjectCreate`` no longer accepts ``template_id`` and
``ProjectDetail`` nests a project with its repositories.
"""

from __future__ import annotations

from models.project import Project, ProjectCreate, ProjectDetail
from models.repository import Repository, RepositoryCreate


def test_project_roundtrips():
    project = Project(
        id="p1",
        name="Payments",
        connection_id="c1",
        tenant_id="default",
        description="the payments org container",
        created_by="op@example.com",
        created_at="2026-07-08T00:00:00Z",
        updated_at="2026-07-08T00:00:00Z",
    )
    assert Project.model_validate_json(project.model_dump_json()) == project
    # The removed fields are gone from the schema.
    for gone in ("template_id", "repo_url", "agent_id", "status"):
        assert gone not in Project.model_fields
    # description defaults to "".
    assert Project.model_fields["description"].default == ""
    # E24/T6 — tenant_id is REQUIRED on Project and ProjectCreate (no default).
    assert Project.model_fields["tenant_id"].is_required()
    assert ProjectCreate.model_fields["tenant_id"].is_required()


def test_repository_roundtrips():
    repo = Repository(
        id="r1",
        project_id="p1",
        name="checkout-agent",
        repo_url="https://github.com/acme/checkout-agent",
        agent_id="a1",
        template_name="strands-agentcore",
        status="provisioning",
        created_by="op@example.com",
        created_at="2026-07-08T00:00:00Z",
        updated_at="2026-07-08T00:00:00Z",
    )
    assert Repository.model_validate_json(repo.model_dump_json()) == repo
    # cicd_status defaults to "provisioning"; repo_url is optional.
    assert repo.cicd_status == "provisioning"
    bare = Repository(
        id="r2",
        project_id="p1",
        name="a",
        agent_id="a2",
        template_name="strands-agentcore",
        status="failed",
        created_by="op@example.com",
        created_at="2026-07-08T00:00:00Z",
        updated_at="2026-07-08T00:00:00Z",
    )
    assert bare.repo_url is None


def test_repository_create_carries_agent_config():
    body = RepositoryCreate(
        name="checkout-agent",
        template_name="strands-agentcore",
        agent_config={"framework": "strands", "agent_name": "checkout"},
    )
    assert body.agent_config["framework"] == "strands"
    assert body.agent_config["agent_name"] == "checkout"


def test_project_create_no_longer_accepts_template_id():
    # template_id is not a field anymore; extra is ignored (Pydantic v2 default).
    body = ProjectCreate(name="Payments", connection_id="c1", tenant_id="default", template_id="x")
    assert "template_id" not in ProjectCreate.model_fields
    assert not hasattr(body, "template_id")
    assert body.description == ""


def test_project_detail_nests_project_and_repositories():
    project = Project(
        id="p1",
        name="Payments",
        connection_id="c1",
        tenant_id="default",
        created_by="op@example.com",
        created_at="2026-07-08T00:00:00Z",
        updated_at="2026-07-08T00:00:00Z",
    )
    repo = Repository(
        id="r1",
        project_id="p1",
        name="checkout-agent",
        agent_id="a1",
        template_name="strands-agentcore",
        status="provisioning",
        created_by="op@example.com",
        created_at="2026-07-08T00:00:00Z",
        updated_at="2026-07-08T00:00:00Z",
    )
    detail = ProjectDetail(project=project, repositories=[repo])
    assert ProjectDetail.model_validate_json(detail.model_dump_json()) == detail
    assert detail.project.id == "p1"
    assert detail.repositories[0].project_id == "p1"
    # E27/T11 — the two UI HINTS are ADDITIVE: every other producer of a ProjectDetail
    # (the service read path, every route that isn't GET /projects/{id}) keeps constructing
    # it with two args and reports "no hint" rather than a wrong one.
    assert detail.effective_role is None
    assert detail.ungoverned is False


def _minimal_repo_kwargs() -> dict:
    """The required-only Repository kwargs — mirrors the ``bare`` repo above."""
    return {
        "id": "r1",
        "project_id": "p1",
        "name": "checkout-agent",
        "agent_id": "a1",
        "template_name": "strands-agentcore",
        "status": "provisioning",
        "created_by": "op@example.com",
        "created_at": "2026-07-28T00:00:00Z",
        "updated_at": "2026-07-28T00:00:00Z",
    }


def test_prod_candidate_fields_default_to_none():
    repo = Repository(**_minimal_repo_kwargs())
    assert repo.prod_candidate_image_tag is None
    assert repo.prod_candidate_status is None


def test_prod_candidate_fields_round_trip():
    repo = Repository(**_minimal_repo_kwargs(),
                      prod_candidate_image_tag="a-1-3f9a1c2",
                      prod_candidate_sha="3f9a1c2" * 5 + "abcde",
                      prod_candidate_actor="jorge",
                      prod_candidate_at="2026-07-28T10:00:00Z",
                      prod_candidate_status="pending")
    assert Repository(**repo.model_dump()).prod_candidate_actor == "jorge"


def test_legacy_record_without_prod_candidate_fields_validates():
    # a pre-E27A DDB item must still load (additive-field contract)
    assert Repository(**_minimal_repo_kwargs()).prod_candidate_at is None


# --------------------------------------------------------------------------- #
# E28A/T1b — AGENT_NAME_RE is tightened to 32 chars (C-A3)
# --------------------------------------------------------------------------- #
#
# `agent_name` is not just a label: it is the string the buildspec feeds the runtime module, which
# derives BOTH stage-scoped account-global resource names from it (C-A1):
#
#   runtime_name   = "{agent_name}_{stage}"                 AWS cap: 48
#   exec_role_name = "{agent_name}-{stage}-agentcore-exec"  IAM cap: 64
#
# At the old 48 both ceilings overflow — the role name is ALREADY 48 + len("-agentcore-exec") = 63,
# so any stage suffix at all breaks it. Truncating instead is not an option: two 48-char names
# sharing a prefix would collide silently, which is the same account-global class finding #9 is.
#
# NOTE (deliberate, and the reason this file is the home): before T1b nothing in the suite tested
# `AGENT_NAME_RE` or `validate_agent_config` at all — a grep of tests/ found zero references — so
# the length was free to drift in either direction. These are the first guards on it.


def test_agent_name_re_caps_at_32_so_BOTH_derived_names_fit():
    """The cap is asserted THROUGH the arithmetic it exists to satisfy, not as a bare `32`.

    A restated literal would still pass if someone widened the regex and "updated the test"; this
    version fails unless the longest name the regex ACCEPTS actually fits both AWS ceilings for a
    15-char stage. 15 is the budgeted headroom: `dev`/`prod` are 3/4 today, so this leaves room for
    a real free-form stage (D8) without reopening the overflow."""
    from models.project import AGENT_NAME_RE

    longest = "a" + "b" * 31
    assert len(longest) == 32
    assert AGENT_NAME_RE.match(longest), "a 32-char name must be accepted"
    assert not AGENT_NAME_RE.match(longest + "c"), "33 chars must be refused"

    stage = "s" * 15
    assert len(f"{longest}_{stage}") <= 48, "agent_runtime_name would overflow its 48-char cap"
    assert len(f"{longest}-{stage}-agentcore-exec") <= 64, "the IAM role name would overflow 64"


def test_the_32_cap_fits_the_role_name_the_BACKEND_ACTUALLY_DERIVES():
    """The sibling above pins the cap against a RESTATED arithmetic; this pins it against the real
    producer, which is what the account will actually be asked to create.

    Both are needed and neither subsumes the other. The restated version documents the budget
    (`{name}-{stage}-agentcore-exec`, 15-char stage) and would keep passing if the backend's
    derivation grew a segment; this one imports `agentcore_exec_role_name` — the single Python
    producer, pinned byte-for-byte to the terraform module by
    `test_project_service.py::test_exec_role_name_matches_the_terraform_module_that_creates_it` —
    so the ceiling is measured on the string that reaches IAM. A drift between the two shows up
    as one of these two tests failing rather than as a `CreateRole` failure mid-deploy.

    The LEGACY un-scoped name is checked too: the delete cascade still attempts it (five pre-T1b
    orphans exist live), so it must also be a name IAM would accept as an argument.

    THE BINDING CEILING IS 15, AND IT IS THE RUNTIME NAME THAT SETS IT — not the role name this
    test measures. Corrected in FIX 2 (both reviewers caught it): an earlier version of this
    docstring claimed "the real ceiling is 16, so 15 leaves ONE character of slack", reasoning only
    from `32 + 1 + S + 1 + len("agentcore-exec") <= 64` → `S <= 16`. That is true of the IAM role
    name IN ISOLATION and false of the system. At a 32-char agent name a 16-char stage makes
    `agent_runtime_name` `32 + 1 + 16 = 49` against AWS's 48, and the module's `precondition`
    refuses it at `plan`. Nothing was ever loosened — the module has always been safe — but the
    claim was asserted as fact in the layer whose job is catching exactly that, which is this
    epic's signature defect. Both ceilings are now pinned HERE, so the role name's extra character
    can never again be read as usable headroom."""
    from models.project import AGENT_NAME_RE
    from services.project_service import (
        agentcore_exec_role_name,
        legacy_agentcore_exec_role_name,
    )

    longest = "a" + "b" * 31
    assert AGENT_NAME_RE.match(longest), "guard's premise: 32 chars is the widest accepted name"

    budgeted = agentcore_exec_role_name(longest, "s" * 15)  # the documented headroom
    assert len(budgeted) <= 64, f"{budgeted!r} is {len(budgeted)} chars; IAM allows 64"

    # The BINDING ceiling, both sides of it: the runtime name (48) is what refuses a 16-char stage.
    assert len(f"{longest}_{'s' * 15}") <= 48, (
        "15 is the widest stage: the RUNTIME name binds first, at 48"
    )
    assert len(f"{longest}_{'s' * 16}") > 48, (
        "16 overflows agent_runtime_name (48) even though the role name still fits — the runtime "
        "precondition refuses it at plan"
    )

    # The role name's OWN ceiling, both sides of it. Non-vacuity: without the second assertion the
    # first would keep passing while the derivation grew segments, since 64 would just be slack.
    # 16 fitting here is role-only slack, NOT usable headroom — see the two assertions above.
    assert len(agentcore_exec_role_name(longest, "s" * 16)) <= 64, (
        "16 is the widest stage the ROLE NAME alone tolerates; the binding ceiling is still 15"
    )
    assert len(agentcore_exec_role_name(longest, "s" * 17)) > 64, "17 must overflow"

    assert len(legacy_agentcore_exec_role_name(longest)) <= 64


def test_agent_name_re_still_refuses_the_shapes_AWS_refuses():
    """Non-vacuity: the tightening must not be the ONLY thing pinned, or a regex loosened to
    `.{0,32}` would pass the length test above. `agent_runtime_name` is
    `[a-zA-Z][a-zA-Z0-9_]{0,47}` — a LETTER first, and underscores only. A hyphen is the trap
    worth naming: it is legal in the IAM role name and illegal in the runtime name, so a hyphenated
    agent_name fails at `CreateAgentRuntime` after the role was already created."""
    from models.project import AGENT_NAME_RE

    for ok in ("a", "A1", "my_agent", "platform_agent"):
        assert AGENT_NAME_RE.match(ok), ok
    for bad in ("", "1agent", "_agent", "my-agent", "my agent", "my.agent", "my/agent"):
        assert not AGENT_NAME_RE.match(bad), bad


def test_validate_agent_config_rejects_an_over_long_agent_name():
    """The regex is only load-bearing if the validator actually applies it — this is the API-facing
    path (`project_service.add_repo` step 0), so a 33-char name must be refused at repo-create
    rather than surfacing as a terraform `precondition` failure mid-build (or, worse, an
    `EntityAlreadyExists` at IAM)."""
    import pytest

    from models.project import validate_agent_config

    good = {"framework": "strands", "agent_name": "a" * 32}
    validate_agent_config(good)  # must not raise

    with pytest.raises(ValueError, match="invalid agent_config agent_name"):
        validate_agent_config({"framework": "strands", "agent_name": "a" * 33})


def test_the_frontend_mirrors_the_backend_regex_EXACTLY():
    """The two regexes are edited independently and neither imports the other. A frontend still
    accepting 48 lets an operator type a name the modal calls valid and the backend then rejects
    with a 502 — and if the backend were ever the looser one, the name would reach terraform and
    fail at `plan`. Read out of the .tsx source, so this cannot pass by restating the pattern.

    Not a vitest test because `AGENT_NAME_RE` is a module-private const in a `.tsx` file and
    vitest only collects `src/**/*.test.ts` (vitest.config.ts) — so this cross-file agreement is
    only assertable from here."""
    import re as _re
    from pathlib import Path

    from models.project import AGENT_NAME_RE

    modal = (
        Path(__file__).resolve().parents[2]
        / "frontend/src/components/operations/AddRepoModal.tsx"
    )
    assert modal.is_file(), modal
    (mirrored,) = _re.findall(r"const AGENT_NAME_RE = /\^(.+)\$/;", modal.read_text())
    assert f"^{mirrored}$" == AGENT_NAME_RE.pattern
