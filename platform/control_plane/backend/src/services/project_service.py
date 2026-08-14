"""Project + Repository orchestrator (E22/T4, materialize rewritten in E28B/T3 — AGP pushes
the template itself over a portable provider seam; nothing generates from a provider template).

A **Project** is now an EMPTY CONTAINER scoped to one org (``connection_id``) — it holds
no template, repo, or agent of its own. The moved one-shot materialize+pre-register work
lives on :meth:`add_repo`, which persists a :class:`~models.repository.Repository` under a
project. ``add_repo`` runs in order (spec §5/§7):

  0.  ``validate_agent_config`` — non-strands / bad ``agent_name`` raises ``ValueError``.
  0b. Resolve the project — absent ⇒ ``ProjectError(kind="not_found")``.
  1.  **Pre-register** the agent: a governed Agent record (``auth_type=entra`` +
      ``platform=aws_bedrock`` so it is governed) with ``agent_arn=None`` — the runtime
      does NOT exist yet. The creator sponsors by default (``apply_creator_sponsor``
      back-fills ``sponsor_*`` from ``principal``), and the governance attributes
      (``business_unit`` / ``region`` / ``data_classification``) ride through. Then mint
      its Entra identity via ``provision_identity`` (identity only; NO runtime authorizer,
      which T5 wires later).
  2.  **Materialize** the repo over the PORTABLE provider seam (E28B — ``RepoProvider``):
      ``create_repo`` makes an empty ``org/name``, ``commit_files`` pushes the whole template
      in ONE commit onto the project's ``trunk_branch`` with ``[skip ci]`` in the message,
      then the layered build-only CI repo variables are set.
  3.  **Persist** the ``Repository`` record.

E28B rewrote step 2 from SIX writes to two, and the count is the point. The old path used
GitHub's ``/generate`` (which copies the template in an async internal push), cut a second
branch, committed ``agent.config.json``, set repo vars, created two GitHub Environments and
wrote per-stage env vars — three uncoordinated writers on one tree. Four live defects came from
that, and four fixes only changed which writer won; the last produced two dev builds 11 seconds
apart racing one terraform state lock, where the loser failed, the winner deployed a stale
image, and the record still read success. Now AGP pushes the bytes itself, so there is one
writer and nothing to race. See :meth:`ProjectService._materialize_runners`.

There is NO enrollment gate anymore (E20's ``is_enrolled`` precheck is gone): any template
the AGP template registry (E28B/T2) names can be materialized from directly.

Layered CI vars (spec Class B): ``effective = {**platform_defaults, **project_overrides,
**repo_overrides, "AGENT_ID": agent.id}``. Platform defaults are the ``repo_vars`` ctor
arg; ``repo_overrides`` come from the ``add_repo`` call. The merge SEAM exists here even
though project/repo overrides are empty for now (storage wired in T11/UI).

Persistence is DDB-or-local, cloned verbatim from ``connection_service``, across TWO
partitions in the same table: ``pk="project"`` (containers) and ``pk="repository"``
(materialized repos, filtered by ``project_id``). Injectable clock + id source; a local
dict + lock for the no-table fallback.

FAILURE ENVELOPE: pre-register failures propagate (nothing persisted). A materialize
failure AFTER the identity is minted persists a ``Repository`` with ``status="failed"``
(the minted identity is reusable via the agent's ``/reprovision``) and raises
:class:`ProjectError(kind="materialize_error")` so the route surfaces a fail-loud error
while the failed record stays queryable.

SECURITY: the connection credential is read via ``ConnectionService.get_bearer_token``
(a stored PAT, or a freshly minted GitHub App installation token) and flows ONLY into the
GitHub write client — it is never logged, never returned, never put on a read model.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional
from uuid import uuid4

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from models.agent import (
    UNKNOWN_STAGE,
    AgentCreate,
    AuthType,
    Platform,
    resolve_runtime_arns,
)
from models.deployment import (
    Deployment,
    DeploymentOutcome,
    deployment_seq_key,
    new_deployment_id,
)
from models.project import Project, ProjectDetail, validate_agent_config
from models.repository import (
    MATERIALIZE_STEPS,
    RepoDeleteItemResult,
    RepoDeletePreview,
    RepoDeletePreviewItem,
    RepoDeleteResult,
    RepoDeleteSelection,
    Repository,
    StepStatus,
    default_steps,
)
from services.agent_registry_service import apply_creator_sponsor
from services.connection_service import ConnectionError
from services.github_repo_service import GitHubRepoService
from services.runtime_build_service import RuntimeBuildError
from services.scaffold_files import collect_scaffold_files

# E36/T8: the cross-account credential seam. A tenant stage carrying a ``deploy_role_arn``
# owns its resources in ITS OWN account, which the ambient ECS-task credentials cannot see.
# ``StageUnresolvedError`` is DEFINED there (E36/T16 fix round 1: a leaf service that finds the
# same condition subclasses it without importing this module) and re-exported here, which is
# where every caller and test already imports it from.
from services.tenant_credentials import (  # noqa: F401 — re-exported for existing importers
    StageUnresolvedError,
    TenantCredentialsError,
    stage_client,
)

# E28C/T4: the registry's OWN key derivation, so materialize looks a template up by the same key
# the catalog wrote it under. (No import cycle: ``template_registry`` imports nothing from
# ``services``.)
from services.template_registry import template_id_for

# E28B review #1: the ONE authority on what a template name may be. Imported from the catalog
# service rather than re-declared, because this module now turns that name into a FILESYSTEM PATH —
# two patterns that could drift is exactly how a traversal reopens. (No import cycle:
# ``github_template_service`` does not import this module.)
from services.github_template_service import _NAME_RE as _TEMPLATE_NAME_RE

logger = logging.getLogger(__name__)

_PROJECT_PK = "project"  # container partition
_REPOSITORY_PK = "repository"  # materialized-repo partition
# E28/T3: the APPEND-ONLY delivery-history partition. Its sk is NOT a record id — it is
# ``{repo_id}#{stage}#{started_at}#{id[-4:]}`` (see models.deployment.deployment_seq_key), which
# is what makes the partition time-sortable per repo+stage with no counter and no append race.
_DEPLOYMENT_PK = "deployment"

# E28/T2 (finding P3): the round-trip ceiling on the CROSS-STAGE deployment read.
# That path cannot pass a DDB `Limit` (it would truncate in stage order and could drop the
# newest row — see `list_deployments`), so it pages the prefix itself. Unbounded, one repo with
# a long append-only history could page forever and hang the request.
#
# This ceiling is a TRADE, not a free bound: paging is stage-major, so hitting it can omit the
# genuinely-newest deployment (an early-sorting stage may consume the whole budget alone). It is
# accepted because 20 pages × ~1 MB is tens of thousands of rows away and a hung request is
# worse — but see `list_deployments` for the full statement, and treat the warning it logs as a
# correctness signal, not noise. The real fix, if it ever fires, is a time-major index.
_MAX_DEPLOYMENT_PAGES = 20

# E28/T4: how far back a ROLLBACK target may be found. Read on the stage-scoped path, which is
# DDB-sorted newest-first and exact, so this is simply "the newest N deployments of that stage".
# A real bound, deliberately chosen over an unbounded scan: it is also the depth the UI's own
# history list shows, so a target the operator can see is a target they can pick. An artifact
# older than that is REFUSED rather than deployed unverified — the safe direction.
_ROLLBACK_HISTORY_LIMIT = 100

# E28B/T3 (D-B2) — the materialize push message, PINNED VERBATIM.
#
# ``[skip ci]`` is the mechanism, not a courtesy. Materialize now writes exactly ONE commit, so one
# marker suppresses the entire setup build on every provider — no branch filters, no
# ``github.ref`` conditionals, nothing per-provider to keep correct. Before this, AGP's setup
# writes were indistinguishable from a developer's real push, which is the root cause the design
# names: the workflow cannot tell a setup commit from a merge, so the platform must not look like
# one. GitHub honours the marker natively on ``push``/``pull_request``.
_TEMPLATE_PUSH_MESSAGE = "chore: initialize from template [skip ci]"

# E28B/T3: the CI stage whose credentials the repository's CI variables carry.
#
# NOT a stage the platform picks by searching. A tenant's stage set is open (TenantService requires
# at least ONE stage, not dev+prod), and quietly selecting "the first non-prod stage" would wire a
# tenant's `staging` credentials into a workflow whose environment is literally named `dev`, with
# nothing anywhere saying so. The template names `dev`, so `dev` is what is looked up, by name —
# and a tenant that carries stages but no `dev` is a real misconfiguration that fails loudly (see
# :func:`_build_stage_ci_vars`) rather than getting some other stage's push role.
#
# `prod` is deliberately unreachable here: GitHub pushes to the dev registry and POSTs
# ``stage: 'dev'``, while a prod deploy is AGP copying an approved digest into the prod account.
# GitHub is never trusted with the production account.
_CI_STAGE = "dev"

# The branch ``GitHubRepoService.create_repo``'s ``auto_init=True`` seeds. GitHub's own default is
# `main`; an org may configure another, and AGP cannot read that setting from a connection record.
#
# This is NOT a trunk default (that is ``Project.trunk_branch``) — it is the name of the ref
# materialize must RECLAIM when the project's trunk differs. See ``_adopt_trunk``.
_AUTO_INIT_BRANCH = "main"

# The three CI vars sourced from the tenant's stage config rather than from platform defaults.
# They stay REPO-LEVEL (see ``set_repo_vars`` for why that is deliberate and must not be reverted).
#
# It had ZERO readers after the env-scoped writes were deleted — it used to name the keys that were
# POPPED from the repo-level set. A dead constant naming three live variables invites someone to
# "reconnect" it to the pop it came from, which is exactly the regression that would leave every
# build with no registry, no region and no push role. So it now has ONE reader, and it is an
# assertion rather than a lookup: it pins that `_build_stage_ci_vars` produces exactly these keys.
_STAGE_SCOPED_VAR_KEYS = ("ECR_REPOSITORY", "AWS_REGION", "AWS_ECR_PUSH_ROLE_ARN")


def _step_error_hint(err: Exception) -> str:
    """The SAFE short hint persisted on a failed materialize step, and shown to the operator.

    ``type(err).__name__`` alone was the rule, and for arbitrary exceptions it still is: a
    provider/Graph/boto exception message can carry a token, a URL or a response body, and this
    string lands on a read model the console renders. Fail closed on anything unrecognised.

    :class:`ProjectError` is the ONE exception this module raises deliberately, and every one of its
    messages is a CURATED LITERAL written here (grep the constructor calls — no interpolated token,
    no provider body, no ``str(exc)`` of a foreign error). Those messages are the only place the
    operator can learn WHAT to fix — "tenant 'x' carries no 'dev' stage (stages present: [...])"
    tells them to change tenant config, whereas ``"ProjectError"`` tells them nothing and invites a
    pointless retry (D-B2b requirement 4).

    So the type name is kept as the fail-closed default, and the message is surfaced only for the
    type whose messages this module authors.
    """
    if isinstance(err, ProjectError):
        return err.message
    return type(err).__name__


# E28C/T5 (D-C5): the materialize steps whose failure marks THEIR OWN row and lets the run
# CONTINUE, instead of flipping the record to ``failed`` and stopping.
#
# The default is deliberately the opposite (E25C/T2: any step failure is terminal), because a
# half-materialized repository must not read `ready`. Membership here is therefore narrow and has
# to be argued per step: it belongs to work on a resource the REPOSITORY DOES NOT DEPEND ON.
#
# ``provision_langfuse`` qualifies — a Langfuse outage does not make the pushed template, the CI
# variables or the Entra identity any less correct, and stopping the run there would leave a fully
# built repo stuck at ``provisioning`` with ``finalize`` never run, so ``retry_materialize`` would
# be the only way to ``ready``. It is the same reasoning that makes the ``langfuse`` teardown item
# non-blocking in :meth:`ProjectService.delete_repo`.
#
# BOTH halves are the contract and both are test-pinned: the row must FAIL (a swallow here would
# report `done` on an agent that traces into nothing) and the run must CONTINUE. Nothing may be
# added to this set for being merely flaky — that is a reason to fix the step.
_BEST_EFFORT_STEPS = frozenset({"provision_langfuse"})

# The delete-cascade line-items that are REPORTED but excluded from the record-gating predicate in
# :meth:`ProjectService.delete_repo` — a failure on one is visible to the operator yet never keeps
# the DDB row "for retry".
#
# The test for membership is whether a RETRY OF THIS CASCADE could plausibly change the outcome.
# ``langfuse`` (E26/T7) is a third-party service the record does not depend on; ``exec_role``
# (E28C/T5) fails on a missing IAM grant, which is a Terraform change, not something a retry
# reaches. Every OTHER item is retryable and must keep blocking: a surviving repo, image, runtime or
# identity is a live resource that becomes unreachable the moment the row is gone.
_NON_BLOCKING_ITEMS = frozenset({"langfuse", "exec_role"})


def _build_stage_ci_vars(tenant, tenant_id: str) -> Dict[str, str]:
    """The three stage-sourced CI vars for :data:`_CI_STAGE`, or raise naming what the tenant has.

    Looked up BY NAME. A tenant carrying stages but no ``dev`` cannot satisfy the template's
    ``environment: dev`` jobs, so this refuses instead of substituting another stage: substituting
    would hand a workflow labelled `dev` some other stage's ECR repository and push role, and the
    only evidence would be a deploy landing in the wrong account. The message names the stages the
    tenant ACTUALLY carries, because the fix is a tenant config change and the operator needs to
    see what they have.

    Raising here fails the ``set_repo_vars`` STEP (``run_materialize`` records it and stops), which
    is the correct blast radius: the repo and its template exist, and a retry after fixing the
    tenant resumes from this step."""
    stages = getattr(tenant, "stages", None) or {}
    cfg = stages.get(_CI_STAGE)
    if cfg is None:
        raise ProjectError(
            f"tenant {tenant_id!r} carries no {_CI_STAGE!r} stage, so the repository's CI "
            f"variables cannot be sourced (stages present: {sorted(stages)})",
            kind="materialize_error",
        )
    stage_vars = {
        "ECR_REPOSITORY": cfg.ecr_repo_uri,
        "AWS_REGION": cfg.region,
        "AWS_ECR_PUSH_ROLE_ARN": cfg.push_role_arn,
    }
    # The keys are the CONTRACT with the template's workflow (`vars.ECR_REPOSITORY`,
    # `vars.AWS_REGION`, `vars.AWS_ECR_PUSH_ROLE_ARN`), so a rename here silently breaks every build
    # rather than failing anything offline. Cheap, local, and it gives the constant a real reader.
    assert set(stage_vars) == set(_STAGE_SCOPED_VAR_KEYS), (
        f"stage CI vars drifted from the template's contract: {sorted(stage_vars)}"
    )
    return stage_vars

# E28/T2 (design D5): the AgentCore runtime's Terraform state key, STAGE-SCOPED.
#
# Before T2 this was `agentcore-runtime/{agent_id}/terraform.tfstate` — no stage segment — so
# every stage of an agent shared ONE state file and therefore ONE `agent_arn`: a prod deploy
# re-applied over the dev runtime, and dev and prod were not environments but a promotion
# ceremony in front of a single mutable runtime. It also left rollback structurally
# impossible, since there was no per-stage state to roll back.
#
# The same key is produced in TWO places and they MUST agree byte-for-byte: here, and in
# `infrastructure/modules/codebuild/buildspec.yml` (`terraform init -backend-config="key=…"`,
# with `$AGENT_ID`/`$STAGE`). The buildspec is the WRITER; this side only deletes/inspects, so
# a drift is silent — the delete path would target an object that never existed and state
# would leak forever. `tests/test_project_service.py` reads the real buildspec and asserts the
# two agree; keep that test if you touch either literal.
#
# The BUCKET is deliberately NOT stage-scoped: `STATE_BUCKET` is a single control-plane
# variable (`modules/codebuild/main.tf:402`) and stays so. Only the key gains the segment.
_RUNTIME_STATE_PREFIX = "agentcore-runtime"


# E28/T2 fix-1: the page ceiling on LISTING an agent's state objects before teardown. Same
# "the store keeps handing back a token" valve as `_MAX_DEPLOYMENT_PAGES`, on the delete path.
# One agent holds one object per stage, so a single page (1000 keys) is already absurdly
# generous — a second page means something is wrong, and 5 is purely defensive.
_MAX_STATE_LIST_PAGES = 5


def runtime_state_key(agent_id: str, stage: str) -> str:
    """The S3 key of one agent's runtime Terraform state FOR ONE STAGE.

    ``stage`` is free-form (D8) — a tenant's stage set is open, so this must scope by whatever
    the stage is called (`uat`, a region name, …) and never by a dev/prod literal."""
    return f"{runtime_state_prefix(agent_id)}{stage}/terraform.tfstate"


def runtime_state_prefix(agent_id: str) -> str:
    """The S3 prefix holding EVERY stage's state for one agent (trailing slash).

    This exists because stage-scoping turned "one agent → one state object" into "one agent →
    N objects", so teardown has to reclaim a prefix rather than delete a known key."""
    return f"{_RUNTIME_STATE_PREFIX}/{agent_id}/"


# E28A/T2: the AgentCore runtime's IAM execution role — the ONE place its name is derived.
#
# Terraform CREATES it (`modules/agentcore_runtime/main.tf`, `aws_iam_role.exec` +
# `aws_iam_role_policy.exec`); this module only ever DELETES it. The role therefore exists
# nowhere but Terraform state — and the delete cascade removes that state object in the same
# operation (`_delete_runtime_state`), so before this reclaim the role became unreachable by
# IaC and untracked by AGP simultaneously and leaked forever. That was verified live: five
# orphaned `*-agentcore-exec` roles in the account, every one from a deleted repo.
#
# The leak is not cosmetic. IAM role names are ACCOUNT-GLOBAL, so re-materializing a repo
# under the same agent name makes Terraform's `aws_iam_role.exec` fail with
# `EntityAlreadyExists` — a delete that permanently burns the name it used.
#
# Centralized in ONE helper rather than inlined at the call site because E28A/T1b STAGE-SCOPED
# this name (one role per stage instead of one per agent). With a scattered literal that rename
# would have drifted silently against the module — and since the name is what the delete path
# targets, a drift means the teardown deletes nothing while reporting success.
# `tests/test_project_service.py` pins both names against the REAL main.tf for that reason.
_AGENTCORE_EXEC_ROLE_SUFFIX = "agentcore-exec"

# The INLINE policy Terraform attaches to that role (`aws_iam_role_policy.exec`'s `name`).
# IAM refuses `DeleteRole` while a role still carries an inline policy (`DeleteConflict`), so
# this name is load-bearing for the role delete, not just for the policy's own reclaim.
_AGENTCORE_EXEC_POLICY_NAME = "runtime-exec"


def agentcore_exec_role_name(agent_name: str, stage: str) -> str:
    """The IAM exec-role name Terraform gives ONE STAGE of one agent's AgentCore runtime.

    Derived from the agent NAME (the registry record's ``name``) and the STAGE, which are
    exactly the two values the buildspec feeds the module as ``agent_name`` / ``stage`` — NOT
    from the agent id. Keep this the single producer on the Python side; see the notes on
    :data:`_AGENTCORE_EXEC_ROLE_SUFFIX`.

    STAGE IS REQUIRED, deliberately. A defaulted stage would let a caller that has not thought
    about which stage it means silently derive one real role name and miss the others — and
    since a missing role reads as ``NoSuchEntity`` (success), that mistake is invisible. The
    pre-T1b un-scoped name still exists in the account, but it is produced by the separately
    named :func:`legacy_agentcore_exec_role_name` so that reclaiming it is always an explicit
    choice rather than the accidental result of omitting an argument."""
    return f"{agent_name}-{stage}-{_AGENTCORE_EXEC_ROLE_SUFFIX}"


def legacy_agentcore_exec_role_name(agent_name: str) -> str:
    """The PRE-T1b, un-stage-scoped exec-role name — for reclaim ONLY, never for creation.

    Terraform stopped producing this shape when E28A/T1b stage-scoped the module's resource
    names, but every role created before that rollout is still sitting in the account under it
    (five such orphans were verified live). IAM role names are ACCOUNT-GLOBAL and nothing else
    reclaims them, so a cascade that only tried the new per-stage names would leak the old name
    forever — and because the leaked name is the one a re-materialize of the same agent would
    need, that leak is also what re-raises ``EntityAlreadyExists``.

    Kept as its own function rather than "``agentcore_exec_role_name`` with stage omitted" so
    that no creation path can reach it by accident: this name must never be produced as the
    name to CREATE, only as one more name to attempt deleting."""
    return f"{agent_name}-{_AGENTCORE_EXEC_ROLE_SUFFIX}"


def _iam_error_code(err: Exception) -> str:
    """The AWS error code off a boto exception, or ``""``.

    Tolerates a ``BotoCoreError`` (a client-side transport failure carries no ``response``),
    which is why this reads through ``getattr`` rather than indexing ``err.response``."""
    response = getattr(err, "response", None) or {}
    return response.get("Error", {}).get("Code") or ""


# E27/T8: how long a repo left at cicd_status="promoting" blocks a SECOND promote.
# Derived from the runtime-provision CodeBuild project's own `build_timeout = 60`
# (infrastructure/modules/codebuild/main.tf:323): past that the build cannot still be
# running, so a record still reading "promoting" is STUCK, not in flight — and the
# buildspec's status write is `2>/dev/null || true` and no-ops on an unset REPO_SK, so
# stuck is a real state with no reconciler to clear it. Bounding the guard is what keeps
# a stuck record recoverable instead of permanently unpromotable.
_PROMOTE_IN_FLIGHT_MINUTES = 60

# E27A/T5: the prod-candidate block on Repository — the single artifact that merged to `main`
# and awaits an OWNER's approval. Grouped because they are written and cleared as ONE unit
# (there is only ever one candidate; a newer merge overwrites all five) and because
# :meth:`ProjectService._save_repo` must skip them wholesale: they are written EXCLUSIVELY by
# `record_prod_candidate` (from the candidate route) and cleared exclusively by a successful
# `promote_repo`, so any other backend save of a stale read must leave them alone.
#
# E28B/T4 added a SIXTH member, ``prod_candidate_digest``. It joins the tuple rather than sitting
# beside it precisely because of the "one unit" argument above: a candidate holding a digest but no
# tag (or the reverse) is a half-state, and a leftover digest after a promote would let a second
# approval deploy bytes whose candidate had already been consumed.
_PROD_CANDIDATE_FIELDS = (
    "prod_candidate_image_tag",
    "prod_candidate_digest",
    "prod_candidate_sha",
    "prod_candidate_actor",
    "prod_candidate_at",
    "prod_candidate_status",
)

# The ONLY attributes `record_prod_candidate` may write. It writes them TARGETED rather than
# saving the whole record, because the whole-record save is a read-modify-write: it would also
# re-SET `last_promoted_*` from a record read BEFORE a promotion stamped them, reverting the
# promotion audit to `None`. That is not merely lost history — `_promotion_in_flight` is
# measured FROM `last_promoted_at`, so a reverted stamp makes the guard fail open and a second
# promote can start a second CodeBuild run against the SAME Terraform state key. This route
# fires on every merge to `main`, so the window is routinely open.
_CANDIDATE_WRITE_FIELDS = frozenset(_PROD_CANDIDATE_FIELDS) | {"updated_at"}


