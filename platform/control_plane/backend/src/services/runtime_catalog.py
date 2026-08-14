"""The portable runtime-catalog seam (E29/T3 — contract C-3).

WHY THIS EXISTS
---------------
Before E29, "what agents exist?" had exactly one answer shape: an operator typed an AgentCore
runtime ARN into the registration wizard. That is not a discovery mechanism, it is a
transcription exercise — and it is only *possible* because an ARN is a string a human can copy.
A Databricks app has no such copyable identity that AGP can use: its handle is a URL the
workspace publishes and the caller must READ, and its inventory is behind two different REST
listings whose union is "the agents on this workspace".

So governance needs a verb, not a text field: **list the agents a tenant actually runs, on
whatever platform that tenant runs them.** One method is enough for that, and one method is
therefore all this seam has:

``list_agents(tenant, stage)``   the agents on one tenant's one stage, as the platform reports them.

**THE COUNT IS THE POINT** (``repo_provider``'s warning, restated for a seam that starts at
one). ``repo_provider`` grew from five methods to eight, and each addition had to be argued as a
portable noun-read first. A second method here — ``get_agent``, ``list_stages``,
``describe_runtime`` — is how a platform-specific verb creeps into a platform-neutral seam. AGP
does not need one: registration copies a :class:`DiscoveredAgent` onto an agent record, and
everything AGP knows afterwards it knows from its own registry.

WHY A ``Protocol`` AND NOT AN ABC
---------------------------------
Same reason ``repo_provider`` gives: satisfied *structurally*, so an adapter neither inherits
from nor registers with this, and the two adapters below share no base class, no constructor
shape, and no dependency. ``DatabricksCatalog`` speaks httpx via
``databricks_workspace_service``; ``AgentCoreCatalog`` speaks boto3 and assumes an IAM role.
A base class would have had to be an ancestor of both, which would mean inventing a common
"client" concept that does not exist.

E29 ships TWO adapters, which is what makes this a seam rather than an assumption. It is also
why the pins below are worth writing down: with two implementations, "the contract" is whatever
both actually do.

HANDLES ARE READ, NEVER CONSTRUCTED (C-3's binding pin)
-------------------------------------------------------
``runtime_handle`` is the value a later invoke POSTs to. Every adapter reads it out of the
platform's own response and NONE of them builds one from a name:

* A Databricks serving endpoint's ``endpoint_url`` is populated ONLY for route-optimized
  endpoints (research §2.2), so a constructed URL would be wrong for most records — and wrong in
  the worst way, since it would look plausible right up to a 404 at invoke time.
* An AgentCore ARN embeds an account id, and this repo forbids hardcoding one anywhere. A
  constructed ARN would either need a caller to supply that id or would silently target the
  control plane's own account instead of the tenant's.
* The Apps response field names are UNVERIFIED until the live test (C-2's own warning), so the
  adapter reads every key it recognises and SKIPS a record whose handle it cannot find. Skipping
  loses one row; inventing a handle poisons the registry.

A record without a readable handle is therefore dropped with a safe log line — but a LISTING
that fails raises. That asymmetry is deliberate and is ``read_tree``'s rule: a partial inventory
presented as complete is worse than a failed step, because an operator reads "3 agents" as "this
workspace has 3 agents", not as "3 agents plus however many AGP could not see".

CREDENTIALS
-----------
``repo_provider`` takes ``token`` as a parameter on every method and says so loudly: credentials
stay with the caller, so an adapter cannot widen its own authority. C-3's signature has no token
parameter, so this seam cannot follow that rule literally — a ``Tenant`` and a stage name is all
a caller passes. The property is preserved differently: an adapter may reach ONLY the credential
the tenant record already points at (a stage's ``sp_client_secret_arn``, a stage's
``deploy_role_arn``), and it resolves nothing it was not pointed at. A catalog handed a tenant
with no credential FAILS; it never falls back to ambient credentials, and it never reads another
tenant's secret. Secrets are never logged and never folded into an exception message.

``already_registered`` IS NOT AN ADAPTER'S BUSINESS
---------------------------------------------------
It is AGP's own knowledge, not the platform's, so both adapters leave it ``False`` and
:func:`mark_already_registered` applies it over a set of handles the CALLER collected from the
registry. A catalog that queried the registry itself would be a seam with two responsibilities
and — the practical cost — would make every adapter test need a registry fake.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from dataclasses import dataclass
from typing import Iterable, Optional, Protocol, runtime_checkable

import anyio.to_thread
import boto3

from models.tenant import (
    SP_SECRET_KEY,
    DatabricksStageConfig,
    Tenant,
    TenantStageConfig,
)
from services.databricks_workspace_service import (
    DatabricksError,
    DatabricksWorkspaceService,
)

logger = logging.getLogger(__name__)

# A ``CatalogError.kind`` reaches an HTTP body, so it is held to the same shape as
# ``databricks_workspace_service``'s ``_SAFE_KIND`` (the ``_safe_probe_detail`` idiom): letters
# and underscores only, bounded length. A Databricks ``PERMISSION_DENIED`` passes through as
# itself; anything that could carry a workspace path, an ARN, or a principal id does not.
_SAFE_KIND = re.compile(r"^[A-Za-z_]{1,64}$")

# The kinds this module raises on its own behalf (platform kinds pass through).
_KIND_NO_CREDENTIAL = "no_credential"
_KIND_CREDENTIAL_UNREADABLE = "credential_unreadable"
_KIND_UNKNOWN_STAGE = "unknown_stage"
_KIND_WRONG_SHAPE = "wrong_stage_shape"
_KIND_ASSUME_ROLE_FAILED = "assume_role_failed"
_KIND_LISTING_FAILED = "listing_failed"
_KIND_PAGINATION_OVERFLOW = "pagination_overflow"

# Serving-endpoint filter values (research §2.2). Agents are ``CUSTOM_MODEL`` served entities;
# the other three entity types (``FOUNDATION_MODEL``, ``EXTERNAL_MODEL``, ``FEATURE_SPEC``) are
# models a workspace serves, not agents a tenant governs. And ``databricks-`` is RESERVED for
# Databricks' own endpoints, which is the cheap documented way to keep the foundation-model
# catalogue — dozens of rows on any real workspace — out of a customer's agent inventory.
_AGENT_ENTITY_TYPE = "CUSTOM_MODEL"
_RESERVED_ENDPOINT_PREFIX = "databricks-"

# Hard page bound, mirroring ``databricks_workspace_service._MAX_PAGES``: the exit condition of
# a paginated listing is upstream-controlled, so a service that never stops handing out tokens
# must not be able to spin a request slot forever.
_MAX_PAGES = 100

# The discovery kinds. Strings rather than an Enum because they cross to the frontend as-is and
# T8 maps them onto create-body fields; C-3 pins these three literals.
KIND_APP = "app"
KIND_SERVING_ENDPOINT = "serving_endpoint"
KIND_AGENTCORE_RUNTIME = "agentcore_runtime"


class CatalogError(Exception):
    """A discovery listing failed. Carries a SAFE message + a ``.kind`` code.

    ONE exception type for the whole seam, deliberately: the route's only job is to turn a
    failed discovery into a 502 with a safe code, and it must not have to know that Databricks
    raises ``DatabricksError`` while boto3 raises ``ClientError``/``BotoCoreError``. Each
    adapter translates its platform's failures into this, so adding a third platform cannot
    make the route grow an ``except`` clause.

    ``.kind`` is validated on construction rather than at the route, because the value can
    ORIGINATE upstream (a Databricks error code is passed through as itself, which is the useful
    behavior — an operator seeing ``PERMISSION_DENIED`` learns something real). A code that
    fails the shape check becomes ``"unknown"``: an upstream string is a safe *code* only if it
    looks like one."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.message = message
        # ``fullmatch``, never ``.match`` (FIX round 1): this module's own rule, and the reason
        # ``models.tenant`` states it too — ``$`` also matches just before a trailing newline, so
        # ``.match`` would accept "PERMISSION_DENIED\n<injected line>" and put it in an HTTP body.
        self.kind = kind if _SAFE_KIND.fullmatch(kind or "") else "unknown"


