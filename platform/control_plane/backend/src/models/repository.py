"""Pydantic models for Repositories — template-materialized repos under a Project (E20/T8).

Plain ``BaseModel``s — the ``connection``/``project`` idiom. A ``Repository`` is the
persisted record of "operator materialized a repo+agent from a template into a project":
the platform pre-registered the agent (Entra identity minted, NO runtime ARN yet),
materialized a repo from the ops-template scaffold, and wired the build-only CI repo
vars. Each ``Repository`` belongs to exactly one ``Project`` (``project_id``); a project
holds 1:N of them.

``RepositoryCreate`` is the write-only input — the body of ``POST /projects/{id}/repos``.
``agent_config`` carries the caller's agent knobs (a free-form dict here); repo-create (T9)
validates only the two governance-critical fields via
:func:`models.project.validate_agent_config`, which is now its WHOLE job. E28B/T3 deleted the
``agent.config.json`` commit — nothing writes this dict into the repo, because the runtime never
read that file (the buildspec takes ``AGENT_NAME``/``MODEL_ID`` from the governed registry
record). ``agent_name``/``framework``/``model_id`` reach the platform via the pre-registered
Agent, so this input seeds governance and validates; it does not ship.

No boto3, no I/O — persistence + orchestration live in ``project_service``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


# Ordered — index defines timeline order. Pre-register-agent runs SYNC pre-202, so it
# is NOT in this list. These are the BACKGROUND steps only.
#
# E28B/T3 (D-B2) — WHY THESE KEYS CHANGED, after E28A deliberately refused to change them.
#
# This list held EIGHT keys and was treated as a frozen backend↔frontend contract: E28A/T3 folded
# a `dev` branch cut into the BODY of `commit_config` rather than adding a ninth key, precisely to
# avoid moving it. E28B must move it anyway, because five of those eight steps STOP EXISTING —
# they are not renamed, they are deleted:
#
#   generate_repo   → create_repo     AGP creates an empty repo and pushes the content itself,
#                                     so there is no async template copy left to race.
#   commit_config   → push_template   ONE commit carrying the WHOLE template (RepoProvider.
#                                     commit_files), message-marked `[skip ci]`.
#   the `dev`-branch cut E28A/T3 folded into commit_config, plus create_env_dev,
#   create_env_prod and set_env_vars
#                                   → DELETED, and so are the provider methods behind them
#                                     (E28B/T7 removed `create_branch`, `create_environment` and
#                                     `set_environment_variables` from `github_repo_service`).
#                                     A second branch is a second write that fires a build;
#                                     GitHub Environments are a GitHub-only concept.
#
# Materialize made SIX sequential writes into a repository GitHub was concurrently writing to.
# Four defects came out of that, and four successive fixes only changed WHICH writer won — the
# last one traded a wasted `main` build for two dev builds 11 seconds apart racing one terraform
# state lock, where the loser failed, the winner deployed a stale image, and the record still read
# `deployed`. E28B removes five of the writes so there is one writer and nothing to race.
#
# MIGRATION (D-B2, verified in the frontend before relying on it): historical and in-flight
# records carry the OLD eight keys. Both timelines render ``steps[].label`` from the record
# VERBATIM (``ProjectRepositoriesTab.MaterializeTimeline``, ``RepositoryDetail.OverviewTab``) and
# use ``steps[].key`` only as a React list key — there is NO client-side key→label map anywhere in
# the frontend, so an old record keeps rendering its own stored labels rather than crashing or
# blanking a row. Nothing may introduce such a map: a key this list no longer names must always
# degrade to the label the record stored.
# E28C/T5 (D-C5) — SIX, because one step was MISSING rather than wrong. Every change above
# removed steps; ``provision_langfuse`` is the one addition, and it exists because
# ``POST /agents`` has provisioned a per-agent Langfuse project + key since E26/T4 while
# ``add_repo`` — which registers its agent straight through ``registry.create`` — never did. The
# envelope's ``langfuse_key_secret_name`` therefore stayed ``None`` on every repo-created agent,
# buildspec.yml read that None, the ``LANGFUSE_SECRET_NAME`` var never reached the runtime, and
# the container traced into nothing. The Observability tab then rendered a zero indistinguishable
# from "no traffic yet".
#
# It is a TIMELINE step and not an inline ``add_repo`` call for two reasons: provisioning is a
# ~30s third-party round-trip while ``add_repo`` is the fast pre-202 half, and a step gets a
# visible success/failure row that an inline best-effort call cannot.
#
# ORDER: last before ``finalize``. ``finalize`` is the terminal step that flips the record to
# ``ready``, so a best-effort step after it would leave a `ready` record with a step still
# pending. It is also the ONLY best-effort member of this list (see
# ``ProjectService._BEST_EFFORT_STEPS``): its failure marks its own row and the run CONTINUES,
# because a Langfuse outage must not strand a repository whose code, CI vars and identity are all
# already correct.
MATERIALIZE_STEPS: List[dict] = [
    {"key": "mint_identity",      "label": "Mint Entra identity"},
    {"key": "create_repo",        "label": "Create repository"},
    {"key": "push_template",      "label": "Push template contents"},
    {"key": "set_repo_vars",      "label": "Set repository CI variables"},
    {"key": "provision_langfuse", "label": "Provision Langfuse tracing"},
    {"key": "finalize",           "label": "Finalize repository record"},
]


class StepState(BaseModel):
    key: str
    label: str
    status: StepStatus = StepStatus.PENDING
    error: Optional[str] = None          # SAFE short hint only — never a token/body
    started_at: Optional[str] = None     # ISO8601 str (repo uses str timestamps, not datetime)
    completed_at: Optional[str] = None


def default_steps() -> List[StepState]:
    return [StepState(key=s["key"], label=s["label"]) for s in MATERIALIZE_STEPS]


class Repository(BaseModel):
    """Read-model — the persisted repository record. Carries NO token, ever."""

    id: str
    project_id: str
    name: str
    repo_url: Optional[str] = None
    agent_id: str
    # WHICH on-disk template scaffold AGP pushed into this repo (E22/T4 keyed it by name, and the
    # name is still the key — but E28B/T3 changed what it names). It is NOT "the provider template
    # this repo was generated from": nothing generates from a provider template any more.
    # ``push_template`` resolves it to ``{AGENT_TEMPLATES_DIR}/{template_name}`` and commits those
    # bytes itself. Free-form on purpose — AGP forwards a directory's contents and inspects no
    # path, so a template author owns the layout.
    template_name: str
    cicd_status: str = "provisioning"
    status: str  # provisioning | ready | failed
    created_by: str
    created_at: str
    updated_at: str
    # Live per-step materialize timeline (E25C). Additive — older items without this
    # field validate and get a full pending timeline via default_steps().
    steps: List[StepState] = Field(default_factory=default_steps)
    # E27 — the last image tag that successfully deployed to DEV. Written out-of-band by
    # the CodeBuild buildspec; the promote route reads it so no tag is ever hand-entered.
    last_dev_image_tag: Optional[str] = None
    # E28B/T4 (D-B3) — the DIGEST counterpart of the tag field above.
    #
    # WHY BOTH, and why the tag is NOT retired. A tag is a mutable pointer: the tenant ECR
    # repository is mutable and the image build is not reproducible (a floating
    # `python:3.11-slim` base, ranged deps, no lockfile), so the bytes behind one tag can differ
    # between the moment a human approves and the moment prod deploys. The digest names the bytes
    # themselves, which is the only value an approval can honestly attest to. The tag stays
    # because it is the human-readable label every existing surface renders, because a ROLLBACK
    # still validates against the tag's succeeded ``Deployment`` rows, and because a rollback to
    # pre-E28B code must still find a tag to deploy — a digest-only record would be undeployable
    # by the previous release.
    last_dev_digest: Optional[str] = None
    # E27 promotion audit (borrowed from marketplace's decided_by/decided_at idiom).
    # Actor comes from the validated principal — NEVER a request body.
    last_promoted_by: Optional[str] = None
    last_promoted_at: Optional[str] = None
    last_promoted_image_tag: Optional[str] = None
    # E28B/T4 (D-B3) — the digest prod actually SERVES. Same alongside-not-replacing rule as
    # ``last_dev_digest``; written by the same buildspec helper on the prod branch.
    last_promoted_digest: Optional[str] = None
    last_promotion_build_id: Optional[str] = None
    # E27A — the single prod candidate: what merged to main and awaits an OWNER's approval.
    # Written ONLY by the prod-candidate route (OIDC-proven actor/sha — never a request
    # body), overwritten wholesale by a newer merge, and cleared on a successful promote.
    # Additive — pre-E27A items lacking all five validate and read as "no candidate".
    prod_candidate_image_tag: Optional[str] = None
    prod_candidate_sha: Optional[str] = None       # main's merge commit sha (full)
    prod_candidate_actor: Optional[str] = None     # OIDC-proven GitHub login
    prod_candidate_at: Optional[str] = None        # ISO-8601 UTC
    prod_candidate_status: Optional[str] = None    # "pending" | None
    # E28B/T4 (D-B3) — THE APPROVED ARTIFACT. This is the field an OWNER's production approval
    # actually attests to, and it is the one value on this record that must never be re-derived:
    # ``promote_repo`` passes it to the deploy verbatim, and the buildspec deploys
    # ``<repo>@<digest>`` rather than looking the tag up again. Re-reading the tag at deploy time
    # would reopen exactly the window the digest closes, because between approval and deploy the
    # tag may point at different bytes.
    prod_candidate_digest: Optional[str] = None    # "sha256:<64 hex>"


class RepositoryCreate(BaseModel):
    """Write-only input — the POST /projects/{id}/repos body."""

    name: str
    # The on-disk template scaffold to push (keyed by name — see ``Repository.template_name``).
    template_name: str
    agent_config: Dict[str, Any]
    # Governance description for the pre-registered Agent record (shown in the Agents
    # view). Optional — the service falls back to a name-derived default when empty.
    purpose: Optional[str] = None
    # Governance attributes for the pre-registered Agent (E22/T4). Optional — the owner
    # (sponsor) is back-filled from the principal; BU/region/classification ride through.
    business_unit: Optional[str] = None
    region: Optional[str] = None
    data_classification: Optional[str] = None
    # Class-B repo-level CI var overrides (E22). Merged over platform defaults + project
    # overrides in the service's layered CI vars; empty/absent leaves the base layer intact.
    repo_overrides: Optional[Dict[str, str]] = None


# --------------------------------------------------------------------------- #
# Delete cascade (E23/T4) — the DELETE /projects/{id}/repos/{rid} contract.
# --------------------------------------------------------------------------- #


class RepoRollbackRequest(BaseModel):
    """Write-only input — the ``POST /projects/{id}/repos/{repo_id}/rollback`` body (E28/T4).

    ``image_tag`` is REQUIRED and has no default: a rollback with no target must be a 422, never a
    deploy of some server-chosen tag. It is NOT trusted — the service accepts it only if it has a
    ``succeeded`` ``Deployment`` row for this repo in this stage (see
    :meth:`services.project_service.ProjectService.rollback_repo`), which is what keeps the route
    from being a deploy-anything primitive.

    ``stage`` is free-form (D8) and defaults to ``"prod"`` — the rollback that matters. No
    dev/prod literal is validated here; an unknown stage simply has no succeeded rows and is
    refused by the same check.

    But a BLANK stage must be rejected outright (``min_length=1`` + the whitespace validator),
    because empty is not merely an unknown stage — it DISABLES the stage-scoped validation. The
    service's ``list_deployments`` branches on ``if stage:``, so ``stage=""`` falls through to the
    CROSS-stage read and ``_has_succeeded`` would then accept a tag that only ever succeeded in
    ``dev`` as a valid PROD rollback target. Verified: it currently fails closed only by luck,
    via an unrelated ``KeyError`` on ``tenant.stages[""]`` deeper in the build service. Rejecting
    it here makes the refusal intentional and local.

    The ACTOR is deliberately absent: it comes from the validated principal, never the body."""

    image_tag: str
    stage: str = Field(default="prod", min_length=1)

    @field_validator("stage")
    @classmethod
    def _stage_not_blank(cls, v: str) -> str:
        """``min_length`` alone would still admit ``" "``, which is falsy nowhere but names no
        stage — same bypass, one space wider."""
        if not v.strip():
            raise ValueError("stage must not be blank")
        return v


class RepoDeleteSelection(BaseModel):
    """Operator's per-step opt-in for the teardown cascade. Every step defaults ON —
    an unchecked step is skipped (never failed)."""

    record: bool = True  # OPS DDB row + governed agent registry entry
    github: bool = True
    image: bool = True
    runtime: bool = True  # AgentCore runtime + its TF state object
    identity: bool = True


class RepoDeleteItemResult(BaseModel):
    """The outcome of one teardown step. ``reason`` is a SAFE short hint (never a token/body)."""

    item: str  # "github" | "image" | "runtime" | "identity" | "record"
    outcome: str  # "deleted" | "failed" | "skipped"
    reason: Optional[str] = None


class RepoDeleteResult(BaseModel):
    """The full per-step cascade result. ``record_removed`` is True only when the OPS row +
    registry entry were actually deleted (all selected steps succeeded and ``record`` was on)."""

    items: List[RepoDeleteItemResult]
    record_removed: bool


# --------------------------------------------------------------------------- #
# Delete pre-check (E23/T11) — the GET /projects/{id}/repos/{rid}/delete-preview
# contract. A READ-ONLY reachability probe so the delete modal only offers the
# artifacts that still exist (deletes NOTHING).
# --------------------------------------------------------------------------- #


class RepoDeletePreviewItem(BaseModel):
    """The reachability of one teardown artifact. ``state`` is ``present`` (still there),
    ``gone`` (already deleted / unreachable), or ``unknown`` (the probe could not tell —
    the frontend treats ``unknown`` as selectable+checked, assume present)."""

    item: str  # "github" | "image" | "runtime" | "identity" | "record"
    state: str  # "present" | "gone" | "unknown"


class RepoDeletePreview(BaseModel):
    """The full per-artifact reachability pre-check (READ-ONLY — probes only, deletes nothing)."""

    items: List[RepoDeletePreviewItem]


# --------------------------------------------------------------------------- #
# Pull requests on a repository (E28/T14 — design D14+D15, contract C2).
#
# The wire shape of the four ``/repositories/{repo_id}/pull-requests`` verbs. It is a
# PROJECTION of GitHub's PR object, never a mirror: only the fields the Ops surface renders
# cross, so a provider body cannot leak through a pass-through. Nothing here is persisted —
# AGP stores no PR state at all, because GitHub is the system of record for a pull request
# and a cached copy would be a second answer to a question with one authority.
#
# ``can_approve`` IS THE HEADLINE FIELD. GitHub refuses a review on your own PR, and AGP acts
# as the LINKED HUMAN (never as its App — see ``services.github_pr_service``), so the refusal
# is computed BEFORE the affordance is offered rather than discovered as a 422. It is a state
# the UI renders calmly with ``approve_blocked_reason`` beside it, not an error.
#
# ``mergeable`` is deliberately three-valued. GitHub computes mergeability ASYNCHRONOUSLY and
# answers ``null`` while the computation is in flight; ``null`` is NOT ``false``, and
# collapsing the two would tell an operator a clean PR cannot be merged.
# --------------------------------------------------------------------------- #


class PullRequestView(BaseModel):
    """One pull request, as the Ops surface sees it. Carries NO token and no provider body.

    ``author`` is a GITHUB LOGIN — a provider currency, never an AGP principal and never
    joined to an Entra oid (E27A §6). The two are proven by different issuers and AGP holds
    no mapping between them, so nothing here may render one as the other.
    """

    number: int
    title: str
    state: str  # "open" | "closed" | "merged"
    author: str  # GitHub login
    head_sha: str
    url: str
    # False when the linked human IS the author (D15). A capability the caller does not hold
    # is ABSENT from the UI, not disabled — so this decides whether the button renders.
    can_approve: bool
    # Why ``can_approve`` is false, so the refusal can be STATED rather than implied. A SAFE
    # short sentence only — never a provider body.
    approve_blocked_reason: Optional[str] = None
    # None ⇒ the provider has not computed mergeability yet. NOT the same as False.
    mergeable: Optional[bool] = None


class PullRequestCreate(BaseModel):
    """Write-only input — the ``POST /repositories/{repo_id}/pull-requests`` body.

    ``base`` is OPTIONAL and has no stage-shaped default (D8): a tenant's branch/stage set is
    open, so an omitted base means "the repository's own default branch", resolved by the
    provider rather than guessed by AGP. Writing a literal here would be the same hardcode the
    design forbids one layer down.

    The AUTHOR is deliberately absent: it is whichever GitHub human the caller's E27B link
    names, resolved from the validated Entra principal, and can never be supplied by a body.
    """

    title: str = Field(min_length=1)
    head: str = Field(min_length=1)
    base: Optional[str] = None
    body: Optional[str] = None

    @field_validator("title", "head")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        """``min_length`` alone still admits ``" "`` — a title of one space names nothing and
        a head of one space is not a branch."""
        if not v.strip():
            raise ValueError("must not be blank")
        return v
