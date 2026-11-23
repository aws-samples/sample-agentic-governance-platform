"""The teardown steps and probes for one agent's artifacts: the GitHub repo, the image, every per-stage
runtime and its Terraform state, the exec roles, the identity and the Langfuse project.

Functions over a ``ProjectService`` passed as ``svc``, whose collaborators are read at call time.
:mod:`services.project_teardown` drives them, and ``ProjectService`` delegates ``_stage_cfg`` here. The
naming functions derive the state keys and exec-role names Terraform creates.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import TYPE_CHECKING, List, Optional

from botocore.exceptions import BotoCoreError, ClientError

from models.agent import UNKNOWN_STAGE, is_harness_arn, resolve_runtime_arns
from models.repository import RepoDeleteItemResult
from services.connection_service import ConnectionError
from services.project_materialize import _await
from services.tenant_credentials import StageUnresolvedError, TenantCredentialsError

if TYPE_CHECKING:
    from services.project_service import ProjectService

logger = logging.getLogger(__name__)

_RUNTIME_STATE_PREFIX = "agentcore-runtime"
_MAX_STATE_LIST_PAGES = 5


def runtime_state_key(agent_id: str, stage: str) -> str:
    """The S3 key of one agent's runtime Terraform state FOR ONE STAGE.

    ``stage`` is free-form — a tenant's stage set is open, so this must scope by whatever
    the stage is called (`uat`, a region name, …) and never by a dev/prod literal."""
    return f"{runtime_state_prefix(agent_id)}{stage}/terraform.tfstate"


def runtime_state_prefix(agent_id: str) -> str:
    """The S3 prefix holding EVERY stage's state for one agent (trailing slash).

    This exists because stage-scoping turned "one agent → one state object" into "one agent →
    N objects", so teardown has to reclaim a prefix rather than delete a known key."""
    return f"{_RUNTIME_STATE_PREFIX}/{agent_id}/"


_AGENTCORE_EXEC_ROLE_SUFFIX = "agentcore-exec"
_AGENTCORE_EXEC_POLICY_NAME = "runtime-exec"


def agentcore_exec_role_name(agent_name: str, stage: str) -> str:
    """The IAM exec-role name Terraform gives ONE STAGE of one agent's AgentCore runtime.

    Derived from the agent NAME (the registry record's ``name``) and the STAGE, the two values the
    buildspec feeds the module as ``agent_name`` / ``stage``, never from the agent id. Terraform
    creates the role and this module only deletes it, so the test suite pins this producer against
    the real module.

    STAGE IS REQUIRED: a defaulted stage would derive one real role name and miss the others, and
    a missing role reads as ``NoSuchEntity`` (success). The un-scoped name older deployments carry
    is produced by :func:`legacy_agentcore_exec_role_name`, so reclaiming it is an explicit
    choice."""
    return f"{agent_name}-{stage}-{_AGENTCORE_EXEC_ROLE_SUFFIX}"


def legacy_agentcore_exec_role_name(agent_name: str) -> str:
    """The un-stage-scoped exec-role name Terraform produced before the module scoped it, for
    reclaim ONLY. IAM role names are account-global, so a cascade that only tried the per-stage
    names would leak this one forever and a re-materialize of the same agent would hit
    ``EntityAlreadyExists``. Its own function so no creation path reaches it by accident."""
    return f"{agent_name}-{_AGENTCORE_EXEC_ROLE_SUFFIX}"


def _iam_error_code(err: Exception) -> str:
    """The AWS error code off a boto exception, or ``""``.

    Tolerates a ``BotoCoreError`` (a client-side transport failure carries no ``response``),
    which is why this reads through ``getattr`` rather than indexing ``err.response``."""
    response = getattr(err, "response", None) or {}
    return response.get("Error", {}).get("Code") or ""


