"""The teardown workflow: the ordered best-effort delete of a repository, its read-only preview, and the
guarded delete of an empty project.

Functions over a ``ProjectService`` passed as ``svc``, whose collaborators are read at call time.
``ProjectService`` delegates ``delete_repo``, ``preview_delete`` and ``delete_project`` here. The agent leg
runs the steps and probes of :mod:`services.project_runtime_teardown`; the MCP-server leg tears its identity
down through :func:`services.mcp_teardown.teardown_mcp_identity`. The line-item builders shape both reports.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, List, Optional

from botocore.exceptions import BotoCoreError, ClientError

from core.state_store import STORE_FAULTS
from models.agent import is_harness_arn
from models.repository import (
    RepoDeleteItemResult,
    RepoDeletePreview,
    RepoDeletePreviewItem,
    RepoDeleteResult,
    RepoDeleteSelection,
    Repository,
)
from services import project_runtime_teardown
from services.agent_registry_service import MalformedAgentRecordError
from services.mcp_server_service import MalformedMcpRecordError
from services.mcp_teardown import teardown_mcp_identity
from services.project_errors import ProjectError

if TYPE_CHECKING:
    from services.project_service import ProjectService

logger = logging.getLogger(__name__)

_NON_BLOCKING_ITEMS = frozenset({"langfuse", "exec_role"})


def delete_repo(
    svc: ProjectService, *, project_id: str, repo_id: str, selection: RepoDeleteSelection
) -> RepoDeleteResult:
    """Ordered best-effort teardown of a materialized repo+agent, the inverse of
    :meth:`ProjectService.add_repo`.

    Each SELECTED step runs in its own try/except and appends a :class:`RepoDeleteItemResult`;
    an unselected step is skipped, never failed. Order: github → image → runtime (+ TF state,
    then the exec role as its own item) → identity (+ Langfuse) → record LAST. The record
    (registry entry + OPS row) is deleted only when every other selected BLOCKING step
    succeeded; otherwise the row stays for retry. The items in :data:`_NON_BLOCKING_ITEMS` are
    reported but excluded from that predicate; an item qualifies when a retry of the cascade
    could not plausibly change its outcome. A raised step exception yields
    ``outcome="failed"`` with a SAFE reason (``type(err).__name__``) and ``logger.exception``.
    """
    repo = svc._resolve_delivery_repo(project_id, repo_id)

    project = svc._get_project(project_id)
    if project is None:
        raise ProjectError("Repository not found", kind="repo_not_found")

    if repo.artifact_kind == "mcp_server":
        return _delete_mcp_repo(svc, project, repo, selection)

    try:
        agent = svc._registry.get(repo.agent_id)
    except MalformedAgentRecordError:
        logger.warning("[project] registry record for repo %s is unreadable", repo_id)
        raise ProjectError("registry record unreadable", kind="record_unreadable") from None
    agent_arn = getattr(agent, "agent_arn", None) if agent else None
    agent_id = repo.agent_id or ""

    items: List[RepoDeleteItemResult] = [
        project_runtime_teardown._run_github_step(svc, project, repo)
        if selection.github
        else _skipped("github"),
        project_runtime_teardown._run_step(
            svc, "image", lambda: project_runtime_teardown._delete_image(svc, agent_id)
        )
        if selection.image
        else _skipped("image"),
    ]
    items.extend(_runtime_items(svc, selection, agent, agent_arn, agent_id, project.tenant_id))
    items.extend(_identity_items(svc, selection, agent))
    record, record_removed = _record_item(svc, selection, repo, agent, items)
    items.append(record)
    return RepoDeleteResult(items=items, record_removed=record_removed)


def _runtime_items(
    svc: ProjectService,
    selection: RepoDeleteSelection,
    agent,
    agent_arn: Optional[str],
    agent_id: str,
    tenant_id: str,
) -> List[RepoDeleteItemResult]:
    harness_stages = [
        stage
        for stage, arn in project_runtime_teardown._runtime_targets_to_delete(agent, agent_arn)
        if is_harness_arn(arn)
    ]
    if selection.runtime and harness_stages:
        return [
            _harness_runtime_not_torn_down(),
            _harness_exec_role_not_torn_down(agent.name, harness_stages),
        ]
    if selection.runtime:
        return [
            _no_record("runtime")
            if agent is None
            else project_runtime_teardown._run_step(
                svc,
                "runtime",
                lambda: project_runtime_teardown._delete_runtime(svc, agent, agent_arn, agent_id, tenant_id),
            ),
            _exec_role_item(*project_runtime_teardown._delete_exec_role(svc, agent, tenant_id)),
        ]
    return [_skipped("runtime"), _skipped("exec_role")]


def _identity_items(svc: ProjectService, selection: RepoDeleteSelection, agent) -> List[RepoDeleteItemResult]:
    if selection.identity and agent is None:
        return [_no_record("identity"), _no_record("langfuse")]
    if selection.identity:
        return [
            project_runtime_teardown._run_step(
                svc, "identity", lambda: project_runtime_teardown._delete_identity(svc, agent)
            ),
            project_runtime_teardown._run_step(
                svc, "langfuse", lambda: project_runtime_teardown._delete_langfuse(svc, agent)
            ),
        ]
    return [_skipped("identity"), _skipped("langfuse")]


def _record_item(
    svc: ProjectService,
    selection: RepoDeleteSelection,
    repo: Repository,
    agent,
    items: List[RepoDeleteItemResult],
) -> tuple[RepoDeleteItemResult, bool]:
    if not selection.record:
        return _skipped("record"), False
    if any(i.outcome == "failed" for i in items if i.item not in _NON_BLOCKING_ITEMS):
        return (
            RepoDeleteItemResult(
                item="record",
                outcome="skipped",
                reason="kept for retry — an earlier step failed",
            ),
            False,
        )
    try:
        removed = svc._registry.delete(repo.agent_id)
        if removed is None and agent is not None:
            return (
                RepoDeleteItemResult(
                    item="record",
                    outcome="failed",
                    reason="record not found at delete time — retry to remove the row",
                ),
                False,
            )
        svc._delete_repo(repo.id)
    except Exception as err:
        logger.exception("[project] record delete failed for repo %s", repo.id)
        return RepoDeleteItemResult(item="record", outcome="failed", reason=type(err).__name__), False
    return (
        RepoDeleteItemResult(item="record", outcome="deleted")
        if removed is not None
        else RepoDeleteItemResult(item="record", outcome="skipped", reason="not found in the registry")
    ), True


def preview_delete(svc: ProjectService, *, project_id: str, repo_id: str) -> RepoDeletePreview:
    """READ-ONLY reachability pre-check for the delete cascade.

    Probes each teardown artifact best-effort and reports its ``state``: ``present``, ``gone``
    (already deleted / unreachable) or ``unknown`` (the probe raised; the frontend treats it
    as selectable and checked). Deletes NOTHING, and a probe raising never hides a present or
    unknown item. Same guards as :func:`delete_repo`: an unknown repo, or one belonging to
    another project, raises ``ProjectError(kind="repo_not_found")``.
    """
    repo = svc._get_repo(repo_id)
    if repo is None or repo.project_id != project_id:
        raise ProjectError("Repository not found", kind="repo_not_found")
    project = svc._get_project(project_id)
    if project is None:
        raise ProjectError("Repository not found", kind="repo_not_found")

    if repo.artifact_kind == "mcp_server":
        try:
            mcp = _load_repo_mcp(svc, repo)
        except (MalformedMcpRecordError, ClientError, BotoCoreError):
            logger.warning("[delete-preview] MCP record for repo %s is unreadable", repo.id)
            mcp = None
        identity_state = engine_state = "unknown"
        engine_note = None
        if mcp is not None:
            owned = mcp.entra_app_id or mcp.entra_sp_id or mcp.cedar_policy_engine_id
            identity_state = "present" if owned else "gone"
            engine_state = "present" if mcp.cedar_policy_engine_id else "gone"
            if not mcp.cedar_policy_engine_id and getattr(mcp, "gateway_id", None):
                engine_note = "none on record — an engine on the gateway is left in place"
        gateway = _mcp_gateway_line(repo, mcp, ever_built=_mcp_ever_built(svc, repo))
        return RepoDeletePreview(
            items=[
                RepoDeletePreviewItem(
                    item="github", state=project_runtime_teardown._probe_github(svc, project, repo)
                ),
                RepoDeletePreviewItem(
                    item="runtime",
                    state="manual" if gateway.outcome == "manual" else "gone",
                    note=gateway.reason,
                ),
                RepoDeletePreviewItem(item="identity", state=identity_state),
                RepoDeletePreviewItem(
                    item="policy_engine",
                    state=engine_state,
                    note="removed with the Entra MCP app" if engine_state == "present" else engine_note,
                ),
                RepoDeletePreviewItem(item="record", state="present"),
            ]
        )

    try:
        agent = svc._registry.get(repo.agent_id)
    except MalformedAgentRecordError:
        logger.warning("[delete-preview] registry record for repo %s is unreadable", repo_id)
        agent = None
    agent_arn = getattr(agent, "agent_arn", None) if agent else None
    agent_id = repo.agent_id or ""

    items = [
        RepoDeletePreviewItem(
            item="github", state=project_runtime_teardown._probe_github(svc, project, repo)
        ),
        RepoDeletePreviewItem(item="image", state=project_runtime_teardown._probe_image(svc, agent_id)),
        RepoDeletePreviewItem(
            item="runtime",
            state="unknown"
            if agent is None
            else project_runtime_teardown._probe_runtime(svc, agent, agent_arn, project.tenant_id),
        ),
        RepoDeletePreviewItem(
            item="identity",
            state="unknown" if agent is None else project_runtime_teardown._probe_identity(svc, agent),
        ),
        RepoDeletePreviewItem(item="record", state="present"),
    ]
    return RepoDeletePreview(items=items)


def _mcp_ever_built(svc: ProjectService, repo) -> bool:
    """Has the repo any deployment row (every build writes ``started`` first)? An unreadable
    history counts as built — the safe side, it can only make the gateway line ``manual``."""
    try:
        return svc._rows.has_deployments(repo.id)
    except STORE_FAULTS:
        logger.warning("[delete] deployment history for repo %s is unreadable", repo.id)
        return True


def _load_repo_mcp(svc: ProjectService, repo):
    """The MCP record an MCP repo points at, or None (no registry wired / not found)."""
    if svc._mcp_registry is None:
        return None
    return svc._mcp_registry.get(repo.mcp_server_id)


def _delete_mcp_repo(svc: ProjectService, project, repo, selection: RepoDeleteSelection) -> RepoDeleteResult:
    """The MCP-server leg of :func:`delete_repo`. Order: github → identity (the Entra MCP
    app, then its Cedar engine, through ``teardown_mcp_identity``) → MCP record LAST.

    The same item keys as the agent cascade, so the delete modal renders one report shape.
    The gateway (``runtime``) is built by the repo's Terraform and never destroyed by AGP: it
    is reported ``manual`` with its state key, or ``absent`` if never built, never a false
    ``deleted``. ``image`` is reported not used; ``exec_role``/``langfuse`` rows are omitted
    because the modal maps them onto the runtime/identity selections. Identity and engine
    BLOCK the record: their ids live on it, so a failure keeps the row and "delete again" is
    the retry. A record whose ids cannot be read is refused before any step.
    """
    try:
        mcp = _load_repo_mcp(svc, repo)
    except MalformedMcpRecordError:
        logger.warning("[project] MCP record for repo %s is unreadable", repo.id)
        raise ProjectError("registry record unreadable", kind="record_unreadable") from None
    items: List[RepoDeleteItemResult] = []

    items.append(
        project_runtime_teardown._run_github_step(svc, project, repo)
        if selection.github
        else _skipped("github")
    )
    items.append(_mcp_not_used("image"))
    items.append(_mcp_gateway_line(repo, mcp, ever_built=_mcp_ever_built(svc, repo)))
    if not selection.identity:
        items.append(_skipped("identity"))
        items.append(
            RepoDeleteItemResult(
                item="policy_engine",
                outcome="skipped",
                reason="left in place — identity not selected",
            )
        )
    elif mcp is None:
        items.append(_no_record("identity"))
        items.append(_no_record("policy_engine"))
    else:
        items.extend(asyncio.run(teardown_mcp_identity(mcp)))

    record_removed = False
    if not selection.record:
        items.append(_skipped("record"))
    elif any(i.outcome == "failed" for i in items if i.item not in _NON_BLOCKING_ITEMS):
        items.append(
            RepoDeleteItemResult(
                item="record",
                outcome="skipped",
                reason="kept for retry — an earlier step failed",
            )
        )
    else:
        try:
            removed = svc._mcp_registry.delete(repo.mcp_server_id) if svc._mcp_registry is not None else None
            if removed is None and mcp is not None:
                items.append(
                    RepoDeleteItemResult(
                        item="record",
                        outcome="failed",
                        reason="record not found at delete time — retry to remove the row",
                    )
                )
            else:
                svc._delete_repo(repo.id)
                items.append(
                    RepoDeleteItemResult(item="record", outcome="deleted")
                    if removed is not None
                    else RepoDeleteItemResult(
                        item="record", outcome="skipped", reason="not found in the registry"
                    )
                )
                record_removed = True
        except Exception as err:
            logger.exception("[project] record delete failed for repo %s", repo.id)
            items.append(RepoDeleteItemResult(item="record", outcome="failed", reason=type(err).__name__))

    return RepoDeleteResult(items=items, record_removed=record_removed)


def delete_project(svc: ProjectService, project_id: str) -> None:
    """Delete an empty project container — GUARDED: blocked while it still holds repos.

    Unlike :func:`delete_repo`, a Project owns no runtime/identity of its own, so this is
    a plain guarded record delete. An unknown id is a 404
    (``ProjectError(kind="not_found")``); a project that still has repositories raises
    ``ProjectError(kind="has_repositories")`` and is NOT deleted (the operator tears the
    repos down first). Only an empty container is removed."""
    project = svc._get_project(project_id)
    if project is None:
        raise ProjectError("Project not found", kind="not_found")
    if svc._load_repos_for(project_id):
        raise ProjectError("Project has repositories", kind="has_repositories")
    svc._delete_project(project_id)


def _skipped(item: str) -> RepoDeleteItemResult:
    """A teardown step the operator did not select — skipped, never failed."""
    return RepoDeleteItemResult(item=item, outcome="skipped", reason="not selected")


def _no_record(item: str) -> RepoDeleteItemResult:
    """An agent-derived step with no registry record to read its ids from."""
    return RepoDeleteItemResult(
        item=item, outcome="skipped", reason="no registry record — nothing known to tear down"
    )


HARNESS_RUNTIME_SKIP_REASON = (
    "harness teardown isn't automated yet — delete the harness in the AgentCore console "
    "(its Terraform state and managed memory are left too)"
)


def _harness_runtime_not_torn_down() -> RepoDeleteItemResult:
    """A harness agent's runtime step: AGP cannot tear a harness down yet."""
    return RepoDeleteItemResult(item="runtime", outcome="skipped", reason=HARNESS_RUNTIME_SKIP_REASON)


