"""
Langfuse Project Provisioning Service.

Creates per-agent Langfuse projects with dedicated API keys
and stores them in AWS Secrets Manager.

Uses Langfuse's internal tRPC API with session auth (via the auto-login
Lambda@Edge on CloudFront) to create projects and API keys.

E36/T13 — WHICH ACCOUNT THE PER-AGENT SECRET LANDS IN, AND WHO CAN READ IT.
A REGISTERED agent (``POST /agents``) supplies its own runtime ARN, and that runtime may live
in a TENANT account. The secret was always created with the backend's AMBIENT ECS-task
credentials, i.e. in the CONTROL-PLANE account, and nothing ever told the runtime the secret
existed — so for a cross-account tenant the agent got a Langfuse project it could not
authenticate to, and for EVERY registered agent the two env vars the SDK reads
(``LANGFUSE_HOST`` / ``LANGFUSE_SECRET_NAME``) were never set: the terraform module writes them
declaratively (``modules/agentcore_runtime/main.tf``) and a registered agent's runtime is not
ours to apply. This module now (1) parses the owning account out of ``agent.agent_arn``,
(2) creates the secret through :func:`services.tenant_credentials.stage_client` in that account,
(3) injects the two env vars onto the runtime, and (4) attaches a resource policy letting the
runtime's exec role read that one secret.

BEST-EFFORT REMAINS THE WHOLE CONTRACT. The register-time hook
(``routes/agents.py:provision_langfuse_best_effort``) swallows failures by design — observability
wiring must never fail a registration — so EVERY leg added here degrades to a logged no-op:
an unresolvable account, a failed AssumeRole, an unreachable runtime and a rejected resource
policy all fall back or return, and none of them raises. That is deliberately the OPPOSITE
direction from ``tenant_credentials``' own "a failed assume must RAISE" rule, which exists for
TEARDOWN, where a swallowed failure is reported to an operator as ``deleted``. Here nothing is
reported as done: the fallback is loud in the log, and the honest alternative — abandoning a key
already minted inside Langfuse — would leave the agent with a project whose credentials exist
nowhere.
"""

import json
import logging
import re
from typing import Optional

import boto3
import requests

from services.tenant_credentials import (
    StageUnresolvedError,
    TenantCredentialsError,
    stage_client,
)

logger = logging.getLogger(__name__)

# The account id out of ANY AWS ARN (an ``agent_arn`` or a ``deploy_role_arn``): the 5th
# colon-delimited field. PARSED, never sliced — same argument as
# ``tenant_credentials._safe_role_label``: nothing validates ``agent_arn`` (it is a
# caller-supplied field on ``POST /agents``) or ``deploy_role_arn``, so a shape we merely assume
# would silently mis-target an account. ``[0-9]{12}`` not ``\d{12}`` (``\d`` matches Unicode
# digits) and ``[^:]*`` for service/region so a global-service ARN with an empty region parses.
_ARN_ACCOUNT_RE = re.compile(r"^arn:aws[\w-]*:[^:]*:[^:]*:(?P<account>[0-9]{12}):")

