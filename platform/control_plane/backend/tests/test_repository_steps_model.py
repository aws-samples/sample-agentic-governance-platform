"""The materialize timeline contract: the step keys, their order, their labels, and the legacy shape."""

from models.repository import MATERIALIZE_STEPS, Repository, StepState, StepStatus, default_steps


def test_default_steps_are_all_pending_in_materialize_order():
    steps = default_steps()
    assert [s.key for s in steps] == [s["key"] for s in MATERIALIZE_STEPS]
    assert len(steps) == 6
    assert all(s.status == StepStatus.PENDING for s in steps)


def test_the_step_keys_and_labels_are_the_timeline_contract_in_order():
    """Pinned BY NAME and in order: the count alone would admit a renamed or reordered step, and the
    keys are what the backend dispatches materialize work on. ``provision_langfuse`` sits BEFORE
    ``finalize`` because ``finalize`` is the terminal step that flips the record to ``ready``; a
    best-effort step after it would leave a ready repo with a step still running.

    The labels are the contract too, not just their non-emptiness: the frontend renders ``label``
    verbatim and keeps no key-to-label map, so this list is the only place the operator's timeline
    wording exists. A label edit is a UI change that reaches production through a data structure, so
    it has to break a test deliberately."""
    assert [s["key"] for s in MATERIALIZE_STEPS] == [
        "mint_identity",
        "create_repo",
        "push_template",
        "set_repo_vars",
        "provision_langfuse",
        "finalize",
    ]
    assert all(s["label"].strip() for s in MATERIALIZE_STEPS)
    assert [s["label"] for s in MATERIALIZE_STEPS] == [
        "Mint Entra identity",
        "Create repository",
        "Push template contents",
        "Set repository CI variables",
        "Provision Langfuse tracing",
        "Finalize repository record",
    ]


def test_a_historical_eight_step_record_still_validates_and_keeps_its_own_labels():
    """The migration contract. Historical and in-flight records carry the OLD keys, and ``steps`` is
    stored data: this model must read them back unchanged rather than rejecting a key it no longer
    names or re-deriving a label. The frontend renders the stored label, so what is validated here is
    exactly what an operator sees on an old repo."""
    stored = [
        {"key": "mint_identity", "label": "Mint Entra identity", "status": "done"},
        {"key": "generate_repo", "label": "Generate repo from template", "status": "done"},
        {"key": "commit_config", "label": "Commit agent.config.json", "status": "done"},
        {"key": "set_repo_vars", "label": "Set repository CI variables", "status": "done"},
        {"key": "create_env_dev", "label": "Create unprotected stage environments", "status": "failed"},
        {"key": "create_env_prod", "label": "Create protected prod environment", "status": "pending"},
        {"key": "set_env_vars", "label": "Set per-stage environment variables", "status": "pending"},
        {"key": "finalize", "label": "Finalize repository record", "status": "pending"},
    ]
    repo = Repository.model_validate(
        {
            "id": "r1",
            "project_id": "p1",
            "name": "n",
            "agent_id": "a1",
            "template_name": "strands-agentcore",
            "status": "failed",
            "created_by": "u",
            "created_at": "t",
            "updated_at": "t",
            "steps": stored,
        }
    )
    assert len(repo.steps) == 8
    assert [s.key for s in repo.steps] == [s["key"] for s in stored]
    assert [s.label for s in repo.steps] == [s["label"] for s in stored]


def test_repository_defaults_steps_when_absent():
    """An older item without a steps field validates and gets a full pending timeline."""
    repo = Repository(
        id="r1",
        project_id="p1",
        name="n",
        agent_id="a1",
        template_name="strands-agentcore",
        status="provisioning",
        created_by="u",
        created_at="t",
        updated_at="t",
    )
    assert len(repo.steps) == 6
    assert repo.steps[0].key == "mint_identity"


def test_step_state_roundtrips_json():
    s = StepState(key="create_repo", label="X", status=StepStatus.FAILED, error="403")
    assert StepState.model_validate_json(s.model_dump_json()).status == StepStatus.FAILED