def _run_step(svc: ProjectService, item: str, action) -> RepoDeleteItemResult:
    """Run one best-effort teardown ``action``; map success/failure to a result. A raised
    exception → ``outcome="failed"`` with a SAFE reason (``type(err).__name__`` — never a
    token/body) + ``logger.exception``.

    One exception is pinned out of the generic mapping. A
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
        return RepoDeleteItemResult(item=item, outcome="failed", reason=f"assume_role_failed: {err.message}")
    except StageUnresolvedError as err:
        logger.exception("[project] teardown step '%s' could not resolve the owning account", item)
        return RepoDeleteItemResult(item=item, outcome="failed", reason=f"stage_unresolved: {err.message}")
    except Exception as err:
        logger.exception("[project] teardown step '%s' failed", item)
        return RepoDeleteItemResult(item=item, outcome="failed", reason=type(err).__name__)


def _stage_cfg(svc: ProjectService, tenant_id: Optional[str], stage: Optional[str]):
    """The :class:`TenantStageConfig` for ``stage``, or None ⇒ use the ambient client.
    Raises :class:`StageUnresolvedError` when the stage IS named but unresolvable.

    None is returned only when the account is genuinely NOT KNOWN and ambient is therefore the
    right account: no ``tenant_id`` or tenant service (the legacy wiring), or no stage /
    :data:`UNKNOWN_STAGE` (a scalar-only record that does not know which account its runtime
    belongs to). Everything else raises: a tenant lookup that fails, a missing tenant record,
    or a tenant that no longer lists the stage. In all three the resource may exist in an
    account we did not identify, so the caller reports the item failed rather than asking the
    control-plane account and believing its NotFound. A stage with an empty
    ``deploy_role_arn`` is deploy-in-place and stays ambient by design.
    """
    if not tenant_id or not stage or stage == UNKNOWN_STAGE or svc._tenants is None:
        return None
    try:
        tenant = svc._tenants.get(tenant_id)
    except Exception as err:
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
            "[project] the tenant record does not list stage '%s'; the owning account is unknown",
            stage,
        )
        raise StageUnresolvedError(f"tenant no longer lists stage '{stage}'")
    return stages[stage]


def _stage_iam(svc: ProjectService, tenant_id: Optional[str], stage: Optional[str], agent_id: str):
    """The IAM client for ``stage``'s account. Raises :class:`TenantCredentialsError` or
    :class:`StageUnresolvedError`.

    Returns ``svc._iam`` unchanged unless the resolved stage carries a
    ``deploy_role_arn``. Deliberately NOT routed through the seam's own ambient branch:
    ``svc._iam`` is the client the whole pre-existing suite injects, and building a fresh
    one per role per stage would spend a client construction to reach the same account.
    """
    cfg = _stage_cfg(svc, tenant_id, stage)
    if cfg is None or not cfg.deploy_role_arn:
        return svc._iam
    return svc._stage_client("iam", cfg, session_suffix=f"teardown-{agent_id}")


def _stage_control(svc: ProjectService, tenant_id: Optional[str], stage: Optional[str], agent_id: str):
    """The AgentCore control client for ``stage``'s account, or None ⇒ the identity
    service's own ambient client. Raises :class:`TenantCredentialsError` or
    :class:`StageUnresolvedError`."""
    cfg = _stage_cfg(svc, tenant_id, stage)
    if cfg is None or not cfg.deploy_role_arn:
        return None
    return svc._stage_client("bedrock-agentcore-control", cfg, session_suffix=f"teardown-{agent_id}")


def _delete_github(svc: ProjectService, project, repo) -> None:
    """Delete the materialized GitHub repo (idempotent 404→ok in the write client). The
    connection token flows straight to the client — never logged, never surfaced."""
    connection = svc._conn.get_connection(project.connection_id)
    token = svc._conn.get_bearer_token(project.connection_id)
    svc._rollout.delete_repo(connection.org, repo.name, token, connection.base_url)


def _delete_image(svc: ProjectService, agent_id: str) -> None:
    """Delete the agent's ECR images (idempotent). No injected cleaner ⇒ no-op."""
    if svc._ecr is None:
        return
    svc._ecr.delete_images(agent_id)


