"""Pydantic models for Repositories: template-materialized repos under a Project.

A ``Repository`` is the persisted record of "an operator materialized a repo and its artifact from a
template into a project": the platform pre-registered the agent (or MCP server), pushed the template
scaffold into a new repository and wired the build-only CI variables. Each repository belongs to
exactly one ``Project`` (``project_id``); a project holds any number of them.

``RepositoryCreate`` is the write-only input of ``POST /projects/{id}/repos``. Its ``agent_config``
seeds the pre-registered agent's governance fields and is validated by
``models.project.validate_agent_config``; nothing writes it into the repository, because the runtime
reads ``AGENT_NAME`` and ``MODEL_ID`` from the governed registry record.

No boto3, no I/O: persistence and orchestration live in ``services.project_service``.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, get_args

from pydantic import ConfigDict, Field, ValidationInfo, field_validator, model_validator

from models.base import WireModel

logger = logging.getLogger(__name__)


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


MATERIALIZE_STEPS: List[dict] = [
    {"key": "mint_identity", "label": "Mint Entra identity"},
    {"key": "create_repo", "label": "Create repository"},
    {"key": "push_template", "label": "Push template contents"},
    {"key": "set_repo_vars", "label": "Set repository CI variables"},
    {"key": "provision_langfuse", "label": "Provision Langfuse tracing"},
    {"key": "finalize", "label": "Finalize repository record"},
]


class StepState(WireModel):
    key: str
    label: str
    status: StepStatus = StepStatus.PENDING
    error: Optional[str] = Field(
        default=None, description="A safe short hint; never a token or a response body"
    )
    started_at: Optional[str] = Field(default=None, description="ISO-8601; the repo stores string timestamps")
    completed_at: Optional[str] = None


def default_steps() -> List[StepState]:
    """A fresh pending timeline from ``MATERIALIZE_STEPS``, the ordered background steps of a
    materialize run.

    Pre-registering the agent runs synchronously before the 202, so it is not a step. ``finalize`` is
    the terminal step that flips the record to ``ready``; ``provision_langfuse`` is the only best-effort
    step (``ProjectService._BEST_EFFORT_STEPS``) and sits last before it, so a Langfuse outage marks
    its own row and never strands a repository whose code, CI variables and identity are correct. It
    is a timeline step rather than an inline call because it is a third-party round trip of up to
    thirty seconds whose success or failure the operator should see as its own row. A stored record
    keeps the keys and labels it was written with; the frontend renders ``steps[].label`` verbatim and
    keeps no key-to-label map, so a key this list no longer names still renders.
    ``tests/test_repository_steps_model.py`` pins the keys, the order, the labels and the legacy shape;
    ``frontend/src/components/operations/repositoryDetailTabs.test.ts`` pins the frontend half.
    """
    return [StepState(key=s["key"], label=s["label"]) for s in MATERIALIZE_STEPS]


class EvalEvidence(WireModel):
    """The prod candidate's eval run, as the Promote dialog states it. ``checks_*`` are ``None`` while
    the run has no summary (running, or errored before scoring)."""

    run_id: str
    status: str
    checks_passed: Optional[int] = None
    checks_total: Optional[int] = None
    judge_means: dict[str, Optional[float]] = Field(default_factory=dict)
    judge_min_mean: Optional[float] = None


CicdStatus = Literal["provisioning", "ready", "promoting", "deployed", "failed", "unknown"]


class Repository(WireModel):
    """Read model: the persisted repository record. Carries no token, ever.

    The ``last_*`` and ``prod_candidate_*`` scalars are a denormalized "latest" cache; the history is
    the append-only ``Deployment`` partition. The gateway fields, ``last_dev_image_tag`` and
    ``last_dev_digest`` are written only by the buildspec and skipped by ``_save_repo``;
    ``dev_outcome_for_candidate``, ``build_kind``, ``eval_gate``, ``eval_regressions`` and
    ``eval_evidence`` are computed at read time and never persisted. ``agent_id`` is a GSI hash key,
    so ``_write_repo`` never sets it to NULL for an MCP row. ``push_template`` commits the registered
    template's source repository at its tip, or the on-disk scaffold ``template_name`` names under
    ``AGENT_TEMPLATES_DIR`` when the name is unregistered or the record carries no source pair.
    """

    id: str
    project_id: str
    name: str
    repo_url: Optional[str] = None
    agent_id: Optional[str] = Field(
        default=None, description="The pre-registered agent; set iff artifact_kind is agent"
    )
    artifact_kind: Literal["agent", "mcp_server"] = Field(
        default="agent",
        description=(
            "What the repo builds; every record written before MCP repos existed is an agent, hence "
            "the default. Exactly one of agent_id and mcp_server_id is set to match"
        ),
    )
    mcp_server_id: Optional[str] = Field(
        default=None, description="The pre-registered MCP server; set iff artifact_kind is mcp_server"
    )
    gateway_arn: Optional[str] = Field(
        default=None, description="Written only by the buildspec's gateway arm"
    )
    gateway_id: Optional[str] = None
    gateway_url: Optional[str] = None
    template_name: str = Field(
        description=(
            "The scaffold directory the repository was created from; free-form, since AGP forwards the "
            "directory's contents and inspects no path"
        )
    )
    cicd_status: CicdStatus = "provisioning"
    status: str = Field(description="provisioning, ready or failed")
    created_by: str
    created_at: str
    updated_at: str
    steps: List[StepState] = Field(
        default_factory=default_steps,
        description="The live per-step materialize timeline; an older item without it gets a pending one",
    )
    last_dev_image_tag: Optional[str] = Field(
        default=None,
        description="The last image tag that deployed to dev, written by the buildspec and read by promote",
    )
    last_dev_digest: Optional[str] = Field(
        default=None,
        description=(
            "The digest of last_dev_image_tag. A tag is a mutable pointer (the tenant ECR repository is "
            "mutable, and while the template's constraints.txt pins the dependencies the base image tag "
            "still floats), so the digest names the bytes an approval can attest to; the tag stays as the "
            "label every surface renders and as what a rollback validates"
        ),
    )
    last_promoted_by: Optional[str] = Field(
        default=None, description="From the validated principal, never a body"
    )
    last_promoted_at: Optional[str] = None
    last_promoted_image_tag: Optional[str] = None
    last_promoted_digest: Optional[str] = Field(
        default=None, description="The digest prod serves, written by the buildspec on the prod branch"
    )
    last_promotion_build_id: Optional[str] = None
    last_build_started_at: Optional[str] = Field(
        default=None,
        description=(
            "When the last non-prod delivery (Build now, a non-prod rollback) started; the in-flight "
            "guard's clock for those paths, kept apart from last_promoted_at"
        ),
    )
    prod_candidate_image_tag: Optional[str] = Field(
        default=None,
        description=(
            "The single prod candidate: what merged to main and awaits an owner's approval. Written only "
            "by the prod-candidate route from OIDC-proven facts, overwritten by a newer merge, cleared on "
            "a successful promote; content-addressed as <agent_id>-<first seven of the tree sha>"
        ),
    )
    prod_candidate_sha: Optional[str] = Field(default=None, description="main's merge commit sha, full")
    prod_candidate_actor: Optional[str] = Field(default=None, description="The OIDC-proven GitHub login")
    prod_candidate_at: Optional[str] = Field(default=None, description="ISO-8601 UTC")
    prod_candidate_status: Optional[str] = Field(default=None, description="pending, or None")
    prod_candidate_digest: Optional[str] = Field(
        default=None,
        description=(
            "sha256:<64 hex>. The approved artifact: promote_repo passes it to the deploy verbatim and "
            "the buildspec deploys <repo>@<digest>, never re-reading the tag, which may point at "
            "different bytes by then"
        ),
    )
    dev_outcome_for_candidate: Optional[str] = Field(
        default=None,
        description=(
            "The dev-only fact promote_repo gates on, read back so the Promote affordance cannot drift "
            "from the refusal: a DeploymentOutcome value, or None when no dev row names the candidate's tag"
        ),
    )
    build_kind: Optional[Literal["container", "config"]] = Field(
        default=None,
        description=(
            "The template chain's build kind, so the UI knows which repos get Build now before any tag "
            "exists; None when the chain could not be read"
        ),
    )
    eval_gate: Optional[Literal["passed", "failed", "running", "none"]] = Field(
        default=None,
        description=(
            "The promote eval gate for the prod candidate's digest; none means no candidate or no run "
            "for it, None a repo without an agent or an unreadable store"
        ),
    )
    eval_regressions: Optional[list[str]] = Field(
        default=None, description="The candidate run's regressions versus the prod build; flags, not a block"
    )
    eval_evidence: Optional[EvalEvidence] = Field(
        default=None, description="The candidate run's evidence for the Promote dialog"
    )

    @field_validator("cicd_status", mode="before")
    @classmethod
    def _fold_cicd_status(cls, value: Any, info: ValidationInfo) -> Any:
        if value in get_args(CicdStatus):
            return value
        logger.warning(
            "%s %r is outside its set on %s; folded to %r",
            "cicd_status",
            value,
            info.data.get("id"),
            "unknown",
        )
        return "unknown"

    @model_validator(mode="after")
    def _artifact_id_matches_kind(self) -> "Repository":
        if self.artifact_kind == "agent":
            if not self.agent_id:
                raise ValueError("an agent repository needs agent_id")
            if self.mcp_server_id is not None:
                raise ValueError("an agent repository must not carry mcp_server_id")
        else:
            if not self.mcp_server_id:
                raise ValueError("an mcp_server repository needs mcp_server_id")
            if self.agent_id is not None:
                raise ValueError("an mcp_server repository must not carry agent_id")
        return self


class AgentRepositoryView(WireModel):
    """The agent → repository edge ``GET /agents/{agent_id}/repository`` answers: six fields copied
    from the owning ``Repository``, each typed as the field it copies; ``repo_id`` is the
    repository's ``id``."""

    project_id: str
    repo_id: str
    name: str
    prod_candidate_status: Optional[str] = None
    eval_gate: Optional[Literal["passed", "failed", "running", "none"]] = None
    eval_regressions: Optional[list[str]] = None