def _mcp_not_used(item: str) -> RepoDeleteItemResult:
    """An agent-only artifact an MCP-server repo never has."""
    return RepoDeleteItemResult(item=item, outcome="skipped", reason="not used by MCP servers")


def _mcp_gateway_line(repo: Repository, mcp, *, ever_built: bool) -> RepoDeleteItemResult:
    """An MCP repo's gateway line, shared by the preview note and the cascade.

    Built by the repo's Terraform and never removed by AGP, so it is ``manual`` with the how; the
    state key is the buildspec's gateway arm's (``agentcore-gateway/$MCP_SERVER_ID/$STAGE``).
    ``absent`` ("never built") needs BOTH no recorded ids AND no deployment row at all
    (``ever_built`` False) — every build writes a ``started`` row first. Any attempt without ids
    (in flight, failed, or died mid-apply) may have left a gateway the repo row never recorded,
    so it is ``manual``."""
    key = f"agentcore-gateway/{repo.mcp_server_id}/dev/"
    built = (
        repo.gateway_arn
        or repo.gateway_id
        or getattr(mcp, "gateway_arn", None)
        or getattr(mcp, "gateway_id", None)
    )
    if built:
        reason = (
            "Not removed by AGP — Terraform-managed. Delete it with terraform destroy on state "
            f"key {key}, or in the AgentCore console"
        )
    elif ever_built:
        reason = (
            "Not removed by AGP — a build ran but recorded no gateway, so it may have left a "
            f"partial one. Check Terraform state key {key} and destroy it there"
        )
    else:
        return RepoDeleteItemResult(
            item="runtime", outcome="absent", reason="never built — nothing to remove"
        )
    return RepoDeleteItemResult(item="runtime", outcome="manual", reason=reason)