@dataclass(frozen=True)
class DiscoveredAgent:
    """One agent as a platform reports it — C-3's projection.

    A PROJECTION, following ``repo_provider.RepoView``: only the facts AGP acts on cross, so a
    platform response body cannot reach a client through this. A stdlib frozen dataclass rather
    than a ``BaseModel`` for the same two reasons given there — the seam module holds no
    dependency it does not need, and this is EVIDENCE about a platform at a moment in time, so a
    mutated instance is a record that disagrees with what was actually observed.

    ``runtime_handle`` is READ from the platform's response, never constructed (see the module
    docstring — this is C-3's binding pin, not a stylistic preference).

    ``state`` is the platform's RAW state string and is DISPLAY-ONLY. It is deliberately not
    mapped onto AGP's six-value runtime-status union: that union is a governance claim with a
    closed set of meanings, its only producer is ``agent_identity_service`` (T10 extends it for
    Databricks), and a discovery listing that also asserted runtime status would be a second
    producer of the same fact — with no way to tell which one a UI was showing.

    ``already_registered`` is ``False`` from every adapter; :func:`mark_already_registered`
    is what sets it. See the module docstring for why that is not an adapter's business.
    """

    name: str
    runtime_handle: str  # app URL (databricks) | runtime ARN (agentcore) — READ, never built
    kind: str  # "app" | "serving_endpoint" | "agentcore_runtime"
    state: str  # raw platform state string, display-only
    created_by: str = ""
    already_registered: bool = False