class AgentConfig(WireModel):
    """The agent knobs of a ``RepositoryCreate``: ``agent_name``, ``framework`` and ``model_id``. All
    three are optional because an MCP-server template sends ``{}`` or nothing. Any other key passes
    through to the agent record as an open map."""

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    agent_name: Optional[str] = None
    framework: Optional[str] = None
    model_id: Optional[str] = None


class RepositoryCreate(WireModel):
    """Write-only input: the ``POST /projects/{id}/repos`` body."""

    name: str
    template_name: str = Field(description="The on-disk template scaffold to push, by name")
    agent_config: AgentConfig = Field(
        default_factory=AgentConfig, description="The agent knobs; empty for an MCP-server template"
    )
    mcp_name: Optional[str] = Field(
        default=None,
        description="The MCP record name for an mcp_server template (default: the repo name); else ignored",
    )
    purpose: Optional[str] = Field(
        default=None,
        description="Governance description of the pre-registered agent; derived from the name when empty",
    )
    business_unit: Optional[str] = Field(
        default=None, description="Governance attribute of the pre-registered agent"
    )
    region: Optional[str] = Field(
        default=None, description="Governance attribute of the pre-registered agent"
    )
    data_classification: Optional[str] = Field(
        default=None, description="Governance attribute of the pre-registered agent"
    )
    repo_overrides: Optional[Dict[str, str]] = Field(
        default=None,
        description=(
            "Repo-level CI variable overrides, merged over the platform defaults and the project "
            "overrides; empty leaves the base layer intact"
        ),
    )


