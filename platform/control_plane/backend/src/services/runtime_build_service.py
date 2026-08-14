"""Runtime-build orchestration (E22/T6, hardened in E22/T6b): resolve a Git connection,
mint a ready clone token into a short-lived scratch secret, then StartBuild the
runtime-provision CodeBuild project.

This replaces the old EventBridge → CodeBuild trigger (deleted in T7). A GitHub Action,
authenticated via GitHub OIDC at the route, calls `start_runtime_build`. We resolve the
org's connection (org / base_url — NON-secret metadata) and issue `codebuild.start_build`
with the same env-override contract the old `agentcore_runtime_trigger` input_transformer
produced, swapping the ARCHIVE_* pair for git-clone vars pointing at the org's private
infra repo.

WHY THE SCRATCH SECRET (T6b): the buildspec's `agentcore_runtime` branch clones with
`TOKEN=$(jq -r '.token')`. A PAT connection's secret is `{"token": <pat>}` → works; a
GitHub App connection's secret is `{"private_key": <pem>}` (NO `.token`) → TOKEN empty →
`git clone` fails silently. So instead of passing the connection's own secret_arn (which
would only work for PAT), we resolve a READY bearer token via `get_bearer_token` (uniform
for BOTH types — PAT returns the stored token; App freshly mints a ~1h installation token),
write `{"token": <token>}` to a short-lived per-build scratch secret, and pass THAT scratch
ARN as GIT_SECRET_ARN. The buildspec is unchanged and `jq -r '.token'` works for both.

SECURITY: the resolved token is written ONLY to Secrets Manager and is NEVER logged and
NEVER placed in the StartBuild env payload — only the scratch ARN goes into env. CodeBuild
reads the scratch secret itself at build time under its own role (its role already carries
`secretsmanager:*`). The scratch secret is short-lived (one per build, keyed by the unique
`<agent_id>-<short_sha>` image tag).

CLEANUP (E36/T9) — three legs, because no single one covers every case:
  1. The buildspec's `post_build` force-deletes `$GIT_SECRET_ARN` (WARN-not-fail). That covers
     every build that STARTED, on success and on failure alike.
  2. `_delete_secret_best_effort` here covers the one case (1) cannot: the secret is
     written BEFORE StartBuild, so a StartBuild that faults leaves a token behind with no build
     to purge it. We do NOT delete on the happy path — CodeBuild still has to read it.
  3. `scripts/sweep_runtime_build_tokens.py` is the operator backstop for the already-accumulated
     backlog and for anything (1) or (2) missed (e.g. a build killed before post_build).

Determinism / testability: the boto3 codebuild + secretsmanager clients and the connection
service are injected — tests pass fakes that record `start_build` kwargs / secret writes and
a fake connection service returning a Connection + a ready bearer token.
"""

from __future__ import annotations

import json
import logging

from services.tenant_service import TenantError

logger = logging.getLogger(__name__)

# The forced per-org private infra repo the runtime build clones (E22 rollout convention;
# mirrors the RolloutService FORCED infra repo name).
_INFRA_REPO_NAME = "agp-runtime-infra"


