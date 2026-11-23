"""Per-agent Langfuse projects and API keys, stored in AWS Secrets Manager.

Uses Langfuse's internal tRPC API behind a session the auto-login Lambda@Edge establishes. A
registered agent's runtime may live in a tenant account, so the per-agent secret is created
through ``tenant_credentials.stage_client`` in the account its ``agent_arn`` names, the runtime is
told the secret's name through its env, and a resource policy lets the runtime's execution role
read that one secret.

Provisioning is best effort end to end: an unresolvable account, a failed AssumeRole, an
unreachable runtime and a rejected resource policy all degrade to a logged fallback and never
raise, because observability wiring must never fail a registration. Teardown runs the other way:
a failed assume raises, and a not-found in the wrong account is reported as failed, never as
deleted.
"""

import json
import logging
import re
from typing import Optional

import httpx

from core import aws_clients
from services.tenant_credentials import (
    StageUnresolvedError,
    TenantCredentialsError,
    stage_client,
)

logger = logging.getLogger(__name__)

_ARN_ACCOUNT_RE = re.compile(r"^arn:aws[\w-]*:[^:]*:[^:]*:(?P<account>[0-9]{12}):")

_SESSION_ID_CHARS = 8


def _secret_read_policy(role_arn: str) -> dict:
    """The Secrets Manager resource policy granting ONE principal ``GetSecretValue``.

    ``Resource: "*"`` is not a wildcard over secrets: a resource policy is attached TO a secret
    and ``*`` means "this secret" (a policy naming any other ARN is rejected). The grant is
    deliberately the single read action — the runtime never rotates, tags or describes its key —
    and it is attached to ONE secret for ONE role, so it is the narrowest form available.
    """
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AgpAgentRuntimeReadLangfuseKeys",
                "Effect": "Allow",
                "Principal": {"AWS": role_arn},
                "Action": "secretsmanager:GetSecretValue",
                "Resource": "*",
            }
        ],
    }


class LangfuseAccountUnresolvedError(StageUnresolvedError):
    """Teardown could not determine which account holds an agent's per-agent secret.

    Raised only by ``delete_agent_project``, when the ambient delete did not demonstrably remove
    anything. A ``StageUnresolvedError`` because it is that condition reached from another entry
    point, so every ``except StageUnresolvedError`` arm renders it as ``stage_unresolved:
    <message>``. The message names a lookup state, never an account id.
    """