class RepoPromoteRequest(WireModel):
    """Write-only input: the optional ``POST /projects/{id}/repos/{repo_id}/promote`` body.

    ``override_reason`` bypasses the eval gate when the candidate has no passing run; the service
    strips it, refuses a blank one or one over 500 characters, and records who, why and the digest.
    Extra keys are ignored, so a stray ``image_tag`` still cannot choose what ships.
    """

    override_reason: Optional[str] = None


class RepoRollbackRequest(WireModel):
    """Write-only input: the ``POST /projects/{id}/repos/{repo_id}/rollback`` body.

    ``image_tag`` is required with no default: a rollback with no target is a 422, never a deploy of a
    server-chosen tag. It is not trusted; the service accepts it only with a ``succeeded`` ``Deployment``
    row for this repo in this stage (``ProjectService.rollback_repo``). ``stage`` is free-form and
    defaults to ``prod``; an unknown stage has no succeeded rows and is refused by the same check, but a
    blank stage is rejected outright because ``list_deployments`` branches on ``if stage:`` and an empty
    one would fall through to the cross-stage read and accept a tag that only ever succeeded in dev.
    The actor comes from the validated principal, never the body.
    """

    image_tag: str
    stage: str = Field(default="prod", min_length=1)

    @field_validator("stage")
    @classmethod
    def _stage_not_blank(cls, v: str) -> str:
        """``min_length`` alone would still admit ``" "``, which names no stage."""
        if not v.strip():
            raise ValueError("stage must not be blank")
        return v