def _harness_exec_role_not_torn_down(agent_name: str, stages: List[Optional[str]]) -> RepoDeleteItemResult:
    """A harness agent's exec-role step. The role is the harness module's
    ``<agent>-<stage>-harness-exec`` (agentcore_harness/main.tf) — an IAM fact, not AgentCore's."""
    named = sorted({s for s in stages if s})
    roles = ", ".join(f"{agent_name}-{s}-harness-exec" for s in named) or (
        f"{agent_name}-<stage>-harness-exec"
    )
    return RepoDeleteItemResult(
        item="exec_role",
        outcome="skipped",
        reason=f"not automated for harnesses yet — delete the {roles} role in IAM",
    )


def _exec_role_item(
    hints: Optional[List[str]], assume_failures: Optional[List[str]] = None
) -> RepoDeleteItemResult:
    """The ``exec_role`` cascade line-item, from the outcome of
    :func:`services.project_runtime_teardown._delete_exec_role`.

    ``assume_failures`` wins: the platform never got into the account that holds the role, so it
    reports ``failed`` with the ``assume_role_failed:`` prefix (a distinct operator action: grant
    the trust, or reclaim by hand). ``failed`` rather than a new outcome value because the delete
    modal keys its Retry affordance off that string. Then: ``None`` hints ⇒ ``skipped`` (nothing
    was attempted; reporting ``deleted`` would claim a teardown that never ran), ``[]`` ⇒
    ``deleted`` with no reason, non-empty ⇒ ``failed`` naming the surviving roles (role name and
    exception type only).
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