@runtime_checkable
class RuntimeCatalog(Protocol):
    """What AGP needs from an agent-hosting platform. ONE method (see the module docstring for
    why a second is the failure mode, not the next feature).

    Structural: an implementation neither inherits from nor registers with this. The signature
    below is the contract — ``test_runtime_catalog`` compares it, parameter by parameter,
    against both adapters, because a Protocol is not enforced at runtime and a silently drifted
    parameter name is exactly the change that ships a "conforming" adapter no caller can call.
    """

    async def list_agents(self, tenant: Tenant, stage: str) -> list[DiscoveredAgent]:
        """The agents on ``tenant``'s ``stage``, as the platform reports them.

        Takes the TENANT, not a workspace URL or an account id: the target is DERIVED
        server-side from the stored record, so a caller cannot point discovery at a workspace
        or an account the tenant does not own (the ``runtime_build_service`` trust boundary,
        restated for a read).

        **RAISES ``CatalogError`` rather than returning a shortened answer.** An unreachable
        platform, an unreadable credential, an unknown stage, a stage of the wrong platform's
        shape — all raise. An EMPTY list means the platform was reached and reported no agents,
        which is an ordinary answer (a Databricks SP with no app grants sees exactly that,
        research §5.3) and must stay distinguishable from "AGP could not look".

        Individual records with no readable handle are SKIPPED with a safe log line. That is
        the one place a shortened answer is allowed, and only because the alternative is
        inventing a handle — see the module docstring.
        """
        ...