class RepoDeleteSelection(WireModel):
    """The operator's per-step opt-in for the teardown cascade. Every step defaults on; an unchecked
    step is skipped, never failed."""

    record: bool = Field(default=True, description="The OPS row and the governed registry entry")
    github: bool = True
    image: bool = True
    runtime: bool = Field(default=True, description="The AgentCore runtime and its Terraform state object")
    identity: bool = True


class RepoDeleteItemResult(WireModel):
    """The outcome of one teardown step. ``reason`` is a safe short hint, never a token or a body."""

    item: str = Field(description="github, image, runtime, identity or record")
    outcome: str = Field(
        description=(
            "deleted, failed, skipped, absent or manual; absent means nothing existed to remove, manual "
            "that it exists and AGP does not remove it (the reason says how), both unlike skipped, "
            "which may still exist"
        )
    )
    reason: Optional[str] = None


class RepoDeleteResult(WireModel):
    """The full per-step cascade result. ``record_removed`` is True only when the OPS row and the
    registry entry were deleted: every selected step succeeded and ``record`` was on."""

    items: List[RepoDeleteItemResult]
    record_removed: bool


class RepoDeletePreviewItem(WireModel):
    """The reachability of one teardown artifact for the delete modal; a read-only probe that deletes
    nothing. The frontend treats ``unknown`` as selectable and checked."""

    item: str = Field(description="github, image, runtime, identity, record or policy_engine")
    state: str = Field(
        description=(
            "present, gone, unknown or manual; manual means it exists but AGP does not remove it, shown as "
            "a note rather than a checkbox"
        )
    )
    note: Optional[str] = Field(
        default=None, description="A short line for an item the modal states rather than offers"
    )


class RepoDeletePreview(WireModel):
    """The full per-artifact reachability pre-check: probes only, deletes nothing."""

    items: List[RepoDeletePreviewItem]


class PullRequestView(WireModel):
    """One pull request, as the Ops surface sees it: a projection of GitHub's PR object, never a mirror,
    so a provider body cannot leak through a pass-through. Nothing here is persisted; GitHub is the
    system of record. Carries no token.

    ``author`` is a GitHub login, a provider currency never joined to an Entra oid: the two are proven
    by different issuers and AGP holds no mapping between them. ``can_approve`` is the headline field:
    GitHub refuses a review on your own PR and AGP acts as the linked human, so the refusal is computed
    before the affordance is offered and rendered calmly with ``approve_blocked_reason``.
    ``mergeable`` is three-valued because GitHub computes mergeability asynchronously and answers
    ``null`` while it is in flight; ``null`` is not ``false``.
    """

    number: int
    title: str
    state: str = Field(description="open, closed or merged")
    author: str = Field(description="A GitHub login")
    head_sha: str
    url: str
    can_approve: bool = Field(
        description="False when the linked human is the author; the approve button renders only when True"
    )
    approve_blocked_reason: Optional[str] = Field(
        default=None, description="Why can_approve is false; a safe short sentence, never a provider body"
    )
    mergeable: Optional[bool] = Field(
        default=None,
        description="None while the provider has not computed mergeability; not the same as False",
    )
    merge_commit_sha: Optional[str] = Field(
        default=None,
        description=(
            "The commit the merge produced, which the merge build runs; None until merged, since GitHub "
            "also fills it on an open PR with a test merge commit no build runs"
        ),
    )


class PullRequestCreate(WireModel):
    """Write-only input: the ``POST /repositories/{repo_id}/pull-requests`` body.

    ``base`` is optional with no stage-shaped default: a tenant's branch set is open, so an omitted base
    means the repository's own default branch, resolved by the provider. The author is absent: it is
    whichever GitHub human the caller's link names, resolved from the validated Entra principal.
    """

    title: str = Field(min_length=1)
    head: str = Field(min_length=1)
    base: Optional[str] = None
    body: Optional[str] = None

    @field_validator("title", "head")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        """``min_length`` alone still admits ``" "``: a title of one space names nothing and a head of one
        space is not a branch."""
        if not v.strip():
            raise ValueError("must not be blank")
        return v