class LangfuseProvisioningService:
    """Provisions Langfuse projects and API keys for agents."""

    def __init__(
        self,
        langfuse_host: str,
        langfuse_secret_name: str,
        region: str,
        registry=None,
        agp_project_name: str = "agp",
        tenants=None,
    ):
        self.langfuse_host = langfuse_host.rstrip("/")
        self.langfuse_secret_name = langfuse_secret_name
        self.region = region
        self._registry = registry
        self._agp_project_name = agp_project_name
        self._sm = aws_clients.client("secretsmanager", region=region)
        self._session = None
        self._tenants = tenants
        self._stage_client = stage_client
        self._platform_account: Optional[str] = None
        self._platform_account_probed = False

    def _get_session(self) -> httpx.Client:
        """An authenticated client over Langfuse's CSRF and credentials-callback flow; the auto-login
        Lambda@Edge in front of Langfuse performs the actual authentication."""
        if self._session:
            return self._session

        session = httpx.Client()
        r = session.get(f"{self.langfuse_host}/api/auth/csrf", timeout=10)
        r.raise_for_status()
        csrf = r.json().get("csrfToken", "")

        session.post(
            f"{self.langfuse_host}/api/auth/callback/credentials",
            data={
                "csrfToken": csrf,
                "email": "admin@langfuse.com",
                "password": "x",
                "callbackUrl": self.langfuse_host,
            },
            timeout=10,
            follow_redirects=False,
        )

        r = session.get(f"{self.langfuse_host}/api/auth/session", timeout=10)
        if r.status_code != 200 or not r.json().get("user"):
            raise RuntimeError("Failed to establish Langfuse session")

        self._session = session
        return session

    def _get_seed_keys(self):
        """Get seed project keys for API auth fallback."""
        secret = json.loads(self._sm.get_secret_value(SecretId=self.langfuse_secret_name)["SecretString"])
        return (secret.get("langfuse_public_key", ""), secret.get("langfuse_secret_key", ""))

    def _platform_account_id(self) -> Optional[str]:
        """The control-plane account id, or None when the probe failed. None is "unknown", and every caller
        stays on the ambient client for it; only a success is cached, so one transient STS failure does
        not pin the process to the ambient path.
        """
        if not self._platform_account_probed:
            try:
                account = aws_clients.client("sts", region=self.region).get_caller_identity()["Account"]
            except Exception:
                logger.exception(
                    "[langfuse] could not resolve the platform account; per-agent secrets stay "
                    "on the ambient client (the next agent re-probes)"
                )
                return None
            self._platform_account = account
            self._platform_account_probed = True
        return self._platform_account

    def _stage_cfg_for(self, agent):
        """The ``TenantStageConfig`` owning the agent's runtime, or None for the ambient client: the
        provisioning view of ``_resolve_stage_cfg``, which discards the reason.
        """
        return self._resolve_stage_cfg(agent)[0]

    def _resolve_stage_cfg(self, agent):
        """``(cfg, unresolved)``: the stage owning the agent's runtime and, when there is none, whether
        ambient is the right answer (``None``) or merely the only one available (a short safe reason).

        The account is parsed out of ``agent.agent_arn`` and matched against the account inside each
        stage's ``deploy_role_arn``, the only field naming a credential this service can assume. The
        same-account short-circuit runs before the tenant lookup, so a single-account agent never costs
        an AssumeRole or a tenant read. Every unknown warns per agent, naming the agent and never an
        account id.
        """
        account = _ARN_ACCOUNT_RE.match(getattr(agent, "agent_arn", "") or "")
        if account is None:
            return None, None
        account_id = account.group("account")
        platform = self._platform_account_id()
        if platform is None:
            logger.warning(
                "[langfuse] the platform account is unknown, so agent %s's runtime account "
                "cannot be compared against it; creating its secret in the platform account "
                "(if the runtime lives elsewhere it will not be able to read it)",
                agent.id,
            )
            return None, "the platform account could not be determined"
        if account_id == platform:
            return None, None

        tenant_id = getattr(agent, "tenant_id", None)
        if self._tenants is None or not tenant_id:
            logger.warning(
                "[langfuse] agent %s's runtime is in another account and no tenant stage can be "
                "resolved for it; creating its secret in the platform account (the runtime will "
                "not be able to read it)",
                agent.id,
            )
            return None, "the runtime is in another account and no tenant stage resolves it"
        try:
            tenant = self._tenants.get(tenant_id)
        except Exception:
            logger.exception(
                "[langfuse] tenant lookup failed while resolving agent %s's runtime account; "
                "creating its secret in the platform account",
                agent.id,
            )
            return None, "the tenant lookup failed"
        stages = (getattr(tenant, "stages", None) or {}) if tenant is not None else {}
        for _name, cfg in sorted(stages.items()):
            if getattr(cfg, "deploy_role_arn", ""):
                match = _ARN_ACCOUNT_RE.match(cfg.deploy_role_arn)
                if match and match.group("account") == account_id:
                    return cfg, None
        logger.warning(
            "[langfuse] no tenant stage owns the account holding agent %s's runtime; creating "
            "its secret in the platform account (the runtime will not be able to read it)",
            agent.id,
        )
        return None, "no tenant stage owns the account holding the runtime"

    def _stage_scoped(self, service_name: str, agent, cfg):
        """A client for ``cfg``'s account, or None so the caller falls back to ambient; a failed
        AssumeRole or client construction is logged, not raised.
        """
        if cfg is None:
            return None
        try:
            return self._stage_client(service_name, cfg, session_suffix=f"lf-{agent.id[:_SESSION_ID_CHARS]}")
        except TenantCredentialsError as err:
            logger.warning(
                "[langfuse] could not reach the account owning agent %s's runtime as %s (%s); "
                "staying on the platform account",
                agent.id,
                service_name,
                err.message,
            )
            return None
        except Exception:
            logger.exception(
                "[langfuse] cross-account client construction failed for agent %s (%s); staying "
                "on the platform account",
                agent.id,
                service_name,
            )
            return None

    def _agent_sm(self, agent, cfg=None):
        """The Secrets Manager client for the account holding the agent's per-agent secret; ``self._sm``
        stays the client for the admin secret, a control-plane resource.
        """
        if cfg is None:
            cfg = self._stage_cfg_for(agent)
        return self._stage_scoped("secretsmanager", agent, cfg) or self._sm

    def _agent_secret_name(self, agent) -> str:
        """The Secrets Manager name of the per-agent key pair, keyed on the stable agent id."""
        return f"langfuse-agent-{agent.id}-keys"

    def provision_agent_project(self, agent) -> dict:
        """Create a Langfuse project and key for one agent, store ``{public_key, secret_key,
        project_name, project_id}`` in Secrets Manager (tagged ``managed_by=agp``) in the account owning
        the agent's runtime, and write ``langfuse_project_id`` and ``langfuse_key_secret_name`` onto the
        envelope.

        Idempotent: an envelope already carrying a project id whose stored key is still readable
        returns the existing join; a stored key deleted out of band is re-minted, the create path reusing
        the existing project by name. Returns the non-secret join ``{"project_id", "secret_name",
        "public_key"}``; the secret key lives only in Secrets Manager.
        """
        secret_name = self._agent_secret_name(agent)
        sm = self._agent_sm(agent)

        if agent.langfuse_project_id:
            try:
                existing = json.loads(sm.get_secret_value(SecretId=secret_name)["SecretString"])
                logger.info(
                    "Langfuse project already provisioned for agent %s (%s), reusing",
                    agent.id,
                    agent.langfuse_project_id,
                )
                return {
                    "project_id": agent.langfuse_project_id,
                    "secret_name": secret_name,
                    "public_key": existing.get("public_key", ""),
                }
            except sm.exceptions.ResourceNotFoundException:
                logger.warning(
                    "Langfuse secret missing for already-provisioned agent %s (%s); re-minting key",
                    agent.id,
                    agent.langfuse_project_id,
                )

        project_name = f"{self._agp_project_name}-{agent.name}"
        session = self._get_session()

        r = session.post(
            f"{self.langfuse_host}/api/trpc/projects.create",
            json={"json": {"name": project_name, "orgId": "seed-org"}},
            timeout=10,
        )
        if r.status_code == 200:
            project_id = r.json()["result"]["data"]["json"]["id"]
            logger.info("Created Langfuse project %s (%s)", project_name, project_id)
        else:
            logger.info("Project creation returned %s, checking if it exists", r.status_code)
            r2 = httpx.get(
                f"{self.langfuse_host}/api/public/projects",
                auth=self._get_seed_keys(),
                timeout=10,
            )
            projects = r2.json().get("data", [])
            project = next((p for p in projects if p["name"] == project_name), None)
            if not project:
                raise RuntimeError(f"Failed to create or find project {project_name}: {r.text[:200]}")
            project_id = project["id"]
            logger.info("Found existing Langfuse project %s (%s)", project_name, project_id)

        r = session.post(
            f"{self.langfuse_host}/api/trpc/projectApiKeys.create",
            json={"json": {"projectId": project_id}},
            timeout=10,
        )
        r.raise_for_status()
        key_data = r.json()["result"]["data"]["json"]
        public_key = key_data["publicKey"]
        secret_key = key_data["secretKey"]
        logger.info("Minted API key for Langfuse project %s", project_name)

        secret_value = json.dumps(
            {
                "public_key": public_key,
                "secret_key": secret_key,
                "project_name": project_name,
                "project_id": project_id,
            }
        )
        try:
            sm.create_secret(
                Name=secret_name,
                SecretString=secret_value,
                Tags=[
                    {"Key": "agent_id", "Value": agent.id},
                    {"Key": "managed_by", "Value": "agp"},
                ],
            )
        except sm.exceptions.ResourceExistsException:
            sm.put_secret_value(SecretId=secret_name, SecretString=secret_value)
        logger.info("Stored Langfuse keys in secret %s", secret_name)

        agent.langfuse_project_id = project_id
        agent.langfuse_key_secret_name = secret_name
        if self._registry is not None:
            self._registry.persist_identity(agent)

        return {
            "project_id": project_id,
            "secret_name": secret_name,
            "public_key": public_key,
        }

    def wire_agent_runtime(self, agent, secret_name: str, identity) -> None:
        """Tell the agent's runtime about the project it was given: merge ``LANGFUSE_HOST`` and
        ``LANGFUSE_SECRET_NAME`` onto its env without waiting for READY, then grant the runtime's
        execution role ``secretsmanager:GetSecretValue`` on that one secret through a resource policy
        (required cross-account, harmless same-account). Neither leg raises. Skipped when the agent names
        no runtime; the deploy that creates one writes both variables itself.
        """
        runtime_arn = getattr(agent, "agent_arn", "") or ""
        if not runtime_arn:
            return

        cfg = self._stage_cfg_for(agent)
        control = self._stage_scoped("bedrock-agentcore-control", agent, cfg)
        try:
            role_arn = identity.set_runtime_environment(
                runtime_arn,
                {
                    "LANGFUSE_HOST": self.langfuse_host,
                    "LANGFUSE_SECRET_NAME": secret_name,
                },
                wait_ready=False,
                control_client=control,
            )
        except Exception:
            logger.exception(
                "[langfuse] runtime env injection best-effort no-op for agent %s (the runtime "
                "is unreachable; its traces will not be attributed)",
                agent.id,
            )
            return

        if not role_arn:
            logger.warning(
                "[langfuse] no execution role returned for agent %s's runtime; skipping the "
                "secret read grant",
                agent.id,
            )
            return

        try:
            self._agent_sm(agent, cfg).put_resource_policy(
                SecretId=secret_name,
                ResourcePolicy=json.dumps(_secret_read_policy(role_arn)),
                BlockPublicPolicy=True,
            )
            logger.info(
                "[langfuse] granted agent %s's runtime read access to secret %s",
                agent.id,
                secret_name,
            )
        except Exception:
            logger.exception(
                "[langfuse] secret read grant best-effort no-op for agent %s (secret %s); the "
                "runtime may not be able to read its Langfuse key",
                agent.id,
                secret_name,
            )

    def delete_agent_project(self, agent) -> None:
        """Best-effort, idempotent teardown of an agent's Langfuse project and Secrets Manager secret.

        The project delete swallows every failure. The secret is deleted in the account that holds it,
        resolved through ``_resolve_stage_cfg``: a resolvable cross-account stage uses ``stage_client``
        directly and a failed assume propagates as ``TenantCredentialsError``; an unresolvable account
        still attempts the ambient delete, whose not-found carries no information about the account that
        matters and raises ``LangfuseAccountUnresolvedError``, so the caller reports the item failed,
        never deleted. A not-found on a resolved account is success.
        """
        if agent.langfuse_project_id:
            try:
                session = self._get_session()
                r = session.post(
                    f"{self.langfuse_host}/api/trpc/projects.delete",
                    json={"json": {"projectId": agent.langfuse_project_id}},
                    timeout=10,
                )
                r.raise_for_status()
                logger.info(
                    "Deleted Langfuse project %s for agent %s",
                    agent.langfuse_project_id,
                    agent.id,
                )
            except Exception:
                logger.exception(
                    "[langfuse] project delete best-effort no-op for agent %s (already gone or unreachable)",
                    agent.id,
                )

        secret_name = agent.langfuse_key_secret_name or self._agent_secret_name(agent)
        cfg, unresolved = self._resolve_stage_cfg(agent)
        if cfg is None:
            sm = self._sm
        else:
            sm = self._stage_client(
                "secretsmanager", cfg, session_suffix=f"lf-{agent.id[:_SESSION_ID_CHARS]}"
            )
        try:
            sm.delete_secret(SecretId=secret_name, ForceDeleteWithoutRecovery=True)
            logger.info("Deleted Langfuse secret %s for agent %s", secret_name, agent.id)
            return
        except sm.exceptions.ResourceNotFoundException:
            if unresolved is None:
                return
            logger.warning(
                "[langfuse] secret %s for agent %s is not in the platform account and its "
                "owning account is unresolvable — reporting the teardown as failed rather "
                "than as done",
                secret_name,
                agent.id,
            )
        except Exception:
            logger.exception(
                "[langfuse] secret delete best-effort no-op for agent %s (already gone or unreachable)",
                agent.id,
            )
            if unresolved is None:
                return
        raise LangfuseAccountUnresolvedError(unresolved)