def _delete_runtime(
    svc: ProjectService, agent, agent_arn: Optional[str], agent_id: str, tenant_id: Optional[str] = None
) -> None:
    """Delete EVERY per-stage AgentCore runtime (idempotent), then best-effort the TF-state
    objects.

    An agent owns one runtime PER STAGE: every ARN :func:`resolve_runtime_arns` resolves, plus
    a legacy scalar the map does not name (a pre-stage-scoping deploy redeployed once has a map
    naming only the redeployed stage while the scalar still names a running runtime), de-duped.
    Each runtime is deleted under the credentials of the account that OWNS it, resolved per
    stage via :func:`_stage_control`; a failed AssumeRole, an unresolvable stage or a client
    construction error is recorded, never retried ambiently, and the loop continues to the
    other stages. The step still FAILS after attempting them all when any runtime survives,
    with assume failures first, unresolved stages second, in the raised message. No agent or
    ARN is an already-done success; a state delete failure is logged, never raised.

    The exec-role reclaim is driven by :func:`services.project_teardown.delete_repo` as its own non-blocking
    item, on the agent's NAME rather than an ARN; once the registry entry is gone a retry has nothing to
    derive the name from and the role must be removed by hand."""
    targets = _runtime_targets_to_delete(agent, agent_arn)
    if targets:
        failures: List[str] = []
        assume_failures: List[str] = []
        unresolved: List[str] = []
        for stage, arn in targets:
            try:
                control = _stage_control(svc, tenant_id, stage, agent_id)
            except TenantCredentialsError as err:
                logger.warning(
                    "[project] runtime delete could not assume the tenant role for stage '%s': %s",
                    stage,
                    err.message,
                )
                assume_failures.append(err.message)
                failures.append(arn)
                continue
            except StageUnresolvedError as err:
                logger.warning(
                    "[project] runtime delete could not resolve the owning account for stage '%s': %s",
                    stage,
                    err.message,
                )
                unresolved.append(err.message)
                failures.append(arn)
                continue
            except Exception:
                logger.exception("[project] runtime client resolution failed for stage '%s'", stage)
                failures.append(arn)
                continue
            try:
                svc._identity.delete_runtime(arn, control_client=control)
            except Exception:
                logger.exception("[project] runtime delete failed for %s", arn.rsplit("/", 1)[-1])
                failures.append(arn)
        _delete_runtime_state(svc, agent_id)
        if assume_failures:
            raise TenantCredentialsError(
                f"{len(failures)} of {len(targets)} runtimes could not be deleted: "
                + "; ".join(dict.fromkeys(assume_failures))
            )
        if unresolved:
            raise StageUnresolvedError(
                f"{len(failures)} of {len(targets)} runtimes could not be deleted: "
                + "; ".join(dict.fromkeys(unresolved))
            )
        if failures:
            raise RuntimeError(f"{len(failures)} of {len(targets)} runtimes could not be deleted")


def _runtime_targets_to_delete(agent, agent_arn: Optional[str]) -> List[tuple]:
    """Every runtime the cascade must reclaim, as ``(stage, ARN)`` — de-duped, order-stable.

    The STAGE selects the tenant stage whose account owns that runtime. A scalar the map does not
    name carries stage ``None`` — the record does not know its account, so the teardown
    stays on the ambient client rather than assuming into a guessed one."""
    if agent is None:
        return []
    targets: List[tuple[Optional[str], str]] = list(resolve_runtime_arns(agent).items())
    named = {arn for _, arn in targets}
    if agent_arn and agent_arn not in named:
        targets.append((None, agent_arn))
    return targets


def _delete_identity(svc: ProjectService, agent) -> None:
    """Drive ``identity.delete_identity(agent)`` (idempotent): any awaitable it returns is run
    to completion on a fresh loop, as in :func:`services.project_materialize._provision_identity`.
    No agent ⇒ already-done success."""
    if agent is None:
        return
    result = svc._identity.delete_identity(agent)
    if inspect.isawaitable(result):
        asyncio.run(_await(result))