def mark_already_registered(
    agents: Iterable[DiscoveredAgent], known_handles: set[str]
) -> list[DiscoveredAgent]:
    """Flag every discovered agent whose handle AGP already governs.

    A pure function over handles, separate from the adapters (module docstring). ``known_handles``
    is collected by the caller from the registry and holds BOTH currencies — an agent's
    ``runtime_handle`` (Databricks) and its ``agent_arn`` (AgentCore) — because one registry
    holds both platforms and a discovery list must not offer an operator a re-registration
    either way.

    A BLANK handle never matches. Registry rows legitimately carry neither field (a
    pre-registered agent whose runtime does not exist yet), so ``""`` reaches this set easily —
    and if it matched, every discovered record would be flagged as already governed the moment
    one such row existed. That is the most dangerous possible false positive: it hides real,
    ungoverned agents from the operator who came here to find them.
    """
    live = {h for h in known_handles if h}
    return [
        dataclasses.replace(a, already_registered=True) if a.runtime_handle in live else a
        for a in agents
    ]


def _stage_config(tenant: Tenant, stage: str, expected: type):
    """Resolve ``tenant.stages[stage]`` and confirm its shape, or raise a safe CatalogError.

    Platform dispatch is the CALLER's job (the route picks the adapter off ``tenant.platform``),
    but an adapter handed the wrong shape must SAY so rather than ``AttributeError`` its way to
    a 500 — a mis-typed tenant is a governance bug worth a legible error."""
    cfg = tenant.stages.get(stage)
    if cfg is None:
        # The stage NAME is echoed nowhere: stage names are customer-chosen strings and this
        # message is composed here, so only the fixed text crosses.
        raise CatalogError("the tenant has no such stage", kind=_KIND_UNKNOWN_STAGE)
    if not isinstance(cfg, expected):
        raise CatalogError(
            "the tenant's stage config does not match its platform", kind=_KIND_WRONG_SHAPE
        )
    return cfg