# STS caps ``RoleSessionName`` at 64 chars; ``stage_client`` truncates, but the suffix is kept
# short on purpose so the session name stays readable in CloudTrail: ``agp-lf-<8 hex>``.
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

    Raised ONLY by :meth:`LangfuseProvisioningService.delete_agent_project`, and only when
    the ambient delete did not demonstrably remove anything — see that method for why the
    teardown direction is the OPPOSITE of provisioning's swallow-and-fall-back. The caller
    reports it as a ``failed`` cascade line-item (T8's honest vocabulary) instead of the
    ``deleted`` that a "not found in the control-plane account" used to buy.

    A :class:`~services.tenant_credentials.StageUnresolvedError` (E36/T16 fix round 1)
    because it IS that condition, reached from another entry point: "we cannot tell which
    account owns this". Subclassing is what carries T8's vocabulary — every existing
    ``except StageUnresolvedError`` arm (``ProjectService._run_step``, both route cascades)
    renders it as ``stage_unresolved: <message>``, where a bare ``Exception`` fell through to
    the generic branch and reported the bare type name, discarding the very reason this class
    is built to carry.

    The message names the reason only (a stage/lookup state) — never an account id, per the
    hard project rule.
    """


class LangfuseProvisioningService:
    """Provisions Langfuse projects and API keys for use cases."""

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
        # E26: the AgentRegistryService used to persist the C1 join onto the agent
        # envelope (``persist_identity``). Optional so callers that never provision —
        # e.g. teardown via ``delete_agent_project`` — do not have to supply one.
        self._registry = registry
        # E26: project-name prefix — the AGP project token. Per-agent project name is
        # ``f"{agp_project_name}-{agent.name}"``.
        self._agp_project_name = agp_project_name
        self._sm = boto3.client("secretsmanager", region_name=region)
        self._session = None
        # E36/T13: the duck-typed TenantService (``get(tenant_id) -> record.stages``) used to find
        # which stage owns the account the agent's runtime lives in. Optional — None is the legacy
        # wiring every test that does not opt into a tenant has, and it means "stay ambient".
        self._tenants = tenants
        # Held as an attribute (the ``project_service`` idiom) so a test can inject one without
        # patching a module global; the pinned signature stays exactly
        # ``stage_client(service, cfg, *, session_suffix)``.
        self._stage_client = stage_client
        # The control-plane account, probed ONCE (``sts:GetCallerIdentity`` is free but not free
        # enough to repeat per agent) and lazily — never at construction, because the singleton is
        # built at first request and an STS round-trip must not sit in that path unless it is used.
        self._platform_account: Optional[str] = None
        self._platform_account_probed = False

    def _get_session(self) -> requests.Session:
        """Get an authenticated session via Langfuse's CSRF/auto-login flow."""
        if self._session:
            return self._session

        session = requests.Session()
        # Get CSRF token
        r = session.get(f"{self.langfuse_host}/api/auth/csrf", timeout=10)
        r.raise_for_status()
        csrf = r.json().get("csrfToken", "")

        # Trigger session creation via credentials callback
        # The auto-login Lambda@Edge handles actual authentication
        session.post(
            f"{self.langfuse_host}/api/auth/callback/credentials",
            data={"csrfToken": csrf, "email": "admin@langfuse.com", "password": "x", "callbackUrl": self.langfuse_host},
            timeout=10,
            allow_redirects=False,
        )

        # Verify session
        r = session.get(f"{self.langfuse_host}/api/auth/session", timeout=10)
        if r.status_code != 200 or not r.json().get("user"):
            raise RuntimeError("Failed to establish Langfuse session")

        self._session = session
        return session

    def _get_seed_keys(self):
        """Get seed project keys for API auth fallback."""
        secret = json.loads(self._sm.get_secret_value(SecretId=self.langfuse_secret_name)["SecretString"])
        return (secret.get("langfuse_public_key", ""), secret.get("langfuse_secret_key", ""))

    # =====================================================================
    # Per-agent provisioning (Epic 26, Task 4 — contract C2)
    # =====================================================================
    #
    # One agent = one Langfuse PROJECT = one API key. Observability is structural:
    # the agent authenticates with its own project key so its traces land in its own
    # project (no trace tags). The join (``langfuse_project_id`` +
    # ``langfuse_key_secret_name``) is written onto the agent's registry envelope; the
    # key VALUES live ONLY in Secrets Manager — they NEVER touch the envelope or a log.

    # -- the account seam for a REGISTERED agent's runtime (E36/T13) --------- #

    def _platform_account_id(self) -> Optional[str]:
        """The control-plane account id, or None when it could not be determined.

        None is NOT "no account": it is "we do not know which account we are in", and every
        caller treats it as a reason to stay on the ambient client. Guessing the other way — a
        failed probe read as "this agent must be cross-account" — would send every provisioning
        through an AssumeRole the ECS task role may not hold for that stage.

        ONLY A SUCCESS IS CACHED (E36/T13, fix round 1). The probe flag used to be set BEFORE the
        call, so one transient ``sts:GetCallerIdentity`` blip pinned the whole process to the
        ambient path for the lifetime of this singleton — every cross-account agent registered
        afterwards would have had its secret created where its runtime cannot read it, and the
        operator would have seen ONE warning at process start instead of one per affected agent.
        Leaving the flag False on the failure path costs at most one extra STS round-trip per
        registration while STS is unreachable, and recovers on its own.
        """
        if not self._platform_account_probed:
            try:
                account = boto3.client(
                    "sts", region_name=self.region
                ).get_caller_identity()["Account"]
            except Exception:  # noqa: BLE001 — best-effort; unknown ⇒ ambient (today's path)
                logger.warning(
                    "[langfuse] could not resolve the platform account; per-agent secrets stay "
                    "on the ambient client (the next agent re-probes)"
                )
                return None
            self._platform_account = account
            self._platform_account_probed = True
        return self._platform_account

    def _stage_cfg_for(self, agent):
        """The :class:`TenantStageConfig` owning the agent's runtime, or None ⇒ ambient client.

        The PROVISIONING view of :meth:`_resolve_stage_cfg`: it discards the "why" because
        every unknown means the same thing on the create path — stay ambient, warn, never
        block a registration. Teardown needs the "why" (see
        :meth:`delete_agent_project`), which is the only reason the two are split; keeping
        one derivation is what stops a delete from addressing a different account than the
        create did.
        """
        return self._resolve_stage_cfg(agent)[0]

    def _resolve_stage_cfg(self, agent):
        """``(cfg, unresolved)`` — the stage owning the agent's runtime and, when there is
        none, whether ambient is the RIGHT answer or merely the only one available.

        ``unresolved`` is None when ambient is genuinely correct (nothing to parse, or the
        runtime is in THIS account) and a short SAFE reason string when we simply could not
        tell. Both callers treat ``cfg`` identically; they differ on ``unresolved``.

        Leg 1 of the T13 contract. The account is PARSED out of ``agent.agent_arn`` and matched
        against the account inside each stage's ``deploy_role_arn`` — NOT against
        ``TenantStageConfig.account_id``, even though that field exists. ``deploy_role_arn`` is
        the only field that names a credential we can actually assume: a stage carrying an
        ``account_id`` with no ``deploy_role_arn`` is deploy-in-place, and "matching" it would
        hand ``stage_client`` a cfg it can only answer ambiently for anyway.

        THE SAME-ACCOUNT SHORT-CIRCUIT COMES FIRST, BEFORE THE TENANT LOOKUP, and that ordering
        is load-bearing: the shipped tenant's deploy role lives in the PLATFORM account
        (``modules/default_tenant``), so a resolution that looked at the stages first would
        AssumeRole for every single-account agent — turning today's working ambient path into one
        that depends on a grant and on the tenant store being readable.

        None on every unknown (no parseable ARN, unknown platform account, no tenant service, no
        ``tenant_id``, a tenant lookup that raises, no record, no matching stage). EVERY case
        where we cannot tell that the runtime is here WARNS, and warns PER AGENT naming that
        agent — a degradation that is invisible in the log is the same defect as no degradation
        handling at all (the unknown-platform-account branch used to share the silent
        same-account return). NO ACCOUNT ID IN ANY MESSAGE — the hard project rule; the agent id
        is the actionable half.
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
        except Exception:  # noqa: BLE001 — best-effort; a store outage ⇒ ambient + a warning
            logger.warning(
                "[langfuse] tenant lookup failed while resolving agent %s's runtime account; "
                "creating its secret in the platform account",
                agent.id,
            )
            return None, "the tenant lookup failed"
        stages = (getattr(tenant, "stages", None) or {}) if tenant is not None else {}
        # Sorted for determinism: two stages could name the same account (dev+prod in one
        # tenant account), and which cfg we pick must not depend on dict ordering.
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
        """A client for ``cfg``'s account, or None ⇒ the caller falls back to ambient.

        A failed AssumeRole is a WARNING plus the platform-account fallback, not a raise — see
        the module docstring for why this inverts ``tenant_credentials``' teardown rule. The
        error's message is already SAFE (role NAME + exception type, never an account id).
        """
        if cfg is None:
            return None
        try:
            return self._stage_client(
                service_name, cfg, session_suffix=f"lf-{agent.id[:_SESSION_ID_CHARS]}"
            )
        except TenantCredentialsError as err:
            logger.warning(
                "[langfuse] could not reach the account owning agent %s's runtime as %s (%s); "
                "staying on the platform account",
                agent.id,
                service_name,
                err.message,
            )
            return None
        except Exception:  # noqa: BLE001 — best-effort; never block provisioning
            logger.warning(
                "[langfuse] cross-account client construction failed for agent %s (%s); staying "
                "on the platform account",
                agent.id,
                service_name,
            )
            return None

    def _agent_sm(self, agent, cfg=None):
        """The Secrets Manager client for the account holding the agent's PER-AGENT secret.

        Only the per-agent secret moves. ``self._sm`` stays the client for the ADMIN secret
        (``_get_seed_keys``), which is a control-plane resource by definition.
        """
        if cfg is None:
            cfg = self._stage_cfg_for(agent)
        return self._stage_scoped("secretsmanager", agent, cfg) or self._sm

    def _agent_secret_name(self, agent) -> str:
        """Secrets Manager name holding the per-agent project key pair.

        Keyed on the (stable, unique) agent id — NOT the display name, which can change
        and contain spaces — so the SM name is deterministic + idempotent across re-runs.
        """
        return f"langfuse-agent-{agent.id}-keys"

    def provision_agent_project(self, agent) -> dict:
        """Create a Langfuse project + key for one agent and record the join (C2).

        Idempotent: if the agent envelope already carries a ``langfuse_project_id`` the
        existing project is returned WITHOUT creating a new one (reads the stored public
        key from Secrets Manager). If that stored secret is missing (deleted out-of-band),
        the short-circuit falls through to re-mint a key + re-create the secret rather than
        dead-locking — the create path's list-and-match fallback reuses the existing project
        by name, so the agent recovers a usable key. Otherwise: create the project
        (``agp``-prefixed name, seed org) via the internal tRPC API, mint a project key, store
        ``{public_key, secret_key, project_name, project_id}`` in Secrets Manager (tagged
        ``managed_by=agp``), then write ``langfuse_project_id`` + ``langfuse_key_secret_name``
        onto the agent envelope (persisted via the registry, if one was injected).

        Returns ``{"project_id", "secret_name", "public_key"}`` — the NON-secret join
        (the secret key is never returned, only stored in Secrets Manager).

        E36/T13: every call against the PER-AGENT secret (the idempotency read included) runs on
        the client for the account that owns the agent's RUNTIME — see :meth:`_stage_cfg_for`.
        The idempotency read has to move with the write or it would answer NotFound about the
        control-plane account on every run for a cross-account agent, re-minting a key each time.
        """
        secret_name = self._agent_secret_name(agent)
        sm = self._agent_sm(agent)

        # Idempotency — short-circuit if the envelope already carries the project id AND
        # its stored key is still retrievable. If the secret was deleted out-of-band the
        # method must NOT dead-lock: fall through to re-mint a key for the project (the
        # create path's list-and-match fallback reuses the existing project by name).
        if agent.langfuse_project_id:
            try:
                existing = json.loads(
                    sm.get_secret_value(SecretId=secret_name)["SecretString"]
                )
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
                # Stored key gone (manual delete / torn write / partial teardown). Do NOT
                # raise — re-provision below to restore a usable key for the existing project.
                logger.warning(
                    "Langfuse secret missing for already-provisioned agent %s (%s); "
                    "re-minting key",
                    agent.id,
                    agent.langfuse_project_id,
                )

        project_name = f"{self._agp_project_name}-{agent.name}"
        session = self._get_session()

        # Create the project via tRPC, targeting the seed org.
        r = session.post(
            f"{self.langfuse_host}/api/trpc/projects.create",
            json={"json": {"name": project_name, "orgId": "seed-org"}},
            timeout=10,
        )
        if r.status_code == 200:
            project_id = r.json()["result"]["data"]["json"]["id"]
            logger.info("Created Langfuse project %s (%s)", project_name, project_id)
        else:
            # Project might already exist — list via the public API + match by name.
            logger.info(
                "Project creation returned %s, checking if it exists", r.status_code
            )
            r2 = requests.get(
                f"{self.langfuse_host}/api/public/projects",
                auth=(self._get_seed_keys()),
                timeout=10,
            )
            projects = r2.json().get("data", [])
            project = next((p for p in projects if p["name"] == project_name), None)
            if not project:
                raise RuntimeError(
                    f"Failed to create or find project {project_name}: {r.text[:200]}"
                )
            project_id = project["id"]
            logger.info(
                "Found existing Langfuse project %s (%s)", project_name, project_id
            )

        # Mint the project API key via tRPC.
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

        # Store the key pair in Secrets Manager. The secret VALUE never leaves here.
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

        # Write the C1 join onto the agent envelope (only the NAME + project id).
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
        """Tell the agent's RUNTIME about the project it was just given (E36/T13, legs 3+4).

        Two writes, in this order, and NEITHER can raise:

        1. **Env injection.** ``set_runtime_environment(agent.agent_arn, {"LANGFUSE_HOST": …,
           "LANGFUSE_SECRET_NAME": …}, control_client=…)`` — a MERGE, so it never clobbers the
           runtime's other env, and idempotent, so re-registering converges. Without it a
           registered agent's runtime has no idea any of this exists: those two variables are
           written declaratively by the terraform module for agents WE deploy, and a registered
           agent's runtime is not ours to apply. ``wait_ready=False`` (E36/T13, fix round 1):
           the poll buys leg 4 NOTHING — the ``roleArn`` it grants to comes from the GET that
           happens BEFORE the poll, and leg 4 writes to SECRETS MANAGER, not to the runtime, so
           there is no second runtime write to race. What the poll does cost is real: Starlette
           runs a sync ``BackgroundTask`` on the shared ``anyio.to_thread`` pool (~40 workers,
           which every sync boto3 call in the backend competes for) and
           :meth:`~services.agent_identity_service.AgentIdentityService._poll_runtime_ready`
           sleeps up to 300 s, so waiting would park one of those workers for five minutes per
           registration and a bulk import would starve the pool. Not waiting cannot hide a
           failure either — a non-converged update leaves the two variables absent from the
           runtime, which is exactly what the next wiring run observes. Same reasoning T12
           applied to reconcile-on-read.
        2. **The read grant.** A resource policy on THAT ONE secret allowing
           ``secretsmanager:GetSecretValue`` to the runtime's exec role — the ``roleArn``
           :meth:`~services.agent_identity_service.AgentIdentityService.set_runtime_environment`
           now returns from the ``get_agent_runtime`` it already performs, which is why leg 4
           costs zero extra AWS calls. Required cross-account (an identity-policy grant in the
           tenant account cannot reach a control-plane secret without the resource policy's
           consent) and harmless same-account, where it is a second, narrower statement of a
           permission the exec role's own policy already carries. Attached unconditionally
           because "unconditionally" is what makes it idempotent.

        ORDERING NOTE, stated because it is a real (small) wart: the env lands BEFORE the grant,
        so a runtime that restarts in that window sees a secret name it may not yet be allowed to
        read. It is that way because the roleArn we grant to is a by-product of the injection's
        own read — the alternative is a third ``get_agent_runtime`` to invert the order — and the
        failure mode is one restart of a self-healing SDK path, versus a permanent extra call on
        every registration.

        Skipped entirely when the agent names no runtime (a pre-registration record): there is
        nothing to inject into, and the deploy that creates the runtime writes both variables
        declaratively anyway.
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
        except Exception:  # noqa: BLE001 — best-effort; never block/abort registration
            logger.warning(
                "[langfuse] runtime env injection best-effort no-op for agent %s (the runtime "
                "is unreachable; its traces will not be attributed)",
                agent.id,
            )
            return

        if not role_arn:
            # Nothing to grant TO. Only reachable if a caller's double returns no roleArn — the
            # real method replays that field into its own update, so a live runtime always has it.
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
        except Exception:  # noqa: BLE001 — best-effort; never block/abort registration
            logger.warning(
                "[langfuse] secret read grant best-effort no-op for agent %s (secret %s); the "
                "runtime may not be able to read its Langfuse key",
                agent.id,
                secret_name,
            )

    def delete_agent_project(self, agent) -> None:
        """Best-effort/idempotent teardown of an agent's Langfuse project + SM secret (C2).

        Used by the E23 repo-delete cascade (T7) and by ``DELETE /agents/{id}``'s
        registered-external cascade (E36/T16). Deletes the Langfuse project (internal tRPC)
        and the per-agent Secrets Manager secret. Already-gone (404 / NotFound / any error on
        the best-effort project delete) == success, so it is safe to call unconditionally
        from an ordered teardown even for an agent that never got a Langfuse project.

        E36/T16 — WHICH ACCOUNT THE SECRET IS DELETED IN, AND WHEN "DELETED" MAY BE CLAIMED.
        The secret delete ran on the AMBIENT client, i.e. always in the control-plane
        account, while T13's CREATE places a registered agent's secret in the account that
        owns its runtime. For a cross-account agent that made the teardown a truthful answer
        to the wrong question: Secrets Manager answers ``ResourceNotFoundException`` *here*,
        the swallow below reads it as already-gone, and the cascade reports ``deleted`` while
        the tenant account keeps the secret. Exactly the defect class T8 removed from the
        runtime + exec-role steps.

        So the secret leg now resolves the account through the SAME helper the create path
        uses (:meth:`_resolve_stage_cfg`) and diverges from provisioning in the two places
        where "best-effort" would otherwise mean "lie":

          - a resolvable cross-account stage ⇒ ``stage_client`` directly, and a
            :class:`~services.tenant_credentials.TenantCredentialsError` PROPAGATES. It is
            NOT routed through :meth:`_stage_scoped`, whose ambient fallback is right for
            creation (a key already minted must land somewhere) and wrong here (falling back
            reproduces the original defect). ``project_service._run_step`` renders it as
            ``assume_role_failed: <role name>``.
          - an UNRESOLVABLE account ⇒ the ambient delete is still ATTEMPTED, because it is
            the only account we can reach and the secret may well be in it. If it actually
            deletes something, that is proof and the teardown succeeded. If it answers
            not-found, that answer carries no information about the account that matters, so
            :class:`LangfuseAccountUnresolvedError` is raised and the caller reports the item
            ``failed`` — never ``deleted``.

        Everything else stays swallowed, including a non-not-found SM failure on a RESOLVED
        account: that is the pre-existing best-effort contract and both callers already treat
        this method as non-blocking, so widening the raise would change more than the account
        defect this task exists for.
        """
        # (1) Best-effort delete of the Langfuse project. Any failure (already-gone 404,
        # session failure, tRPC shape drift) is swallowed — teardown must not be blocked.
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
            except Exception:  # noqa: BLE001 — best-effort; already-gone == success
                logger.warning(
                    "[langfuse] project delete best-effort no-op for agent %s "
                    "(already gone or unreachable)",
                    agent.id,
                )

        # (2) Delete the per-agent secret IN THE ACCOUNT THAT HOLDS IT (E36/T16). NotFound is
        # success only when we know that account; see the docstring.
        secret_name = agent.langfuse_key_secret_name or self._agent_secret_name(agent)
        cfg, unresolved = self._resolve_stage_cfg(agent)
        if cfg is None:
            sm = self._sm
        else:
            # A failed assume RAISES (no ambient fallback) — the teardown rule of
            # ``tenant_credentials``, and the whole point of resolving at all.
            sm = self._stage_client(
                "secretsmanager", cfg, session_suffix=f"lf-{agent.id[:_SESSION_ID_CHARS]}"
            )
        try:
            sm.delete_secret(SecretId=secret_name, ForceDeleteWithoutRecovery=True)
            logger.info("Deleted Langfuse secret %s for agent %s", secret_name, agent.id)
            return
        except sm.exceptions.ResourceNotFoundException:
            if unresolved is None:
                return  # already gone in the account that owns it — success
            logger.warning(
                "[langfuse] secret %s for agent %s is not in the platform account and its "
                "owning account is unresolvable — reporting the teardown as failed rather "
                "than as done",
                secret_name,
                agent.id,
            )
        except Exception:  # noqa: BLE001 — best-effort; never block teardown
            logger.warning(
                "[langfuse] secret delete best-effort no-op for agent %s "
                "(already gone or unreachable)",
                agent.id,
            )
            if unresolved is None:
                return
        raise LangfuseAccountUnresolvedError(unresolved)