def _delete_langfuse(svc: ProjectService, agent) -> None:
    """Best-effort/idempotent teardown of the agent's Langfuse project + SM secret.

    Drives ``LangfuseProvisioningService.delete_agent_project`` — itself idempotent
    + best-effort (already-gone == success; it does not raise). It reads the agent
    envelope's ``langfuse_project_id`` / ``langfuse_key_secret_name``, so this MUST run
    before the record step deletes the registry entry. No injected provisioner (Langfuse
    unconfigured) ⇒ no-op; no agent ⇒ already-done success — either maps to a ``deleted``
    (success) line-item via :func:`_run_step`, exactly like the other no-op-able steps.

    This runs under :func:`_run_step` so it is REPORTED as a cascade line-item: a raise
    is caught by ``_run_step`` and surfaced as ``outcome="failed"`` (SAFE reason
    ``type(err).__name__`` — never a secret value/key) so the operator SEES the failure.
    It is kept NON-BLOCKING by the record-gating predicate in :func:`services.project_teardown.delete_repo`,
    which EXCLUDES the ``langfuse`` item — so a Langfuse failure can never abort the cascade nor
    keep the DDB row for retry. Hence, UNLIKE the sibling steps, we do NOT swallow here."""
    if svc._langfuse is None or agent is None:
        return
    svc._langfuse.delete_agent_project(agent)