class ProjectError(Exception):
    """A project operation failed. Carries a SAFE message + a ``.kind`` hint
    (``{"not_found","materialize_error","has_repositories","nothing_to_retry","no_dev_build",
    "no_prod_candidate","promote_failed","promote_in_flight","unknown_rollback_target",
    "rollback_failed"}``) the route maps to a fixed HTTP status/detail — never ``str(exc)``
    (which could carry a store/GitHub message)."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


# ``StageUnresolvedError`` was DEFINED here; it now lives in ``services.tenant_credentials``,
# imported (and re-exported) above, so a leaf service can subclass it without importing this
# module — see that class and E36/T16's fix round 1.


class ProjectService:
    def __init__(
        self,
        *,
        table_name: str = "",
        registry,
        identity,
        connection_service,
        github_repo_service: Optional[GitHubRepoService] = None,
        tenant_service=None,
        ecr_image_service=None,
        runtime_build_service=None,
        ecr_repository: str = "",
        langfuse_provisioning=None,
        runtime_state_bucket: str = "",
        agent_templates_dir: str = "",
        template_registry=None,
        repo_vars: Optional[Dict[str, str]] = None,
        region: str = "us-east-1",
        new_id=lambda: str(uuid4()),
        now=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.table_name = table_name
        self.region = region
        self._registry = registry
        self._identity = identity
        self._conn = connection_service
        # E23/T4 teardown deps: the ECR image cleaner (T3) and the runtime TF-state bucket.
        self._ecr = ecr_image_service
        # E26/T7 teardown dep: the Langfuse provisioner (its idempotent C2
        # ``delete_agent_project`` tears down the agent's Langfuse project + SM secret).
        # Optional — None (unconfigured/not injected) ⇒ the langfuse step is a no-op.
        self._langfuse = langfuse_provisioning
        self._runtime_state_bucket = runtime_state_bucket
        # E27/T8 promotion deps: the EXISTING RuntimeBuildService (used unchanged — it is
        # auth-agnostic; the GitHub-OIDC checks live only on the /builds/runtime route) plus
        # the platform-default ECR repo passed as its `ecr_repo` fallback (the tenant's own
        # stage ECR wins inside the build service).
        self._builds = runtime_build_service
        self._ecr_repository = ecr_repository
        # ``_rollout`` is the GitHub write client (the collaborator the tests assert
        # ``create_repo`` / ``commit_files`` on). Named for the repo-
        # materialization role it plays.
        self._rollout = github_repo_service or GitHubRepoService()
        # E28B/T3: where the on-disk template scaffolds live. ``push_template`` reads the named
        # template from here (``collect_scaffold_files``) and pushes those bytes itself, which is
        # what removes the provider's async template copy as a second writer.
        #
        # A ctor arg wired from the route (``api/routes/projects.py``), mirroring how
        # ``template_rollout_service`` already takes it from ``api/routes/connections.py`` — NOT
        # ``from core.config import settings`` inside the service, which only three services do and
        # which would make this untestable without env config. Defaulted so every existing test
        # construction keeps working; a test that exercises ``push_template`` injects a tmp_path.
        self._agent_templates_dir = Path(agent_templates_dir or ".")
        # E28C/T4 (D-C2): the template CATALOG, which materialize DEREFERENCES. A record carrying
        # T2's ``source_org``/``source_repo`` pair names a real repository, so ``push_template``
        # reads that repo's bytes at use-time instead of the image's baked-in seed — the whole
        # point of this epic, because a customer who iterates their template in their own org was
        # otherwise silently shipped starter bytes.
        #
        # ``None`` (unwired / a legacy construction) ⇒ the on-disk seed, which is the pre-28C
        # world and the correct answer for a service with no pointer store. It is NOT the same as
        # a catalog that FAILS to read: that raises and fails the step, because "AGP could not
        # look" is not evidence that a record has no source (the rule T1 gave ``read_repo``).
        #
        # A ctor arg, mirroring ``tenant_service``, wired from ``api/routes/projects.py`` — the
        # route already builds a ``TemplateRegistry`` for the templates surface, and it is the
        # SAME ``template`` partition of the SAME projects table.
        self._templates = template_registry
        # E25/T4: tenant lookup for the per-STAGE ecr/region/push-role values. E28B/T7 corrects
        # what these are WRITTEN as: there are no GitHub Environments any more (both creates and
        # the env-scoped variable write are deleted — a GitHub-only concept, and two more writes to
        # a fresh repo), so ``set_repo_vars`` writes the tenant's DEV stage values into the
        # REPOSITORY-wide set. They still resolve under the template's ``environment: dev`` jobs
        # because a lookup falls back to the repository set. When None (legacy / tests without a
        # tenant service) the platform defaults in ``self._repo_vars`` already carry the three keys.
        self._tenants = tenant_service
        # Platform-default CI vars (the base layer of the Class-B merge in _materialize_repo).
        self._repo_vars = dict(repo_vars or {})
        self._new_id = new_id
        self._now = now

        self._ddb = None
        self._table = None
        if table_name:
            try:
                self._ddb = boto3.resource("dynamodb", region_name=region)
                self._table = self._ddb.Table(table_name)
            except Exception:  # pragma: no cover — degrade to local fallback.
                self._table = None

        # Lazy s3 client for the runtime TF-state teardown (E23/T4) — built ONLY when a
        # bucket is configured (mirror the _ddb lazy-build guard); None ⇒ no state delete.
        self._s3 = None
        if runtime_state_bucket:
            try:
                self._s3 = boto3.client("s3", region_name=region)
            except Exception:  # pragma: no cover — degrade to no-op state delete.
                self._s3 = None

        # E28A/T2: IAM client for the runtime exec-role reclaim. Built UNCONDITIONALLY (unlike
        # `_s3`, which is gated on a configured bucket) because there is no config value that
        # says whether the role exists — Terraform always creates it alongside the runtime, so
        # the reclaim is gated on the RUNTIME step, not on wiring. IAM is global; the region
        # only keeps client construction uniform with the sibling services. None ⇒ no-op
        # reclaim, exactly like an absent `_s3` skips the state delete.
        self._iam = None
        try:
            self._iam = boto3.client("iam", region_name=region)
        except Exception:  # pragma: no cover — degrade to no-op exec-role reclaim.
            self._iam = None

        # E36/T8: the CROSS-ACCOUNT credential seam. ``self._iam`` above (and the identity
        # service's control client) carry AMBIENT ECS-task credentials, which reach the
        # CONTROL-PLANE account only — so for a tenant stage that deploys into its own
        # account the teardown never addressed the resource at all: IAM answered
        # ``NoSuchEntity`` and AgentCore answered ``ResourceNotFoundException`` about an
        # account that never held them, and both of those are the IDEMPOTENT-already-done
        # state. The cascade reported ``deleted`` on a live runtime and an account-global
        # role. Not a swallowed error — a truthful answer to the wrong question.
        #
        # A plain attribute holding the module function, following this file's client
        # injection idiom (tests override ``svc._iam`` / ``svc._s3`` after construction, so
        # they override this the same way). Bound as an INSTANCE attribute, not a method, so
        # the pinned signature stays exactly ``stage_client(service, cfg, *, session_suffix)``
        # — the shape T13 consumes.
        self._stage_client = stage_client

        # Local fallback caches (used when no DDB table is configured).
        self._local_projects: Dict[str, Project] = {}
        self._local_repos: Dict[str, Repository] = {}
        # E28/T3: the append-only deployment history. A LIST, not a dict — the partition is
        # append-only and keyed by a composite sort key, so there is no id to key on and no
        # row to replace. Sorted on read (the DDB branch lets DynamoDB do it).
        self._local_deployments: List[Deployment] = []
        self._local_lock = threading.Lock()

        # E25C/T2: the materialize INPUTS for a persisted-but-not-yet-materialized repo,
        # keyed by repo_id. ``add_repo`` stashes them (agent object + agent_config +
        # overrides + connection/tenant) and returns; ``run_materialize`` (the BackgroundTask
        # that runs after the 202 response, in-process) reads them to drive the steps.
        # Process-local (BackgroundTasks run in the same process) — cleared on success.
        self._pending_materialize: Dict[str, dict] = {}

    # -- mode helper --------------------------------------------------------

    @property
    def _has_ddb(self) -> bool:
        return bool(self.table_name) and self._table is not None

    # ===================================================================== #
    # Public API
    # ===================================================================== #

    def list_projects(self) -> List[Project]:
        return self._load_all_projects()

    def get_project(self, id: str) -> Optional[ProjectDetail]:
        """A project + its repositories (filtered by ``project_id``); None if absent."""
        project = self._get_project(id)
        if project is None:
            return None
        return ProjectDetail(project=project, repositories=self._load_repos_for(id))

    def create_project(
        self,
        *,
        name: str,
        connection_id: str,
        tenant_id: str,
        description: str,
        created_by: str,
    ) -> Project:
        """Persist an EMPTY container in the ``project`` partition. No agent, no repo.

        NO ``trunk_branch`` parameter (E36/T15, item 24 option B): the create API stopped carrying
        one, so every project takes ``Project.trunk_branch``'s default — the single branch the
        shipped template's workflow can build. The field is still read (materialize's push, the
        prod-candidate gate), so the plumbing stays literal-free; only the way to choose a value is
        gone. Stored pre-E28B records lack the key and hydrate as ``"main"`` — what they
        effectively were."""
        ts = self._now().isoformat()
        record = Project(
            id=self._new_id(),
            name=name,
            connection_id=connection_id,
            tenant_id=tenant_id,
            description=description,
            created_by=created_by,
            created_at=ts,
            updated_at=ts,
        )
        self._save_project(record)
        return record

    def list_repositories(self) -> List[Repository]:
        """Flat scan of the ``repository`` partition (for the /repositories page)."""
        return self._load_all_repos()

    def find_repository_by_agent_id(self, agent_id: str) -> Optional[Repository]:
        """The Repository that owns ``agent_id`` (its 1:1 pre-registered agent), or None.

        Scans the ``repository`` partition (mirrors :meth:`list_repositories`). Used by the
        builds route (E25/I1) to bind a GitHub-OIDC token's proven ``repository`` to the
        agent_id it claims to build — the OIDC repo identity must own the agent."""
        return next((r for r in self._load_all_repos() if r.agent_id == agent_id), None)

    def add_repo(
        self,
        *,
        project_id: str,
        name: str,
        template_name: str,
        agent_config: Dict,
        created_by: str,
        principal,
        business_unit: Optional[str] = None,
        region: Optional[str] = None,
        data_classification: Optional[str] = None,
        purpose: Optional[str] = None,
        repo_overrides: Optional[Dict[str, str]] = None,
    ) -> Repository:
        """Pre-register the agent + persist a PENDING repo record, then return (E25C/T2).

        ``template_name`` names an on-disk template scaffold (E28B — its contents are pushed
        by ``push_template``; the registry is the catalog, not the content store).
        ``principal`` defaults the agent's sponsor;
        ``business_unit`` / ``region`` / ``data_classification`` are governance attributes
        stamped on the pre-registered Agent. There is NO enrollment gate.

        E25C/T2 — this now does ONLY the SYNC work (fast, so the route can return 202): it
        validates the config, resolves the project, PRE-REGISTERS the governed Agent record
        (a fail-loud 409/400/404 surface), and persists a ``Repository`` with a full
        ``pending`` step timeline (``default_steps()``) and ``cicd_status="provisioning"``.
        The identity mint + the side-effecting provider steps are DEFERRED to
        :meth:`run_materialize`, which the route schedules as a BackgroundTask after the
        202 response. The materialize INPUTS (the pre-registered agent + config/overrides)
        are stashed in ``_pending_materialize`` for that background run to consume."""
        # 0) Validate agent.config BEFORE any side effect — non-strands / bad name raises.
        validate_agent_config(agent_config)

        # 0a) P-B5 (E28C/T4) — ``template_name`` IS VALIDATED HERE, at the boundary.
        #
        # It was already validated, correctly, inside ``_resolve_scaffold_dir`` — which closed a
        # real arbitrary-file-read (E28B review #1). But that runs in ``push_template``, STEP 3 OF
        # 5: a request carrying ``../../etc`` was refused only AFTER an Entra identity had been
        # minted and a repository created in the customer's org. Two irreversible side effects, and
        # an operator left with a half-materialized agent, for a request that was never going to
        # succeed. The traversal was closed; the ORDER was not.
        #
        # The deeper check STAYS (it is the layer that survives a symlink and any future change to
        # how that path is built, and E28C's repo arm never reaches it at all) — this is the
        # boundary refusal, not a replacement.
        #
        # ONE AUTHORITY: the same imported ``_NAME_RE`` the catalog uses. A second pattern here is
        # exactly how a traversal reopens after someone "tidies" one of them.
        if not _TEMPLATE_NAME_RE.match(template_name or ""):
            raise ProjectError(
                f"invalid template name {template_name!r} — a template name must match "
                f"{_TEMPLATE_NAME_RE.pattern}",
                kind="invalid_template_name",
            )

        # 0b) Resolve the parent project — an unknown id is a 404, not a materialize failure.
        project = self._get_project(project_id)
        if project is None:
            raise ProjectError("Project not found", kind="not_found")

        # 1) Pre-register: a governed Agent record (entra + aws_bedrock) with NO runtime
        #    ARN yet. The creator sponsors by default (apply_creator_sponsor back-fills
        #    sponsor_* from principal), and the governance attributes ride through. This is
        #    SYNC (pre-202) so a duplicate agent_name fails loud as a 409 before we return.
        #    Identity minting is DEFERRED to run_materialize (the 'mint_identity' step).
        agent = self._registry.create(
            apply_creator_sponsor(
                AgentCreate(
                    name=agent_config["agent_name"],
                    purpose=(purpose or "").strip()
                    or f"Agent for repository '{name}' (template '{template_name}')",
                    auth_type=AuthType.ENTRA,
                    platform=Platform.AWS_BEDROCK,
                    framework=agent_config.get("framework"),
                    model_id=agent_config.get("model_id"),
                    business_unit=business_unit,
                    region=region,
                    data_classification=data_classification,
                    agent_arn=None,
                    # E24/T6: repos (and their agents) inherit the PROJECT's tenant
                    # (spec §3). Project.tenant_id is required — a legacy stored record
                    # hydrates as "default" in _hydrate_project, so it is always set.
                    tenant_id=project.tenant_id,
                ),
                principal,
            ),
            created_by=created_by,
            # E27/T5: the agent inherits the PROJECT's identity too, so the per-project
            # roles gate its mutation routes. Stamped HERE, at the single pre-register call
            # site that already sets tenant_id — every materialized agent is
            # project-governed from birth, and an agent registered directly (no project)
            # keeps project_id=None and stays tenant-gated only. A SERVER-SIDE keyword, not
            # an AgentCreate field, so no request body can ever supply it.
            project_id=project_id,
        )

        ts = self._now().isoformat()
        repo_id = self._new_id()

        # 2) Persist the repository record with a full PENDING timeline — repo_url is None
        #    until 'generate_repo' fills it in run_materialize. cicd_status/status stay
        #    "provisioning" (the badge's in-flight state); run_materialize flips them to
        #    "ready" on success or "failed" on the first failing step.
        record = Repository(
            id=repo_id,
            project_id=project_id,
            name=name,
            repo_url=None,
            agent_id=agent.id,
            template_name=template_name,
            cicd_status="provisioning",
            status="provisioning",
            created_by=created_by,
            created_at=ts,
            updated_at=ts,
            steps=default_steps(),
        )
        self._save_repo(record, include_cicd_status=True)  # the initial row

        # 3) Stash the materialize inputs for the BackgroundTask (run_materialize reads
        #    them to drive the steps). The connection token is NOT stashed — it is read
        #    fresh in each step via get_bearer_token so it never lingers in memory.
        self._pending_materialize[repo_id] = {
            "agent": agent,
            "name": name,
            "connection_id": project.connection_id,
            "template_name": template_name,
            "agent_config": agent_config,
            "repo_overrides": repo_overrides,
            "tenant_id": project.tenant_id,
            # D-B5: the trunk comes from the PROJECT, so no branch literal lives on this path.
            "trunk_branch": project.trunk_branch,
        }
        return record

    def get_repo(self, repo_id: str) -> Optional[Repository]:
        """Public read of a single Repository record (E25C/T2 — the timeline read path).

        A thin re-export of the internal :meth:`_get_repo` DDB-or-local read so the route
        (and T3's status/retry endpoints) can load the live per-step ``steps`` timeline
        without reaching a private method. None when absent."""
        return self._get_repo(repo_id)

    def run_materialize(self, repo_id: str) -> None:
        """Run the background materialize steps for a persisted-pending repo (E25C/T2).

        The BackgroundTask target scheduled by ``POST /projects/{id}/repos`` AFTER the 202
        response. Runs each step in :data:`MATERIALIZE_STEPS` order, wrapping it
        ``_save_repo_step(RUNNING)`` → do the work → ``_save_repo_step(DONE)``. A step
        already ``done`` is SKIPPED (resume support for T3's retry). On ANY step raising:
        mark that step ``failed`` with a SAFE short hint, flip the record to
        ``cicd_status="failed"``/``status="failed"``, STOP (no later steps), and SWALLOW the
        exception — this runs after the response, so it must NEVER raise. Terminal success:
        the ``finalize`` step flips ``cicd_status`` → ``"ready"``.

        The pending inputs come from ``_pending_materialize`` (stashed by ``add_repo``); an
        absent entry (e.g. a stale/duplicate schedule) is a no-op."""
        pending = self._pending_materialize.get(repo_id)
        if pending is None:
            logger.warning("[project] run_materialize called for unknown/stale repo %s", repo_id)
            return

        repo = self._get_repo(repo_id)
        if repo is None:  # pragma: no cover — record was persisted by add_repo.
            return
        done_keys = {s.key for s in repo.steps if s.status == StepStatus.DONE}

        # Dispatch table: step key → the work to run. Built LAZILY inside the per-step
        # protection so its eager connection+token fetch (get_bearer_token can hit the
        # network / GitHub App) is attributed to the first step and SWALLOWED — this runs
        # after the response, so run_materialize must NEVER raise on any path (E25C/T2 fix).
        runners: Optional[Dict[str, Callable[[], None]]] = None
        for step in MATERIALIZE_STEPS:
            key = step["key"]
            if key in done_keys:  # resume: never re-run a completed step (T3 retry).
                continue
            self._save_repo_step(repo_id, key, StepStatus.RUNNING)
            try:
                if runners is None:
                    runners = self._materialize_runners(repo_id, pending)
                runner = runners.get(key)
                if runner is None:  # a MATERIALIZE_STEPS key with no matching runner.
                    raise KeyError(f"no materialize runner for step '{key}'")
                runner()
            except Exception as err:  # noqa: BLE001 — swallow: runs after the response.
                logger.exception("[project] materialize step '%s' failed for %s", key, repo_id)
                self._save_repo_step(repo_id, key, StepStatus.FAILED, error=_step_error_hint(err))
                if key in _BEST_EFFORT_STEPS:
                    # E28C/T5: the step's OWN row carries the failure and the run CONTINUES —
                    # the record is not flipped to failed and no later step is skipped. See
                    # :data:`_BEST_EFFORT_STEPS` for which steps qualify and why.
                    continue
                self._mark_repo_failed(repo_id)
                # Drop the stash on terminal failure too (mirror the success path) — it
                # holds the agent object and would otherwise linger in process memory.
                self._pending_materialize.pop(repo_id, None)
                return
            self._save_repo_step(repo_id, key, StepStatus.DONE)

        # All steps done → drop the stashed inputs (they carry the agent object).
        self._pending_materialize.pop(repo_id, None)

    def retry_materialize(self, repo_id: str) -> Repository:
        """Reset the failed materialize + RE-DERIVE the inputs, so a scheduled
        ``run_materialize`` resumes from the failed step (E25C/T3).

        The route schedules the background run; this does ONLY the sync prep:
          1. Reset every non-``done`` step (the ``failed`` one + any later ``pending``) back
             to ``pending`` (cleared error/timestamps) — already-``done`` steps stay ``done``
             so ``run_materialize``'s done-skip loop resumes from the first failure — PLUS the
             terminal ``finalize`` step, which is reset EVEN WHEN ``done`` (see below).
          2. Flip ``cicd_status``/``status`` back to ``"provisioning"`` (the in-flight badge).
          3. RE-DERIVE the materialize inputs from DURABLE state and re-stash them: the T2
             success/failure path POPS ``_pending_materialize``, so a failed repo (exactly
             what retry targets) has NO stash. The agent is already registered — fetched from
             the registry by ``agent_id`` (NOT re-registered); the project gives
             connection/tenant; the ``agent_config`` is rebuilt from the governed agent
             record's fields. The connection token is NOT stashed (read fresh per step).

        WHY A ``done`` ``finalize`` IS RESET TOO (E28C live fix). ``finalize`` is the ONLY writer
        of ``cicd_status``/``status`` → ``"ready"``, and step 2 above unconditionally flips the
        record to ``"provisioning"``. So any retry that leaves ``finalize`` ``done`` hands
        ``run_materialize`` a run with no step left to put the record back — the repo reads
        ``"provisioning"`` forever, beside a Complete timeline.

        That was unreachable until E28C/T5. Every step failure used to be terminal, so a ``failed``
        step and a ``done`` ``finalize`` could not coexist: the run stopped at the failure and
        ``finalize`` was still ``pending``, hence reset by rule 1. :data:`_BEST_EFFORT_STEPS`
        created the combination — ``provision_langfuse`` fails its OWN row, the run CONTINUES, and
        ``finalize`` completes. Retrying that repo reset only the Langfuse step and stranded it; a
        real repo was found in exactly that state. Re-running ``finalize`` is safe because
        :meth:`_finalize_repo` is idempotent: it stamps ``repo_url`` from the record when the run
        carries none and flips the two status fields, nothing else.

        Reset BY KEY, not by position, and only when rule 1 reset something — the all-``done``
        guard below still refuses. Every OTHER ``done`` step stays ``done``: resume-skip is the
        point of retry (re-pushing a template would be a second write to a live tree).

        Unknown repo → ``ProjectError(kind="not_found")`` (the route maps to 404). A repo
        whose steps are ALL ``done`` (a double-click retry after a successful run, or a
        stale-UI retry on a ready repo) → ``ProjectError(kind="nothing_to_retry")`` (409):
        NOTHING is reset, re-stashed, or flipped, so the repo is never left stuck at
        ``"provisioning"`` with no step left to drive it back to ``"ready"``."""
        repo = self._get_repo(repo_id)
        if repo is None:
            raise ProjectError("Repository not found", kind="not_found")

        # 0) Guard: only proceed when at least one step will actually be reset/run. If every
        #    step is already ``done`` there is nothing to resume — flipping cicd_status →
        #    "provisioning" here would strand the repo, since the only path back to "ready"
        #    (the ``finalize`` step) would be skipped by run_materialize's done-skip loop.
        if all(step.status == StepStatus.DONE for step in repo.steps):
            raise ProjectError("Nothing to retry", kind="nothing_to_retry")

        # 1) Re-derive the inputs from durable state (the stash is gone on a failed repo).
        agent = self._registry.get(repo.agent_id)
        if agent is None:  # pragma: no cover — the pre-registered agent should still exist.
            raise ProjectError("Repository not found", kind="not_found")
        project = self._get_project(repo.project_id)
        if project is None:  # pragma: no cover — parent project should still exist.
            raise ProjectError("Repository not found", kind="not_found")
        # Rebuild the agent.config from the governed agent record (the T2 stash's agent_config is
        # not durably persisted). E28B/T3: NO materialize step reads this any more — the
        # ``agent.config.json`` commit is gone, because the runtime never read that file (the
        # buildspec takes AGENT_NAME/MODEL_ID from the governed registry record). It is
        # reconstructed here purely to keep the stash shape identical to ``add_repo``'s, so the two
        # entry points cannot drift.
        agent_config = {
            "agent_name": agent.name,
            "framework": agent.framework,
            "model_id": agent.model_id,
        }
        self._pending_materialize[repo_id] = {
            "agent": agent,
            "name": repo.name,
            "connection_id": project.connection_id,
            "template_name": repo.template_name,
            "agent_config": agent_config,
            # Class-B repo overrides are not durably persisted (empty for now — see
            # add_repo); re-derive as None. set_repo_vars is idempotent on resume.
            "repo_overrides": None,
            "tenant_id": project.tenant_id,
            # D-B5: re-derived from the project, exactly like add_repo — a retried
            # ``push_template`` must target the SAME branch the first attempt did, or the retry
            # would create a second branch (and a second build) instead of converging.
            "trunk_branch": project.trunk_branch,
        }

        # 2) Reset every non-done step → pending (keeps done steps done for resume-skip), PLUS
        #    ``finalize`` even when done: it is the only writer of "ready" and step 3 flips the
        #    record to "provisioning". Reachable since _BEST_EFFORT_STEPS — see the docstring.
        ts = self._now().isoformat()
        for step in repo.steps:
            if step.status != StepStatus.DONE or step.key == "finalize":
                step.status = StepStatus.PENDING
                step.error = None
                step.started_at = None
                step.completed_at = None
        repo.cicd_status = "provisioning"
        repo.status = "provisioning"
        repo.updated_at = ts
        self._save_repo(repo, include_cicd_status=True)  # deliberate reset to in-flight
        return repo

    def promote_repo(self, project_id: str, repo_id: str, *, promoted_by: str) -> Repository:
        """Promote the repo's PROD CANDIDATE to prod (E27/T8, narrowed by E27A/T5) — the
        epic's headline action.

        The image tag is resolved SERVER-SIDE from ``repo.prod_candidate_image_tag`` — the
        artifact that merged to ``main``, registered out-of-band by the candidate route from an
        OIDC-proven merge. E27A narrowed this off ``last_dev_image_tag``: promoting "whatever
        last landed on dev" could ship an artifact nobody reviewed, whereas the candidate is by
        construction what a merge to ``main`` offered. ``last_dev_image_tag`` is still on the
        record (it is what dev is RUNNING, which the FE shows alongside the candidate) but is
        no longer a precondition for, or an input to, a promotion.

        No caller ever supplies a tag, so there is no input through which an arbitrary image
        could be pushed to production. The deploy itself is the EXISTING
        :meth:`RuntimeBuildService.start_runtime_build` with ``stage="prod"`` — unchanged, and
        it derives the target tenant/account from the agent.

        ``promoted_by`` is the validated principal's identity, NEVER a request body value.
        Attribution is SPLIT and neither system copies the other's fact: the provider owns who
        wrote/reviewed/merged the code (mirrored onto the record as ``prod_candidate_actor``
        from the OIDC token), AGP owns who authorized the DEPLOY (``last_promoted_by``).

        The candidate is CONSUMED by a successful start and left INTACT by any refusal or
        failure — see step 5. So a second Promote on the same merge is refused rather than
        re-deploying the same image, while a failed promote stays retryable.

        The consumption is a COMPARE-AND-CLEAR (E27A/T5). ``start_runtime_build`` blocks for
        SECONDS (a Secrets Manager write plus a CodeBuild ``StartBuild``), and a merge to
        ``main`` can register a newer candidate inside that window. Clearing off the record read
        BEFORE the build would erase that merge with no error anywhere — the Promote button
        would simply go quiet and nobody would be told a merge was waiting, i.e. silent loss of
        a governance-relevant event. So the record is RE-READ once the build has started and the
        five fields are cleared only while the STORED candidate is still the one promoted (tag
        AND sha); a newer one is left untouched for the OWNER to promote next. The
        ``last_promoted_*`` audit is stamped EITHER way — the deploy genuinely happened, which is
        audit data regardless of what has merged since.

        Failure discipline mirrors ``marketplace_service._apply_grant``: every input is
        resolved BEFORE anything is mutated, and a build failure still ATTRIBUTES the attempt
        (actor + tag are stamped) and persists ``cicd_status="failed"`` BEFORE raising, so the
        record shows a failed promotion the operator can retry rather than silently staying
        at its previous status. Only the traceback is logged — the raised message is a curated
        safe literal and ``from None`` keeps the build service's own message out of the chain.

        EVERY DEPENDENCY failure of the build call goes through the SAME
        stamp-then-persist-then-raise path — the build service's own ``RuntimeBuildError``;
        the connection service's ``ConnectionError`` (``start_runtime_build``'s FIRST
        statement resolves the connection and its docstring delegates that mapping to the
        caller — note this is the SERVICE's ``ConnectionError``, imported above, NOT the
        builtin, so it does not cover ``ConnectionResetError`` & co.); any ``ValueError``,
        which covers every envelope/record-shape fault the registry and tenant reads can
        produce (``MalformedAgentRecordError``, ``UnknownRegistryStatusError`` and pydantic's
        ``ValidationError`` are all ``ValueError`` subclasses — stated as ``ValueError`` so a
        future registry error class needs no edit here); and ``ClientError`` /
        ``BotoCoreError``, because ``AgentRegistryService._hydrate`` deliberately RE-RAISES
        every non-``ResourceNotFound`` ``ClientError`` (a throttle or an IAM drift on
        ``GetRegistryRecord``) and a transport fault surfaces as ``BotoCoreError``.

        Anything else is a programming error and is left to propagate: ``TypeError`` /
        ``AttributeError`` / ``KeyError`` are NOT ``ValueError`` subclasses, so a signature
        drift still surfaces as the bug it is. A bare ``except Exception`` would swallow those
        and launder an AGP bug into a plausible ``failed`` record an operator would retry.

        BOUNDED in-flight guard: a repo already at ``cicd_status="promoting"`` refuses a second
        promote, so two CodeBuild runs cannot race the same Terraform state key. The refusal is
        bounded by ``_PROMOTE_IN_FLIGHT_MINUTES`` (the runtime CodeBuild project's own
        ``build_timeout``) measured from ``last_promoted_at``: past that window the build
        cannot still be running, so the record is STUCK rather than in flight and a retry is
        allowed. Without the bound, a record stranded at ``promoting`` (the buildspec's status
        write is best-effort and no-ops on an unset ``REPO_SK``, and there is no reconciler)
        would become PERMANENTLY unpromotable — a worse failure than the double deploy.

        Raises ``ProjectError`` with kind ``not_found`` (unknown repo, or one belonging to a
        different project), ``no_prod_candidate`` (nothing has merged to ``main`` since the last
        promotion), ``promote_in_flight`` (a promotion started inside the window), or
        ``promote_failed`` (the build could not be started). ``no_dev_build`` is no longer
        raised here — it is retained on ``ProjectError`` (and mapped by the route) so pre-E27A
        callers and stored expectations behave unchanged."""
        # 1) Resolve the repo — unknown, or belonging to another project, is a 404. See
        #    `_resolve_delivery_repo` for why the ownership check is the load-bearing half.
        repo = self._resolve_delivery_repo(project_id, repo_id)

        # 2) Resolve the tag BEFORE mutating anything — the PROD CANDIDATE (E27A/T5), i.e. what
        #    merged to `main` and was therefore reviewed, NOT `last_dev_image_tag` (whatever
        #    last landed on dev). Absent ⇒ nothing has merged to `main` since the last
        #    promotion, so there is nothing to approve.
        #
        #    Treated as FALSY-or-missing, never `is None` (E27/T7's reasoning, carried over
        #    intact): a pre-E27A row has the field unset, and a partial write could leave it
        #    empty. Either way there is no image to promote — promoting an empty tag would
        #    deploy `<ecr_repo>:` to PRODUCTION.
        tag = repo.prod_candidate_image_tag
        if not tag:
            raise ProjectError("no prod candidate to promote", kind="no_prod_candidate")
        # E28B/T4 (D-B3) — the APPROVED DIGEST, resolved here and passed to the deploy verbatim.
        # It is read off the record and NEVER re-derived from the tag: a fresh registry lookup at
        # deploy time would return whatever the (mutable) tag points at NOW, which may not be the
        # bytes this approval attested to. Absent ⇒ a pre-E28B / tag-only candidate, which still
        # promotes over the legacy tag path rather than being refused — refusing would strand
        # every candidate registered before this epic.
        candidate_digest = repo.prod_candidate_digest
        # Captured HERE, alongside the tag, for the E28/T4 delivery record: the compare-and-clear
        # in step 5 can overwrite the candidate block with a NEWER merge's fields, and the row must
        # carry the sha of the artifact this promote actually shipped.
        candidate_sha = repo.prod_candidate_sha

        # 2b) Refuse a CONCURRENT promote — two builds would race the same TF state key — but
        #     only while the first can still plausibly be running (see the docstring).
        if self._promotion_in_flight(repo):
            raise ProjectError("a promotion is already in flight", kind="promote_in_flight")

        # 3) Resolve the parent project for its connection (the build service clones the org's
        #    private infra repo through it) and assert a build service is wired.
        project = self._resolve_delivery_context(
            project_id, repo_id, verb="promote", noun="promotion", kind="promote_failed"
        )

        # 4) Start the prod deploy. `ecr_repo` is only the platform FALLBACK — the build
        #    service prefers the tenant stage's own ecr_repo_uri.
        try:
            build_id = self._builds.start_runtime_build(
                agent_id=repo.agent_id,
                image_tag=tag,
                ecr_repo=self._ecr_repository,
                connection_id=project.connection_id,
                stage="prod",
                image_digest=candidate_digest,
            )
        except (RuntimeBuildError, ConnectionError, ValueError, ClientError, BotoCoreError):
            # EVERY failure to start the deploy is one 502-with-attribution. Enumerated, NOT
            # a bare `except Exception`: a TypeError/AttributeError/KeyError is an AGP bug
            # (none of them is a ValueError subclass) and must surface as one rather than be
            # recorded as a failed promotion.
            #
            # The five-move epilogue (log traceback → attribute → `failed` → best-effort persist
            # → terminal FAILED row) is rollback's too, so it lives in one place; see
            # `_record_failed_delivery` for why each move is ordered as it is. `stamp=True`
            # unconditionally — promote only ever ships prod, so there is no non-prod case whose
            # prod cache must be left alone.
            #
            # `from None` stays HERE, at the throw: it suppresses the build service's own message
            # from the exception chain, and that suppression must be visible where the exception
            # is actually raised rather than buried in the helper.
            raise self._record_failed_delivery(
                repo,
                noun="promotion",
                kind="promote_failed",
                stage="prod",
                image_tag=tag,
                actor=promoted_by,
                stamp=True,
            ) from None

        # 5) Success: the record is IN FLIGHT to prod until the build reports back, and the
        #    candidate is CONSUMED — the approval it was waiting for has happened. Note the
        #    asymmetry with the failure path above, which deliberately leaves it intact: a
        #    failed promote must stay RETRYABLE, and clearing it there would turn a transient
        #    CodeBuild fault into a permanent 409 recoverable only by pushing a new commit to
        #    `main`. Both flow through the SAME `include_prod_candidate` opt-in, so the cleared
        #    values actually reach the store (a save without it would leave the row `pending`
        #    while the returned object read clear — T7's clobber class, mirrored).
        # E28A/T6 — the tag is PROVISIONAL here. `start_runtime_build` returning proves only that
        # CodeBuild accepted the build; `last_promoted_image_tag` means "what prod SERVES", so it
        # is written only for a tag prod has demonstrably run. `_has_succeeded` is the EXISTING
        # rollback gate, reused rather than reimplemented: it reads the repo+stage slice of the
        # append-only partition, so neither another repo's nor a dev-only success can satisfy it.
        # For a first-time promote this is False, which is the honest answer — what was ATTEMPTED
        # is the delivery row appended below, and the FE derives "is prod serving?" by joining
        # `last_promotion_build_id` to that row, not by trusting the scalar.
        self._stamp_promotion(
            repo,
            promoted_by=promoted_by,
            tag=tag,
            build_id=build_id,
            serving=self._has_succeeded(repo_id, "prod", tag),
            digest=candidate_digest,
        )
        repo.cicd_status = "promoting"
        # COMPARE-AND-CLEAR: `start_runtime_build` above blocked for seconds, so re-read and
        # consume the candidate ONLY while the stored one is still what we just shipped. A merge
        # to `main` landing inside that window registered a NEWER candidate, and clearing off
        # our pre-build snapshot would erase it silently — the Promote button goes quiet and
        # nobody is told a merge is waiting. Leaving it costs nothing: the in-flight guard
        # already refuses a second deploy until this one settles.
        fresh = self._get_repo(repo_id)
        # The DIGEST is part of the identity comparison (E28B/T4), not decoration. The tag is the
        # git TREE sha, so a rebuild of an unchanged tree produces the SAME tag with DIFFERENT
        # bytes — and the build is not reproducible (floating base image, ranged deps, no
        # lockfile), so that is the expected outcome rather than an edge case. Comparing only
        # tag+sha would read such a rebuild as "still the candidate we shipped" and CLEAR it,
        # silently discarding a merge-triggered rebuild an OWNER never got to approve.
        superseded = fresh is not None and (
            fresh.prod_candidate_image_tag != tag
            or fresh.prod_candidate_digest != candidate_digest
            or fresh.prod_candidate_sha != repo.prod_candidate_sha
        )
        if superseded:
            # Carry the newer candidate onto the record we are about to save, so the audit stamp
            # and the `promoting` transition land WITHOUT rewriting the candidate block.
            for field in _PROD_CANDIDATE_FIELDS:
                setattr(repo, field, getattr(fresh, field))
        else:
            self._clear_prod_candidate(repo)
        self._save_repo(  # the promoting transition IS the point
            repo, include_cicd_status=True, include_prod_candidate=True
        )
        # E28/T4 (D7) — APPEND the delivery record. This is the write the `last_promoted_*` stamp
        # above cannot be: those four scalars are overwritten by the NEXT promote, so after two
        # releases they remember only the second and the first is gone. Two promotes must leave
        # two rows. `source_sha` is read from the CANDIDATE we just shipped (the sha the OIDC
        # candidate route proved), which is why it is captured before the compare-and-clear
        # decision above could have replaced it with a newer merge's.
        self._append_delivery(
            repo,
            stage="prod",
            image_tag=tag,
            build_id=build_id,
            actor=promoted_by,
            source_sha=candidate_sha,
        )
        return repo

    def _promotion_in_flight(self, repo: Repository) -> bool:
        """Is a promotion GENUINELY still running for ``repo`` (E27/T8)?

        ``cicd_status == "promoting"`` alone is not enough: nothing guarantees the status is
        ever cleared (the buildspec's write is best-effort), so treating it as a permanent
        block would strand the repo. The status counts as in-flight only for
        ``_PROMOTE_IN_FLIGHT_MINUTES`` after ``last_promoted_at`` — the CodeBuild project's own
        build timeout, past which the build is necessarily over. An unparseable/absent
        timestamp is treated as NOT in flight, i.e. the guard fails OPEN: the cost of a
        double promote (a spurious ``failed``, same image tag either way) is far lower than a
        repo that can never be promoted again."""
        if repo.cicd_status != "promoting":
            return False
        started_at = repo.last_promoted_at
        if not started_at:
            return False
        try:
            started = datetime.fromisoformat(started_at)
        except ValueError:
            logger.warning(
                "[project] repo %s has an unparseable last_promoted_at; not blocking promote",
                repo.id,
            )
            return False
        if started.tzinfo is None:  # pragma: no cover — every writer stamps an aware isoformat
            started = started.replace(tzinfo=timezone.utc)
        # Only a delta INSIDE [0, window) blocks. The lower clamp matters: a FUTURE-dated
        # timestamp (clock skew on the writing task, a restored/migrated row, a hand edit)
        # makes the delta negative, and a negative delta is always `< window` — which would
        # refuse the promote until wall-clock caught up, i.e. the exact "permanently
        # unpromotable" outcome the bound exists to prevent. A future stamp is treated as
        # stale, consistent with the fail-open asymmetry applied to every other bad input.
        delta = self._now() - started
        return timedelta(0) <= delta < timedelta(minutes=_PROMOTE_IN_FLIGHT_MINUTES)

    def _stamp_promotion(
        self,
        repo: Repository,
        *,
        promoted_by: str,
        tag: str,
        build_id: Optional[str],
        serving: bool = True,
        digest: Optional[str] = None,
    ) -> None:
        """Write the promotion-audit fields onto ``repo`` (E27/T8, narrowed by E28A/T6). Applied
        on BOTH the success and the failure path so an attempted promotion is always attributed.

        Three of the four are ALWAYS written, because all three are true the instant an OWNER's
        authorization is accepted: WHO authorized it, WHEN, and WHICH build carries it. In
        particular ``last_promoted_at`` is :meth:`_promotion_in_flight`'s own clock — deferring it
        to the build's terminal path would disarm the concurrency guard for the whole build
        duration and let a second delivery race the same Terraform state key.

        ``serving=False`` skips ONLY ``last_promoted_image_tag``. That field means "the tag prod
        SERVES", not "the tag we last tried": :meth:`promote_repo` stamps the moment
        ``start_runtime_build`` returns, which proves only that CodeBuild ACCEPTED the build — the
        ``terraform apply`` can fail minutes later, and it did live, leaving the record claiming
        prod served an image it had never run while the Deployments tab correctly showed nothing
        serving. What was ATTEMPTED is not lost: it is the delivery row appended by the same
        caller. Callers that pass ``serving=True`` are asserting the tag is one prod has
        demonstrably run — a prod ROLLBACK (whose target :meth:`_has_succeeded` already proved) is
        the case that must keep repointing this cache, since prod genuinely serves the older image
        and leaving the cache on the newer tag would make every list row lie."""
        ts = self._now().isoformat()
        repo.last_promoted_by = promoted_by
        repo.last_promoted_at = ts
        if serving:
            repo.last_promoted_image_tag = tag
            # The digest rides the SAME `serving` gate as the tag (E28B/T4), never its own: both
            # answer one question — "what does prod serve?" — and splitting the gate would let the
            # pair disagree, which is strictly worse than both being absent. A caller asserting
            # `serving` has proved prod ran these bytes; `digest=None` (a legacy tag-only
            # promotion) leaves the field untouched rather than blanking a known-good digest.
            if digest:
                repo.last_promoted_digest = digest
        repo.last_promotion_build_id = build_id
        repo.updated_at = ts

    def record_prod_candidate(
        self,
        agent_id: str,
        *,
        image_tag: str,
        sha: str,
        actor: str,
        image_digest: str = "",
    ) -> Repository:
        """Register/overwrite the repo's single prod candidate (E27A/T5).

        Called by ``POST /builds/prod-candidate`` after a merge to ``main``. Every argument
        comes from the VALIDATED GitHub-OIDC token, never from the request body — the
        attribution rule E27 established: a body-asserted actor would let any holder of the
        build credential claim someone else's merge.

        Resolved by ``agent_id`` because the OIDC path has NO project context: the token proves
        a repository and the route has already bound it to the agent it claims. Raises
        ``ProjectError(kind="not_found")`` when no repository owns ``agent_id``, writing
        NOTHING (an unknown agent must not invent a row).

        ``image_digest`` (E28B/T4, D-B3) is THE APPROVABLE VALUE — the exact bytes a later
        ``promote_repo`` deploys. It defaults to ``""`` rather than being required so a pre-E28B
        caller (and the still-live ``POST /builds/prod-candidate`` route, which has no digest in
        scope) keeps working unchanged; a candidate with no digest is registered tag-only and
        promotes over the legacy tag path. Both are stored TOGETHER — see
        :data:`_PROD_CANDIDATE_FIELDS` on why a half-populated candidate is not a state anything
        may act on.

        Overwrites all six fields UNCONDITIONALLY and sets ``prod_candidate_status="pending"``.
        There is no queue and no history: ``main``'s HEAD is the only prod candidate, so a newer
        merge simply replaces the older one and there is no expiry, no "behind by N", and no
        decline-vs-supersede semantics to model (design §4 — the complexity E27 §7 rejected).

        The save is TARGETED (:meth:`_save_prod_candidate`) — the five fields plus
        ``updated_at``, nothing else. It deliberately does NOT go through :meth:`_save_repo`:
        that SETs the whole record minus another writer's attributes, and ``last_promoted_*``
        is in no skip set, so this method's stale read (it resolves by SCANNING the partition)
        would revert a promotion stamped in the meantime — defeating the in-flight guard, which
        is measured from ``last_promoted_at``. It also leaves ``cicd_status`` alone: registering
        a candidate is a merge to ``main``, not a deployment, so the badge must keep reading
        whatever dev last did."""
        repo = self.find_repository_by_agent_id(agent_id)
        if repo is None:
            raise ProjectError("Repository not found", kind="not_found")
        ts = self._now().isoformat()
        repo.prod_candidate_image_tag = image_tag
        # Normalized to None, never "": the promote path tests this field for truthiness to decide
        # whether it has an approved digest to deploy, and an empty string is a present-but-unusable
        # value that would reach the buildspec as `<repo>@` — the E28A/T1b rule (never deploy
        # something you could not name) applied one layer earlier.
        repo.prod_candidate_digest = image_digest or None
        repo.prod_candidate_sha = sha
        repo.prod_candidate_actor = actor
        repo.prod_candidate_at = ts
        repo.prod_candidate_status = "pending"
        repo.updated_at = ts
        self._save_prod_candidate(repo)
        logger.info(
            "[project] prod candidate registered for repo %s (agent %s, tag %s)",
            repo.id, agent_id, image_tag,
        )
        return repo

    @staticmethod
    def _clear_prod_candidate(repo: Repository) -> None:
        """Consume the prod candidate on ``repo`` (E27A/T5) — all five fields back to ``None``.

        Cleared as ONE unit, because a half-cleared candidate is a state nothing can act on:
        the promote guard reads only the tag, so a leftover ``pending`` status or actor would
        show a candidate in the UI that the route refuses. Applied ONLY on a successful promote
        (the approval consumed it); the caller must pair it with a save that opts in via
        ``include_prod_candidate``, or the cleared values never reach the store."""
        for field in _PROD_CANDIDATE_FIELDS:
            setattr(repo, field, None)

    def _append_delivery(
        self,
        repo: Repository,
        *,
        stage: str,
        image_tag: str,
        build_id: Optional[str],
        actor: str,
        outcome: DeploymentOutcome = DeploymentOutcome.STARTED,
        source_sha: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Append ONE ``Deployment`` row for an AGP-initiated delivery (E28/T4, D7).

        BEST-EFFORT by design: the deploy has already been decided (started, or definitively
        refused by the build service) by the time this runs, so a store fault here must not
        convert a 202 into a 500 or replace the promotion's own curated 502 with an unrelated
        one. A missing history row is a gap in a list; a raised error here would be a lie about
        what happened to production.

        ``actor_kind`` is pinned to ``"entra"`` because every caller of this helper is an
        AGP-principal action (promote / rollback) attributed to an Entra oid. The OIDC build path
        appends its own row with ``actor_kind="github"`` — a GitHub login and an Entra oid are two
        different currencies (C1) and this helper must never be reused to launder one as the
        other.

        A terminal ``outcome`` is appended already CLOSED (``completed_at``). The partition is
        append-only (C1): nothing ever updates a row, so there is no ``started`` row to close in
        place and a terminal state is expressed as its own complete row."""
        ts = self._now().isoformat()
        try:
            self.append_deployment(
                repo_id=repo.id,
                agent_id=repo.agent_id,
                stage=stage,
                image_tag=image_tag,
                source_sha=source_sha,
                build_id=build_id,
                outcome=outcome,
                actor=actor,
                actor_kind="entra",
                completed_at=ts if outcome is not DeploymentOutcome.STARTED else None,
                error=error,
            )
        except (ClientError, BotoCoreError):
            logger.exception(
                "[project] could not append the deployment record for repo %s (stage %s, tag %s)",
                repo.id, stage, image_tag,
            )

    def _resolve_delivery_repo(self, project_id: str, repo_id: str) -> Repository:
        """Resolve the repo an AGP-initiated delivery (promote / rollback) names — the FIRST move
        of both methods, shared verbatim.

        The project-ownership check is the load-bearing half, not the lookup: without it an OWNER
        of project A could promote or roll back a repo that lives under project B. Both facts
        collapse to the SAME ``not_found`` — a repo under another project must not be
        distinguishable from one that does not exist, or the 404 becomes an enumeration oracle
        for other tenants' repo ids."""
        repo = self._get_repo(repo_id)
        if repo is None or repo.project_id != project_id:
            raise ProjectError("Repository not found", kind="not_found")
        return repo

    def _resolve_delivery_context(
        self, project_id: str, repo_id: str, *, verb: str, noun: str, kind: str
    ) -> Project:
        """Resolve the parent project and assert a build service is wired — the LAST resolve move
        of both promote and rollback, shared verbatim.

        Deliberately NOT fused with :meth:`_resolve_delivery_repo` into one top-of-method call,
        even though the two runs are adjacent in the source. Each method interposes its OWN
        validation between them — promote's ``no_prod_candidate`` + in-flight guard, rollback's
        ``unknown_rollback_target`` + in-flight guard — and that ORDER is observable: a caller
        hitting a misconfigured platform *and* an unpromotable repo must still be told the
        ordinary 409 ("nothing has merged to promote") rather than the 502 a fused resolve would
        surface first. The seam is therefore two helpers, matching the two contiguous runs, not
        one convenient wrapper that quietly re-ranks which refusal wins.

        The project is read for its ``connection_id`` — the build service clones the org's private
        infra repo through it. A missing build service is a MISCONFIGURATION, not a caller error,
        and is refused HERE, before any state change: nothing is stamped, nothing is persisted,
        and no delivery row is appended, so the record stays exactly as it was and the operator
        can retry once the service is wired."""
        project = self._get_project(project_id)
        if project is None:  # pragma: no cover — the repo's parent should still exist.
            raise ProjectError("Repository not found", kind="not_found")
        if self._builds is None:
            logger.error(
                "[project] %s refused for repo %s: no runtime build service", verb, repo_id
            )
            raise ProjectError(f"{noun} failed", kind=kind)
        return project

    def _record_failed_delivery(
        self,
        repo: Repository,
        *,
        noun: str,
        kind: str,
        stage: str,
        image_tag: str,
        actor: str,
        stamp: bool,
    ) -> ProjectError:
        """The FAILURE EPILOGUE shared by promote and rollback: a delivery the build service
        refused to start is recorded, persisted and attributed BEFORE the caller raises.

        Five moves, in this order, and the order is the contract:

        1. Log the TRACEBACK only — never the exception VALUE, which may carry provider detail.
        2. ATTRIBUTE the attempt (``_stamp_promotion`` with ``build_id=None``): who tried to
           deliver what is audit data whether or not the deploy started. Gated by ``stamp``
           because rollback stamps the prod cache only for ``stage == "prod"`` — repointing the
           tag/actor/build-id trio from a ``uat`` rollback would corrupt a PROD fact with
           non-prod data. Promote passes ``stamp=True`` unconditionally: it only ever ships prod.
        3. ``cicd_status="failed"`` for EVERY stage regardless of ``stamp`` — a delivery that
           never started must not leave the record reading ``promoting``, or the bounded
           in-flight guard would refuse retries for its full window over a build that does not
           exist.
        4. BEST-EFFORT persist. A store fault here must not REPLACE the curated 502 with an
           unmapped 500: the failure the operator needs to see is the delivery's, not the audit
           write's (a correlated pair during a regional event, not a coincidence). The SUCCESS
           paths deliberately do NOT do this — there the build HAS started and a save failure
           must surface.
        5. Append a terminal ``FAILED`` delivery row (E28/T4). A delivery that could not START is
           still an ATTEMPT; without the row the history cannot distinguish "the deploy failed"
           from "nobody ever tried", which is the one thing an operator reads this surface for.
           Appended CLOSED rather than opened-and-later-updated, because the partition is
           append-only by contract (C1) and nothing ever revisits a row.

        RETURNS the ``ProjectError`` instead of raising it, so the ``raise ... from None`` stays
        visible at each call site. That is not a style preference: ``from None`` suppresses the
        build service's own message from the exception chain, and burying it in a helper would
        make the suppression — a security property of this path — invisible where the exception
        is actually thrown."""
        logger.exception(
            "[project] %s build failed for repo %s (agent %s, tag %s, stage %s)",
            noun, repo.id, repo.agent_id, image_tag, stage,
        )
        if stamp:
            self._stamp_promotion(repo, promoted_by=actor, tag=image_tag, build_id=None)
        repo.cicd_status = "failed"
        try:
            self._save_repo(repo, include_cicd_status=True)
        except (ClientError, BotoCoreError):
            logger.exception(
                "[project] could not persist the failed %s for repo %s", noun, repo.id
            )
        self._append_delivery(
            repo,
            stage=stage,
            image_tag=image_tag,
            build_id=None,
            actor=actor,
            outcome=DeploymentOutcome.FAILED,
            error=f"failed to start the {noun} build",
        )
        return ProjectError(f"{noun} failed", kind=kind)

    def rollback_repo(
        self, project_id: str, repo_id: str, *, image_tag: str, rolled_back_by: str,
        stage: str = "prod",
    ) -> Repository:
        """Redeploy a PREVIOUSLY-SUCCEEDED image tag to ``stage`` (E28/T4, D5+D7).

        The counterpart to :meth:`promote_repo`, and structurally impossible before E28/T3: the
        ``last_promoted_*`` scalars only ever remember the LATEST release, so there was no record
        of an earlier artifact to roll back TO. The append-only ``Deployment`` partition is that
        record, and this method's whole safety argument rests on it.

        **THE VALIDATION IS THE FEATURE.** Unlike promote — which accepts no tag at all and
        resolves it server-side — a rollback must take a target, and an unvalidated target would
        make this route a deploy-anything primitive: any tag a caller could name would be applied
        to production. So ``image_tag`` is accepted ONLY if it has a ``SUCCEEDED``
        :class:`~models.deployment.Deployment` row **for this repo, in this stage**. Each clause
        of that is load-bearing:

        * **This repo** — the tenant ECR registry is SHARED by every materialized agent
          (``modules/agent_ecr``), so another repo's tag names a real, pullable image. A
          repo-unscoped check would make repo B's artifact deployable over repo A's runtime.
        * **This stage** — an artifact proven good in ``dev`` is not thereby approved for
          ``prod``; accepting it would route a never-reviewed dev build into production through
          the rollback door, which is exactly the narrowing E27A made to promote.
        * **``SUCCEEDED``** — ``started`` means "we asked", ``failed`` means "it did not run".
          Neither is evidence the image ever served traffic, so neither is a rollback target.

        ``stage`` is free-form (D8) and defaults to ``"prod"``. It needs NO allowlist of its own:
        an unknown stage simply has no succeeded rows and is refused by the check above. The
        ``("dev","prod")`` allowlist on the OIDC ``/builds/runtime`` route is a DIFFERENT trust
        boundary (a GitHub token with no human principal) and is deliberately untouched.

        ``rolled_back_by`` is the validated principal's identity, never a body value — the same
        attribution rule as ``promoted_by``.

        **The ``last_promoted_*`` cache after a PROD rollback: it is REPOINTED at the rolled-back
        tag.** Those four fields are the denormalized "what is live in prod" cache the list row
        reads without a second query (D7), and after a rollback prod genuinely serves the older
        image — so leaving them on the newer tag would make every list row and every "prod
        version" badge state something false, which is worse on this surface than losing the
        newer tag from the cache (the append-only history still has it, and now has the rollback
        row too). A NON-prod rollback leaves the tag/actor/build-id trio alone: those three describe
        the prod delivery specifically, and repointing them from a ``uat`` rollback would corrupt a
        prod fact with non-prod data. It DOES stamp ``last_promoted_at`` and ``cicd_status``,
        because those two are the in-flight guard's own state, not the prod cache — see Concurrency.

        **The prod candidate is NOT consumed.** A rollback is not an approval of ``main`` — the
        pending candidate must survive so the OWNER can still promote the fix once the incident is
        over. Clearing it would make that fix unreachable without pushing a fresh commit.

        **Concurrency** follows promote's precedent rather than inventing a second mechanism: the
        SAME bounded :meth:`_promotion_in_flight` guard refuses while a delivery is in flight, so
        two CodeBuild runs cannot race the same stage-scoped Terraform state key — in EITHER
        order, because a rollback also stamps the in-flight state that promote refuses on. The
        bound is what keeps a stuck record recoverable instead of permanently un-rollbackable.

        The guard arms for EVERY stage. An earlier revision gated both stamps on ``prod``, which
        left non-prod entirely unguarded — two sequential ``uat`` rollbacks both started builds
        against the same state key. Since ``cicd_status`` is a single per-repo field, the guard is
        repo-wide rather than per-stage: a ``uat`` rollback in flight also blocks a ``prod`` one.
        That is deliberate and is the safe direction (refuse, not race); a genuinely per-stage lock
        would need per-stage state, which is a design change, not a flag.

        Failure discipline mirrors promote exactly: every input is resolved BEFORE anything is
        mutated; a build failure still ATTRIBUTES the attempt, persists ``cicd_status="failed"``
        and appends a ``FAILED`` row before raising; and the exception set is ENUMERATED (a
        ``TypeError``/``AttributeError``/``KeyError`` is an AGP bug and must surface as one, not be
        laundered into a plausible failed rollback).

        Raises ``ProjectError`` with kind ``not_found`` (unknown repo, or one belonging to a
        different project), ``unknown_rollback_target`` (the tag has no succeeded row for this
        repo+stage — an ordinary 409 state the UI renders), ``promote_in_flight``, or
        ``rollback_failed``."""
        # 1) Resolve the repo. The project-ownership check is the same one promote makes, so it is
        #    the same helper — a rollback must not become the door through which an OWNER of
        #    project A reaches a repo living under project B.
        repo = self._resolve_delivery_repo(project_id, repo_id)

        # 2) VALIDATE the target BEFORE mutating anything. Falsy-not-None for the same reason
        #    promote uses it: an empty tag would deploy `<ecr_repo>:` to production.
        #
        #    A BLANK `stage` is refused here too, not only at the model: empty does not merely name
        #    an unknown stage, it DISABLES the stage scoping — `list_deployments` branches on
        #    `if stage:`, so `""` reads ACROSS stages and would let a dev-only tag validate as a
        #    prod rollback target. The route's `RepoRollbackRequest` already rejects it, but this
        #    method is a public service entrypoint and must not depend on one caller's model.
        if not stage or not stage.strip():
            raise ProjectError("stage must not be blank", kind="unknown_rollback_target")
        if not image_tag or not self._has_succeeded(repo_id, stage, image_tag):
            # The offending tag is logged (operator diagnostics) but NEVER echoed to the caller.
            logger.warning(
                "[project] rollback refused for repo %s: no succeeded %s deployment for tag %s",
                repo_id, stage, image_tag,
            )
            raise ProjectError(
                "no such succeeded deployment", kind="unknown_rollback_target"
            )

        # 3) Refuse a CONCURRENT delivery — two builds would race the same stage-scoped TF state
        #    key. Promote's OWN bounded guard, reused rather than reimplemented.
        if self._promotion_in_flight(repo):
            raise ProjectError("a promotion is already in flight", kind="promote_in_flight")

        # Promote's own resolve, reused: the parent project (for its connection) plus the
        # build-service assert, refused BEFORE any state change.
        project = self._resolve_delivery_context(
            project_id, repo_id, verb="rollback", noun="rollback", kind="rollback_failed"
        )

        # 4) Start the deploy of the OLD artifact. Same call promote makes — there is one deploy
        #    path, so a rollback cannot diverge from a promote in what it actually provisions.
        try:
            build_id = self._builds.start_runtime_build(
                agent_id=repo.agent_id,
                image_tag=image_tag,
                ecr_repo=self._ecr_repository,
                connection_id=project.connection_id,
                stage=stage,
            )
        except (RuntimeBuildError, ConnectionError, ValueError, ClientError, BotoCoreError):
            # Enumerated, not `except Exception` — see promote_repo for the full argument.
            #
            # Promote's epilogue, shared. `stamp` carries the ONE difference between the two:
            # only a PROD rollback repoints the tag/actor/build-id trio, because those three
            # describe the prod delivery specifically and stamping them from a `uat` rollback
            # would corrupt a prod fact with non-prod data. `cicd_status="failed"` is persisted
            # for every stage regardless (see the success path for why the guard must arm beyond
            # prod): a non-prod rollback that never started must not leave the record reading
            # `promoting`, or the bounded guard would refuse retries for its full window over a
            # build that does not exist.
            raise self._record_failed_delivery(
                repo,
                noun="rollback",
                kind="rollback_failed",
                stage=stage,
                image_tag=image_tag,
                actor=rolled_back_by,
                stamp=stage == "prod",
            ) from None

        # 5) Success. For prod, REPOINT the denormalized cache at what prod now serves (see the
        #    docstring) and go in-flight. The prod candidate is deliberately left INTACT — a
        #    rollback is not an approval of `main` — so this save does NOT opt into
        #    `include_prod_candidate`.
        if stage == "prod":
            self._stamp_promotion(
                repo, promoted_by=rolled_back_by, tag=image_tag, build_id=build_id
            )
            # E28B/T4 — a rollback CANNOT name its target's digest, so it must not keep the old
            # one. A rollback target is validated against succeeded ``Deployment`` rows, and those
            # rows record a TAG only (contract C1 — no digest field), so nothing here knows which
            # bytes the older tag resolves to. Leaving `last_promoted_digest` on the newer release
            # would leave the two prod scalars DISAGREEING — tag pointing at the rolled-back image,
            # digest at the one we just rolled away from — and the digest is the field a later
            # approval would trust. Cleared to None (honestly unknown) and re-populated by the
            # buildspec, which is the only writer that knows what the apply actually deployed.
            repo.last_promoted_digest = None
        else:
            # ARM THE IN-FLIGHT GUARD FOR EVERY STAGE, not just prod. `_promotion_in_flight` reads
            # `cicd_status == "promoting"` AND `last_promoted_at` (its clock), so gating BOTH stamps
            # on prod left non-prod completely unguarded: two sequential `uat` rollbacks both
            # reached `start_runtime_build` and raced the same stage-scoped Terraform state key —
            # the exact failure the guard exists to prevent, and the reason my T4b claim that
            # rollback is "serialized in either order" was true only for prod.
            #
            # Only the TIMESTAMP is stamped here, never the tag/actor/build-id trio: those three
            # are the prod delivery cache the list row reads, and repointing them from a `uat`
            # rollback would corrupt a prod fact with non-prod data. `last_promoted_at` is
            # therefore doing double duty as "when the last delivery started" — which is already
            # how the guard uses it, and how promote's own failure path writes it.
            repo.last_promoted_at = self._now().isoformat()
            repo.updated_at = repo.last_promoted_at
        repo.cicd_status = "promoting"
        self._save_repo(repo, include_cicd_status=True)
        self._append_delivery(
            repo,
            stage=stage,
            image_tag=image_tag,
            build_id=build_id,
            actor=rolled_back_by,
        )
        logger.info(
            "[project] rollback started for repo %s (stage %s, tag %s, build %s)",
            repo_id, stage, image_tag, build_id,
        )
        return repo

    def _has_succeeded(self, repo_id: str, stage: str, image_tag: str) -> bool:
        """Has ``image_tag`` ever SUCCEEDED for this repo in this stage (E28/T4)?

        The rollback target check. Reads the repo+stage slice of the append-only partition —
        which is scoped by the sk prefix, so another repo's or another stage's succeeded row can
        never satisfy it — and looks for a ``SUCCEEDED`` outcome on the exact tag.

        ``_ROLLBACK_HISTORY_LIMIT`` bounds the read. It is a REAL limit: an artifact that last
        succeeded further back than that many deployments ago is not offered as a rollback
        target. That direction is the safe one (a refusal, not an unverified deploy), and the
        UI's own history list is bounded the same way — a target the operator cannot see listed
        is not one they are asking for.

        Fails CLOSED: :meth:`list_deployments` degrades to ``[]`` on an unreachable store, and
        an empty history here means REFUSE. "I cannot read the history" and "this tag never
        succeeded" are the same value to a degrading read but opposite answers to a
        deploy-to-production question, so the ambiguity must resolve to no deploy.

        A BLANK ``stage`` is refused rather than passed through, because
        :meth:`list_deployments` branches on ``if stage:`` — an empty string would read ACROSS
        stages and let a ``dev``-only tag answer True for a ``prod`` rollback. The caller checks
        this too; it is repeated here so the PRIMITIVE cannot be misused by a future caller."""
        if not stage or not stage.strip():
            return False
        return any(
            d.image_tag == image_tag and d.outcome is DeploymentOutcome.SUCCEEDED
            for d in self.list_deployments(repo_id, stage=stage, limit=_ROLLBACK_HISTORY_LIMIT)
        )

    def _materialize_runners(self, repo_id: str, pending: dict) -> Dict[str, Callable[[], None]]:
        """Build the per-step work closures for :meth:`run_materialize` (E25C/T2, rewritten in
        E28B/T3 — D-B2/D-B5).

        One closure per :data:`MATERIALIZE_STEPS` key: ``mint_identity`` → ``create_repo`` →
        ``push_template`` → ``set_repo_vars`` → ``finalize``. The connection token is read FRESH
        here (never stashed) and forwarded straight to the write client — never logged, never
        returned.

        WHAT CHANGED, AND WHY IT IS A REDESIGN RATHER THAN A FIFTH FIX. This method used to make
        SIX writes to a brand-new repository: ``generate_from_template`` (which GitHub answers 201
        to and then copies the template in a SEPARATE internal push landing seconds later),
        ``create_branch`` (a ref creation — fires a build), ``commit_file`` (a tree write — fires a
        build), ``set_repo_variables``, ``create_environment`` ×2 and ``set_environment_variables``.
        Three writers touched one tree with no coordination: GitHub's async copy, AGP's branch cut,
        AGP's config commit. Four live defects came out of that, and all four "fixes" only changed
        which writer won — the last one traded a wasted `main` build for two dev builds 11 seconds
        apart racing one terraform state lock, where the build carrying the operator's config LOST
        and failed, the build carrying the bare template won, and the record still read
        ``cicd_status: deployed``.

        There are now TWO writes and only one that touches the tree: create the repo, then push the
        whole template in ONE commit (``RepoProvider.commit_files``) whose message carries
        ``[skip ci]``. Nothing is left to race — not because the ordering is finally right, but
        because there is no second writer. ``_wait_for_template_copy`` stops being load-bearing
        (AGP pushes the bytes itself), the `dev`/`main` literals are gone (the trunk is project
        config — D-B5), and both GitHub Environments are gone (a GitHub-only concept).

        ``agent.config.json`` is no longer written at all. The runtime never read it: the buildspec
        takes ``AGENT_NAME``/``MODEL_ID`` from the governed registry record
        (``get-registry-record`` → ``jq -r '.name'``). It was a decorative copy of state that lives
        elsewhere, and four findings existed only to keep that copy on the right branch."""
        agent = pending["agent"]
        name = pending["name"]
        connection_id = pending["connection_id"]
        template_name = pending["template_name"]
        repo_overrides = pending["repo_overrides"]
        tenant_id = pending["tenant_id"]
        # D-B5: the branch the template lands on. Read from the PROJECT (never a literal here);
        # ``add_repo``/``retry_materialize`` resolve it, and ``Project.trunk_branch`` defaults it
        # for every stored pre-E28B project.
        trunk = pending["trunk_branch"]

        connection = self._conn.get_connection(connection_id)
        token = self._conn.get_bearer_token(connection_id)
        # Carries mutable per-run state (the created repo_url) between step closures.
        state: Dict[str, str] = {}

        def mint_identity() -> None:
            # Identity ONLY (no runtime authorizer, safe with agent_arn=None).
            self._provision_identity(agent)

        def create_repo() -> None:
            # An EMPTY repository, via the portable seam (``RepoProvider.create_repo``) rather than
            # GitHub's ``/generate``. That is the whole point: ``/generate`` copies the template in
            # an async internal push AGP cannot order itself against, so the platform's own setup
            # writes were indistinguishable from — and raced — a provider-issued one. Creating an
            # empty repo and pushing the content ourselves (``push_template``) removes the other
            # writer instead of trying to out-order it. ``/generate`` may return later as a pure
            # optimization; it must never be the mechanism.
            #
            # Idempotent: an already-existing repo is a benign re-run (a retried materialize must
            # converge), so the url is read back rather than raised over.
            repo_url = self._rollout.create_repo(
                connection.org, name, private=True, token=token, base_url=connection.base_url
            )
            state["repo_url"] = repo_url
            # Persist the url onto the record NOW (E25C/T2 resume): a later resumed run that
            # skips this done step has an empty ``state``, so ``finalize`` must be able to
            # read the url off the persisted record rather than wipe it to None.
            self._persist_repo_url(repo_id, repo_url)

        def push_template() -> None:
            # THE ONLY TREE WRITE MATERIALIZE MAKES. One commit, N files, on the trunk.
            #
            # AGP forwards BYTES and never inspects layout, so a template author may move
            # ``build.yml``, restructure directories or add a workflow and it flows through
            # untouched. Read from disk (``collect_scaffold_files``) — the template registry (T2)
            # is the CATALOG, a pointer store, not the content store.
            #
            # ``[skip ci]`` in the message is load-bearing, not hygiene: one commit means one CI
            # trigger, so one marker suppresses the whole materialize build on every provider, with
            # no branch filters and no ``github.ref`` conditionals to keep correct. That is what
            # replaced the three workflow runs materialize used to burn (two of them wasted on
            # `main`), and what stops a setup push from looking like a developer's real push.
            #
            # Idempotent BY CONTENT: git objects are content-addressed, so re-running with
            # identical files produces the tree already on the branch — no commit is written and
            # the ref does not move. A retried materialize is therefore safe rather than merely
            # tolerable, and it produces no second push event.
            #
            # E28C/T4 (D-C2): WHERE the bytes come from is no longer "disk, always". A template
            # record carrying a structural source is dereferenced to its repo; only a record
            # without one falls back to the seed. The resolved label names which it was, and is
            # stamped on the step BEFORE the push so a failed push still shows what was resolved.
            files, label = self._resolve_template_bytes(
                connection_id, connection, token, template_name
            )
            self._save_repo_step_label(repo_id, "push_template", label)
            self._rollout.commit_files(
                connection.org,
                name,
                files,
                branch=trunk,
                message=_TEMPLATE_PUSH_MESSAGE,
                token=token,
                base_url=connection.base_url,
            )
            # ADOPT THE TRUNK (D-B5). ``create_repo`` auto-inits — a precondition of the push
            # above, since the git-data API 409s on a truly empty repo — and the provider names
            # that seeded branch from the ORG's default-branch setting, which AGP does not choose.
            # So a project whose trunk differs is left with the template on one branch and
            # ``default_branch`` (plus every PR, plus ``build.yml``'s ``branches:`` filter) on
            # ANOTHER: a repo that looks materialized and never builds.
            #
            # Both calls are no-ops when the trunk is already what auto-init produced, so the
            # common case spends nothing. Ordered re-point-then-delete because the provider
            # REFUSES to delete the default branch.
            #
            # NOT a second tree write: neither call creates a commit or changes any file, so
            # nothing here re-introduces a writer that could race the push. It is ref bookkeeping
            # on a repository AGP has just finished writing.
            self._adopt_trunk(connection, name, trunk, token)

        def set_repo_vars() -> None:
            # Class-B layering: platform defaults < project overrides < repo overrides, with
            # AGENT_ID/CONNECTION_ID always stamped last (never overridable).
            #
            # E28B/T3 — THE STAGE-SCOPED KEYS NOW STAY REPO-LEVEL, AND THAT IS DELIBERATE.
            # DO NOT "fix" this back to environment scope.
            #
            # ECR_REPOSITORY / AWS_REGION / AWS_ECR_PUSH_ROLE_ARN used to be POPPED here whenever a
            # tenant service was wired, because ``set_env_vars`` wrote them under per-stage GitHub
            # Environments instead. E28B deletes both ``create_environment`` calls and
            # ``set_environment_variables`` (a GitHub-only concept, and two more writes to a fresh
            # repo), so nothing would write them at all — materialize would report ``ready`` while
            # EVERY build failed with no registry, no region and no push role. That is the same
            # "record says success, reality disagrees" class this epic exists to delete, so the
            # keys move to the repository-wide set rather than disappearing.
            #
            # Repo-level RESOLVES CORRECTLY under the template's ``environment: dev`` jobs: GitHub
            # looks a variable up environment-first and falls back to the repository set, so
            # ``vars.ECR_REPOSITORY`` still resolves with no environment defined at all.
            #
            # ONE stage's credentials is not a limitation, it is the security property. The
            # workflow pushes to the DEV registry and POSTs ``stage: 'dev'``; a prod deploy is AGP
            # copying an already-approved DIGEST into the prod account. GitHub is never trusted
            # with the production account, so prod's push role must never reach CI.
            project_overrides: Dict[str, str] = {}
            effective_vars = {
                **self._repo_vars,
                **project_overrides,
                **(repo_overrides or {}),
                "AGENT_ID": agent.id,
                "CONNECTION_ID": connection_id,
            }
            # A wired tenant service sources the three stage keys from its ``dev`` stage. With
            # NO tenant service (legacy / unwired construction) the platform defaults in
            # ``self._repo_vars`` already carry all three, so there is nothing to do.
            if self._tenants is not None:
                effective_vars.update(_build_stage_ci_vars(self._tenants.get(tenant_id), tenant_id))
            # E31 — THE PER-ORG PUSH ROLE WINS, IN BOTH BRANCHES. DO NOT move this back inside
            # the ``self._tenants is None`` arm.
            #
            # It used to live there only, which made it dead code in production: the ONLY
            # construction site (``api/routes/projects.py``) wires a TenantService
            # UNCONDITIONALLY, so ``self._tenants`` is never None and the ``else`` always won.
            # Concretely, with org A connected and org B connecting second: B's per-org role
            # ``<prefix>-ecr-push-B`` is created and trusted correctly, its ARN is stored on B's
            # connection record — and then never stamped, because the tenant stage's
            # ``push_role_arn`` is the SHARED role, whose trust names org A only. Every build in
            # org B failed at "Configure AWS credentials" with `Not authorized to perform
            # sts:AssumeRoleWithWebIdentity`, while the connection read CONNECTED and materialize
            # read `ready` — the "record says success, reality disagrees" class again.
            #
            # This is also what makes ``ensure_shared_role``'s adopt-don't-retrust defensible:
            # the shared role is now genuinely only the single-org/legacy FALLBACK (stamped when a
            # connection has no per-org role), so leaving its trust as found no longer strands
            # every org but the first.
            #
            # PRECEDENCE, and why this position rather than earlier: it is stamped AFTER the
            # tenant stage update and AFTER the Class-B override layers, so neither a repo
            # override (which must never redirect the push role — see
            # ``test_a_repo_override_cannot_shadow_the_tenants_stage_credentials``) nor the stage
            # config can shadow it. The stage lookup still runs FIRST and unconditionally, so a
            # tenant with no ``dev`` stage still fails loudly here rather than being silently
            # rescued by a per-org ARN.
            #
            # The OTHER two stage keys (ECR_REPOSITORY / AWS_REGION) are untouched: the per-org
            # role is a push credential, not a registry, and it is scoped to the same shared
            # agent-images repo the stage names.
            #
            # Falsy/absent ⇒ nothing happens, so an inert push-role service (unconfigured env)
            # or a connection provisioned before E22 keeps the tenant stage value exactly as
            # before. That is the whole non-GitHub / not-yet-provisioned path.
            if getattr(connection, "ecr_push_role_arn", None):
                effective_vars["AWS_ECR_PUSH_ROLE_ARN"] = connection.ecr_push_role_arn
            self._rollout.set_ci_vars(
                connection.org,
                name,
                effective_vars,
                # ``scope=None`` is the repository-wide set. A NAMED scope must already exist on
                # GitHub (an env-scoped write 404s otherwise), and creating one would be exactly
                # the second write to a fresh repository that E28B exists to remove.
                scope=None,
                token=token,
                base_url=connection.base_url,
            )

        def provision_langfuse() -> None:
            # E28C/T5 (D-C5): give the repo-created agent the observability wiring `POST /agents`
            # has given manually-registered ones since E26/T4.
            #
            # THE SAME provisioning path, reused rather than reimplemented: the route schedules
            # `provision_langfuse_best_effort`, whose entire body is a LANGFUSE_HOST guard around
            # `service.provision_agent_project(agent)` plus a swallow. This calls that same
            # method, on the same injected service — which is what writes
            # `langfuse_key_secret_name` onto the envelope and persists it. That field is the one
            # value the chain needs: buildspec.yml lifts it into the `langfuse_secret_name` tfvar,
            # the runtime module passes it as `LANGFUSE_SECRET_NAME`, and the container resolves
            # the key pair from Secrets Manager itself. No key VALUE transits tfvars or env here.
            #
            # The route's WRAPPER is deliberately not reused: it swallows every failure into a
            # logged no-op, which is correct for a background task with nowhere to report, and
            # exactly wrong for a step that owns a timeline row — the row would read `done` on an
            # agent that will trace into nothing, the same "record says success, reality
            # disagrees" class this epic exists to delete. So the call raises and the BEST-EFFORT
            # tolerance lives one level up, in `run_materialize`'s `_BEST_EFFORT_STEPS`, where it
            # can mark the row failed AND continue.
            #
            # NOT CONFIGURED ⇒ a clean no-op. Failing the row instead would paint a red step on
            # every materialize in an environment that has no Langfuse at all, which trains
            # operators to ignore this row — and an ignored row is how the original silent zero
            # survived two epics.
            #
            # TWO conditions, and the SECOND is the one that matters in production. ``None`` is a
            # legacy/unwired construction that the live wiring never produces: ``routes/projects.py``
            # injects ``get_langfuse_service()`` UNCONDITIONALLY, and that service constructs
            # perfectly happily with ``LANGFUSE_HOST=""`` — which is the shipped default
            # (``core/config.py``: "empty ⇒ not configured"). So on a Langfuse-unconfigured
            # deployment the provisioner is a real object whose host is ``""``, and calling it
            # raises ``requests.MissingSchema`` on the first URL it builds. That is a FAILED row on
            # every single materialize, which is exactly the outcome the paragraph above rules out.
            #
            # The host check is what ``provision_langfuse_best_effort`` performs before calling
            # (``if not settings.LANGFUSE_HOST: return``) — asked of the injected service rather
            # than of module-level settings, because the service is what this code has and its
            # ``langfuse_host`` is the value that would actually be used. ``getattr`` keeps a test
            # double that models only ``provision_agent_project`` working.
            if self._langfuse is None:
                return
            if not getattr(self._langfuse, "langfuse_host", ""):
                return
            self._langfuse.provision_agent_project(agent)

        def finalize() -> None:
            # Terminal success: persist the created repo_url + flip cicd_status -> "ready".
            self._finalize_repo(repo_id, state.get("repo_url"))

        return {
            "mint_identity": mint_identity,
            "create_repo": create_repo,
            "push_template": push_template,
            "set_repo_vars": set_repo_vars,
            "provision_langfuse": provision_langfuse,
            "finalize": finalize,
        }

    def _resolve_template_bytes(
        self, connection_id: str, connection, token: str, template_name: str
    ) -> tuple[Dict[str, bytes], str]:
        """The bytes ``push_template`` commits, and the LABEL naming where they came from
        (E28C/T4 — D-C2). Exactly three arms, and one of them is a refusal.

        THE DEFECT THIS CLOSES. E28B made materialize push the template's bytes itself, and read
        them from DISK — the image's baked-in seed. A customer who registered a template and then
        iterated it in their own org for six months got the SEED on the next materialize, silently,
        under a step that said "Push template contents" either way. The record claimed the template
        shipped; the repository disagreed. Templates are the source of truth now, so the pointer is
        DEREFERENCED at use-time.

        1. **The record carries both halves of T2's source pair** ⇒ read the repo.
           ``read_repo`` resolves the tip ONCE and ``read_tree`` reads AT that sha, never at a
           branch name: a template author pushing mid-materialize would otherwise contribute half
           an old tree and half a new one — a tree that never existed as a commit.
        2. **No usable pair** (a pre-28C record, an upload pointing outside the org, an
           unregistered name, or no catalog wired at all) ⇒ the on-disk seed, via the EXISTING
           :meth:`_resolve_scaffold_dir` and its two-layer traversal guard. LOUDLY: the label names
           the fallback, because a silent seed push is the defect and "silent" is the part that
           made it one. A seed push an operator can SEE is a legitimate bootstrap.
        3. **A pair that does not dereference** — the repo is gone, or exists with no commit ⇒
           **FAIL**. NO disk fallback, even though the seed may be right there and would produce a
           working repository: shipping starter bytes where the customer expected their own
           iterated template is precisely the trust violation this epic ends. Disk-as-factory-reset
           is an explicit operator action on the reconcile surface (D-C3), never an automatic
           degradation.

        WHY BOTH HALVES, AND WHY TRUTHINESS. ``read_repo`` takes org AND repo positionally, so half
        a pair is not a pointer at all; and the fields are ``Optional[str]``, which admits ``""`` —
        an ``is not None`` check would send an empty org to the provider. The arm is chosen on
        USABILITY, not on whether a field was assigned.

        WHY THE EMPTY REPO IS A FAILURE AND NOT A SEED. T1 answers a ``RepoView`` with an EMPTY
        ``head_sha`` for a repository that exists with no commit. There is nothing to ship, so it is
        indistinguishable from missing as far as a materialize is concerned — and the blank ref is
        never forwarded to ``read_tree``, which refuses one exactly so nobody reads "whatever HEAD
        is now". Falling back to the seed here would be arm 3 with the guard removed.

        A STORE FAULT RAISES rather than degrading to the seed (the registry is strict, and this
        does not soften it): folding "AGP could not read its catalog" into "this record has no
        source" would ship seed bytes over a dereferenceable template every time the store
        hiccupped — an intermittent silent downgrade, strictly worse than a loud failure a retry
        fixes.
        """
        record = self._get_template_record(connection_id, template_name)
        source_org = (record.source_org or "") if record is not None else ""
        source_repo = (record.source_repo or "") if record is not None else ""

        if not (source_org and source_repo):
            # ARM 2 — the seed, named as the seed.
            scaffold_dir = self._resolve_scaffold_dir(template_name)
            files = collect_scaffold_files(scaffold_dir)
            if not files:
                # Fail LOUDLY and locally. ``commit_files`` refuses an empty mapping (an empty tree
                # would delete the branch's contents), but its message would describe the symptom;
                # this names the cause — a template name with no scaffold on disk, which is a
                # misconfiguration or a bad ``template_name``, not a provider fault.
                raise ProjectError(
                    f"template '{template_name}' has no files on disk — nothing to push",
                    kind="materialize_error",
                )
            return files, f"Fetch template (seed: {template_name})"

        # ARM 1/3 — dereference the pointer. ``read_repo`` answers None for NOT-FOUND and NOTHING
        # else (every other failure raises, which fails the step with the provider's own hint), so
        # None here is real absence rather than an outage misread as one.
        view = self._rollout.read_repo(
            source_org, source_repo, token=token, base_url=connection.base_url
        )
        if view is None or not view.head_sha:
            # ARM 3. Missing and empty share this BRANCH and both REMEDIES deliberately: both mean
            # "the record points at bytes AGP cannot ship", and either way the operator re-seeds or
            # deregisters. They no longer share a SENTENCE. `adopt`'s precondition is
            # `_probe(...) is not None`, so an existing-but-never-committed repo IS adoptable — and
            # telling that operator their repo is "missing" sends them hunting for a deleted
            # repository they can see in the provider UI. Naming which of the two facts holds is
            # what makes the failure actionable rather than merely honest.
            fault = (
                f"template repo {source_repo} missing"
                if view is None
                else f"template repo {source_repo} exists but has no commits — nothing to ship"
            )
            raise ProjectError(
                f"{fault} — re-seed or deregister on the Templates page",
                kind="materialize_error",
            )
        files = self._rollout.read_tree(
            source_org, source_repo, ref=view.head_sha, token=token, base_url=connection.base_url
        )
        if not files:
            # A tree that reads as empty at a real sha. ``commit_files`` would refuse it (an empty
            # tree deletes the branch), but its message would name the symptom; this names the
            # cause and keeps the arm's failures attributable to the arm.
            raise ProjectError(
                f"template repo {source_repo} has no files at {view.head_sha[:7]} — nothing to "
                f"push",
                kind="materialize_error",
            )
        # PROVENANCE IS STATED, NOT GUESSED. The frontend renders ``steps[].label`` verbatim (no
        # key→label map — pinned since E28B/T3), so this string IS the operator-facing answer to
        # "which bytes did this agent get".
        return files, f"Fetch template ({source_repo}@{view.head_sha[:7]})"

    def _get_template_record(self, connection_id: str, template_name: str):
        """The catalog row for ``template_name`` under ``connection_id``, or None (E28C/T4).

        None means "no such row" OR "no catalog wired" — both are arm 2, and both are truthful
        absences of a pointer. A STORE FAULT PROPAGATES (the registry is strict by design): see
        :meth:`_resolve_template_bytes` for why an unreadable catalog must not read as "no source".

        Scoped by connection, and keyed with the registry's OWN key derivation
        (``template_id_for``) rather than passing the name straight through — the id is derived
        from the name, and re-deriving it here by hand is how the two could drift.
        """
        if self._templates is None:
            return None
        return self._templates.get(connection_id, template_id_for(template_name))

    def _save_repo_step_label(self, repo_id: str, step_key: str, label: str) -> None:
        """Persist a step's LABEL, leaving its status/timestamps alone (E28C/T4).

        Separate from :meth:`_save_repo_step` because the two carry different facts: status is a
        transition the runner loop drives, while this is evidence the runner DISCOVERED (which
        bytes it resolved). Keeping them apart is what lets the label survive the later DONE write
        — ``_save_repo_step`` never touches ``label``.

        A record's stored labels are what the console renders (no client-side key→label map), so a
        historical repo keeps reading the label it was materialized with. A vanished record is a
        no-op, mirroring :meth:`_save_repo_step`."""
        repo = self._get_repo(repo_id)
        if repo is None:  # pragma: no cover — the record was persisted by add_repo.
            return
        for step in repo.steps:
            if step.key == step_key:
                step.label = label
                break
        repo.updated_at = self._now().isoformat()
        self._save_repo(repo)

    def _resolve_scaffold_dir(self, template_name: str) -> Path:
        """The on-disk scaffold dir for ``template_name`` — validated and CONTAINED.

        THE VULNERABILITY THIS CLOSES (E28B review, #1). ``template_name`` arrives from the
        ``POST /projects/{id}/repos`` body, is a bare ``str`` on ``RepositoryCreate``, and E28B made
        it a FILESYSTEM PATH for the first time (pre-epic it was a GitHub repo name handed to
        ``/generate``, which never touched the disk). Unvalidated, it was an arbitrary-file-read
        that ends in a repository the caller names:

          * ``Path(base) / "/etc/ssl"`` → ``/etc/ssl`` — an absolute segment REPLACES the base;
          * ``base / "../backend/src/core"`` harvested 10 files including ``config.py`` and
            ``security_github_oidc.py``, all of which ``commit_files`` would then push to a repo the
            caller chose. ``/app/src`` is the whole backend source.

        TWO layers, because either alone is one refactor away from reopening:

          1. the NAME must match :data:`~services.github_template_service._NAME_RE` — imported, not
             re-written, so the catalog and the pusher cannot disagree about what a template name
             is. That regex admits no ``/``, no ``.``, no leading ``-``, so it already rejects
             ``..``, absolute paths and Windows separators.
          2. containment is ASSERTED AFTER RESOLUTION. The regex reasons about the string; this
             reasons about the resulting path, so it also covers a SYMLINK inside the templates dir
             pointing out of it — which no pattern can see — and any future path construction here.

        Raises ``ProjectError(kind="materialize_error")`` with a SAFE message: it echoes the
        rejected name (caller-supplied, already in their possession) and never a resolved absolute
        path, which would confirm the container's layout to someone probing for it.
        """
        if not _TEMPLATE_NAME_RE.match(template_name or ""):
            raise ProjectError(
                f"invalid template name {template_name!r} — a template name must match "
                f"{_TEMPLATE_NAME_RE.pattern}",
                kind="materialize_error",
            )
        base = self._agent_templates_dir.resolve()
        candidate = (self._agent_templates_dir / template_name).resolve()
        if candidate != base and not candidate.is_relative_to(base):
            # Unreachable via the regex today; kept because it is the layer that survives a symlink
            # and a future change to how this path is built. Deliberately does NOT name the paths.
            logger.error(
                "[project] refusing template %r — it resolves outside the templates directory",
                template_name,
            )
            raise ProjectError(
                f"invalid template name {template_name!r} — it resolves outside the "
                f"templates directory",
                kind="materialize_error",
            )
        return candidate

    def _adopt_trunk(self, connection, name: str, trunk: str, token: str) -> None:
        """Make ``trunk`` the repository's ONLY branch and its default (E28B/T3 — D-B5).

        Called once, immediately after the template push. Two provider calls, both no-ops when the
        trunk is already the branch ``create_repo``'s ``auto_init`` produced:

          1. point ``default_branch`` at ``trunk`` — otherwise PRs open against the seeded branch
             and the template sits somewhere the provider does not serve as HEAD;
          2. delete the seeded branch — otherwise it lingers with the auto-init README, Actions
             watches it, and ``build.yml``'s ``branches:`` filter can fire on a branch that never
             held the agent's code.

        The ORDER is required, not stylistic: GitHub refuses to delete the default branch, so a
        delete-first implementation fails on every non-default trunk.

        The seeded branch's name is the ORG's default-branch setting, which AGP cannot read from
        the connection record — so it is inferred as :data:`_AUTO_INIT_BRANCH`, GitHub's own
        default. A DIFFERENT org setting leaves one stray ref: undesirable, but strictly better
        than the pre-fix state, and it cannot leave the trunk non-default (step 1 is
        unconditional). Deliberately NOT probed with an extra round trip — see the report.
        """
        if trunk == _AUTO_INIT_BRANCH:
            return  # the seeded branch IS the trunk: already default, nothing stray to reclaim.
        self._rollout.set_default_branch(
            connection.org, name, trunk, token=token, base_url=connection.base_url
        )
        self._rollout.delete_branch(
            connection.org, name, _AUTO_INIT_BRANCH, token=token, base_url=connection.base_url
        )

    def delete_repo(
        self, *, project_id: str, repo_id: str, selection: RepoDeleteSelection
    ) -> RepoDeleteResult:
        """Ordered best-effort teardown of a materialized repo+agent (E23/T4).

        The inverse of :meth:`add_repo`. Each SELECTED step runs in its own try/except and
        appends a :class:`RepoDeleteItemResult`; an UNSELECTED step is skipped (never
        failed). Order: **github → image → runtime(+TF state) → identity(+langfuse) →
        record LAST**.

        The record step (registry entry + OPS row) is deleted ONLY when every other selected
        BLOCKING step succeeded — otherwise the row STAYS so the operator can retry. The
        ``langfuse`` item (E26/T7) is REPORTED but NON-BLOCKING: it is excluded from that
        record-gating predicate, so a Langfuse teardown failure is visible in ``items`` yet
        never aborts the cascade nor keeps the row for retry. The registry entry is destroyed
        by the record step, so ``agent``/``agent_arn``/entra ids are captured up-front. A
        raised step exception yields ``outcome="failed"`` with a SAFE reason
        (``type(err).__name__`` — never a token/body) and ``logger.exception``.
        """
        # 1) Resolve the repo — unknown, or belonging to another project, is a 404.
        repo = self._get_repo(repo_id)
        if repo is None or repo.project_id != project_id:
            raise ProjectError("Repository not found", kind="not_found")

        # 2) Resolve the parent project (for org/connection/token) — unknown is a 404.
        project = self._get_project(project_id)
        if project is None:
            raise ProjectError("Repository not found", kind="not_found")

        # 3) Capture the agent + its ids NOW — the record step (last) deletes the registry
        #    entry, after which these are gone.
        agent = self._registry.get(repo.agent_id)
        agent_arn = getattr(agent, "agent_arn", None) if agent else None

        items: List[RepoDeleteItemResult] = []

        # -- github --------------------------------------------------------
        if selection.github:
            items.append(self._run_github_step(project, repo))
        else:
            items.append(_skipped("github"))

        # -- image ---------------------------------------------------------
        if selection.image:
            items.append(self._run_step("image", lambda: self._delete_image(repo.agent_id)))
        else:
            items.append(_skipped("image"))

        # -- runtime (+ TF state) -----------------------------------------
        if selection.runtime:
            items.append(
                self._run_step(
                    "runtime",
                    # E36/T8: the tenant selects which ACCOUNT each per-stage runtime is
                    # deleted in — see `_stage_control`.
                    lambda: self._delete_runtime(
                        agent, agent_arn, repo.agent_id, project.tenant_id
                    ),
                )
            )
            # -- exec role (E28C/T5) --------------------------------------
            # Its OWN line-item, ordered right after the runtime it belongs to, and REPORTED but
            # NON-BLOCKING — the same shape as ``langfuse`` below and for a related reason.
            #
            # This ran INSIDE the runtime step until E28C, where it had to swallow every failure:
            # a raise would flip an already-deleted runtime to ``failed``, which BLOCKS the record
            # delete and traps the DDB row behind the very role that leaked. So the failure was
            # swallowed — and with it the REPORT. Every cascade said ``deleted`` while the live
            # answer was always ``AccessDenied`` (the task role's grant could not match the role
            # name), and six account-global roles accumulated behind clean teardown reports.
            #
            # Separating the item is what lets both things be true: the operator SEES that a role
            # survived, and the row is still reclaimed. Retrying cannot fix a missing IAM grant, so
            # gating the record on this would trap every row for a cause no retry addresses.
            #
            # Run unconditionally on the runtime SELECTION and never gated on an ARN: deleting a
            # runtime needs an ARN, deleting a role needs only the agent's name, and a half-failed
            # provision (registry entry present, ARN null) is exactly the shape that otherwise
            # leaks forever.
            items.append(_exec_role_item(*self._delete_exec_role(agent, project.tenant_id)))
        else:
            items.append(_skipped("runtime"))
            # The reclaim rides the runtime selection: an operator KEEPING the runtime keeps the
            # role it needs to pull images and write logs. Reported as an explicit "skipped" so a
            # surviving role is never silently absent from the result.
            items.append(_skipped("exec_role"))

        # -- identity (+ Langfuse teardown, E26/T7) -----------------------
        if selection.identity:
            items.append(self._run_step("identity", lambda: self._delete_identity(agent)))
            # E26/T7: tear down the agent's Langfuse project + SM secret alongside its
            # identity — this is the agent's OWN observability, torn down when the agent
            # goes. Runs HERE (before the record step) because it reads the agent
            # envelope's langfuse_project_id/langfuse_key_secret_name, which the record
            # step deletes. It is REPORTED as its own cascade line-item so an operator
            # SEES whether the Langfuse project/secret was torn down or failed — but it is
            # deliberately NON-BLOCKING: the record-gating predicate below EXCLUDES it, so
            # a Langfuse failure can never abort the cascade nor trap the DDB row.
            items.append(self._run_step("langfuse", lambda: self._delete_langfuse(agent)))
        else:
            items.append(_skipped("identity"))
            # Langfuse is grouped with identity (the agent's own resource) — deselecting
            # identity skips it too. Reported as an explicit "skipped" item (never
            # silently absent) so an un-torn-down Langfuse project stays visible.
            items.append(_skipped("langfuse"))

        # -- record (LAST, conditional) -----------------------------------
        record_removed = False
        if selection.record:
            # Delete the record only when NO other BLOCKING step failed. An unselected
            # step is "skipped" (not "deleted") but must not block the record delete. The
            # "langfuse" item is REPORTED but NON-BLOCKING (E26/T7) — explicitly excluded
            # here so a Langfuse teardown failure never keeps the row for retry, and
            # "exec_role" joins it (E28C/T5): a denied reclaim is an IAM GRANT problem, which
            # no operator retry of this cascade can fix, so gating the row on it would trap
            # every row behind a leaked role while repo, image, runtime and identity are gone.
            others_ok = not any(
                i.outcome == "failed" for i in items if i.item not in _NON_BLOCKING_ITEMS
            )
            if others_ok:
                try:
                    self._registry.delete(repo.agent_id)
                    self._delete_repo(repo_id)
                    items.append(RepoDeleteItemResult(item="record", outcome="deleted"))
                    record_removed = True
                except Exception as err:
                    logger.exception("[project] record delete failed for repo %s", repo_id)
                    items.append(
                        RepoDeleteItemResult(
                            item="record", outcome="failed", reason=type(err).__name__
                        )
                    )
            else:
                items.append(
                    RepoDeleteItemResult(
                        item="record",
                        outcome="skipped",
                        reason="kept for retry — an earlier step failed",
                    )
                )
        else:
            items.append(_skipped("record"))

        return RepoDeleteResult(items=items, record_removed=record_removed)

    def preview_delete(self, *, project_id: str, repo_id: str) -> RepoDeletePreview:
        """READ-ONLY reachability pre-check for the delete cascade (E23/T11).

        Probes each teardown artifact best-effort and reports its ``state`` —
        ``present`` (still there), ``gone`` (already deleted / unreachable), or ``unknown``
        (the probe raised — the frontend treats ``unknown`` as selectable+checked, assume
        present). Deletes NOTHING. The delete modal uses this to offer only the artifacts
        that still exist, and to never present an already-gone (AccessDenied-on-gone)
        runtime as a re-deletable item. A probe raising NEVER hides a present/unknown — we
        report honestly.

        Same 404 guards as :meth:`delete_repo`: an unknown repo, or one belonging to
        another project, raises ``ProjectError(kind="not_found")``.
        """
        # Same resolve/ownership guards as delete_repo.
        repo = self._get_repo(repo_id)
        if repo is None or repo.project_id != project_id:
            raise ProjectError("Repository not found", kind="not_found")
        project = self._get_project(project_id)
        if project is None:
            raise ProjectError("Repository not found", kind="not_found")

        agent = self._registry.get(repo.agent_id)
        agent_arn = getattr(agent, "agent_arn", None) if agent else None

        items = [
            RepoDeletePreviewItem(item="github", state=self._probe_github(project, repo)),
            RepoDeletePreviewItem(item="image", state=self._probe_image(repo.agent_id)),
            RepoDeletePreviewItem(
                item="runtime",
                state=self._probe_runtime(agent, agent_arn, project.tenant_id),
            ),
            RepoDeletePreviewItem(item="identity", state=self._probe_identity(agent)),
            # The record is what we're deleting — the row exists (we just read it).
            RepoDeletePreviewItem(item="record", state="present"),
        ]
        return RepoDeletePreview(items=items)

    def delete_project(self, project_id: str) -> None:
        """Delete an empty project container — GUARDED: blocked while it still holds repos.

        Unlike :meth:`delete_repo`, a Project owns no runtime/identity of its own, so this is
        a plain guarded record delete. An unknown id is a 404
        (``ProjectError(kind="not_found")``); a project that still has repositories raises
        ``ProjectError(kind="has_repositories")`` and is NOT deleted (the operator tears the
        repos down first). Only an empty container is removed."""
        project = self._get_project(project_id)
        if project is None:
            raise ProjectError("Project not found", kind="not_found")
        if self._load_repos_for(project_id):
            raise ProjectError("Project has repositories", kind="has_repositories")
        self._delete_project(project_id)

    # ===================================================================== #
    # Steps
    # ===================================================================== #

    def _provision_identity(self, agent) -> None:
        """Drive ``identity.provision_identity(agent)`` (async in production; a plain
        Mock in tests). Runs the coroutine on a fresh loop when awaitable so the sync
        orchestration doesn't require an already-running event loop."""
        result = self._identity.provision_identity(agent)
        if inspect.isawaitable(result):
            asyncio.run(result)

    # -- materialize step persistence (E25C/T2) ----------------------------

    def _save_repo_step(
        self, repo_id: str, step_key: str, status: StepStatus, error: Optional[str] = None
    ) -> None:
        """Persist ONE StepState transition on a repo's timeline (E25C/T2).

        Read the record → update the matching ``StepState`` (status + started_at on RUNNING
        / completed_at on DONE|FAILED + a SAFE ``error`` hint on FAILED) → bump ``updated_at``
        → put_item. Mirrors :meth:`DeploymentService.update_status`' read→mutate→put shape.
        The ``error`` is a SHORT SAFE hint only (``type(err).__name__`` / a curated string) —
        NEVER a token or GitHub/Graph body. A vanished record is a no-op."""
        repo = self._get_repo(repo_id)
        if repo is None:  # pragma: no cover — the record was persisted by add_repo.
            return
        ts = self._now().isoformat()
        for step in repo.steps:
            if step.key == step_key:
                step.status = status
                if status == StepStatus.RUNNING:
                    step.started_at = ts
                    # Clear any stale error from a prior failed attempt (E25C/T2 resume): a
                    # step re-run to RUNNING must not show a lingering error from before.
                    step.error = None
                elif status == StepStatus.DONE:
                    step.completed_at = ts
                    step.error = None  # a recovered step shows no lingering error.
                elif status == StepStatus.FAILED:
                    step.completed_at = ts
                    step.error = error
                break
        repo.updated_at = ts
        self._save_repo(repo)

    def _mark_repo_failed(self, repo_id: str) -> None:
        """Flip the record's ``cicd_status``/``status`` to ``"failed"`` (E25C/T2). Called
        after a step is marked failed so the badge + the failed-record query reflect it."""
        repo = self._get_repo(repo_id)
        if repo is None:  # pragma: no cover
            return
        repo.cicd_status = "failed"
        repo.status = "failed"
        repo.updated_at = self._now().isoformat()
        self._save_repo(repo, include_cicd_status=True)  # the failed transition IS the point

    def _persist_repo_url(self, repo_id: str, repo_url: Optional[str]) -> None:
        """Persist the generated ``repo_url`` onto the record the moment ``generate_repo``
        completes (E25C/T2 resume) — so a resumed run that skips the done ``generate_repo``
        step can still read the url off the record in ``_finalize_repo``. A vanished record
        is a no-op."""
        repo = self._get_repo(repo_id)
        if repo is None:  # pragma: no cover
            return
        repo.repo_url = repo_url
        repo.updated_at = self._now().isoformat()
        self._save_repo(repo)

    def _finalize_repo(self, repo_id: str, repo_url: Optional[str]) -> None:
        """Terminal success: stamp the generated ``repo_url`` and flip BOTH ``cicd_status`` and
        ``status`` → ``"ready"`` (E25C/T2's badge state, plus E28A/T5).

        ``status`` used to be left at ``"provisioning"`` here, and an earlier revision of this
        docstring called that the terminal success value — which was wrong, not merely terse.
        The field had only two writers (``"provisioning"`` at create/retry, ``"failed"`` on a
        dead step) and NOTHING wrote a success value, so a fully materialized healthy repo
        advertised itself as still provisioning forever, beside a Complete timeline. Writing
        ``"ready"`` here closes the three-value cycle (``provisioning`` → ``ready`` | ``failed``,
        reset to ``provisioning`` by :meth:`retry_materialize`) and matches what
        :meth:`add_repo` already documented would happen.

        ``status`` is in no :meth:`_save_repo` skip set, so it rides the existing
        ``include_cicd_status`` save with no new opt-in — but the save is still a whole-record
        SET, so the CodeBuild-owned ``last_dev_image_tag`` must (and does) stay skipped.

        On a RESUMED run where ``generate_repo`` was already ``done`` and skipped, the
        ephemeral ``state`` dict is empty so ``repo_url`` arrives as None — DON'T overwrite
        the already-persisted url with None. Prefer the passed url, else keep what
        ``generate_repo`` persisted onto the record."""
        repo = self._get_repo(repo_id)
        if repo is None:  # pragma: no cover
            return
        if repo_url is not None:
            repo.repo_url = repo_url
        repo.cicd_status = "ready"
        repo.status = "ready"  # E28A/T5 — the success value the field never had
        repo.updated_at = self._now().isoformat()
        self._save_repo(repo, include_cicd_status=True)  # the ready transition IS the point

    # -- teardown steps (E23/T4) -------------------------------------------

    def _run_step(self, item: str, action) -> RepoDeleteItemResult:
        """Run one best-effort teardown ``action``; map success/failure to a result. A raised
        exception → ``outcome="failed"`` with a SAFE reason (``type(err).__name__`` — never a
        token/body) + ``logger.exception``.

        E36/T8 pins ONE exception out of the generic mapping. A
        :class:`TenantCredentialsError` means the step never reached the account that owns the
        resource, which is a different operator action from every other failure here: not
        "retry", but "the platform cannot get into that account — grant the trust or reclaim
        by hand". ``type(err).__name__`` alone would render as ``TenantCredentialsError``,
        naming neither the role nor the hop, so the prefixed message is carried through. The
        message is already SAFE by construction (role name + exception type, never an account
        id — see :mod:`services.tenant_credentials`), which is why it may be surfaced at all.
        """
        try:
            action()
            return RepoDeleteItemResult(item=item, outcome="deleted")
        except TenantCredentialsError as err:
            logger.exception("[project] teardown step '%s' could not assume the tenant role", item)
            return RepoDeleteItemResult(
                item=item, outcome="failed", reason=f"assume_role_failed: {err.message}"
            )
        except StageUnresolvedError as err:
            # A THIRD operator action (E36/T8 fix 1): not "retry" and not "grant the trust",
            # but "the tenant/stage config this record points at is unavailable". Its own
            # prefix because the previous behaviour — ask the control-plane account and read
            # its NotFound as already-done — is precisely the false `deleted` this task exists
            # to remove. The message is a stage name only; see :meth:`_stage_cfg`.
            logger.exception(
                "[project] teardown step '%s' could not resolve the owning account", item
            )
            return RepoDeleteItemResult(
                item=item, outcome="failed", reason=f"stage_unresolved: {err.message}"
            )
        except Exception as err:
            logger.exception("[project] teardown step '%s' failed", item)
            return RepoDeleteItemResult(item=item, outcome="failed", reason=type(err).__name__)

    # -- the cross-account credential seam (E36/T8) ------------------------- #

    def _stage_cfg(self, tenant_id: Optional[str], stage: Optional[str]):
        """The :class:`TenantStageConfig` for ``stage``, or None ⇒ use the ambient client.
        Raises :class:`StageUnresolvedError` when the stage IS named but unresolvable.

        None — ambient — is returned only for the states in which the account is genuinely NOT
        KNOWN, and where ambient is therefore the RIGHT account rather than a guess:

        * no ``tenant_id``/no tenant service — the legacy wiring, and the shape every test
          that does not opt in to a tenant has;
        * no stage, or :data:`UNKNOWN_STAGE` — a legacy scalar-only record admitting it does
          not know which stage (hence which account) its runtime belongs to. Substituting
          ``"dev"`` would assume into an account picked by coin-flip.

        EVERYTHING ELSE RAISES (E36/T8 fix 1). These three used to return None as well, and
        that made them the last surviving way to report a failure as ``deleted``:

        * ``self._tenants.get(...)`` RAISES (a DynamoDB throttle, a timeout) — this is the
          sharp one. It is not "we never knew the account", it is "we could not look it up
          right now", and answering the control-plane account instead turns a transient store
          failure into a live runtime reported as torn down;
        * the tenant record is MISSING — ``get`` returns None. Previously silent, no log;
        * the tenant no longer LISTS the stage the record names. Previously silent too.

        In all three the resource may well exist in an account we simply did not identify, so
        the caller must report the item ``failed``, never ask a different account and believe
        its answer. T1's stage MERGE is what keeps the third from being reached by an ordinary
        tenant edit (a wholesale ``PUT /tenants/{id}`` used to drop ``prod`` silently).

        A stage that resolves with an EMPTY ``deploy_role_arn`` is unaffected: it is
        deploy-in-place, ambient-by-design, and stays byte-for-byte on today's behaviour — the
        callers check ``not cfg.deploy_role_arn`` for exactly that.
        """
        if not tenant_id or not stage or stage == UNKNOWN_STAGE or self._tenants is None:
            return None
        try:
            tenant = self._tenants.get(tenant_id)
        except Exception as err:
            # No tenant id in the line: the STAGE is the actionable half and a tenant id is a
            # customer identifier. `logger.exception` keeps the store's own cause in the logs.
            logger.exception(
                "[project] tenant lookup failed for stage '%s'; the owning account is unknown",
                stage,
            )
            raise StageUnresolvedError(f"tenant lookup failed for stage '{stage}'") from err
        if tenant is None:
            logger.warning(
                "[project] no tenant record for stage '%s'; the owning account is unknown",
                stage,
            )
            raise StageUnresolvedError(f"no tenant record for stage '{stage}'")
        stages = getattr(tenant, "stages", None) or {}
        if stage not in stages:
            logger.warning(
                "[project] the tenant record does not list stage '%s'; the owning account is "
                "unknown",
                stage,
            )
            raise StageUnresolvedError(f"tenant no longer lists stage '{stage}'")
        return stages[stage]

    def _stage_iam(self, tenant_id: Optional[str], stage: Optional[str], agent_id: str):
        """The IAM client for ``stage``'s account. Raises :class:`TenantCredentialsError` or
        :class:`StageUnresolvedError`.

        Returns ``self._iam`` unchanged unless the resolved stage carries a
        ``deploy_role_arn``. Deliberately NOT routed through the seam's own ambient branch:
        ``self._iam`` is the client the whole pre-existing suite injects, and building a fresh
        one per role per stage would spend a client construction to reach the same account.
        """
        cfg = self._stage_cfg(tenant_id, stage)
        if cfg is None or not cfg.deploy_role_arn:
            return self._iam
        return self._stage_client("iam", cfg, session_suffix=f"teardown-{agent_id}")

    def _stage_control(self, tenant_id: Optional[str], stage: Optional[str], agent_id: str):
        """The AgentCore control client for ``stage``'s account, or None ⇒ the identity
        service's own ambient client. Raises :class:`TenantCredentialsError` or
        :class:`StageUnresolvedError`."""
        cfg = self._stage_cfg(tenant_id, stage)
        if cfg is None or not cfg.deploy_role_arn:
            return None
        return self._stage_client(
            "bedrock-agentcore-control", cfg, session_suffix=f"teardown-{agent_id}"
        )

    def _delete_github(self, project, repo) -> None:
        """Delete the materialized GitHub repo (idempotent 404→ok in the write client). The
        connection token flows straight to the client — never logged, never surfaced."""
        connection = self._conn.get_connection(project.connection_id)
        token = self._conn.get_bearer_token(project.connection_id)
        self._rollout.delete_repo(connection.org, repo.name, token, connection.base_url)

    def _delete_image(self, agent_id: str) -> None:
        """Delete the agent's ECR images (T3, idempotent). No injected cleaner ⇒ no-op."""
        if self._ecr is None:
            return
        self._ecr.delete_images(agent_id)

    def _delete_runtime(
        self, agent, agent_arn: Optional[str], agent_id: str, tenant_id: Optional[str] = None
    ) -> None:
        """Delete EVERY per-stage AgentCore runtime (T2, idempotent) then best-effort the
        TF-state objects and the IAM exec role (E28A/T2).

        When there is no agent/ARN nothing was provisioned → treated as an already-done
        success. A ``_delete_runtime_state`` failure is logged but does NOT flip this step to
        failed — the runtime itself is gone.

        N RUNTIMES, NOT ONE (E28A/T1, D-A4). T1b stage-scopes the runtime module's resource
        names, so an agent genuinely owns one runtime PER STAGE. This deletes every ARN
        :func:`resolve_runtime_arns` resolves — the ``agent_arns`` map when populated, else the
        legacy scalar — PLUS a scalar the map does not name. That last case is the migration
        shape and is the reason the union is not simply "the map when present": an agent
        deployed under pre-T1b code and then redeployed once under T1b has a map naming only
        the redeployed stage, while the scalar still names a REAL, RUNNING runtime from before.
        Dropping it would leak precisely the runtime the pre-E28A record was tracking. The
        de-dupe matters too — C-A2 has the buildspec write the scalar as a DUPLICATE of the
        last stage's ARN, so a blind union would call ``DeleteAgentRuntime`` on it twice.

        TOLERANCE IS PER RUNTIME, mirroring :meth:`_delete_runtime_state`'s per-KEY tolerance
        and for the same reason: the runtimes are independent, so a partial reclaim strictly
        beats abandoning the rest, and every survivor is individually nameable in the logs.
        But UNLIKE the state/IAM reclaims this step still FAILS after attempting them all — a
        surviving runtime is a live, billing, now-unreachable resource, so the row must stay
        for the operator to retry. Swallowing here would report a leak as a clean teardown.

        THE EXEC-ROLE RECLAIM IS NO LONGER CALLED FROM HERE (E28C/T5). It used to run inside this
        method, outside the ``agent_arn`` guard, and had to SWALLOW every failure — a raise would
        flip an already-deleted runtime to ``failed`` and trap the DDB row behind a leaked IAM
        role. That swallow also erased the report, so a denied reclaim read as a clean teardown and
        six account-global roles leaked unnoticed. :meth:`delete_repo` now drives
        :meth:`_delete_exec_role` as its own non-blocking ``exec_role`` line-item, which reports
        honestly without being able to fail this step. What it must NOT lose is the reason the call
        sat outside the guard: deleting a runtime needs an ARN, deleting the role needs only the
        agent's NAME, and a half-failed provision (registry entry present, ARN null) is exactly the
        shape that otherwise leaks forever — so the caller runs it unconditionally on the runtime
        selection, never gated on an ARN.

        What that still does NOT cover, stated plainly because the gap is invisible from the
        outside: the role name is derived from the REGISTRY entry, so the reclaim only runs while
        that entry is still readable. The record step deletes the registry entry BEFORE the DDB
        row, so if the row delete then fails the row survives with ``record=failed`` and no
        registry entry — and on the operator's retry ``agent`` is None, the reclaim no-ops, and the
        role LEAKS and must be removed by hand. ``Repository`` persists no agent name, so a retry
        has nothing left to derive it from; closing that gap means persisting the name on the row.
        A repo deleted twice is therefore NOT handled — do not read the reclaim as idempotent once
        the registry entry is gone. The ``exec_role`` item reports ``skipped`` in that state rather
        than claiming a teardown it could not attempt.

        CROSS-ACCOUNT (E36/T8). Each runtime is deleted under the credentials of the account
        that OWNS it, resolved per STAGE via :meth:`_stage_control`. A failed AssumeRole raises
        :class:`TenantCredentialsError` out of this method — NEVER a silent ambient retry,
        which would land the probe in the control-plane account, answer NotFound, and report
        the leak as a clean teardown (the whole defect). Assume failures take PRECEDENCE over
        ordinary ones in the raised message because the operator's next action differs (an
        unreachable account is not a retry); the count still covers both. Per-RUNTIME tolerance
        is unchanged: a bad assume for one stage still attempts every other stage.

        THREE WAYS TO NOT GET A CLIENT, all reported, none ambient (E36/T8 fix 1): the assume
        failed (:class:`TenantCredentialsError`), the owning account could not be identified
        (:class:`StageUnresolvedError`), or the client construction itself failed (anything
        else). The last two used to be missing — an unresolvable stage silently used the
        control-plane client and a construction failure escaped mid-loop, skipping
        :meth:`_delete_runtime_state` and leaking the TF-state objects."""
        targets = self._runtime_targets_to_delete(agent, agent_arn)
        if targets:
            failures: List[str] = []
            assume_failures: List[str] = []
            unresolved: List[str] = []
            for stage, arn in targets:
                try:
                    control = self._stage_control(tenant_id, stage, agent_id)
                except TenantCredentialsError as err:
                    # No ARN and no account id in the line (RID-not-ARN rule); the role name
                    # inside `err.message` is the actionable fact and is already safe.
                    logger.warning(
                        "[project] runtime delete could not assume the tenant role for "
                        "stage '%s': %s",
                        stage,
                        err.message,
                    )
                    assume_failures.append(err.message)
                    failures.append(arn)
                    continue
                except StageUnresolvedError as err:
                    # E36/T8 fix 1. We do not know which account holds this runtime, so there
                    # is no client to fall back to: the ambient one belongs to the control
                    # plane, where the answer is always NotFound and NotFound is read as
                    # already-done. Report it and keep going to the other stages.
                    logger.warning(
                        "[project] runtime delete could not resolve the owning account for "
                        "stage '%s': %s",
                        stage,
                        err.message,
                    )
                    unresolved.append(err.message)
                    failures.append(arn)
                    continue
                except Exception:
                    # E36/T8 fix 1 (spec I-B). The seam's FINAL `boto3.client(...)` sits
                    # outside its own try, so a bad region / unknown service raises something
                    # that is neither of the two above. Letting it escape mid-loop would lose
                    # per-runtime tolerance AND skip `_delete_runtime_state` below, leaking the
                    # TF-state objects — the same generic guard the exec-role reclaim and the
                    # preview probe both carry, and for the same reason.
                    logger.exception(
                        "[project] runtime client resolution failed for stage '%s'", stage
                    )
                    failures.append(arn)
                    continue
                try:
                    self._identity.delete_runtime(arn, control_client=control)
                except Exception:
                    # Log per RUNTIME so every survivor is nameable, then keep going: the
                    # runtimes are independent and a partial reclaim beats stranding the rest.
                    # RID-NOT-ARN (`arn.rsplit("/", 1)[-1]`, the house idiom): a full runtime ARN
                    # carries the AWS account id, which a hard project rule bans anywhere — logs
                    # included. The RID still identifies the runtime uniquely, so the line stays
                    # chaseable. The sibling reclaims are careful the same way (the state delete
                    # logs the S3 key, the exec-role reclaim logs the role name).
                    logger.exception(
                        "[project] runtime delete failed for %s", arn.rsplit("/", 1)[-1]
                    )
                    failures.append(arn)
            # The Terraform state objects live in the CONTROL-PLANE bucket, not the tenant's,
            # so this stays on the ambient s3 client (E36/T8).
            self._delete_runtime_state(agent_id)
            if assume_failures:
                raise TenantCredentialsError(
                    f"{len(failures)} of {len(targets)} runtimes could not be deleted: "
                    + "; ".join(dict.fromkeys(assume_failures))
                )
            if unresolved:
                # After the credential hop, before the ordinary failures — same precedence
                # rule and same reason: an unreachable/unidentified account is not a retry.
                raise StageUnresolvedError(
                    f"{len(failures)} of {len(targets)} runtimes could not be deleted: "
                    + "; ".join(dict.fromkeys(unresolved))
                )
            if failures:
                raise RuntimeError(
                    f"{len(failures)} of {len(targets)} runtimes could not be deleted"
                )

    @staticmethod
    def _runtime_targets_to_delete(agent, agent_arn: Optional[str]) -> List[tuple]:
        """Every runtime the cascade must reclaim, as ``(stage, ARN)`` — de-duped, order-stable.

        The STAGE is what E36/T8 needs and what :meth:`_runtime_arns_to_delete` threw away: it
        selects the tenant stage whose account owns that runtime. A scalar the map does not
        name carries stage ``None`` — the record does not know its account, so the teardown
        stays on the ambient client rather than assuming into a guessed one."""
        if agent is None:
            return []
        targets = list(resolve_runtime_arns(agent).items())
        named = {arn for _, arn in targets}
        if agent_arn and agent_arn not in named:
            targets.append((None, agent_arn))
        return targets

    @staticmethod
    def _runtime_arns_to_delete(agent, agent_arn: Optional[str]) -> List[str]:
        """Every runtime ARN the cascade must reclaim for ``agent`` — de-duped, order-stable.

        The map (or, for a legacy record, the scalar standing in for it) UNION a scalar the map
        does not name. See :meth:`_delete_runtime` for why both halves are load-bearing.

        Kept as the ARN-only projection of :meth:`_runtime_targets_to_delete` so there is still
        ONE derivation of "which runtimes does this agent own" — two would be free to disagree,
        which is the defect class :meth:`_exec_role_names_to_delete` documents at length."""
        return [arn for _, arn in ProjectService._runtime_targets_to_delete(agent, agent_arn)]

    def _delete_identity(self, agent) -> None:
        """Drive the async ``identity.delete_identity(agent)`` (idempotent, T2) via the same
        ``asyncio.run`` + ``inspect.isawaitable`` guard as :meth:`_provision_identity`. No
        agent ⇒ already-done success."""
        if agent is None:
            return
        result = self._identity.delete_identity(agent)
        if inspect.isawaitable(result):
            asyncio.run(result)

    def _delete_langfuse(self, agent) -> None:
        """Best-effort/idempotent teardown of the agent's Langfuse project + SM secret (E26/T7).

        Drives the C2 ``LangfuseProvisioningService.delete_agent_project`` — itself idempotent
        + best-effort (already-gone == success; it does not raise). It reads the agent
        envelope's ``langfuse_project_id`` / ``langfuse_key_secret_name``, so this MUST run
        before the record step deletes the registry entry. No injected provisioner (Langfuse
        unconfigured) ⇒ no-op; no agent ⇒ already-done success — either maps to a ``deleted``
        (success) line-item via :meth:`_run_step`, exactly like the other no-op-able steps.

        This runs under :meth:`_run_step` so it is REPORTED as a cascade line-item: a raise
        from C2 is caught by ``_run_step`` and surfaced as ``outcome="failed"`` (SAFE reason
        ``type(err).__name__`` — never a secret value/key) so the operator SEES the failure.
        It is kept NON-BLOCKING by the record-gating predicate in :meth:`delete_repo`, which
        EXCLUDES the ``langfuse`` item — so a Langfuse failure can never abort the cascade nor
        keep the DDB row for retry. Hence, UNLIKE the sibling steps, we do NOT swallow here."""
        if self._langfuse is None or agent is None:
            return
        self._langfuse.delete_agent_project(agent)

    def _delete_runtime_state(self, agent_id: str) -> None:
        """Best-effort delete of EVERY stage's runtime TF-state object for this agent.

        Since E28/T2 the key is stage-scoped (:func:`runtime_state_key`), so one agent owns N
        objects — one per stage it was ever deployed to. This therefore LISTS the agent's
        prefix and deletes what is actually there, rather than deleting one known key.

        Listing beats iterating a tenant's stages: a stage REMOVED from the tenant after a
        deploy still has state in the bucket, and a ``tenant.stages`` loop would never see it
        (leaking it forever). The bucket is the only honest inventory. ``delete_repo`` also has
        no guaranteed tenant service, so there is no stage list to iterate here anyway.

        No bucket/client ⇒ return. A ``ClientError``/``BotoCoreError`` is logged, never raised
        (mirror ``s3_service.delete_project``) — a client-side transport error
        (endpoint/timeout) must NOT flip the already-deleted runtime item to failed.

        Tolerance is PER KEY: one stage's delete failing must not strand the others, since the
        objects are independent and a partial reclaim is strictly better than abandoning the
        rest. Each failure is logged individually, so every leaked object is nameable in the
        logs rather than hidden behind a single "the loop died" line."""
        if not self._runtime_state_bucket or self._s3 is None:
            return
        prefix = runtime_state_prefix(agent_id)
        try:
            keys = self._list_runtime_state_keys(prefix)
        except (ClientError, BotoCoreError):
            logger.exception("[project] runtime TF-state listing failed for %s", agent_id)
            return
        for key in keys:
            try:
                self._s3.delete_object(Bucket=self._runtime_state_bucket, Key=key)
            except (ClientError, BotoCoreError):
                logger.exception("[project] runtime TF-state delete failed for key %s", key)

    def _list_runtime_state_keys(self, prefix: str) -> List[str]:
        """Every stored object key under ``prefix``, following ``NextContinuationToken``.

        Paged for the same reason the deployment read is: a truncated listing would leave
        objects behind SILENTLY (teardown would report success), and a state file left in the
        bucket is what makes a re-materialized agent adopt a dead runtime.

        Bounded by ``_MAX_STATE_LIST_PAGES`` for the same reason the deployment read is bounded:
        a store that keeps handing back a continuation token would otherwise loop forever and
        hang the teardown. Truncating here costs nothing the contract does not already allow
        (this path is best-effort — it may delete fewer objects than exist), but it IS a state
        leak, so hitting the ceiling logs loudly enough to be chased."""
        keys: List[str] = []
        token: Optional[str] = None
        for _ in range(_MAX_STATE_LIST_PAGES):
            kwargs = {"Bucket": self._runtime_state_bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self._s3.list_objects_v2(**kwargs)
            keys.extend(obj["Key"] for obj in resp.get("Contents", []))
            token = resp.get("NextContinuationToken")
            if not token:
                return keys
        logger.warning(
            "[project] runtime TF-state listing for %s hit the %d-page ceiling — objects beyond "
            "it are NOT deleted and remain in the state bucket as a leak",
            prefix,
            _MAX_STATE_LIST_PAGES,
        )
        return keys

    def _delete_exec_role(self, agent, tenant_id: Optional[str] = None) -> tuple:
        """Reclaim the runtime's IAM exec roles + their inline policies, and REPORT the outcome
        (E28A/T2, made honest in E28C/T5).

        NEVER RAISES, BUT NEVER SILENT EITHER — and the difference between those two is the whole
        point of this method's contract, so do not collapse them back together.

        Until E28C this ran INSIDE the ``runtime`` step and swallowed every failure, because a raise
        there would flip an already-deleted runtime to ``outcome="failed"``, which BLOCKS the record
        delete and traps the DDB row behind the very IAM role that leaked. That reasoning about
        RAISING was correct and still holds. What was wrong is that the swallow also discarded the
        REPORT: the live answer was always ``AccessDenied`` (the ECS task role's ``iam:DeleteRole``
        grant was scoped ``role/{prefix}-ecr-push-*``, which cannot match
        ``{agent}-{stage}-agentcore-exec``), so every cascade reported ``deleted`` while six
        account-global roles accumulated.

        So :meth:`delete_repo` now calls this DIRECTLY — not through ``_delete_runtime`` — and turns
        the return value into its own non-blocking ``exec_role`` line-item (:func:`_exec_role_item`).
        The operator sees a surviving role; the row is still reclaimed. **Do not re-add a swallow
        here, and do not fold this back into the runtime step**: either change silently restores the
        clean-report-over-a-leak that E28C exists to delete.

        RETURNS: ``None`` when nothing was ATTEMPTED (no agent name to derive the role name
        from), ``[]`` when every candidate role is gone, else the SAFE per-role hints for the roles
        that SURVIVE. ``None`` and ``[]`` are deliberately distinct — "AGP did not look" is not
        "there is nothing left" — and the caller renders the first as ``skipped`` rather than
        claiming a teardown it never performed.

        ``self._iam is None`` IS NOT A REASON TO STOP (E36/T8 fix 1, spec I-C). Before the seam,
        the ambient IAM client was the only client there was, so gating on it was gating on "can
        we do this at all". Post-seam it is the CONTROL-PLANE client and nothing more: whether
        it exists says nothing about whether we can assume into a tenant account and reclaim a
        tenant-account role. Gating on it suppressed a cross-account reclaim that would have
        worked, and reported ``skipped`` with the reason ``no agent record to derive the role
        name`` — naming a cause that was not the actual one. The gate is now on ``agent_name``
        alone, which is the only thing genuinely required to derive a name, and a per-stage
        client that resolves to a MISSING ambient client is reported per role instead.

        Idempotent by design: ``NoSuchEntity`` is the ALREADY-DONE state, not an error. A repo
        that predates the module, one whose runtime provision failed before IAM, and a second
        delete attempt all take that path.

        ORDER is load-bearing — the inline policy goes first. IAM refuses ``DeleteRole`` on a
        role that still carries one (``DeleteConflict``), so skipping the policy delete does not
        merely leak the policy, it leaks the whole role.

        Tolerance is PER CALL, mirroring the per-key tolerance in
        :meth:`_delete_runtime_state`: a failed policy delete still ATTEMPTS the role. The two
        reclaims are independent, a partial one beats neither, and the interesting live failure
        (``AccessDenied``) would otherwise hide behind the first call.

        N ROLES, NOT ONE (E28A/T1b). Since the module stage-scopes the role name, an agent
        genuinely owns one role PER STAGE — plus, for anything deployed before that rollout, one
        at the old un-scoped name. Every candidate is attempted; see
        :meth:`_exec_role_names_to_delete` for where the list comes from and why the legacy name
        is always in it. TOLERANCE IS PER ROLE, mirroring the per-key tolerance in
        :meth:`_delete_runtime_state`: a failure on one stage's role still attempts the next.

        NOTE: the names are derived via :func:`agentcore_exec_role_name` /
        :func:`legacy_agentcore_exec_role_name` — the single producers. Do not inline a literal
        here; Terraform is the only thing that CREATES these roles, so a drift is silent.

        RETURNS ``(hints, assume_failures)`` since E36/T8, because "the role survives" and "we
        never got into the account that holds it" are different operator actions and the report
        line has to say which. Both lists are consumed by :func:`_exec_role_item`.

        CROSS-ACCOUNT (E36/T8). Each role is reclaimed under the credentials of the account
        that OWNS it, resolved per STAGE via :meth:`_stage_iam`. PER-ROLE TOLERANCE EXTENDS TO
        THE ASSUME: a stage whose AssumeRole fails is recorded and the loop CONTINUES, because
        raising here would abandon a role in another account that the platform can delete —
        leaking it for a reason that has nothing to do with it."""
        agent_name = getattr(agent, "name", None) if agent else None
        if not agent_name:
            # No name to derive from. Guessing a RoleName would at best burn a denied call and
            # at worst target a role belonging to something else. NOT gated on `self._iam`
            # any more — see the docstring: that client is the control-plane one, and a
            # cross-account reclaim does not need it.
            return None, []
        agent_id = getattr(agent, "id", "") or ""
        hints: List[str] = []
        assume_failures: List[str] = []
        for stage, role_name in self._exec_role_targets_to_delete(agent, agent_name):
            try:
                iam = self._stage_iam(tenant_id, stage, agent_id)
            except TenantCredentialsError as err:
                # `err.message` is the role name + exception type — no account id, no ARN.
                logger.warning(
                    "[project] exec-role reclaim could not assume the tenant role for stage "
                    "'%s': %s",
                    stage,
                    err.message,
                )
                assume_failures.append(err.message)
                continue
            except StageUnresolvedError as err:
                # E36/T8 fix 1. The role may well exist in an account we could not identify —
                # and the ambient client belongs to the control plane, where it is always
                # `NoSuchEntity`, i.e. the already-done state. So this is a SURVIVING role,
                # tagged with why it was never addressed. `err.message` is a stage name only.
                logger.warning(
                    "[project] exec-role reclaim could not resolve the owning account for "
                    "stage '%s': %s",
                    stage,
                    err.message,
                )
                hints.append(f"{role_name} (stage_unresolved)")
                continue
            except Exception as err:
                # NEVER RAISES is load-bearing for this method: its caller sits OUTSIDE
                # `_run_step`, so an escape here would 500 the whole delete route and lose
                # every other line-item's report. Anything the seam can throw that is not a
                # credential failure (a bad region, a client-construction error) is therefore
                # recorded as an ordinary surviving-role hint rather than allowed out.
                logger.exception(
                    "[project] exec-role client resolution failed for %s", role_name
                )
                hints.append(f"{role_name} ({type(err).__name__})")
                continue
            if iam is None:
                # E36/T8 fix 1 (spec I-C). Only reachable for an AMBIENT target (no stage, or a
                # stage with no deploy role) when the control-plane IAM client failed to build:
                # `self._iam` degrades to None there. Reported per ROLE rather than short-
                # circuiting the whole item, so a tenant-account role in the SAME cascade is
                # still reclaimed under its own assumed client.
                hints.append(f"{role_name} (no IAM client)")
                continue
            # Per-ROLE tolerance is preserved by construction: every name is attempted and each
            # one's own outcome is collected, so one denial neither strands the rest nor hides
            # which of N stage roles actually leaked.
            hint = self._reclaim_exec_role(role_name, iam)
            if hint is not None:
                hints.append(hint)
        return hints, assume_failures

    @staticmethod
    def _exec_role_targets_to_delete(agent, agent_name: str) -> List[tuple]:
        """:meth:`_exec_role_names_to_delete` with the STAGE kept — ``(stage, role name)``.

        E36/T8 needs the stage to pick the account the role lives in. The legacy un-scoped name
        carries stage ``None``: it belongs to no stage, so there is no ``deploy_role_arn`` to
        resolve for it and it is reclaimed with the ambient client — which is also where a
        pre-cross-account deployment actually put it."""
        stages = [s for s in resolve_runtime_arns(agent) if s and s != UNKNOWN_STAGE]
        targets = [(stage, agentcore_exec_role_name(agent_name, stage)) for stage in stages]
        legacy = legacy_agentcore_exec_role_name(agent_name)
        if legacy not in [name for _, name in targets]:
            targets.append((None, legacy))
        return targets

    @staticmethod
    def _exec_role_names_to_delete(agent, agent_name: str) -> List[str]:
        """Every exec-role name the cascade must attempt for ``agent`` — de-duped, order-stable.

        One name per stage the record knows about, UNION the pre-T1b un-scoped name.

        THE STAGES COME FROM ``agent_arns``, read through :func:`resolve_runtime_arns` — the same
        single resolver :meth:`_runtime_arns_to_delete` uses, so the roles reclaimed and the
        runtimes reclaimed can never be derived from two different ideas of which stages exist.
        The map's KEYS are the stages the buildspec actually deployed (C-A2), which is the only
        evidence available: nothing else on the record names a stage, and enumerating a guessed
        ``{"dev", "prod"}`` set would be a fabrication that also misses any other stage.

        :data:`UNKNOWN_STAGE` IS SKIPPED, and that is the whole migration subtlety. A legacy
        scalar-only record resolves to one entry under that placeholder key precisely because
        the record does not know its stage — so ``{agent_name}-unknown-agentcore-exec`` is a
        name Terraform never created. Deriving it would produce a guaranteed ``NoSuchEntity``,
        which this method's caller reads as success, while the role that DOES exist went
        untouched.

        THE LEGACY NAME IS ALWAYS ATTEMPTED, not only when the map is empty — for exactly the
        reason :meth:`_delete_runtime` unions the scalar in. An agent deployed under pre-T1b
        code and then redeployed once under T1b has a map naming only the redeployed stage,
        while a REAL role still sits at the old un-scoped name. A populated map is the inventory
        of what T1b knows about, not proof that nothing predates it. The cost of always trying
        is one already-tolerated ``NoSuchEntity`` pair; the cost of not trying is an
        account-global name leaked forever, which is finding #9 reproduced by its own fix.

        Kept as the name-only projection of :meth:`_exec_role_targets_to_delete` (E36/T8) so
        there is still ONE derivation of the role set."""
        return [name for _, name in ProjectService._exec_role_targets_to_delete(agent, agent_name)]

    def _reclaim_exec_role(self, role_name: str, iam) -> Optional[str]:
        """Delete ONE exec role + its inline policy. Never raises; see :meth:`_delete_exec_role`
        for the contract and for why the policy goes first.

        RETURNS a SAFE failure hint when the role was NOT reclaimed, else ``None`` (E28C/T5). This
        used to return nothing at all, and that silence was the defect: the live answer was ALWAYS
        ``AccessDenied`` (the ECS task role's ``iam:DeleteRole`` grant is scoped
        ``role/{prefix}-ecr-push-*``, which cannot match ``{agent}-{stage}-agentcore-exec``), the
        code logged it and returned, and the cascade reported ``deleted``. Six account-global roles
        leaked with a clean teardown report on every one.

        The hint carries the ROLE NAME and ``type(err).__name__`` and NOTHING ELSE. The name is the
        actionable fact — it is what the operator types into the IAM console — and the exception
        BODY is arbitrary provider text that can hold a request id, an ARN carrying the account id,
        or, on a wrapper exception, a credential. This string lands on a read model the console
        renders, so it follows the same fail-closed rule as :func:`_step_error_hint` and
        :meth:`_run_step`'s ``reason``.

        ``NoSuchEntity`` stays SUCCESS, on both calls: it is the idempotent already-done state that
        a pre-module repo, a half-failed provision and every second delete attempt all take —
        PROVIDED the question was asked in the account that owns the role. E36/T8 is what makes
        that proviso true: ``iam`` is the caller's per-stage client (:meth:`_stage_iam`), so a
        ``NoSuchEntity`` is now evidence about the right account rather than about the
        control-plane account, where a tenant's role was always absent."""
        hint: Optional[str] = None
        try:
            iam.delete_role_policy(RoleName=role_name, PolicyName=_AGENTCORE_EXEC_POLICY_NAME)
        except (ClientError, BotoCoreError) as err:
            if _iam_error_code(err) != "NoSuchEntity":
                logger.exception(
                    "[project] runtime exec-role policy delete failed for %s", role_name
                )
                # A surviving inline policy is not merely a leaked policy: IAM answers
                # `DeleteConflict` to `DeleteRole` while one remains, so the whole role leaks.
                hint = f"{role_name} ({type(err).__name__})"
        try:
            iam.delete_role(RoleName=role_name)
        except (ClientError, BotoCoreError) as err:
            if _iam_error_code(err) != "NoSuchEntity":
                logger.exception("[project] runtime exec-role delete failed for %s", role_name)
                # Overwrites a policy-delete hint deliberately: both calls concern ONE role, so
                # one line naming it is the whole fact. The role delete is the later and more
                # consequential of the two.
                hint = f"{role_name} ({type(err).__name__})"
        return hint

    def _run_github_step(self, project, repo) -> RepoDeleteItemResult:
        """Run the github teardown step, mapping a REMOVED connection to ``skipped`` (not
        failed). If the project references a connection that no longer exists, the GitHub repo
        is unreachable for good — a "failed" outcome would trap the record forever (failed
        blocks the record delete). So a ``ConnectionError(kind="not_found")`` yields
        ``skipped``, which does NOT block record removal; it is honest that the platform could
        not reach — let alone delete — the repo. Every OTHER exception (including a
        ``ConnectionError`` of a different kind, e.g. ``secret_error``, AND any non-
        ConnectionError) is a real, retryable problem and flows through the SAME failed
        mapping as :meth:`_run_step` (``type(err).__name__`` reason + ``logger.exception``)."""
        try:
            self._delete_github(project, repo)
            return RepoDeleteItemResult(item="github", outcome="deleted")
        except ConnectionError as err:
            if err.kind == "not_found":
                return RepoDeleteItemResult(
                    item="github",
                    outcome="skipped",
                    reason="connection removed — GitHub repo unreachable",
                )
            logger.exception("[project] teardown step 'github' failed")
            return RepoDeleteItemResult(item="github", outcome="failed", reason=type(err).__name__)
        except Exception as err:
            logger.exception("[project] teardown step 'github' failed")
            return RepoDeleteItemResult(item="github", outcome="failed", reason=type(err).__name__)

    # -- reachability probes (E23/T11, READ-ONLY) --------------------------

    def _probe_github(self, project, repo) -> str:
        """github state: connection missing → gone; repo_exists → present/gone; probe
        error → unknown. The token flows straight to the client — never logged/surfaced."""
        try:
            connection = self._conn.get_connection(project.connection_id)
            token = self._conn.get_bearer_token(project.connection_id)
            exists = self._rollout.repo_exists(
                connection.org, repo.name, token, connection.base_url
            )
            return "present" if exists else "gone"
        except ConnectionError as err:
            if err.kind == "not_found":
                # The connection is gone — the GitHub repo is unreachable for good.
                return "gone"
            logger.exception("[delete-preview] github probe failed for repo %s", repo.id)
            return "unknown"
        except Exception:
            logger.exception("[delete-preview] github probe failed for repo %s", repo.id)
            return "unknown"

    def _probe_image(self, agent_id: str) -> str:
        """image state: any matching image → present; none → gone; probe error → unknown.
        No injected cleaner ⇒ nothing to probe → gone."""
        if self._ecr is None:
            return "gone"
        try:
            return "present" if self._ecr.count_images(agent_id) > 0 else "gone"
        except Exception:
            logger.exception("[delete-preview] image probe failed for agent %s", agent_id)
            return "unknown"

    def _probe_runtime(
        self, agent, agent_arn: Optional[str], tenant_id: Optional[str] = None
    ) -> str:
        """runtime state, AGGREGATED over every per-stage runtime: any still exists → present;
        no agent/arn → gone (nothing provisioned); all gone → gone; otherwise unknown.

        AGGREGATION (E28A/T1). Since T1b an agent owns N runtimes but the preview still offers
        ONE ``runtime`` line-item (its shape is a frontend contract), so the N answers must
        collapse into one. The precedence is ``present`` > ``unknown`` > ``gone``, which is the
        only ordering that cannot mislead: reporting ``gone`` while prod's runtime is live would
        let an operator uncheck the box and leak it, and letting one AccessDenied probe mask a
        runtime another probe PROVED present would do the same. As before, a raise is
        ``unknown`` and never ``gone`` — AccessDenied is ambiguous (a live runtime behind an
        IAM/SCP/wrong-region misconfig returns it too) and must never be inferred as absent.

        SCOPE (E28A/T2): this reports on the AgentCore RUNTIME only. The ``runtime`` cascade
        step also reclaims two artifacts this does NOT probe — the Terraform state objects and
        the IAM exec role — so a ``gone`` here means "no runtime to delete", not "the runtime
        step has nothing left to do". Deliberately NOT extended to cover the role: the only
        honest probe would be ``iam:GetRole``, which this task role is not granted (see the
        exec-role reclaim's notes), so it would return ``unknown`` for EVERY repo — flipping
        genuinely-gone runtimes back to selectable-and-checked and making the preview less
        truthful, not more. The reclaim is idempotent, so acting on a stale ``gone`` is safe;
        the residual gap is that an operator who UNCHECKS a ``gone`` runtime skips a role
        reclaim they could not see was pending.

        CROSS-ACCOUNT (E36/T8), and this probe is why the seam cannot stop at the teardown.
        Left ambient it answers ``gone`` for EVERY tenant-account runtime, the modal presents it
        as already-deleted and UNCHECKED, and the operator skips the runtime step — leaking the
        runtime with no report at all, which defeats the honest reporting from the one screen
        that drives it. So each stage is probed under the account that owns it, and a failed
        AssumeRole takes the EXISTING ambiguous branch: ``unknown``, never ``gone``. Same rule
        AccessDenied already followed, for the same reason — "I could not look" is not "it is
        not there"."""
        targets = self._runtime_targets_to_delete(agent, agent_arn)
        if not targets:
            return "gone"
        agent_id = getattr(agent, "id", "") or ""
        ambiguous = False
        for stage, arn in targets:
            try:
                control = self._stage_control(tenant_id, stage, agent_id)
            except TenantCredentialsError as err:
                logger.warning(
                    "[delete-preview] runtime probe could not assume the tenant role for "
                    "stage '%s': %s",
                    stage,
                    err.message,
                )
                ambiguous = True
                continue
            except StageUnresolvedError as err:
                # E36/T8 fix 1: same rule. "I could not work out which account to ask" is not
                # "it is not there" — and probing the control-plane account instead would
                # answer `gone`, unchecking a live runtime in the modal.
                logger.warning(
                    "[delete-preview] runtime probe could not resolve the owning account for "
                    "stage '%s': %s",
                    stage,
                    err.message,
                )
                ambiguous = True
                continue
            except Exception:
                # A READ-ONLY probe must never 500 the preview route: an unreachable modal is
                # strictly worse than an `unknown` row, which the frontend already handles as
                # selectable+checked.
                logger.exception(
                    "[delete-preview] runtime client resolution failed for stage '%s'", stage
                )
                ambiguous = True
                continue
            try:
                if self._identity.runtime_exists(arn, control_client=control):
                    return "present"   # one live runtime settles it; no need to probe on
            except Exception:
                # RID-not-ARN — a full runtime ARN carries the AWS account id, which a hard
                # project rule bans anywhere, logs included. See :meth:`_delete_runtime`.
                logger.exception(
                    "[delete-preview] runtime probe failed for %s", arn.rsplit("/", 1)[-1]
                )
                ambiguous = True
        return "unknown" if ambiguous else "gone"

    def _probe_identity(self, agent) -> str:
        """identity state (stored-id heuristic — NO live Graph probe): the agent carries a
        stored ``entra_app_id``/``entra_sp_id`` → present; no agent or no ids → gone."""
        if agent is None:
            return "gone"
        if getattr(agent, "entra_app_id", None) or getattr(agent, "entra_sp_id", None):
            return "present"
        return "gone"

    # ===================================================================== #
    # Deployment history (E28/T3, contract C1) — APPEND-ONLY
    # ===================================================================== #

    def append_deployment(
        self,
        *,
        repo_id: str,
        agent_id: str,
        stage: str,
        image_tag: str,
        source_sha: Optional[str] = None,
        build_id: Optional[str] = None,
        outcome: DeploymentOutcome = DeploymentOutcome.STARTED,
        actor: Optional[str] = None,
        actor_kind: Optional[str] = None,
        completed_at: Optional[str] = None,
        error: Optional[str] = None,
    ) -> Deployment:
        """Append ONE delivery record to the ``deployment`` partition and return it.

        Append-only by construction: the sk carries ``started_at`` + a per-row id suffix, so a
        new attempt is always a NEW row and nothing is ever read-modify-written. That removes
        the whole clobber class the repository row suffers from (see :meth:`_save_repo`) and it
        is why history and rollback are possible at all — the ``last_promoted_*`` scalars are
        overwritten by the next promote, so they cannot be the record of what happened.

        ``stage`` is free-form and NOT validated against a dev/prod literal (D8). ``error`` is
        a SAFE short hint only — never a token or a response body. ``actor_kind``
        (``"github"``/``"entra"``) exists so an OIDC-proven login and an Entra oid are never
        rendered as the same currency.

        With no table configured the record lands in the local list instead (the same
        degrade-don't-crash fallback every other write here has)."""
        started_at = self._now().isoformat()
        id = new_deployment_id()
        record = Deployment(
            id=id,
            repo_id=repo_id,
            agent_id=agent_id,
            stage=stage,
            seq_key=deployment_seq_key(repo_id, stage, started_at, id),
            image_tag=image_tag,
            source_sha=source_sha,
            build_id=build_id,
            outcome=outcome,
            actor=actor,
            actor_kind=actor_kind,
            started_at=started_at,
            completed_at=completed_at,
            error=error,
        )
        self._save_deployment(record)
        return record

    def list_deployments(
        self, repo_id: str, stage: Optional[str] = None, limit: int = 50
    ) -> List[Deployment]:
        """The repo's delivery history, NEWEST FIRST — the whole repo, or one ``stage`` of it.

        Two read paths, because the sk is ``{repo_id}#{stage}#{started_at}#{suffix}`` and the
        stage component sits BEFORE the timestamp:

        * **One stage** (``stage`` given) — the sk order within that prefix IS time order, so
          DynamoDB does the sorting: ``begins_with(sk, f"{repo_id}#{stage}#")`` +
          ``ScanIndexForward=False`` + a server-side ``Limit``. Cheap and exact.
        * **All stages** (``stage=None``) — the prefix widens to ``f"{repo_id}#"``, where sk order
          is STAGE-major and only then chronological. Left to DynamoDB that returns stage-grouped
          runs rather than a history, and a ``Limit`` truncates to the alphabetically-first stage
          — it can drop the newest deployment outright, which would show a stale artifact as
          "current" on this product's highest-consequence verb. So this path pages the
          ``repo_id`` prefix itself (the :meth:`_scan_partition` ``LastEvaluatedKey`` loop),
          merges by ``started_at`` descending, and applies ``limit`` AFTER the merge.

        The C1 sk format is a pinned cross-task contract, so the asymmetry is deliberately fixed
        HERE rather than by making the sk time-major.

        ``limit`` therefore means "the newest N rows" on both paths. ``stage=None`` never widens
        past the repo to the whole partition.

        **Bounds on the cross-stage path** (E28/T2, finding P3) — it used to hold the repo's
        ENTIRE append-only history in memory per call, which was fine while nothing called it and
        is not once a repo page reads it:

        * **Memory** is bounded WITHOUT costing correctness: rows arrive newest-first *within*
          each stage, and the global newest-``limit`` set can hold at most ``limit`` rows from
          any one stage — so once ``limit`` rows of a stage have been seen, every later row of
          that stage is provably outranked and is dropped immediately. Retained rows are
          ``limit × distinct stages``, and the result is IDENTICAL to the unbounded read.
        * **Round-trips** are bounded by ``_MAX_DEPLOYMENT_PAGES``, and this bound is NOT free:
          **a ceiling hit can omit the true newest deployment entirely.** Paging is
          STAGE-MAJOR (the sk puts stage before time), so one early-sorting stage can consume
          the whole page budget on its own and a later-sorting stage is then never read at all
          — e.g. 25 pages of ``prod`` history plus one newest ``dev`` row returns a ``prod`` row
          as current and omits the ``dev`` one. That is the same failure class as the DDB
          ``Limit`` this method refuses (a stale artifact presented as current), merely moved to
          a far higher threshold. Note the per-stage drop above does NOT help here: it bounds
          MEMORY, not queries — a dropped row has still cost its page.

          The ceiling is kept anyway, because the alternative is a request that never returns,
          and at ~1 MB/page it takes tens of thousands of rows to reach: remote, not impossible.
          **The ``logger.warning`` on a ceiling hit is the ONLY signal that the answer may be
          wrong rather than merely short** — nothing in the return value distinguishes the two.
          If that warning is ever seen in practice, this needs a time-major secondary index, not
          a bigger ceiling.

        Tolerant like :func:`_parse_rows`: a malformed row is SKIPPED and logged, and an
        unreachable store returns ``[]``. An empty history is a UI state; a 500 is not."""
        if stage:
            return self._query_stage_deployments(repo_id, stage, limit)
        rows = self._all_stage_deployments(repo_id, limit)
        # Merge across stages: started_at is the only cross-stage clock, seq_key breaks a
        # same-millisecond tie deterministically (it carries the unique id suffix).
        rows.sort(key=lambda d: (d.started_at, d.seq_key), reverse=True)
        return rows[:limit]

    def _query_stage_deployments(self, repo_id: str, stage: str, limit: int) -> List[Deployment]:
        """One stage, newest-first, sorted and truncated by DynamoDB (its sk order is time order
        inside a single ``{repo_id}#{stage}#`` prefix)."""
        prefix = f"{repo_id}#{stage}#"
        if self._has_ddb:
            try:
                resp = self._table.query(
                    KeyConditionExpression=Key("pk").eq(_DEPLOYMENT_PK)
                    & Key("sk").begins_with(prefix),
                    ScanIndexForward=False,  # newest first (time-major within one stage)
                    Limit=limit,
                )
                return _parse_rows(Deployment, resp.get("Items", []))
            except ClientError:
                logger.exception("Failed to load deployments for repo %s from DDB", repo_id)
                return []
        rows = self._local_deployments_with_prefix(prefix)
        rows.sort(key=lambda d: d.seq_key, reverse=True)  # mirror ScanIndexForward=False
        return rows[:limit]

    def _all_stage_deployments(self, repo_id: str, limit: int) -> List[Deployment]:
        """The repo's rows that can still make the newest-``limit`` set, unordered — the input to
        the cross-stage merge.

        Reads the ``f"{repo_id}#"`` prefix with NO ``Limit`` and follows ``LastEvaluatedKey``:
        DynamoDB would truncate in stage order, so "newest" computed off a truncated page is a
        silently WRONG answer, not a short one.

        Bounded two ways (P3) — see :meth:`list_deployments` for the argument. Every retained
        row is parsed and counted PER STAGE as it arrives, and once a stage has contributed
        ``limit`` rows its remaining (strictly older, because ``ScanIndexForward=False`` orders
        by time within a stage) rows are discarded rather than accumulated. Paging itself stops
        at ``_MAX_DEPLOYMENT_PAGES``, which is a real ceiling and is logged when hit."""
        if not self._has_ddb:
            rows = self._local_deployments_with_prefix(f"{repo_id}#")
            return _bound_per_stage(sorted(rows, key=lambda d: d.seq_key, reverse=True), limit)

        kept: List[Deployment] = []
        seen: dict = {}
        kwargs = {
            "KeyConditionExpression": Key("pk").eq(_DEPLOYMENT_PK)
            & Key("sk").begins_with(f"{repo_id}#"),
            "ScanIndexForward": False,
        }
        try:
            for page in range(_MAX_DEPLOYMENT_PAGES):
                resp = self._table.query(**kwargs)
                for row in _parse_rows(Deployment, resp.get("Items", [])):
                    if seen.get(row.stage, 0) >= limit:
                        continue  # provably outranked: `limit` newer rows of this stage are held
                    seen[row.stage] = seen.get(row.stage, 0) + 1
                    kept.append(row)
                lek = resp.get("LastEvaluatedKey")
                if not lek:
                    return kept
                kwargs["ExclusiveStartKey"] = lek
        except ClientError:
            logger.exception("Failed to load deployments for repo %s from DDB", repo_id)
            return []
        logger.warning(
            "[project] deployment history for repo %s exceeded the %d-page read ceiling — this "
            "result MAY OMIT THE NEWEST DEPLOYMENT: paging is stage-major, so an early-sorting "
            "stage can exhaust the budget and a later stage go unread. Treat any 'current' "
            "artifact derived from it as unverified",
            repo_id,
            _MAX_DEPLOYMENT_PAGES,
        )
        return kept

    def _local_deployments_with_prefix(self, prefix: str) -> List[Deployment]:
        """The local-fallback read: copies of every appended row whose sk carries ``prefix``."""
        with self._local_lock:
            return [
                d.model_copy(deep=True)
                for d in self._local_deployments
                if d.seq_key.startswith(prefix)
            ]

    def _save_deployment(self, record: Deployment) -> None:
        """``put_item`` the row under its composite sk. A plain put is safe here precisely
        BECAUSE the sk is unique per append — there is no existing row to replace."""
        if self._has_ddb:
            self._table.put_item(Item=_to_item(_DEPLOYMENT_PK, record, sk=record.seq_key))
            return
        with self._local_lock:
            self._local_deployments.append(record.model_copy(deep=True))

    # ===================================================================== #
    # Persistence (DDB-or-local, mirror connection_service.py)
    # ===================================================================== #

    def _get_project(self, id: str) -> Optional[Project]:
        if self._has_ddb:
            try:
                resp = self._table.get_item(Key={"pk": _PROJECT_PK, "sk": id})
                item = resp.get("Item")
                return self._hydrate_project(item) if item else None
            except ClientError:
                logger.exception("Failed to fetch project %s from DDB", id)
                return None
        with self._local_lock:
            record = self._local_projects.get(id)
            return record.model_copy(deep=True) if record else None

    def _load_all_projects(self) -> List[Project]:
        if self._has_ddb:
            try:
                items = self._scan_partition(_PROJECT_PK)
                # E22 resilience (skip malformed rows) + E24 hydration (legacy
                # tenant_id default): try each row, skip what fails validation.
                projects: List[Project] = []
                for item in items:
                    try:
                        projects.append(self._hydrate_project(item))
                    except (ValidationError, ValueError):
                        logger.warning(
                            "Skipping malformed Project row sk=%s (failed validation)",
                            item.get("sk"),
                        )
                return projects
            except ClientError:
                logger.exception("Failed to load projects from DDB")
                return []
        with self._local_lock:
            return [p.model_copy(deep=True) for p in self._local_projects.values()]

    @staticmethod
    def _hydrate_project(item: dict) -> Project:
        """Validate a stored project item; a legacy pre-E24 record WITHOUT ``tenant_id``
        hydrates as ``tenant_id="default"`` (matches the T9 seed tenant) so the required
        field never breaks reads of pre-existing data."""
        data = _strip_keys(item)
        data.setdefault("tenant_id", "default")
        return Project.model_validate(data)

    def _save_project(self, record: Project) -> None:
        if self._has_ddb:
            self._table.put_item(Item=_to_item(_PROJECT_PK, record))
            return
        with self._local_lock:
            self._local_projects[record.id] = record.model_copy(deep=True)

    def _load_repos_for(self, project_id: str) -> List[Repository]:
        return [r for r in self._load_all_repos() if r.project_id == project_id]

    def _load_all_repos(self) -> List[Repository]:
        if self._has_ddb:
            try:
                items = self._scan_partition(_REPOSITORY_PK)
                return _parse_rows(Repository, items)
            except ClientError:
                logger.exception("Failed to load repositories from DDB")
                return []
        with self._local_lock:
            return [r.model_copy(deep=True) for r in self._local_repos.values()]

    def _save_repo(
        self,
        record: Repository,
        *,
        include_cicd_status: bool = False,
        include_prod_candidate: bool = False,
    ) -> None:
        """Persist a repository record WITHOUT clobbering the attributes another writer owns
        out-of-band (E27/T7, extended by E27A/T5).

        A whole-item ``put_item`` replaces the row, so a backend save of a record that was
        read *before* the buildspec's targeted ``update-item`` would wipe
        ``last_dev_image_tag`` (and ``cicd_status``) back to the stale value it read — the
        one cause of a missing tag the promote route cannot distinguish from "never
        deployed". This writes a targeted ``SET`` over only the attributes the backend
        owns instead, leaving co-owned attributes untouched.

        Three ownership classes, each with the same failure mode if it leaks:

        * ``last_dev_image_tag`` and ``last_dev_digest`` are CodeBuild-EXCLUSIVE — the backend
          never writes either. The digest joined the skip set with the field (E28B/T4): it has the
          same single out-of-band writer, so leaving it out would let any backend save revert the
          digest dev is running to a pre-build snapshot while the tag beside it stayed correct —
          a DISAGREEING pair, which is worse than either being stale alone.
        * :data:`_PROD_CANDIDATE_FIELDS` is CANDIDATE-ROUTE-EXCLUSIVE (E27A): they are stamped
          only by :meth:`record_prod_candidate` (which bypasses this method entirely for a
          targeted write — see :meth:`_save_prod_candidate`) and cleared only by a successful
          :meth:`promote_repo`, which opts in via ``include_prod_candidate``. Every
          OTHER save must skip them — a late CodeBuild write-back, a materialize step save or a
          status transition that carried a pre-merge snapshot would otherwise reset the
          candidate to ``None``, and promote cannot tell a wiped candidate from "nothing has
          merged to main", so the Promote button would silently stop working.
        * ``cicd_status`` is co-owned, so a caller that genuinely means to transition it
          (create / retry / finalize / mark-failed / promote) opts in via
          ``include_cicd_status``.

        The opt-ins are INDEPENDENT: clearing a candidate on promote also transitions
        ``cicd_status``, but writing one must NOT (registering a candidate is a merge to
        ``main``, not a deployment)."""
        skip = {"last_dev_image_tag", "last_dev_digest"}
        if not include_cicd_status:
            skip.add("cicd_status")
        if not include_prod_candidate:
            skip.update(_PROD_CANDIDATE_FIELDS)
        self._write_repo(record, skip)

    def _save_prod_candidate(self, record: Repository) -> None:
        """Persist ONLY the prod-candidate block + ``updated_at`` (E27A/T5 fix).

        The inverse of :meth:`_save_repo`: instead of SETting everything except another
        writer's attributes, this names exactly the attributes this writer OWNS
        (:data:`_CANDIDATE_WRITE_FIELDS`) and skips the entire rest of the record.

        That is the point. :meth:`record_prod_candidate` resolves its record by SCANNING the
        partition, so its read is stale by construction; a whole-record save would re-SET
        ``last_promoted_*`` from that stale snapshot and revert a promotion that started in
        the meantime — which defeats :meth:`_promotion_in_flight` (bounded FROM
        ``last_promoted_at``) and lets a second CodeBuild run race the same Terraform state
        key. Writing only what we own removes this writer from the read-modify-write pattern
        entirely; nothing else on the row can be clobbered by a merge to ``main``."""
        skip = set(Repository.model_fields) - _CANDIDATE_WRITE_FIELDS
        self._write_repo(record, skip)

    def _write_repo(self, record: Repository, skip: set) -> None:
        """The single repository write path: a targeted ``SET`` over every serialized attribute
        of ``record`` EXCEPT ``skip``, with the local dict fallback mirroring it exactly (an
        attribute the ``SET`` does not NAME survives in DDB, so locally it is copied back off
        the previous row)."""
        if self._has_ddb:
            self._table.update_item(**_update_kwargs(_REPOSITORY_PK, record, skip))
            return
        with self._local_lock:
            merged = record.model_copy(deep=True)
            previous = self._local_repos.get(record.id)
            if previous is not None:  # mirror the DDB "un-named attributes survive" semantics
                for field in skip:
                    setattr(merged, field, getattr(previous, field))
            self._local_repos[record.id] = merged

    def _get_repo(self, repo_id: str) -> Optional[Repository]:
        if self._has_ddb:
            try:
                resp = self._table.get_item(Key={"pk": _REPOSITORY_PK, "sk": repo_id})
                item = resp.get("Item")
                return Repository.model_validate(_strip_keys(item)) if item else None
            except ClientError:
                logger.exception("Failed to fetch repository %s from DDB", repo_id)
                return None
        with self._local_lock:
            record = self._local_repos.get(repo_id)
            return record.model_copy(deep=True) if record else None

    def _delete_repo(self, repo_id: str) -> None:
        if self._has_ddb:
            self._table.delete_item(Key={"pk": _REPOSITORY_PK, "sk": repo_id})
            return
        with self._local_lock:
            self._local_repos.pop(repo_id, None)

    def _delete_project(self, project_id: str) -> None:
        if self._has_ddb:
            self._table.delete_item(Key={"pk": _PROJECT_PK, "sk": project_id})
            return
        with self._local_lock:
            self._local_projects.pop(project_id, None)

    def _scan_partition(self, pk: str) -> List[dict]:
        items: List[dict] = []
        kwargs = {"KeyConditionExpression": Key("pk").eq(pk)}
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        return items


def _skipped(item: str) -> RepoDeleteItemResult:
    """A teardown step the operator did not select — skipped, never failed (E23/T4)."""
    return RepoDeleteItemResult(item=item, outcome="skipped", reason="not selected")


def _exec_role_item(
    hints: Optional[List[str]], assume_failures: Optional[List[str]] = None
) -> RepoDeleteItemResult:
    """The ``exec_role`` cascade line-item, from :meth:`ProjectService._delete_exec_role`'s
    outcome (E28C/T5).

    ``assume_failures`` (E36/T8) WINS over everything else and is a FOURTH state: the platform
    never got into the account that holds the role, so nothing it could say about the role is
    evidence. It reports ``failed`` with the pinned ``assume_role_failed:`` prefix — a distinct
    prefix because the operator action is distinct: not "retry" (a retry cannot mint a trust
    relationship) but "grant the trust, or reclaim by hand in the tenant account". ``failed``
    rather than a new outcome VALUE because the delete modal keys its Retry affordance and its
    "some steps failed" alert off exactly that string (``DeleteRepositoryModal.tsx``) and closes
    itself when nothing is ``failed`` — a novel value would render the honest report for one
    frame and then hide it, which is this defect wearing the modal's clothes. It stays
    NON-BLOCKING for the record via the caller's gating predicate, like every other
    ``exec_role`` failure.

    Then three states, and the distinction between the first two is the point:

    * ``None`` — nothing was ATTEMPTED, because there is no agent name to derive a role name
      from ⇒ ``skipped``. Reporting ``deleted`` here would claim a teardown that never ran, which
      is the original defect in a different costume. The reason string is now EXACT: E36/T8 fix 1
      removed the ``self._iam is None`` half of that gate, which used to reach this branch — and
      this wording — for a missing control-plane client while the agent name was perfectly fine.
      A missing client is reported per role instead (``… (no IAM client)``).
    * ``[]`` — every candidate role is gone (including via ``NoSuchEntity``, the idempotent
      already-done state) ⇒ ``deleted``, with NO reason: a role name on a green row is noise.
    * non-empty — those roles SURVIVE ⇒ ``failed``, naming them. The names are the actionable
      fact; each hint carries a role name and an exception TYPE and nothing from the exception
      body (see :meth:`ProjectService._reclaim_exec_role`), because this string renders in the
      console.
    """
    if assume_failures:
        return RepoDeleteItemResult(
            item="exec_role",
            outcome="failed",
            reason=f"assume_role_failed: {', '.join(dict.fromkeys(assume_failures))}",
        )
    if hints is None:
        return RepoDeleteItemResult(
            item="exec_role", outcome="skipped", reason="no agent record to derive the role name"
        )
    if not hints:
        return RepoDeleteItemResult(item="exec_role", outcome="deleted")
    return RepoDeleteItemResult(
        item="exec_role", outcome="failed", reason=f"not reclaimed: {', '.join(hints)}"
    )


def _to_item(pk: str, record, sk: Optional[str] = None) -> dict:
    """Serialize a record into a DDB item. ``sk`` defaults to the record's id (projects,
    repositories — one row per entity); the append-only deployment partition passes its
    composite sort key instead."""
    return {"pk": pk, "sk": sk or record.id, **json.loads(record.model_dump_json())}


def _update_kwargs(pk: str, record, skip: set) -> dict:
    """Build ``Table.update_item`` kwargs that SET every serialized attribute of ``record``
    EXCEPT the names in ``skip`` (E27/T7 — attributes another writer owns).

    An upsert, exactly like ``put_item`` for a new row, but it never removes or resets an
    attribute it does not name. Every name goes through ``ExpressionAttributeNames`` because
    the model carries DynamoDB reserved words (``status``, ``name``)."""
    data = {k: v for k, v in json.loads(record.model_dump_json()).items() if k not in skip}
    names = {f"#n{i}": k for i, k in enumerate(data)}
    values = {f":v{i}": data[k] for i, k in enumerate(data)}
    return {
        "Key": {"pk": pk, "sk": record.id},
        "UpdateExpression": "SET " + ", ".join(f"{n} = :v{i}" for i, n in enumerate(names)),
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
    }


def _strip_keys(item: dict) -> dict:
    return {k: v for k, v in item.items() if k not in ("pk", "sk")}


def _bound_per_stage(rows, limit: int):
    """Keep at most ``limit`` rows per stage from ``rows``, which MUST already be newest-first
    within each stage. The local-fallback half of the P3 bound — it mirrors what the DDB loop
    does per page so both read paths hold the same amount of history."""
    seen: dict = {}
    kept = []
    for row in rows:
        if seen.get(row.stage, 0) >= limit:
            continue
        seen[row.stage] = seen.get(row.stage, 0) + 1
        kept.append(row)
    return kept


def _parse_rows(model, items):
    """Validate DDB rows into ``model`` records, SKIPPING (not raising on) any malformed
    row so a single pre-rename / partial-write item never 500s the whole list. Logs the
    row's ``sk`` (never the full row — could be large) on skip."""
    records = []
    for item in items:
        try:
            records.append(model.model_validate(_strip_keys(item)))
        except (ValidationError, ValueError):
            logger.warning(
                "Skipping malformed %s row sk=%s (failed validation)",
                model.__name__,
                item.get("sk"),
            )
    return records