class DatabricksCatalog:
    """Discovery on a Databricks workspace: apps (primary) + serving endpoints (secondary).

    **Apps are primary and endpoints are secondary, and the order is not cosmetic.** Databricks
    publishes a migration guide *away* from serving endpoints for agent hosting (research §1.2),
    so Apps is where new agents land; endpoints are the legacy/secondary shape AGP can still
    discover and register. Both lists are always read and merged — apps first — so an operator
    sees one inventory rather than having to know which shape their agents happen to use.

    ``workspace`` and ``secrets_client`` are injectable (tests pass fakes); built by default so
    production construction stays a one-liner — the ``connection_service``/``agent_identity_service``
    idiom.
    """

    def __init__(
        self,
        *,
        workspace: Optional[DatabricksWorkspaceService] = None,
        secrets_client=None,
        region: str = "us-east-1",
    ) -> None:
        self._workspace = workspace or DatabricksWorkspaceService()
        self._sm = secrets_client
        self._region = region

    # -- credential resolution ------------------------------------------- #

    def _secrets(self):
        if self._sm is None:  # pragma: no cover — the live path; tests always inject.
            self._sm = boto3.client("secretsmanager", region_name=self._region)
        return self._sm

    def _read_sp_secret(self, secret_arn: str) -> str:
        """Read the stage SP's client secret out of Secrets Manager (sync; off-loaded below).

        The exception clause is bare because the failure MEANING is what matters, not its type:
        a missing secret, a denied read, a malformed body and a wrong-shaped JSON all mean the
        same thing to a caller ("AGP holds no usable credential for this stage") and all carry
        upstream text that must not cross. Only a fixed message and a safe code do."""
        try:
            resp = self._secrets().get_secret_value(SecretId=secret_arn)
            secret = json.loads(resp["SecretString"]).get(SP_SECRET_KEY, "")
        except Exception:  # noqa: BLE001 — see the docstring.
            # No secret value and no upstream message in the log — traceback only.
            logger.exception("[runtime_catalog] could not read a Databricks stage secret")
            raise CatalogError(
                "the tenant's Databricks credential could not be read",
                kind=_KIND_CREDENTIAL_UNREADABLE,
            ) from None
        if not secret:
            raise CatalogError(
                "the tenant's Databricks credential is empty", kind=_KIND_NO_CREDENTIAL
            )
        return secret

    # -- the seam ------------------------------------------------------- #

    async def list_agents(self, tenant: Tenant, stage: str) -> list[DiscoveredAgent]:
        cfg = _stage_config(tenant, stage, DatabricksStageConfig)
        if not cfg.sp_client_secret_arn:
            # NOT an empty list. An unconfigured tenant answering "no agents" would tell an
            # operator this workspace hosts nothing — a governance lie about a tenant AGP has
            # simply never been given a credential for.
            raise CatalogError(
                "the tenant's Databricks stage has no stored credential",
                kind=_KIND_NO_CREDENTIAL,
            )
        secret = await anyio.to_thread.run_sync(
            self._read_sp_secret, cfg.sp_client_secret_arn
        )

        try:
            token = await self._workspace.mint_m2m_token(
                cfg.workspace_url, cfg.sp_client_id, secret
            )
            apps = await self._workspace.list_apps(cfg.workspace_url, token)
            endpoints = await self._workspace.list_serving_endpoints(
                cfg.workspace_url, token
            )
        except DatabricksError as err:
            # The platform's own safe code passes through — an operator seeing
            # PERMISSION_DENIED learns something true and actionable. Its MESSAGE does not.
            raise CatalogError("Databricks discovery failed", kind=err.kind) from None

        return [*self._from_apps(apps), *self._from_endpoints(endpoints)]

    # -- projections ---------------------------------------------------- #

    @staticmethod
    def _app_handle(record: dict) -> str:
        """The app's URL, READ from whichever key the response carries.

        ``url`` vs ``app_url`` is the exact ambiguity research §2.1 flagged as UNVERIFIED (the
        REST reference page would not load; T12 pins the real schema against the trial account).
        Reading BOTH is not the same as guessing one: if neither is present the record is
        skipped, because the alternative — deriving a URL from the app's name — produces a
        plausible-looking handle that is wrong."""
        for key in ("url", "app_url"):
            value = record.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    def _from_apps(self, records: list[dict]) -> list[DiscoveredAgent]:
        found: list[DiscoveredAgent] = []
        skipped = 0
        for record in records:
            handle = self._app_handle(record)
            if not handle:
                skipped += 1
                continue
            # ``app_status`` pinned live 2026-08-11 (B2.2): the real payload carries
            # ``app_status``/``compute_status``, no bare ``status`` — the same two-key
            # tolerance ``databricks_identity_service._APP_STATUS_KEYS`` already applies.
            status = record.get("status") or record.get("app_status") or {}
            found.append(DiscoveredAgent(
                name=str(record.get("name") or ""),
                runtime_handle=handle,
                kind=KIND_APP,
                state=str(status.get("state") or "") if isinstance(status, dict) else "",
                created_by=str(record.get("creator") or ""),
            ))
        if skipped:
            # A COUNT and a safe label only — an app record carries customer paths.
            logger.info("[runtime_catalog] skipped %d app(s) with no readable URL", skipped)
        return found

    @staticmethod
    def _is_agent_endpoint(record: dict) -> bool:
        """Is this serving endpoint an agent AGP should offer for registration?

        Two filters, both from research §2.2, and both necessary: ``CUSTOM_MODEL`` is what an
        agent's served entity is (the other three entity types are models a workspace serves),
        and the ``databricks-`` prefix is RESERVED for Databricks' own endpoints — without that
        exclusion the foundation-model catalogue (dozens of rows on any real workspace) would
        dominate a customer's agent inventory."""
        name = str(record.get("name") or "")
        if name.startswith(_RESERVED_ENDPOINT_PREFIX):
            return False
        config = record.get("config")
        entities = config.get("served_entities") if isinstance(config, dict) else None
        if not isinstance(entities, list):
            return False
        return any(
            isinstance(e, dict) and e.get("entity_type") == _AGENT_ENTITY_TYPE
            for e in entities
        )

    @staticmethod
    def _endpoint_handle(record: dict) -> str:
        """The endpoint's invocation URL, READ — top-level first, then the data-plane one.

        ``endpoint_url`` is populated ONLY for route-optimized endpoints (research §2.2), so a
        legitimate record can carry neither key and is then skipped. This is precisely the case
        where constructing the documented
        ``{workspace}/serving-endpoints/{name}/invocations`` shape would be tempting and wrong:
        a route-optimized endpoint's real URL lives on a different host entirely
        (``<id>.serving.cloud.databricks.com``), so the guess would be a handle that 404s at
        invoke time after looking correct in the UI."""
        top = record.get("endpoint_url")
        if isinstance(top, str) and top:
            return top
        info = record.get("data_plane_info")
        query = info.get("query_info") if isinstance(info, dict) else None
        nested = query.get("endpoint_url") if isinstance(query, dict) else None
        return nested if isinstance(nested, str) and nested else ""

    def _from_endpoints(self, records: list[dict]) -> list[DiscoveredAgent]:
        found: list[DiscoveredAgent] = []
        skipped = 0
        for record in records:
            if not self._is_agent_endpoint(record):
                continue
            handle = self._endpoint_handle(record)
            if not handle:
                skipped += 1
                continue
            state = record.get("state") or {}
            found.append(DiscoveredAgent(
                name=str(record.get("name") or ""),
                runtime_handle=handle,
                kind=KIND_SERVING_ENDPOINT,
                # ``state.ready`` is the raw platform string ("READY"/"NOT_READY"), passed
                # through for display — see DiscoveredAgent.state on why it is not mapped.
                state=str(state.get("ready") or "") if isinstance(state, dict) else "",
                created_by=str(record.get("creator") or ""),
            ))
        if skipped:
            logger.info(
                "[runtime_catalog] skipped %d serving endpoint(s) with no readable URL", skipped
            )
        return found