class RuntimeBuildError(Exception):
    """A StartBuild (or scratch-secret write) failed, or the agent/tenant could not be
    resolved. Carries a SAFE message (never a secret) and an optional ``.kind`` hint
    (e.g. ``"not_found"``). The route maps any RuntimeBuildError to a 502."""

    def __init__(self, message: str, *, kind: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


class RuntimeBuildService:
    def __init__(
        self,
        codebuild_client,
        connection_service,
        secretsmanager_client,
        *,
        tenant_service,
        agent_registry,
        codebuild_project_name: str,
        scratch_secret_prefix: str,
    ) -> None:
        self._codebuild = codebuild_client
        self._connections = connection_service
        self._sm = secretsmanager_client
        self._tenants = tenant_service
        self._registry = agent_registry
        self._project_name = codebuild_project_name
        self._scratch_prefix = scratch_secret_prefix

    def start_runtime_build(
        self,
        *,
        agent_id: str,
        image_tag: str,
        ecr_repo: str,
        connection_id: str,
        stage: str,
        image_digest: str = "",
    ) -> str:
        """Resolve the connection, mint a ready clone token into a scratch secret, then
        StartBuild the runtime-provision project.

        ``image_digest`` (E28B/T4, D-B3) is the ``sha256:…`` reference of the exact bytes to
        deploy. When present the buildspec deploys ``<ecr_repo>@<digest>`` instead of
        ``<ecr_repo>:<tag>``, which is what makes "the image a human approved" and "the image prod
        runs" the same thing by construction rather than by inference — a tag is a mutable pointer
        and the image build is not reproducible, so the bytes behind one tag can change between
        approval and deploy.

        It is threaded from the CALLER rather than looked up here, and that is the whole point:
        promote passes the digest recorded on the approved candidate, so the approved value is
        never re-derived from the registry at deploy time (a fresh lookup would return whatever the
        tag points at NOW). Defaults to ``""`` for the legacy/tag-only path — a rollback, whose
        target is validated from tag-keyed ``Deployment`` rows that carry no digest, and any
        pre-E28B candidate. ``image_tag`` stays REQUIRED and unchanged either way: the buildspec
        derives ``AGENT_ID``, the deployment id, the scratch-secret name and both write-back
        helpers from it.

        Returns the CodeBuild build id. Raises `RuntimeBuildError` if the scratch-secret
        write or StartBuild fails (the route maps that to a 502). Connection-resolution
        failures propagate as the connection service's own `ConnectionError` (the route maps
        not_found → 404).
        """
        conn = self._connections.get_connection(connection_id)

        # TRUST BOUNDARY (E25/T5): the deploy tenant is DERIVED server-side from the agent —
        # NEVER taken from the request body. Resolve the agent, then its owning tenant, then
        # the stage's cross-account config. A caller cannot point a build at another tenant's
        # account by lying in the payload; the only body-supplied field is `stage`.
        agent = self._registry.get(agent_id)
        if agent is None:
            raise RuntimeBuildError("unknown agent", kind="not_found")
        # Resolve the owning tenant + the stage's cross-account config as a build failure, not a
        # 500. NOTHING guarantees the tenant carries the requested stage: E28/D8 opened the stage
        # set up, so TenantService now requires only ONE stage (`uat`-only and per-region tenants
        # are legitimate) — an earlier version of this comment claimed a dev+prod write guarantee
        # that no longer exists. A missing stage, a None/unknown tenant_id (an un-stamped/legacy
        # agent; the field is Optional), or a stage-less seed all fault here, so the guard below is
        # LOAD-BEARING — do not remove it. TenantError (unknown tenant) and KeyError (stage the
        # tenant does not carry) both map to a SAFE not_found RuntimeBuildError.
        try:
            tenant = self._tenants.get(agent.tenant_id)
            cfg = tenant.stages[stage]
        except (TenantError, KeyError) as exc:
            raise RuntimeBuildError("unknown tenant or stage", kind="not_found") from exc

        # PLATFORM GATE (E29/T7, ledger OB-3). Everything below reads AWS-only stage fields —
        # `cfg.ecr_repo_uri`, `cfg.deploy_role_arn`, `cfg.account_id`, `cfg.region` — and a
        # Databricks stage (`DatabricksStageConfig`) carries none of them. This was unreachable
        # dead-ish code until E29/T3 made `platform="databricks"` a real stored value; now a
        # Databricks tenant reaching this method would raise `AttributeError` from a pydantic
        # model and surface as a 500. Refused explicitly instead: there is no AgentCore runtime
        # to build for a Databricks agent, so this is a genuine unsupported operation and not a
        # transient fault. Read with `getattr` + an AWS default so a pre-E29 tenant record (and
        # every existing test fake, which is a bare namespace with no `platform`) is unaffected.
        # Compared to the bare wire value, NOT via `str()`: `TenantPlatform` is a `(str, Enum)`,
        # so it compares equal to "aws" but `str()` of it is "TenantPlatform.AWS" — which would
        # have refused every AWS tenant that stored a typed enum rather than a raw string.
        if (getattr(tenant, "platform", "aws") or "aws") != "aws":
            raise RuntimeBuildError(
                "runtime builds are only supported for AWS tenants", kind="unsupported"
            )

        # Resolve a READY bearer token (PAT: stored token; App: freshly minted installation
        # token) and stage it in a short-lived scratch secret so the buildspec's uniform
        # `jq -r '.token'` clone works for BOTH connection types. The token is NEVER logged
        # and NEVER placed in the StartBuild env payload — only the scratch ARN is.
        scratch_arn = self._write_scratch_token(image_tag, connection_id)

        env = [
            {"name": "IAC_TYPE", "value": "agentcore_runtime", "type": "PLAINTEXT"},
            {"name": "DEPLOYMENT_ID", "value": f"agentcore-runtime-{image_tag}", "type": "PLAINTEXT"},
            {"name": "IMAGE_TAG", "value": image_tag, "type": "PLAINTEXT"},
            # E28B/T4 (D-B3): the digest to deploy, EMPTY on the legacy tag-only path. The
            # buildspec branches on emptiness — a digest present means deploy `<repo>@<digest>`
            # and verify it exists in the target registry first; empty means the pre-E28B
            # `<repo>:<tag>`. Always sent (rather than conditionally appended) so the variable is
            # unambiguously defined in the build environment: an ABSENT override and an empty one
            # are indistinguishable to the shell, so sending it always makes the empty case an
            # explicit state rather than a missing one.
            {"name": "IMAGE_DIGEST", "value": image_digest or "", "type": "PLAINTEXT"},
            # Tenant ECR is authoritative; the request's ecr_repo is a fallback used only when
            # the tenant stage has no ecr_repo_uri yet (still provisioning).
            {"name": "ECR_REPO", "value": cfg.ecr_repo_uri or ecr_repo, "type": "PLAINTEXT"},
            # Git-clone contract (replaces the old ARCHIVE_BUCKET/ARCHIVE_KEY pair).
            {"name": "GIT_INFRA_ORG", "value": conn.org, "type": "PLAINTEXT"},
            {"name": "GIT_INFRA_REPO", "value": _INFRA_REPO_NAME, "type": "PLAINTEXT"},
            {"name": "GIT_BASE_URL", "value": conn.base_url or "", "type": "PLAINTEXT"},
            # The SCRATCH Secrets Manager ARN — CodeBuild reads `{"token": ...}` itself. The
            # token is NEVER read into env here; only this ARN goes in the payload.
            {"name": "GIT_SECRET_ARN", "value": scratch_arn, "type": "PLAINTEXT"},
            # Cross-account deploy overrides (E25/T5), all derived from the tenant's stage
            # config. TARGET_ROLE_ARN empty ⇒ the buildspec skips the cross-account assume and
            # deploys in-place (by design).
            #
            # No exec-role env is sent here, by design (E25B/T6). The runtime Terraform module
            # now self-provisions its own AgentCore exec role in the target account: an empty
            # exec_role_arn tfvar makes it create aws_iam_role.exec in-account, scoped to the
            # tenant's in-account ECR (trusts bedrock-agentcore.amazonaws.com). There is no
            # longer any platform-baked EXEC_ROLE_ARN to override or fall back to. deploy_role_arn
            # is a DIFFERENT role kind (trusts the CodeBuild role, for the cross-account assume) —
            # feeding it as the exec role would be wrong — so we never pass it as one. A distinct
            # per-tenant exec role would be a future additive TenantStageConfig field.
            {"name": "TARGET_ROLE_ARN", "value": cfg.deploy_role_arn, "type": "PLAINTEXT"},
            {"name": "TARGET_ACCOUNT_ID", "value": cfg.account_id, "type": "PLAINTEXT"},
            {"name": "AWS_TARGET_REGION", "value": cfg.region, "type": "PLAINTEXT"},
            {"name": "TENANT_ID", "value": agent.tenant_id, "type": "PLAINTEXT"},
            {"name": "STAGE", "value": stage, "type": "PLAINTEXT"},
            # THE REGISTRY ID CODEBUILD READS AND WRITES, SUPPLIED BY US — not by Terraform.
            #
            # The buildspec's `agentcore_runtime` branch calls `agent-registry-control
            # get-registry-record` / `update-registry-record`, and both need an ID: AWS mints
            # registry ids and `RegistryIdentifier` accepts only an ARN or a generated id,
            # never a name. Terraform used to bake this into the CodeBuild project as a
            # project-level env var, which is exactly what forced a from-zero deploy to
            # `terraform apply` TWICE — there is no Terraform resource for the
            # `agent-registry` namespace, so the id had to round-trip through a capture file
            # read during the PLAN walk, before the provisioner that writes it had run.
            #
            # Sending it per build removes that dependency completely, and it costs nothing:
            # `self._registry` is the same AgentRegistryService this method ALREADY used two
            # lines up to resolve the agent, so the id is resolved and memoised by the time we
            # get here. This is also the only path that can supply it — CodeBuild has no
            # backend to ask, and the build's ONLY trigger is this StartBuild (the EventBridge
            # trigger was deleted in E22/T7), so a per-build override reaches every build.
            #
            # An override is what makes the value authoritative: it takes precedence over the
            # project-level env var, so the build reads the registry the CONTROL PLANE is
            # using, not whatever a stale project definition holds — an old id here means the
            # write-back silently lands on a record in the wrong registry (or 404s), and the
            # buildspec treats a failed write-back as an untracked live runtime.
            {
                "name": "AGENT_REGISTRY_ID",
                "value": self._registry.registry_id,
                "type": "PLAINTEXT",
            },
        ]

        # Cross-account image copy on prod promote (E25C/T6b). T6 promotes the SAME image tag
        # with no rebuild, but the image was pushed only to the DEV-stage ECR. For a cross-account
        # tenant the prod ECR is a different repo in a different account, so nothing has the tag →
        # prod ImagePull failure. When promoting to prod AND dev/prod live in DIFFERENT accounts
        # (account_id is the authoritative signal — ecr_repo_uri may be empty early), inject the
        # SOURCE_* overrides the buildspec uses to copy dev_ecr:tag → prod_ecr:tag before applying.
        #
        # SOURCE_ECR_REQUIRED is the signal-vs-action bridge: it fires on EVERY cross-account prod
        # promote, INDEPENDENT of whether dev.ecr_repo_uri is populated yet. If the copy is required
        # but SOURCE_ECR_REPO is empty (dev never built), the buildspec fails LOUD at build time
        # instead of provisioning prod against a nonexistent image (a silent ImagePull failure).
        # Single-account (dev.account_id == prod.account_id) / any dev build inject NEITHER
        # SOURCE_ECR_REQUIRED nor SOURCE_* → the buildspec skips the block entirely (unchanged). A
        # prod-only tenant (no "dev" stage) is treated as no-copy rather than faulting the request.
        dev_cfg = tenant.stages.get("dev") if stage == "prod" else None
        if dev_cfg is not None and dev_cfg.account_id != cfg.account_id:
            env += [
                {"name": "SOURCE_ECR_REQUIRED", "value": "true", "type": "PLAINTEXT"},
                {"name": "SOURCE_ECR_REPO", "value": dev_cfg.ecr_repo_uri, "type": "PLAINTEXT"},
                {"name": "SOURCE_DEPLOY_ROLE_ARN", "value": dev_cfg.deploy_role_arn, "type": "PLAINTEXT"},
                {"name": "SOURCE_REGION", "value": dev_cfg.region, "type": "PLAINTEXT"},
            ]

        try:
            resp = self._codebuild.start_build(
                projectName=self._project_name,
                environmentVariablesOverride=env,
            )
        except Exception as exc:
            logger.exception(
                "[runtime_build] StartBuild failed for agent_id=%s image_tag=%s", agent_id, image_tag
            )
            # E36/T9: the scratch secret was written BEFORE this call, so a StartBuild that
            # fails leaves a LIVE clone token nothing will ever reclaim — no build exists to
            # run the buildspec's post_build purge. This is the ONLY failure path that can
            # clean it up, and it must not change the error the caller sees.
            self._delete_secret_best_effort(scratch_arn)
            raise RuntimeBuildError("Failed to start runtime build") from exc

        return resp["build"]["id"]

    def _delete_secret_best_effort(self, secret_id: str) -> None:
        """Force-delete a scratch secret; every failure is swallowed and logged.

        Mirrors `connection_service._delete_secret_best_effort`. `ForceDeleteWithoutRecovery`
        is required, not an optimisation: the default 30-day recovery window would leave the
        clone token readable (and billing) for a month. Called ONLY when StartBuild failed —
        never on the happy path, where CodeBuild still has to read the secret under its own
        role and the buildspec's post_build purge owns the delete."""
        try:
            self._sm.delete_secret(SecretId=secret_id, ForceDeleteWithoutRecovery=True)
        except self._sm.exceptions.ResourceNotFoundException:
            pass
        except Exception:
            # Best-effort by design: this runs inside an `except` that is about to raise the
            # real (SAFE) error, so a cleanup fault must never replace or mask it. The operator
            # sweep (`scripts/sweep_runtime_build_tokens.py`) is the backstop.
            logger.exception("[runtime_build] scratch-secret delete failed for %s", secret_id)

    def _write_scratch_token(self, image_tag: str, connection_id: str) -> str:
        """Resolve a ready bearer token and write `{"token": <token>}` to a per-build scratch
        secret; return its ARN. The token is never logged and never surfaced in an error.

        Uniform for PAT + App via `get_bearer_token`. On a pre-existing scratch name (retry
        of the same image tag) overwrite via `put_secret_value`. Any SM/token failure raises
        `RuntimeBuildError` with a SAFE message (the token/exception value is never logged —
        traceback only) BEFORE StartBuild is reached, so the token never leaks."""
        name = f"{self._scratch_prefix}{image_tag}"
        try:
            token = self._connections.get_bearer_token(connection_id)
            secret_string = json.dumps({"token": token})
            try:
                resp = self._sm.create_secret(
                    Name=name,
                    SecretString=secret_string,
                    Tags=[
                        {"Key": "managed_by", "Value": "agp"},
                        {"Key": "purpose", "Value": "runtime-build-token"},
                    ],
                )
            except self._sm.exceptions.ResourceExistsException:
                resp = self._sm.put_secret_value(SecretId=name, SecretString=secret_string)
            return resp["ARN"]
        except RuntimeBuildError:
            raise
        except Exception as exc:
            logger.exception(
                "[runtime_build] scratch-token write failed for connection_id=%s image_tag=%s",
                connection_id, image_tag,
            )
            raise RuntimeBuildError("Failed to stage runtime build credential") from exc