def _delete_runtime_state(svc: ProjectService, agent_id: str) -> None:
    """Best-effort delete of EVERY stage's runtime TF-state object for this agent.

    The key is stage-scoped (:func:`runtime_state_key`), so one agent owns N
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
    if not svc._runtime_state_bucket or svc._s3 is None:
        return
    prefix = runtime_state_prefix(agent_id)
    try:
        keys = _list_runtime_state_keys(svc, prefix)
    except (ClientError, BotoCoreError):
        logger.exception("[project] runtime TF-state listing failed for %s", agent_id)
        return
    for key in keys:
        try:
            svc._s3.delete_object(Bucket=svc._runtime_state_bucket, Key=key)
        except (ClientError, BotoCoreError):
            logger.exception("[project] runtime TF-state delete failed for key %s", key)


def _list_runtime_state_keys(svc: ProjectService, prefix: str) -> List[str]:
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
        kwargs = {"Bucket": svc._runtime_state_bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = svc._s3.list_objects_v2(**kwargs)
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


def _delete_exec_role(svc: ProjectService, agent, tenant_id: Optional[str] = None) -> tuple:
    """Reclaim the runtime's IAM exec roles + their inline policies, and REPORT the outcome.

    NEVER RAISES, BUT NEVER SILENT EITHER: a raise would flip an already-deleted runtime to
    failed and trap the row behind the leaked role, and a swallow that also discarded the
    report let account-global roles accumulate behind clean teardowns.
    :func:`services.project_teardown.delete_repo` turns the return value into its own non-blocking
    ``exec_role`` item.

    Returns ``(hints, assume_failures)``: ``hints`` is ``None`` when nothing was ATTEMPTED (no
    agent name to derive a role name from, the only gate), ``[]`` when every candidate role is
    gone, else the SAFE per-role hints for the roles that SURVIVE; ``assume_failures`` names
    the stages whose AssumeRole failed, a different operator action. One role per stage plus
    the legacy un-scoped name is attempted (:func:`_exec_role_targets_to_delete`), each under
    its own account's IAM client (:func:`_stage_iam`); tolerance is per role, and a missing
    ambient client is reported per role rather than short-circuiting the item. ``NoSuchEntity``
    is the already-done state, now asked of the account that owns the role."""
    agent_name = getattr(agent, "name", None) if agent else None
    if not agent_name:
        return None, []
    agent_id = getattr(agent, "id", "") or ""
    hints: List[str] = []
    assume_failures: List[str] = []
    for stage, role_name in _exec_role_targets_to_delete(agent, agent_name):
        try:
            iam = _stage_iam(svc, tenant_id, stage, agent_id)
        except TenantCredentialsError as err:
            logger.warning(
                "[project] exec-role reclaim could not assume the tenant role for stage '%s': %s",
                stage,
                err.message,
            )
            assume_failures.append(err.message)
            continue
        except StageUnresolvedError as err:
            logger.warning(
                "[project] exec-role reclaim could not resolve the owning account for stage '%s': %s",
                stage,
                err.message,
            )
            hints.append(f"{role_name} (stage_unresolved)")
            continue
        except Exception as err:
            logger.exception("[project] exec-role client resolution failed for %s", role_name)
            hints.append(f"{role_name} ({type(err).__name__})")
            continue
        if iam is None:
            hints.append(f"{role_name} (no IAM client)")
            continue
        hint = _reclaim_exec_role(svc, role_name, iam)
        if hint is not None:
            hints.append(hint)
    return hints, assume_failures


def _exec_role_targets_to_delete(agent, agent_name: str) -> List[tuple]:
    """Every exec role the cascade must attempt, as ``(stage, role name)`` — de-duped, order-stable.

    The stage picks the account the role lives in. The legacy un-scoped name
    carries stage ``None``: it belongs to no stage, so there is no ``deploy_role_arn`` to
    resolve for it and it is reclaimed with the ambient client — which is also where a
    pre-cross-account deployment actually put it."""
    stages = [s for s in resolve_runtime_arns(agent) if s and s != UNKNOWN_STAGE]
    targets: List[tuple[Optional[str], str]] = [
        (stage, agentcore_exec_role_name(agent_name, stage)) for stage in stages
    ]
    legacy = legacy_agentcore_exec_role_name(agent_name)
    if legacy not in [name for _, name in targets]:
        targets.append((None, legacy))
    return targets


def _reclaim_exec_role(svc: ProjectService, role_name: str, iam) -> Optional[str]:
    """Delete ONE exec role + its inline policy under ``iam``. Never raises.

    The inline policy goes first: IAM refuses ``DeleteRole`` on a role that still carries one
    (``DeleteConflict``), so a surviving policy leaks the whole role. A failed policy delete
    still attempts the role. Returns a SAFE hint when the role was NOT reclaimed, else None:
    the hint carries the role name and ``type(err).__name__`` and nothing from the exception
    body, because it lands on a read model the console renders. ``NoSuchEntity`` is success on
    both calls."""
    hint: Optional[str] = None
    try:
        iam.delete_role_policy(RoleName=role_name, PolicyName=_AGENTCORE_EXEC_POLICY_NAME)
    except (ClientError, BotoCoreError) as err:
        if _iam_error_code(err) != "NoSuchEntity":
            logger.exception("[project] runtime exec-role policy delete failed for %s", role_name)
            hint = f"{role_name} ({type(err).__name__})"
    try:
        iam.delete_role(RoleName=role_name)
    except (ClientError, BotoCoreError) as err:
        if _iam_error_code(err) != "NoSuchEntity":
            logger.exception("[project] runtime exec-role delete failed for %s", role_name)
            hint = f"{role_name} ({type(err).__name__})"
    return hint


def _run_github_step(svc: ProjectService, project, repo) -> RepoDeleteItemResult:
    """Run the github teardown step, mapping a REMOVED connection to ``skipped`` (not
    failed). If the project references a connection that no longer exists, the GitHub repo
    is unreachable for good — a "failed" outcome would trap the record forever (failed
    blocks the record delete). So a ``ConnectionError(kind="not_found")`` yields
    ``skipped``, which does NOT block record removal; it is honest that the platform could
    not reach — let alone delete — the repo. Every OTHER exception (including a
    ``ConnectionError`` of a different kind, e.g. ``secret_error``, AND any non-
    ConnectionError) is a real, retryable problem and flows through the SAME failed
    mapping as :func:`_run_step` (``type(err).__name__`` reason + ``logger.exception``)."""
    try:
        _delete_github(svc, project, repo)
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


def _probe_github(svc: ProjectService, project, repo) -> str:
    """github state: connection missing → gone; repo_exists → present/gone; probe
    error → unknown. The token flows straight to the client — never logged/surfaced."""
    try:
        connection = svc._conn.get_connection(project.connection_id)
        token = svc._conn.get_bearer_token(project.connection_id)
        exists = svc._rollout.repo_exists(connection.org, repo.name, token, connection.base_url)
        return "present" if exists else "gone"
    except ConnectionError as err:
        if err.kind == "not_found":
            return "gone"
        logger.exception("[delete-preview] github probe failed for repo %s", repo.id)
        return "unknown"
    except Exception:
        logger.exception("[delete-preview] github probe failed for repo %s", repo.id)
        return "unknown"


def _probe_image(svc: ProjectService, agent_id: str) -> str:
    """image state: any matching image → present; none → gone; probe error → unknown.
    No injected cleaner ⇒ nothing to probe → gone."""
    if svc._ecr is None:
        return "gone"
    try:
        return "present" if svc._ecr.count_images(agent_id) > 0 else "gone"
    except Exception:
        logger.exception("[delete-preview] image probe failed for agent %s", agent_id)
        return "unknown"


def _probe_runtime(
    svc: ProjectService, agent, agent_arn: Optional[str], tenant_id: Optional[str] = None
) -> str:
    """runtime state, AGGREGATED over every per-stage runtime: any still exists → present;
    no agent/arn → gone (nothing provisioned); all gone → gone; otherwise unknown.

    The preview offers ONE ``runtime`` line (a frontend contract), so N answers collapse with
    the precedence ``present`` > ``unknown`` > ``gone``, the only ordering that cannot
    mislead an operator into unchecking a live runtime. A raise, a failed AssumeRole, an
    unresolvable stage and a harness ARN (no harness probe exists) are all ``unknown``, never
    ``gone``: "I could not look" is not "it is not there". Each stage is probed under the
    account that owns it. This reports on the AgentCore RUNTIME only; the Terraform state and
    the IAM exec role the runtime step also reclaims are not probed."""
    targets = _runtime_targets_to_delete(agent, agent_arn)
    if not targets:
        return "gone"
    agent_id = getattr(agent, "id", "") or ""
    ambiguous = False
    for stage, arn in targets:
        if is_harness_arn(arn):
            ambiguous = True
            continue
        try:
            control = _stage_control(svc, tenant_id, stage, agent_id)
        except TenantCredentialsError as err:
            logger.warning(
                "[delete-preview] runtime probe could not assume the tenant role for stage '%s': %s",
                stage,
                err.message,
            )
            ambiguous = True
            continue
        except StageUnresolvedError as err:
            logger.warning(
                "[delete-preview] runtime probe could not resolve the owning account for stage '%s': %s",
                stage,
                err.message,
            )
            ambiguous = True
            continue
        except Exception:
            logger.exception("[delete-preview] runtime client resolution failed for stage '%s'", stage)
            ambiguous = True
            continue
        try:
            if svc._identity.runtime_exists(arn, control_client=control):
                return "present"
        except Exception:
            logger.exception("[delete-preview] runtime probe failed for %s", arn.rsplit("/", 1)[-1])
            ambiguous = True
    return "unknown" if ambiguous else "gone"


def _probe_identity(svc: ProjectService, agent) -> str:
    """identity state (stored-id heuristic — NO live Graph probe): the agent carries a
    stored ``entra_app_id``/``entra_sp_id`` → present; no agent or no ids → gone."""
    if agent is None:
        return "gone"
    if getattr(agent, "entra_app_id", None) or getattr(agent, "entra_sp_id", None):
        return "present"
    return "gone"