class AgentCoreCatalog:
    """Discovery on AWS: ``bedrock-agentcore-control.list_agent_runtimes`` in the TENANT's account.

    **In the tenant's account, not the control plane's.** A cross-account tenant's runtimes are
    invisible to the platform's own credentials, so this assumes the stage's ``deploy_role_arn``
    exactly as the runtime build does — that role is the tenant's stored, server-derived door
    into its own account, and an EMPTY one means "read in place", byte-for-byte the buildspec's
    rule (``TARGET_ROLE_ARN`` empty ⇒ skip the assume). An empty role is never assumed: a blank
    ``RoleArn`` is an error, not an identity.

    ``sts_client`` and ``control_factory`` are injectable so tests can pin WHICH credentials the
    control client was built with — the assume-role idiom's only observable effect.
    """

    def __init__(
        self,
        *,
        region: str = "us-east-1",
        sts_client=None,
        control_factory=None,
    ) -> None:
        self._region = region
        self._sts = sts_client
        self._control_factory = control_factory or _default_control_factory

    def _sts_client(self):
        if self._sts is None:  # pragma: no cover — the live path; tests always inject.
            self._sts = boto3.client("sts", region_name=self._region)
        return self._sts

    def _assume(self, role_arn: str) -> dict:
        """Assume ``role_arn`` and return boto3-shaped keyword credentials.

        Bare ``except`` for the same reason as the secret read: a denied assume, an expired
        platform credential and an unreachable STS endpoint all mean "AGP cannot reach this
        tenant's account", and all carry upstream text (role ARNs, principal ids) that must not
        cross into an HTTP body."""
        try:
            resp = self._sts_client().assume_role(
                RoleArn=role_arn, RoleSessionName="agp-runtime-catalog"
            )
            creds = resp["Credentials"]
            return {
                "aws_access_key_id": creds["AccessKeyId"],
                "aws_secret_access_key": creds["SecretAccessKey"],
                "aws_session_token": creds["SessionToken"],
            }
        except Exception:  # noqa: BLE001 — see the docstring.
            logger.exception("[runtime_catalog] could not assume a tenant deploy role")
            raise CatalogError(
                "AGP could not reach the tenant's AWS account", kind=_KIND_ASSUME_ROLE_FAILED
            ) from None

    def _list_runtimes(self, cfg: TenantStageConfig) -> list[dict]:
        """Page ``list_agent_runtimes`` in the stage's account/region (sync; off-loaded below).

        Two loop bounds for the reason ``databricks_workspace_service._list_paginated`` documents:
        the exit condition is upstream-controlled, so a repeated token is caught immediately and
        ``_MAX_PAGES`` catches a slower cycle. Both RAISE — a truncated inventory presented as
        complete is a governance lie, and it is the one a discovery surface would tell most
        convincingly."""
        credentials = self._assume(cfg.deploy_role_arn) if cfg.deploy_role_arn else None
        control = self._control_factory(
            region=cfg.region or self._region, credentials=credentials
        )

        records: list[dict] = []
        token: Optional[str] = None
        seen: set[str] = set()
        pages = 0
        while True:
            pages += 1
            if pages > _MAX_PAGES:
                raise CatalogError(
                    "the AWS account returned too many pages of runtimes",
                    kind=_KIND_PAGINATION_OVERFLOW,
                )
            kwargs = {"nextToken": token} if token else {}
            try:
                resp = control.list_agent_runtimes(**kwargs)
            except Exception:  # noqa: BLE001 — botocore's ClientError/BotoCoreError families
                # plus anything a stubbed client raises; all become one safe code.
                logger.exception("[runtime_catalog] list_agent_runtimes failed")
                raise CatalogError(
                    "the AWS agent runtime listing failed", kind=_KIND_LISTING_FAILED
                ) from None
            page = resp.get("agentRuntimes")
            if isinstance(page, list):
                records.extend(r for r in page if isinstance(r, dict))
            token = resp.get("nextToken") or None
            if not token:
                return records
            token = str(token)
            if token in seen:
                raise CatalogError(
                    "the AWS account repeated a pagination token",
                    kind=_KIND_PAGINATION_OVERFLOW,
                )
            seen.add(token)

    async def list_agents(self, tenant: Tenant, stage: str) -> list[DiscoveredAgent]:
        cfg = _stage_config(tenant, stage, TenantStageConfig)
        # boto3 is blocking; off-load it so the uvicorn loop is never held — the
        # ``agent_credential_service`` idiom (``anyio.to_thread.run_sync``, never asyncio.run).
        records = await anyio.to_thread.run_sync(self._list_runtimes, cfg)

        found: list[DiscoveredAgent] = []
        skipped = 0
        for record in records:
            arn = record.get("agentRuntimeArn")
            if not isinstance(arn, str) or not arn:
                # No handle, no row. An ARN is never rebuilt from the name + account: this repo
                # forbids a hardcoded account id anywhere, and the id is the caller's to not know.
                skipped += 1
                continue
            found.append(DiscoveredAgent(
                name=str(record.get("agentRuntimeName") or ""),
                runtime_handle=arn,
                kind=KIND_AGENTCORE_RUNTIME,
                # The native status string, display-only. AGP's six-value runtime-status union
                # has exactly one producer (``agent_identity_service``) and this is not it.
                state=str(record.get("status") or ""),
                # AgentCore records carry no creator field — honestly empty rather than guessed.
                created_by="",
            ))
        if skipped:
            logger.info("[runtime_catalog] skipped %d runtime(s) with no ARN", skipped)
        return found


def _default_control_factory(*, region: str, credentials: Optional[dict]):
    """Build a ``bedrock-agentcore-control`` client, optionally with assumed credentials.

    A module-level function rather than an inline lambda so the production path is named and
    the injected test double has an obvious counterpart."""
    # pragma: no cover — the live path; tests inject a factory.
    return boto3.client("bedrock-agentcore-control", region_name=region, **(credentials or {}))
