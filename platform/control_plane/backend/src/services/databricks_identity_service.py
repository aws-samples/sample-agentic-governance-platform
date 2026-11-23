"""The platform half of provisioning for a Databricks-hosted agent.

``agent_identity_service.provision_identity`` is platform-neutral: a per-agent Entra app, its
service principal and the two appRoles. This module follows it and is the whole of the difference
between the platforms: AgentCore gets an inbound JWT authorizer on its runtime; Databricks gets an
entry on the account's Entra federation policy and an asserted access list on the app.

Federation is the only binding AGP offers, because in it the agent and Unity Catalog see the real
caller. A tenant the connect-time probe badged ``invoke_unavailable`` is refused
(``federation_unavailable``), naming the two grants federation needs: an account-admin credential
on the tenant, and this Entra tenant's identities synced into the Databricks account. A federation
tenant without user sync is refused (``user_sync_missing``). The ``sp_secret`` mode, in which every
call is attributed to a per-agent Databricks service principal and the audit trail ends at AGP's
boundary, is dormant: the probe never selects it, and it runs only for a record that carries the
mode and only when ``settings.DATABRICKS_ALLOW_SP_SECRET_BINDING`` is on; otherwise it is refused
(``sp_secret_disabled``). Teardown is never gated: removing trust state that exists is always
allowed.

``binding_mode``, ``databricks_sp_id`` and ``databricks_sp_secret_arn`` are client-settable
registration fields, so every run overwrites them from the tenant and the provisioning truth, and a
stored secret ARN is trusted only when it is one this service wrote for this agent
(``DatabricksAgentSecrets.owns_secret_arn``).

Secrets are read from Secrets Manager by the ARN the tenant record stores, at call time, into a
local. They never reach the agent record, a log line or an error message, and the per-agent secret
minted in ``sp_secret`` mode exists only between the mint and the store. Account-level calls use the
tenant's account-admin credential when one is stored and the workspace service principal otherwise.

A ``DatabricksError`` is translated here into a ``ProvisioningError`` whose message is composed
from ``.kind`` alone (``_actionable``); the upstream message is never forwarded. Every provisioning
path is idempotent and resumable, and ``runtime_status`` never raises.
"""

from __future__ import annotations

import logging
from typing import Optional

import anyio.to_thread

from core.config import settings
from models.agent import (
    Agent,
    IdentityStatus,
    RuntimeStatus,
    is_databricks_governed_agent,
)
from models.tenant import (
    ACCOUNT_ADMIN_ID_KEY,
    ACCOUNT_ADMIN_SECRET_KEY,
    TenantBindingMode,
)
from services import databricks_runtime_status
from services.agent_identity_service import ProvisioningError
from services.databricks_agent_secrets import DatabricksAgentSecrets
from services.databricks_runtime_status import _match_app
from services.databricks_workspace_service import DatabricksError
from services.platform_adapter import Artifact, ArtifactPolicy
from services.tenant_service import (
    BINDING_FEDERATION,
    BINDING_INVOKE_UNAVAILABLE,
    BINDING_SP_SECRET,
)

logger = logging.getLogger(__name__)

# The binding modes, declared once beside their writer (``tenant_service``); anything else,
# including the AWS "", is refused rather than defaulted.
_MODE_FEDERATION = BINDING_FEDERATION
_MODE_SP_SECRET = BINDING_SP_SECRET
_MODE_INVOKE_UNAVAILABLE = BINDING_INVOKE_UNAVAILABLE

# Only a Databricks App exposes the permissions surface the asserted ACL needs.
_KIND_APP = "app"

# The residue label of a blocking artifact whose removal failed, as the teardown-incomplete
# message reads it.
_ARTIFACT_LABELS = {"federation_audience": "federation audience"}

# The workspace admins keep CAN_MANAGE on every asserted ACL; the client refuses a PUT without it.
_CAN_USE = "CAN_USE"
_CAN_MANAGE = "CAN_MANAGE"
_ADMINS_ENTRY = {"principal": "admins", "kind": "group", "level": _CAN_MANAGE}
_KIND_SERVICE_PRINCIPAL = "service_principal"

