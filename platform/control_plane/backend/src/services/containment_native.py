"""The kill switch's native runtime lever: a resource-policy Deny on ``InvokeAgentRuntime``.

Live on dev 2026-10-13: ``PutResourcePolicy`` on the runtime ARN refused new invokes
within ~1.1 s (403 "explicit deny in a resource-based policy"). For an OAuth caller the principal
is ``anonymous``, so once ANY resource policy exists every data-plane action needs an explicit
Allow — restore therefore DELETES the policy when there was none before, never leaving a shell.
A missing policy reads back as an empty response (seen live) or ResourceNotFoundException.
Harness ARNs are not runtimes: both levers skip them.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from botocore.exceptions import ClientError

from models.agent import is_harness_arn
from services.tenant_credentials import stage_client

logger = logging.getLogger(__name__)

CONTROL_SERVICE = "bedrock-agentcore-control"
DENY_SID = "AgpContainDenyInvoke"
NOT_A_RUNTIME = {"status": "skipped", "detail": "not a runtime"}


def make_client_factory(stage_cfg: Callable[[Any], Any] = lambda agent: None) -> Callable[[str, Any], Any]:
    """``(service, agent) -> boto3 client`` for the account owning the agent (None cfg = ambient)."""
    return lambda service, agent: stage_client(
        service, stage_cfg(agent), session_suffix=f"contain-{agent.id}"
    )


def runtime_service_name(runtime_arn: str) -> str:
    """The OTel ``service.name`` of a runtime: its id minus the trailing ``-<id>``.

    ``…:runtime/fraud_triage_dev-AbC123[/runtime-endpoint/DEFAULT[:DEFAULT]]`` -> ``fraud_triage_dev``.
    """
    resource = runtime_arn.split("runtime/", 1)[-1]
    runtime_id = resource.split("/", 1)[0].split(":", 1)[0]
    return runtime_id.rsplit("-", 1)[0]


def _error(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        return f"{err.get('Code') or 'ClientError'}: {err.get('Message') or ''}".strip()
    return f"{type(exc).__name__}: {exc}".strip()


def _not_found(exc: Exception) -> bool:
    return (
        isinstance(exc, ClientError)
        and exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException"
    )


def _without_own_deny(policy: str) -> str | None:
    """``policy`` minus AGP's own Deny statements (a re-run after a crash); None when nothing is left."""
    try:
        doc = json.loads(policy)
        statements = doc.get("Statement")
    except (ValueError, AttributeError):
        return policy
    if isinstance(statements, dict):
        statements = [statements]
    if not isinstance(statements, list):
        return policy
    kept = [st for st in statements if not (isinstance(st, dict) and st.get("Sid") == DENY_SID)]
    if len(kept) == len(statements):
        return policy
    if not kept:
        return None
    return json.dumps({**doc, "Statement": kept})


class RuntimeLevers:
    def __init__(self, *, client_factory: Callable[[str, Any], Any]):
        self._client_factory = client_factory

    def deny_invokes(self, agent, runtime_arn: str) -> dict:
        """Save the current policy, then replace it with the single Deny. Never raises."""
        if is_harness_arn(runtime_arn):
            return dict(NOT_A_RUNTIME)
        try:
            client = self._client_factory(CONTROL_SERVICE, agent)
            try:
                before = client.get_resource_policy(resourceArn=runtime_arn).get("policy") or None
            except Exception as exc:
                if not _not_found(exc):
                    raise
                before = None
            if before:
                before = _without_own_deny(before)
            policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": DENY_SID,
                        "Effect": "Deny",
                        "Principal": "*",
                        "Action": "bedrock-agentcore:InvokeAgentRuntime",
                        "Resource": runtime_arn,
                    }
                ],
            }
            client.put_resource_policy(resourceArn=runtime_arn, policy=json.dumps(policy))
        except Exception as exc:
            logger.warning(
                "[contain] runtime deny failed for agent %s: %s", agent.id, _error(exc), exc_info=True
            )
            return {"status": "failed", "detail": _error(exc)}
        return {"status": "ok", "before": before}

    def restore_invokes(self, agent, runtime_arn: str, before: str | None) -> dict:
        """Put back ``before``, or delete the policy when there was none. Never raises."""
        if is_harness_arn(runtime_arn):
            return dict(NOT_A_RUNTIME)
        try:
            client = self._client_factory(CONTROL_SERVICE, agent)
            if before is None:
                try:
                    client.delete_resource_policy(resourceArn=runtime_arn)
                except Exception as exc:
                    if not _not_found(exc):
                        raise
            else:
                client.put_resource_policy(resourceArn=runtime_arn, policy=before)
        except Exception as exc:
            logger.warning(
                "[contain] runtime restore failed for agent %s: %s", agent.id, _error(exc), exc_info=True
            )
            return {"status": "failed", "detail": _error(exc)}
        return {"status": "ok"}
