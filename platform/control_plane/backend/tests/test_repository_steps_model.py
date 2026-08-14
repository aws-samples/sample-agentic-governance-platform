from models.repository import Repository, StepState, StepStatus, default_steps, MATERIALIZE_STEPS


def test_default_steps_are_five_pending():
    """E28B/T3 (D-B2): EIGHT became FIVE. Five of the old steps did not get renamed — they stopped
    existing, because materialize stopped making those writes (a second branch, an
    ``agent.config.json`` commit, two GitHub Environments and their per-stage vars).

    E28C/T5 (D-C5): five became SIX. ``provision_langfuse`` is the one ADDITION in the other
    direction — a step materialize never made at all, which is why repo-created agents traced
    into nothing for two epics. The name is kept as-is deliberately: renaming it to
    ``..._six_pending`` on every count change is churn that hides which revision moved it."""
    steps = default_steps()
    assert [s.key for s in steps] == [s["key"] for s in MATERIALIZE_STEPS]
    assert len(steps) == 6
    assert all(s.status == StepStatus.PENDING for s in steps)


def test_the_step_keys_are_exactly_the_five_D_B2_names():
    """Pinned BY NAME and in order — the count alone would admit a renamed or reordered step, and
    the keys are what the backend dispatches materialize work on.

    E28C/T5: ``provision_langfuse`` is appended BEFORE ``finalize``, and the ORDER is the
    contract, not an aesthetic. ``finalize`` is the terminal step that flips the record to
    ``ready``; a best-effort step placed AFTER it would leave a `ready` repo with a step still
    running, and a Langfuse outage would be the last thing an operator sees on a succeeded
    materialize. Before ``finalize``, the timeline still ends on the step that means "done"."""
    assert [s["key"] for s in MATERIALIZE_STEPS] == [
        "mint_identity",
        "create_repo",
        "push_template",
        "set_repo_vars",
        "provision_langfuse",
        "finalize",
    ]
    # Every step carries a non-empty label: the frontend renders `label` VERBATIM (there is no
    # client-side key→label map), so a blank one would render an empty timeline row.
    assert all(s["label"].strip() for s in MATERIALIZE_STEPS)
    # And the LABELS THEMSELVES are the contract, not just their non-emptiness. Because there is no
    # key→label map anywhere in the frontend BY DESIGN (a record renders the label it was stored
    # with, so an old repo keeps reading its own history), this list is the ONLY place the operator's
    # timeline wording exists — there is no second copy a reviewer could diff it against, and no
    # component test that would notice. A label edit is a UI change that reaches production through
    # a data structure, so it has to break a test deliberately rather than sail through 300 green
    # ones. Pinned in the same order as the keys above.
    assert [s["label"] for s in MATERIALIZE_STEPS] == [
        "Mint Entra identity",
        "Create repository",
        "Push template contents",
        "Set repository CI variables",
        "Provision Langfuse tracing",
        "Finalize repository record",
    ]


def test_a_historical_eight_step_record_still_validates_and_keeps_its_own_labels():
    """THE MIGRATION CONTRACT (D-B2). Historical and in-flight records carry the OLD keys, and
    ``steps`` is stored data — this model must read them back unchanged rather than rejecting a key
    it no longer names or silently re-deriving a label. The frontend renders the stored label, so
    what is validated here is exactly what an operator sees on an old repo."""
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
    repo = Repository(id="r1", project_id="p1", name="n", agent_id="a1",
                      template_name="strands-agentcore", status="failed",
                      created_by="u", created_at="t", updated_at="t", steps=stored)
    assert len(repo.steps) == 8  # the record is read back AS STORED, not coerced to five
    assert [s.key for s in repo.steps] == [s["key"] for s in stored]
    assert [s.label for s in repo.steps] == [s["label"] for s in stored]


def test_repository_defaults_steps_when_absent():
    # Older items (no steps field) validate and get a full pending timeline.
    repo = Repository(id="r1", project_id="p1", name="n", agent_id="a1",
                      template_name="strands-agentcore", status="provisioning",
                      created_by="u", created_at="t", updated_at="t")
    assert len(repo.steps) == 6
    assert repo.steps[0].key == "mint_identity"


def test_step_state_roundtrips_json():
    s = StepState(key="create_repo", label="X", status=StepStatus.FAILED, error="403")
    assert StepState.model_validate_json(s.model_dump_json()).status == StepStatus.FAILED