# Account-console hosts by cloud; not derivable from the workspace URL.
_ACCOUNT_HOSTS = {
    "aws": "https://accounts.cloud.databricks.com",
    "azure": "https://accounts.azuredatabricks.net",
    "gcp": "https://accounts.gcp.databricks.com",
}

# The Entra issuer form an account federation policy must carry, named in the actionable errors.
_ENTRA_ISSUER_FORM = "https://login.microsoftonline.com/<entra-tenant-id>/v2.0"

# The DatabricksError kinds that describe the customer's account rather than a fault in AGP, each
# with a sentence naming who must act.
_ACTIONABLE_KINDS = {
    "federation_policy_missing": (
        "the Databricks account has no Entra OIDC federation policy, so this agent's "
        "audience cannot be trusted — ask your Databricks account admin to create the "
        f"OIDC federation policy for {_ENTRA_ISSUER_FORM}"
    ),
    "federation_policy_ambiguous": (
        "the Databricks account carries more than one Entra OIDC federation policy, so AGP "
        "cannot tell which one governs this agent — ask your Databricks account admin to "
        f"leave exactly one policy for {_ENTRA_ISSUER_FORM}"
    ),
    "federation_policy_unreadable": (
        "the Databricks account's Entra OIDC federation policy could not be read in a shape "
        "AGP recognises, so it refused to write to it rather than risk replacing the "
        "account's trust entries — ask your Databricks account admin to review the policy "
        f"for {_ENTRA_ISSUER_FORM}"
    ),
    "unauthorized": (
        "Databricks refused AGP's credential — ask your Databricks account admin to confirm "
        "the account-admin service principal AGP was given is still active"
    ),
    "forbidden": (
        "Databricks accepted AGP's credential but refused the operation — ask your "
        "Databricks account admin to confirm it holds account-admin (federation policies) "
        "and CAN_MANAGE on the app"
    ),
}


def _actionable(err: DatabricksError, what: str) -> str:
    """A safe, actionable sentence for a Databricks failure, from ``.kind`` alone: the upstream
    message is not forwarded even though the client composes it safely, because an error string
    is where a workspace path or an echoed request form would become a UI element. The kind is
    appended verbatim (it is constrained to ``^[A-Za-z_]{1,64}$`` upstream) so support can grep
    for it."""
    hint = _ACTIONABLE_KINDS.get(err.kind)
    if hint:
        return f"{hint} [{err.kind}]"
    return f"Databricks could not {what} [{err.kind}]"


