"""Pydantic models for Evaluations.

Two halves. ``EvalConfig`` and ``DatasetItem`` are the documented file format the agent repo owns
(``evals/evals.yaml`` and one ``DatasetItem`` per line of ``evals/dataset.jsonl``); both forbid unknown
keys so a typo is refused instead of silently ignored. ``Score``, ``ItemResult``, ``EvalRun`` and
``PromoteOverride`` are the run record AGP writes (S3, DynamoDB and CloudWatch).

No boto3, no I/O: parsing lives in ``services.eval_config``, persistence in ``services.eval_store``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field

from models.base import WireModel

ToolMatch = Literal["exact", "in_order", "any_order", "off"]
ArgsMatch = Literal["exact", "subset", "off"]
ScoreKind = Literal["check", "judge"]
RunStatus = Literal["running", "passed", "failed", "error"]

DATASET_PATH_PATTERN = r"^evals/[A-Za-z0-9._-]+\.jsonl$"

JudgeId = Annotated[
    str, Field(pattern=r"^Builtin\.[A-Za-z]+$", description="An AgentCore Evaluations built-in evaluator id")
]


class ExpectedTool(WireModel):
    """One expected tool call. Unknown keys are forbidden so a typo such as ``arg`` cannot silently skip
    the arguments check."""

    model_config = ConfigDict(extra="forbid")

    name: str
    args: dict[str, Any] | None = Field(
        default=None, description="None means do not check this call's arguments"
    )


class ToolCall(WireModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class DatasetItem(WireModel):
    """One line of ``evals/dataset.jsonl``. Only ``id`` and ``input`` are required."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$", description="Unique within the file")
    input: str = Field(min_length=1, max_length=8000)
    expected_output: str | None = Field(default=None, description="The Builtin.Correctness reference answer")
    assertions: list[str] = Field(default=[], description="For Builtin.GoalSuccessRate")
    expected_tools: list[ExpectedTool] = Field(default_factory=list)
    source_trace_id: str | None = None
    tags: list[str] = Field(default_factory=list)


class ChecksConfig(WireModel):
    model_config = ConfigDict(extra="forbid")

    tools: ToolMatch = "in_order"
    args: ArgsMatch = "subset"
    no_error: bool = True


class GateConfig(WireModel):
    model_config = ConfigDict(extra="forbid")

    checks: Literal["all_pass"] = "all_pass"
    judge_min_mean: float = Field(default=0.8, ge=0, le=1)


class EvalConfig(WireModel):
    """``evals/evals.yaml``. A missing file means these defaults."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    dataset: str = Field(
        default="evals/dataset.jsonl",
        pattern=DATASET_PATH_PATTERN,
        description=(
            "A file directly under evals/ ending .jsonl; AGP commits to this path, so the pattern keeps "
            "it from naming a workflow, a script or evals.yaml itself"
        ),
    )
    checks: ChecksConfig = ChecksConfig()
    judges: list[JudgeId] = Field(
        default=["Builtin.GoalSuccessRate", "Builtin.Correctness"], description="Empty means checks only"
    )
    gate: GateConfig = GateConfig()


class Score(WireModel):
    evaluator: str = Field(description="tools, args, no_error, or a judge id")
    kind: ScoreKind
    value: float | None = Field(description="Checks: 1.0 or 0.0; judges: 0 to 1; None on error")
    passed: bool | None = Field(
        description="None for judges (the gate decides on the mean); False on error for checks"
    )
    label: str | None = None
    explanation: str | None = None
    error: str | None = None


class ItemResult(WireModel):
    item_id: str
    session_id: str | None = None
    output: str | None = None
    tool_calls: list[ToolCall] | None = Field(
        default=None, description="None means no tool-call source produced data"
    )
    latency_ms: int | None = None
    error: str | None = None
    scores: list[Score] = Field(default_factory=list)


class GateResult(WireModel):
    passed: bool
    reasons: list[str] = Field(default_factory=list)


class RunSummary(WireModel):
    items: int
    checks_passed: int
    checks_failed: int
    judge_means: dict[str, float | None] = Field(default_factory=dict)


class RunBaseline(WireModel):
    """The last finished run of the build serving prod, as a finished run compared itself with it."""

    run_id: str
    build: str = Field(description="Short build identity: the tag's commit, else the digest's first hex")
    checks_passed: int
    checks_total: int
    judge_mean: float | None = Field(
        default=None, description="Mean of the judges' means; None when no judge scored"
    )
    digest: str | None = Field(
        default=None,
        description=(
            "The prod build compared with (digest, else tag), so a reader can tell whether it still serves "
            "prod"
        ),
    )
    build_tag: str | None = None


class EvalRun(WireModel):
    run_id: str
    repo_id: str
    agent_id: str
    project_id: str
    stage: Literal["dev"] = "dev"
    git_sha: str | None = None
    digest: str | None = None
    build_tag: str | None = Field(
        default=None,
        description=(
            "The dev image tag of the run's build; a build is identified by its digest when present, else "
            "by this tag (harness and config builds have no digest)"
        ),
    )
    trigger: Literal["manual", "auto"]
    started_by: str
    started_at: datetime
    finished_at: datetime | None = None
    status: RunStatus = "running"
    config: EvalConfig
    item_count: int | None = Field(
        default=None,
        description=(
            "Dataset size at start; sizes the stale-running cutoff. None on runs saved before it existed"
        ),
    )
    summary: RunSummary | None = None
    gate: GateResult | None = None
    baseline: RunBaseline | None = Field(
        default=None,
        description=(
            "Regression versus the prod build, flags only; absent on runs saved before it existed, on "
            "running or errored runs, and when there is nothing to compare with"
        ),
    )
    regressions: list[str] = Field(default_factory=list)


class PromoteOverride(WireModel):
    repo_id: str
    digest: str | None
    by: str = Field(description="The promoter's oid, the stable identity")
    reason: str
    at: datetime
    by_name: str | None = Field(
        default=None,
        description="The promoter's display name at write time; None on rows written before it existed",
    )
    build_tag: str | None = Field(
        default=None,
        description="The candidate's image tag, the build identity when there is no digest; None on old rows",
    )


class EvalFiles(WireModel):
    """``GET .../evals``: the repo's eval files at the trunk head; ``errors`` carries the parse
    errors, each ``<file>:<line>: <message>``, and ``configured`` says whether ``evals/evals.yaml``
    exists (else ``config`` is the defaults)."""

    config: EvalConfig
    items: list[DatasetItem]
    head_sha: str
    errors: list[str]
    configured: bool


class ValidationResult(WireModel):
    ok: bool
    errors: list[str]
    items: int


class HeadSha(WireModel):
    head_sha: str


class DatasetAdded(WireModel):
    head_sha: str
    items: int


class EvalRuns(WireModel):
    """The runs list, also served agent-scoped: ``current_digest`` and ``current_build_tag`` identify
    the current dev build, ``current_run`` its newest run."""

    runs: list[EvalRun]
    overrides: list[PromoteOverride]
    current_digest: str | None
    current_build_tag: str | None
    current_run: EvalRun | None


class RunDetail(WireModel):
    run: EvalRun
    results: list[ItemResult]
    dataset: list[DatasetItem] | None