class DatabricksIdentityService:
    """Provision and deprovision the Databricks half of an agent's identity binding.

    Collaborators are injected, and credentials are never sourced by the collaborator that uses
    them: this service, through its ``DatabricksAgentSecrets``, is the one place that turns a stored
    Secrets Manager ARN into a live credential, and it hands the value straight to a token mint.
    ``tenants`` is optional and only used to resolve a tenant from ``agent.tenant_id`` when the
    caller does not already hold one.
    """

    def __init__(
        self,
        *,
        databricks,
        registry,
        tenants=None,
        secrets_client=None,
        secret_prefix: str = "agp-dev/databricks-tenants/agents/",  # noqa: S107 — a Secrets Manager name prefix, not a credential; expires 2027-10-23; added E49A-50A/T12
        region: str | None = None,
    ) -> None:
        self._databricks = databricks
        self._registry = registry
        self._tenants = tenants
        self._region = region
        self._secrets = DatabricksAgentSecrets(sm=secrets_client, secret_prefix=secret_prefix, region=region)

    async def provision_databricks_runtime(self, agent: Agent, tenant=None) -> Agent:
        """The platform half for a Databricks agent. Returns the agent, or raises
        ``ProvisioningError`` after persisting ``identity_status = failed``, so a re-provision of
        the platform half alone leaves no record stranded at ``pending``."""
        try:
            return await self._provision(agent, tenant)
        except ProvisioningError:
            self._persist_failed(agent)
            raise
        except DatabricksError as err:
            self._persist_failed(agent)
            logger.warning(
                "[databricks_identity] provisioning failed for agent %s (%s)",
                agent.id,
                err.kind,
            )
            raise ProvisioningError(
                f"provisioning failed for agent {agent.id}: {_actionable(err, 'complete provisioning')}"
            ) from err
        except Exception as err:
            self._persist_failed(agent)
            logger.warning(
                "[databricks_identity] provisioning failed for agent %s: %s",
                agent.id,
                type(err).__name__,
            )
            raise ProvisioningError(f"provisioning failed for agent {agent.id}") from err

    async def _provision(self, agent: Agent, tenant) -> Agent:
        """Dispatch on the tenant's binding mode after overwriting every client-settable binding
        field from the tenant: the mode, the OAuth client id (read back from the listing on the
        federation leg only) and the two service-principal pointers, which survive only when the
        ARN passes ``DatabricksAgentSecrets.owns_secret_arn``. The scrub runs before the dispatch so
        a refused provision persists the truth, not the caller's claim."""
        if not is_databricks_governed_agent(agent):
            raise ProvisioningError(f"agent {agent.id} is not a Databricks-governed agent")
        if agent.runtime_kind and agent.runtime_kind != _KIND_APP:
            raise ProvisioningError(
                f"agent {agent.id} names a {agent.runtime_kind!r} runtime; only "
                f"{_KIND_APP!r} runtimes can be identity-bound on Databricks today"
            )

        tenant = self._resolve_tenant(agent, tenant)
        mode: TenantBindingMode = tenant.binding_mode if tenant is not None else ""

        agent.binding_mode = mode
        agent.oauth2_app_client_id = None
        self._secrets.drop_untrusted_sp_pointers(agent)

        if mode == _MODE_FEDERATION:
            agent.databricks_sp_id = None
            agent.databricks_sp_secret_arn = None
            await self._provision_federation(agent, tenant)
        elif mode == _MODE_INVOKE_UNAVAILABLE:
            raise ProvisioningError(
                f"agent {agent.id} cannot be bound: its tenant "
                f"({getattr(tenant, 'id', '?')!r}) is not able to invoke on Databricks "
                f"(federation_unavailable). Federation needs BOTH an account-admin credential "
                f"on the tenant and this Entra tenant's identities synced into the Databricks "
                f"account (user sync) — add them and re-connect the tenant so its capabilities "
                f"are re-probed. AGP will not fall back to service-principal binding, because "
                f"that would silently change what the audit log can prove"
            )
        elif mode == _MODE_SP_SECRET and not settings.DATABRICKS_ALLOW_SP_SECRET_BINDING:
            raise ProvisioningError(
                f"agent {agent.id} carries the dormant per-agent service-principal Databricks "
                f"binding (sp_secret_disabled), which this deployment does not allow: every "
                f"call would be attributed to a service principal instead of the caller. Set "
                f"DATABRICKS_ALLOW_SP_SECRET_BINDING=true to enable it deliberately, or give "
                f"the tenant an account-admin credential + user sync and use federation"
            )
        elif mode == _MODE_SP_SECRET:
            await self._provision_sp_secret(agent, tenant)
        else:
            raise ProvisioningError(
                f"tenant {getattr(tenant, 'id', '?')!r} has no usable Databricks binding "
                f"mode ({mode!r}); connect the tenant so its capabilities are probed"
            )

        agent.identity_status = IdentityStatus.PROVISIONED
        await anyio.to_thread.run_sync(self._registry.persist_identity, agent)
        return agent

    async def _provision_federation(self, agent: Agent, tenant) -> None:
        """User sync verified, then the agent's audience on the account policy, then the app's
        ACL asserted.

        The user-sync check reads the tenant's connect-time capability flag and runs before any
        Databricks call, absent treated as False; there is no live re-probe because that needs
        the optional account-admin credential. The audience added is the agent's Entra client id,
        the ``aud`` of the v2 tokens the app receives, not the ``api://`` URI AgentCore's
        authorizer uses. The tenant's ``entra_group_ids`` are not granted: per-user entries
        arrive from grants. The app's OAuth client id is persisted when the listing carries it
        and cleared otherwise, since the field is client-settable and absent evidence is the
        honest value."""
        capabilities = getattr(tenant, "capabilities", None) or {}
        if not capabilities.get("user_sync"):
            raise ProvisioningError(
                f"agent {agent.id} cannot be bound in federation mode: the Databricks "
                f"account does not have this Entra tenant's identities synced "
                f"(user_sync_missing). Ask your Databricks account admin to enable "
                f"automatic identity management for "
                f"{_ENTRA_ISSUER_FORM}. AGP will not fall back to service-principal "
                f"binding, because that would silently change what the audit log can prove"
            )

        if not agent.entra_app_id:
            raise ProvisioningError(
                f"agent {agent.id} has no Entra app client id, so there is nothing to add to "
                f"the Databricks federation policy — provision the Entra identity first"
            )

        stage, workspace_token, app = await self._resolve_stage_and_app(agent, tenant)

        account_id = (getattr(stage, "account_id", "") or "").strip()
        if not account_id:
            raise ProvisioningError(
                f"tenant {getattr(tenant, 'id', '?')!r} has no Databricks account id on the "
                f"workspace hosting agent {agent.id}, and federation mode writes an "
                f"account-level policy — add the account id to the tenant's stage"
            )

        account_token = await self._account_token(tenant, stage, account_id)
        try:
            await self._databricks.ensure_federation_audience(
                self._account_host(stage),
                account_id,
                account_token,
                agent.entra_app_id,
                present=True,
            )
        except DatabricksError as err:
            raise ProvisioningError(
                f"agent {agent.id} could not be bound in federation mode: "
                f"{_actionable(err, 'update the federation policy')}"
            ) from err

        app_name = str(app.get("name") or "")
        await self._assert_app_acl(agent, stage, workspace_token, app_name)

        client_id = app.get("oauth2_app_client_id")
        if isinstance(client_id, str) and client_id:
            agent.oauth2_app_client_id = client_id
        else:
            agent.oauth2_app_client_id = None
            logger.info(
                "[databricks_identity] app record for agent %s carries no readable oauth2 client id",
                agent.id,
            )

    async def _provision_sp_secret(self, agent: Agent, tenant) -> None:
        """A per-agent Databricks service principal, its OAuth secret, then the asserted ACL with
        that principal at ``CAN_USE``.

        The order is the security property: create the SP, store its SCIM id in a credential-less
        secret entry and persist both pointers, then mint the secret into that entry and persist,
        then assert the ACL. Every write lands before the step that could fail after it, so no
        step leaves a live resource the record does not name. A failed persist of the new entry
        deletes it (it holds no credential yet); a failed persist after the mint deletes nothing,
        because the record already points at the entry and the stored SCIM id is what lets the
        retry reach the principal. The mint is keyed on the body carrying no credential, not on
        the ARN existing, so a retry never mints a second secret."""
        stage, workspace_token, app = await self._resolve_stage_and_app(agent, tenant)

        scim_id = self._secrets.stored_scim_id(agent)
        if not agent.databricks_sp_id:
            created = await self._databricks.create_service_principal(
                stage.workspace_url, workspace_token, f"agp-agent-{agent.id}"
            )
            application_id = str(created.get("application_id") or "")
            scim_id = str(created.get("id") or "")
            if not application_id or not scim_id:
                raise ProvisioningError(
                    f"Databricks returned an incomplete service principal for agent "
                    f"{agent.id}, so it can be neither credentialed nor granted access"
                )
            agent.databricks_sp_id = application_id
            created_arn = self._secrets.store_agent_secret(agent, secret="", scim_id=scim_id)
            agent.databricks_sp_secret_arn = created_arn
            try:
                await anyio.to_thread.run_sync(self._registry.persist_identity, agent)
            except Exception:
                agent.databricks_sp_secret_arn = None
                try:
                    self._secrets.delete_secret_best_effort(created_arn)
                except Exception:
                    logger.exception(
                        "[databricks_identity] orphaned per-agent secret entry for agent %s",
                        agent.id,
                    )
                raise

        if not self._secrets.stored_agent_secret_present(agent):
            if not scim_id:
                raise ProvisioningError(
                    f"agent {agent.id} names a Databricks service principal whose SCIM id AGP "
                    f"no longer holds, so a credential cannot be minted for it — delete the "
                    f"agent's Databricks binding and re-provision to create a fresh principal"
                )
            minted = await self._databricks.create_service_principal_secret(
                stage.workspace_url, workspace_token, scim_id
            )
            secret_arn = self._secrets.store_agent_secret(agent, secret=minted["secret"], scim_id=scim_id)
            agent.databricks_sp_secret_arn = secret_arn
            await anyio.to_thread.run_sync(self._registry.persist_identity, agent)

        await self._assert_app_acl(
            agent,
            stage,
            workspace_token,
            str(app.get("name") or ""),
            extra_entries=[
                {
                    "principal": agent.databricks_sp_id,
                    "kind": _KIND_SERVICE_PRINCIPAL,
                    "level": _CAN_USE,
                }
            ],
        )

    def runtime_status(self, agent: Agent, tenant=None, *, stage: str | None = None) -> RuntimeStatus:
        return databricks_runtime_status.runtime_status(self, agent, tenant, stage=stage)

    async def delete_databricks_runtime(self, agent: Agent, tenant=None) -> None:
        """Remove every artifact ``named_databricks_artifacts`` enumerates, attempting all of
        them, then raise ``ProvisioningError`` if a blocking one survived.

        The federation audience is blocking: it is live trust state on the customer's account,
        and the record is the only place that still names which audience to withdraw, so the
        record survives for a retry. The per-agent secret and the service principal are
        best-effort: ours, reported by id, never a reason to trap the record. Safe to call on an
        agent that was never provisioned, and not gated by
        ``DATABRICKS_ALLOW_SP_SECRET_BINDING``.
        """
        failures: list[str] = []
        for artifact in self.named_databricks_artifacts(agent):
            try:
                await self.remove_databricks_artifact(agent, artifact, tenant)
            except Exception:
                logger.warning(
                    "[databricks_identity] %s removal failed for agent %s",
                    artifact.kind,
                    agent.id,
                    exc_info=True,
                )
                if artifact.policy is ArtifactPolicy.BLOCKING:
                    failures.append(_ARTIFACT_LABELS.get(artifact.kind, artifact.kind))

        if failures:
            raise ProvisioningError(
                f"Databricks teardown incomplete for agent {agent.id}: "
                f"{', '.join(sorted(failures))} could not be removed"
            )

    @staticmethod
    def named_databricks_artifacts(agent: Agent) -> list[Artifact]:
        """What a Databricks record names that teardown must handle, read off the record's own
        fields so the adapter can enumerate them even with no collaborator wired. Never raises.

        ``federation_audience`` (blocking) in federation mode when the record still names an
        audience form; ``agent_secret`` (best-effort) when the record names a service-principal
        id or an ARN, so the partial state of a principal created before its secret was stored is
        still reached; ``platform_sp`` (best-effort, report-only) when the record names a
        principal. ``ref`` carries a safe identifier only: never the secret ARN, which holds the
        AWS account id, and never a credential."""
        artifacts: list[Artifact] = []
        mode = (agent.binding_mode or "").strip()
        if mode == _MODE_FEDERATION and (agent.entra_app_id or agent.entra_app_audience):
            artifacts.append(
                Artifact(
                    kind="federation_audience",
                    ref=str(agent.entra_app_id or agent.entra_app_audience or ""),
                    policy=ArtifactPolicy.BLOCKING,
                )
            )
        if agent.databricks_sp_id or agent.databricks_sp_secret_arn:
            artifacts.append(Artifact(kind="agent_secret", ref=agent.id, policy=ArtifactPolicy.BEST_EFFORT))
        if agent.databricks_sp_id:
            artifacts.append(
                Artifact(
                    kind="platform_sp",
                    ref=str(agent.databricks_sp_id),
                    policy=ArtifactPolicy.BEST_EFFORT,
                )
            )
        return artifacts

    async def remove_databricks_artifact(self, agent: Agent, artifact: Artifact, tenant=None) -> None:
        """Remove one enumerated artifact. Raises only for the blocking federation audience; the
        per-agent secret and the surviving service principal report themselves and return."""
        if artifact.kind == "federation_audience":
            try:
                await self._remove_audience(agent, tenant)
            except ProvisioningError:
                raise
            except Exception as err:
                raise ProvisioningError(
                    f"agent {agent.id}'s federation audience could not be removed ({type(err).__name__})"
                ) from err
        elif artifact.kind == "agent_secret":
            self._teardown_agent_secret(agent)
        elif artifact.kind == "platform_sp":
            self._report_surviving_sp(agent)
        else:
            raise ProvisioningError(
                f"agent {agent.id} names an unknown Databricks artifact kind {artifact.kind!r}"
            )

    def _teardown_agent_secret(self, agent: Agent) -> None:
        """Delete the per-agent secret by its stored ARN, best-effort and self-reporting.

        Teardown can be the first operation an agent sees, so the ARN is trusted only when it is
        one this service wrote for this agent; a foreign ARN is a discarded claim (logged, both
        pointers dropped), not a failure, because raising would let a planted ARN make the record
        undeletable. A delete that fails is reported by agent id, never by ARN, and never raised.
        """
        arn = (agent.databricks_sp_secret_arn or "").strip()
        if arn and not self._secrets.owns_secret_arn(agent, arn):
            logger.warning(
                "[databricks_identity] agent %s names a Databricks secret this service did "
                "not write for it; ignoring the pointer instead of deleting",
                agent.id,
            )
            self._secrets.drop_untrusted_sp_pointers(agent)
            arn = ""
        if arn:
            try:
                self._secrets.delete_secret_best_effort(arn)
                agent.databricks_sp_secret_arn = None
            except Exception:
                logger.exception(
                    "[databricks_identity] agent %s's Databricks service-principal secret "
                    "survives teardown (its name ends with this agent's id) and must be "
                    "deleted by hand",
                    agent.id,
                )

    def _report_surviving_sp(self, agent: Agent) -> None:
        """Log the service principal the workspace client cannot SCIM-delete. The id is a directory
        identity, not a secret. Re-reads the field, so a pointer ``_teardown_agent_secret`` just
        dropped is not reported as this agent's."""
        if agent.databricks_sp_id:
            logger.info(
                "[databricks_identity] agent %s's Databricks service principal %s survives "
                "teardown (the workspace client has no SCIM delete)",
                agent.id,
                agent.databricks_sp_id,
            )

    async def _remove_audience(self, agent: Agent, tenant) -> None:
        """Withdraw both audience forms, the client-id GUID provisioning appends and the ``api://``
        URI older records left on customer policies, in one client call: two back-to-back
        single-audience removals tripped the account API's rate limit."""
        tenant = self._resolve_tenant(agent, tenant)
        stage = self._stage_with_account(tenant)
        account_id = (getattr(stage, "account_id", "") or "").strip()
        if not account_id:
            raise ProvisioningError(
                f"agent {agent.id}'s tenant has no Databricks account id, so its federation "
                f"audience cannot be removed"
            )
        account_token = await self._account_token(tenant, stage, account_id)
        forms = [a for a in (agent.entra_app_id, agent.entra_app_audience) if a]
        if forms:
            await self._databricks.ensure_federation_audience(
                self._account_host(stage),
                account_id,
                account_token,
                forms,
                present=False,
            )

    async def _assert_app_acl(
        self,
        agent: Agent,
        stage,
        workspace_token: str,
        app_name: str,
        extra_entries: Optional[list[dict]] = None,
    ) -> None:
        """Assert the app's access list so AGP owns it: ``admins`` at ``CAN_MANAGE``, the tenant's
        workspace service principal at ``CAN_MANAGE`` (the credential AGP connects with; without
        it the next ACL write is refused), and ``extra_entries`` (the ``sp_secret`` leg's own
        principal at ``CAN_USE``). Nobody else is on the list: a freshly provisioned agent is
        callable by no user until a grant mirrors an Entra assignment onto it, and no group is ever
        granted, because the apps proxy enforces this list itself and a group-wide ``CAN_USE``
        would leave every member a direct route that revoking one user could not close. The list is
        computed from the tenant and the agent, not from a delta, so a re-provision re-asserts the
        same list; the read is not optional, because the stripped entries are logged and that log
        is the only record of what the takeover removed."""
        desired: list[dict] = [
            dict(_ADMINS_ENTRY),
            {
                "principal": str(stage.sp_client_id).strip(),
                "kind": _KIND_SERVICE_PRINCIPAL,
                "level": _CAN_MANAGE,
            },
        ]
        for entry in extra_entries or []:
            principal = str(entry.get("principal") or "").strip()
            if not principal:
                raise ProvisioningError(
                    f"agent {agent.id}'s app access list cannot be asserted: an entry it must "
                    f"carry names no principal"
                )
            if any(e["principal"] == principal for e in desired):
                continue
            desired.append({**entry, "principal": principal})

        try:
            current = await self._databricks.get_app_permissions(
                stage.workspace_url, workspace_token, app_name
            )
        except DatabricksError as err:
            raise ProvisioningError(
                f"agent {agent.id}'s app access list could not be read, so AGP refused to "
                f"replace it: {_actionable(err, 'read the app access list')}"
            ) from err

        self._log_stripped_acl_entries(agent, app_name, current, desired)

        try:
            await self._databricks.set_app_permissions(
                stage.workspace_url, workspace_token, app_name, desired
            )
        except DatabricksError as err:
            raise ProvisioningError(
                f"agent {agent.id}'s app access list could not be asserted, so AGP will not "
                f"report the agent governed: "
                f"{_actionable(err, 'assert the app access list')}"
            ) from err

    def _log_stripped_acl_entries(
        self, agent: Agent, app_name: str, current: list[dict], desired: list[dict]
    ) -> None:
        """Name every entry the assert is about to remove, then count them. Principal names are
        directory identities, logged deliberately. Inherited entries are counted apart because a
        PUT cannot remove them; they survive the assert and show up as drift."""
        wanted = {(e["principal"], e["kind"], e["level"]) for e in desired}
        unwanted = [e for e in current if (e.get("principal"), e.get("kind"), e.get("level")) not in wanted]
        stripped = [e for e in unwanted if not e.get("inherited")]
        inherited = len(unwanted) - len(stripped)
        if inherited:
            logger.warning(
                "[databricks_identity] asserted ACL on app %s for agent %s: %d inherited "
                "entries survive the assert — AGP cannot remove them",
                app_name,
                agent.id,
                inherited,
            )
        if not stripped:
            return
        for entry in stripped:
            logger.warning(
                "[databricks_identity] agent %s: stripping app %s ACL entry — %s (%s) held %s",
                agent.id,
                app_name,
                entry.get("principal"),
                entry.get("kind"),
                entry.get("level"),
            )
        logger.warning(
            "[databricks_identity] asserted ACL on app %s for agent %s: stripped %d pre-existing entries",
            app_name,
            agent.id,
            len(stripped),
        )

    async def _resolve_stage_and_app(self, agent: Agent, tenant):
        """The workspace that lists this agent's app, as ``(stage, workspace token, app record)``.

        A Databricks app URL carries no workspace identity, so the stage is found by listing each
        workspace in sorted order and matching the app's ``url`` against ``runtime_handle``; the
        app name every write is keyed on comes from that listing. Picking a stage positionally
        would let a two-workspace tenant have another workspace's account policy edited. The
        resolved stage key is recorded on ``agent.bound_stage`` so the invoke resolver prefers it.
        """
        stages = getattr(tenant, "stages", None) or {}
        if not stages:
            raise ProvisioningError(
                f"tenant {getattr(tenant, 'id', '?')!r} has no Databricks workspace stage, "
                f"so agent {agent.id} cannot be bound"
            )

        handle = (agent.runtime_handle or "").strip()
        errors: list[str] = []
        for _name, stage in sorted(stages.items(), key=lambda kv: kv[0]):
            workspace_url = getattr(stage, "workspace_url", "") or ""
            if not workspace_url:
                continue
            try:
                token = await self._workspace_token(stage)
                apps = await self._databricks.list_apps(workspace_url, token)
            except DatabricksError as err:
                errors.append(err.kind)
                continue
            app = _match_app(apps, handle)
            if app is not None:
                agent.bound_stage = _name
                return stage, token, app

        raise ProvisioningError(
            f"no Databricks workspace on this tenant lists agent {agent.id}'s app, so AGP "
            f"cannot resolve the app it must grant access on"
            + (f" (workspace read issues: {sorted(set(errors))})" if errors else "")
        )

    def _stage_with_account(self, tenant):
        """The stage carrying a Databricks account id, for teardown, which cannot use the listing
        (the app may be gone) and edits an account-level entry. Stages that disagree on the
        account id are refused rather than guessed at."""
        stages = getattr(tenant, "stages", None) or {}
        with_account = [
            s
            for _n, s in sorted(stages.items(), key=lambda kv: kv[0])
            if (getattr(s, "account_id", "") or "").strip()
        ]
        if not with_account:
            raise ProvisioningError("the tenant has no Databricks account id")
        account_ids = {s.account_id for s in with_account}
        if len(account_ids) > 1:
            raise ProvisioningError(
                "the tenant's workspaces name more than one Databricks account, so AGP "
                "cannot tell which account's federation policy to edit"
            )
        return with_account[0]

    def _account_host(self, stage) -> str:
        cloud = (getattr(stage, "cloud", "") or "aws").strip().lower()
        return _ACCOUNT_HOSTS.get(cloud, _ACCOUNT_HOSTS["aws"])

    async def _workspace_token(self, stage) -> str:
        """Mint a workspace M2M token from the stage's stored service-principal credential."""
        client_id = (getattr(stage, "sp_client_id", "") or "").strip()
        secret = self._secrets.stage_secret(stage)
        if not client_id:
            raise ProvisioningError(
                "the tenant's Databricks workspace stage has no service-principal client id"
            )
        return await self._databricks.mint_m2m_token(stage.workspace_url, client_id, secret)

    async def _account_token(self, tenant, stage, account_id: str) -> str:
        """Mint an account-scoped token; a federation policy does not accept a workspace one.

        The tenant's account-admin credential (``Tenant.account_admin_secret_arn``, both halves
        inside the secret body) is used when stored, since writing a federation policy is an
        account-admin act; the workspace service principal is used only when none was supplied,
        which turns a missing grant into the actionable ``unauthorized``/``forbidden`` message.
        The account console host is per cloud, read from the stage."""
        admin_arn = (getattr(tenant, "account_admin_secret_arn", "") or "").strip()
        if admin_arn:
            body = self._secrets.read_secret_body(admin_arn)
            client_id = str(body.get(ACCOUNT_ADMIN_ID_KEY) or "").strip()
            secret = str(body.get(ACCOUNT_ADMIN_SECRET_KEY) or "")
            if not client_id or not secret:
                raise ProvisioningError(
                    "the tenant's stored Databricks account-admin credential is incomplete, so "
                    "it cannot mint an account token"
                )
        else:
            client_id = (getattr(stage, "sp_client_id", "") or "").strip()
            secret = self._secrets.stage_secret(stage)
            if not client_id:
                raise ProvisioningError(
                    "the tenant has no Databricks credential able to reach its account, and "
                    "federation mode writes an account-level policy"
                )
        return await self._databricks.mint_account_token(
            account_id, client_id, secret, account_host=self._account_host(stage)
        )

    def _resolve_tenant(self, agent: Agent, tenant):
        if tenant is not None:
            return tenant
        if self._tenants is None or not agent.tenant_id:
            raise ProvisioningError(
                f"agent {agent.id} has no tenant, so its Databricks binding cannot be resolved"
            )
        return self._tenants.get(agent.tenant_id)

    def _persist_failed(self, agent: Agent) -> None:
        agent.identity_status = IdentityStatus.FAILED
        try:
            self._registry.persist_identity(agent)
        except Exception:
            logger.exception(
                "[databricks_identity] failed to persist 'failed' status for agent %s",
                agent.id,
            )
