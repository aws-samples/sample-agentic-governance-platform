"""Secrets Manager reads and writes for the identity binding of a Databricks-hosted agent.

``DatabricksAgentSecrets`` reads a tenant's stored workspace and account-admin credentials by the ARN the
tenant record holds, and creates, inspects and deletes the per-agent service-principal secret named
``{secret_prefix}{agent.id}``. ``owns_secret_arn`` is the one ownership rule for a stored ARN: the
identity service applies it to every client-settable pointer, and the invoke path reuses it.
"""

from __future__ import annotations

import json
import logging
import re

from botocore.exceptions import BotoCoreError, ClientError

from core import aws_clients
from models.agent import Agent
from models.tenant import SP_SECRET_KEY
from services.agent_identity_service import ProvisioningError

logger = logging.getLogger(__name__)

_SECRETS_FAULTS = (ClientError, BotoCoreError)

# The keys of a per-agent service-principal secret body. The SCIM id rides in the body because
# minting a secret needs it, the record carries only the application id, and a client-settable
# model field is the wrong home for a value only this module reads.
_AGENT_CREDENTIAL_FIELD = "client_secret"
_AGENT_SCIM_ID_KEY = "scim_id"
_AGENT_OWNER_KEY = "agent_id"

# Secrets Manager appends "-" and six random alphanumerics to a secret's name in its ARN; matched
# at end-of-string as an exact six, so an agent id that contains "-" is never shortened.
_SM_RANDOM_SUFFIX_RE = re.compile(r"-[A-Za-z0-9]{6}$")

# A Secrets Manager secret ARN with the name captured; ``\Z`` so a trailing newline cannot match.
# No account id is pinned: accounts come from ambient credentials.
_SM_SECRET_ARN_RE = re.compile(r"^arn:[a-z0-9-]+:secretsmanager:[a-z0-9-]*:\d*:secret:(?P<name>[^\s]+)\Z")


class DatabricksAgentSecrets:
    def __init__(self, *, secret_prefix: str, region: str | None = None, sm=None) -> None:
        self._sm = sm or aws_clients.client("secretsmanager", region=region)
        self._secret_prefix = secret_prefix

    def stage_secret(self, stage) -> str:
        arn = (getattr(stage, "sp_client_secret_arn", "") or "").strip()
        if not arn:
            raise ProvisioningError(
                "the tenant's Databricks workspace stage has no stored service-principal "
                "secret, so AGP cannot authenticate to the workspace"
            )
        return self.read_secret(arn, SP_SECRET_KEY, _AGENT_CREDENTIAL_FIELD)

    def read_secret_body(self, secret_id: str) -> dict:
        """A stored secret's JSON body, read by the ARN the record holds, never by a name rebuilt
        from a prefix. A non-JSON or non-dict body reads as ``{}``, so a caller that needs a keyed
        value fails with "no usable credential" rather than an opaque parse error."""
        raw = self._read_secret_string(secret_id)
        try:
            body = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return body if isinstance(body, dict) else {}

    def _read_secret_string(self, secret_id: str) -> str:
        """The raw secret string. Any client failure, the client's own ``ResourceNotFound``
        included, logs a traceback with no interpolated value and raises a fixed
        ``ProvisioningError``."""
        try:
            resp = self._sm.get_secret_value(SecretId=secret_id)
        except Exception:
            logger.exception("[databricks_identity] could not read a stored Databricks secret")
            raise ProvisioningError("the tenant's stored Databricks secret could not be read") from None
        return resp.get("SecretString") or ""

    def read_secret(self, secret_id: str, *keys: str) -> str:
        """One credential value from a stored secret, by the first of ``keys`` that carries one.
        The body may be a JSON envelope or a bare string (a secret rotated by hand in the console),
        and the value is returned into a local and never logged."""
        raw = self._read_secret_string(secret_id)
        try:
            body = json.loads(raw)
        except (ValueError, TypeError):
            body = None
        if isinstance(body, dict):
            for key in keys:
                value = body.get(key)
                if isinstance(value, str) and value:
                    return value
            raise ProvisioningError("the tenant's stored Databricks secret carries no usable credential")
        if isinstance(raw, str) and raw:
            return raw
        raise ProvisioningError("the tenant's stored Databricks secret is empty")

    def owns_secret_arn(self, agent: Agent, arn: str) -> bool:
        """Whether ``arn`` is a Secrets Manager secret ARN whose name, less the random suffix,
        ends with ``/{agent.id}`` and has no ``.`` or ``..`` segment.

        Prefix-independent, so a change of ``DATABRICKS_TENANT_SECRET_PREFIX`` cannot make an
        agent stop recognising its own secret and leak a live principal. Anchored at both ends,
        so one id cannot adopt another's principal. No API call, so a foreign ARN is rejected
        before anything is contacted and the predicate is not an existence oracle; the
        ``agent_id`` tag and body key stay for the paths that already read the body."""
        agent_id = (agent.id or "").strip()
        if not agent_id:
            return False
        match = _SM_SECRET_ARN_RE.match(arn.strip())
        if not match:
            return False
        name = _SM_RANDOM_SUFFIX_RE.sub("", match.group("name"))
        segments = name.split("/")
        if "." in segments or ".." in segments:
            return False
        return name.endswith(f"/{agent_id}")

    def drop_untrusted_sp_pointers(self, agent: Agent) -> None:
        """Discard ``databricks_sp_id`` and ``databricks_sp_secret_arn`` together unless the ARN
        is ours: the id without a matching secret is a claim about which principal to grant and
        report, not resumability state. A record with neither field is a first provision and is
        untouched. The ARN is never logged: it names another agent's or the tenant's secret."""
        arn = (agent.databricks_sp_secret_arn or "").strip()
        sp_id = (agent.databricks_sp_id or "").strip()
        if not arn and not sp_id:
            return
        if arn and self.owns_secret_arn(agent, arn):
            return
        logger.warning(
            "[databricks_identity] agent %s carried Databricks service-principal pointers this "
            "service did not write; discarding them and provisioning its own",
            agent.id,
        )
        agent.databricks_sp_id = None
        agent.databricks_sp_secret_arn = None

    def _agent_secret_name(self, agent: Agent) -> str:
        """The per-agent secret's name, used only at create time; reads go by the stored ARN."""
        return f"{self._secret_prefix}{agent.id}"

    def store_agent_secret(self, agent: Agent, *, secret: str, scim_id: str) -> str:
        """Store a per-agent secret body and return its ARN. ``create_secret``, then
        ``put_secret_value`` on ``ResourceExistsException``, so a re-provision after a crash
        between mint and persist overwrites rather than dying on the name. ``secret=""`` writes
        the id-only entry with the key omitted, so "has a credential" is a key-presence question.
        Nothing here logs the value."""
        name = self._agent_secret_name(agent)
        payload = {_AGENT_SCIM_ID_KEY: scim_id, _AGENT_OWNER_KEY: agent.id}
        if secret:
            payload[_AGENT_CREDENTIAL_FIELD] = secret
        body = json.dumps(payload)
        try:
            resp = self._sm.create_secret(
                Name=name,
                SecretString=body,
                Tags=[
                    {"Key": "managed_by", "Value": "agp"},
                    {"Key": "agent_id", "Value": agent.id},
                ],
            )
            return resp["ARN"]
        except getattr(getattr(self._sm, "exceptions", None), "ResourceExistsException", ()):
            resp = self._sm.put_secret_value(SecretId=name, SecretString=body)
            return resp["ARN"]
        except _SECRETS_FAULTS:
            logger.exception(
                "[databricks_identity] could not store the per-agent Databricks secret for agent %s",
                agent.id,
            )
            raise ProvisioningError(
                f"agent {agent.id}'s Databricks credential could not be stored, so it was not recorded"
            ) from None

    def stored_agent_secret_present(self, agent: Agent) -> bool:
        """Whether the per-agent secret entry carries a credential: the gate for minting. Read
        from the body, since the ARN exists as soon as the principal does. An unreadable body or
        a body naming another owner reads as "no credential"."""
        arn = (agent.databricks_sp_secret_arn or "").strip()
        if not arn:
            return False
        try:
            body = self.read_secret_body(arn)
        except ProvisioningError:
            return False
        owner = body.get(_AGENT_OWNER_KEY)
        if isinstance(owner, str) and owner and owner != agent.id:
            return False
        return bool(body.get(_AGENT_CREDENTIAL_FIELD))

    def stored_scim_id(self, agent: Agent) -> str:
        """The principal's SCIM id from the per-agent secret body, or ``""`` when the body is
        unreadable or names another owner; the caller refuses to mint rather than substitute the
        application id, which would 404."""
        arn = (agent.databricks_sp_secret_arn or "").strip()
        if not arn:
            return ""
        try:
            body = self.read_secret_body(arn)
        except ProvisioningError:
            return ""
        owner = body.get(_AGENT_OWNER_KEY)
        if isinstance(owner, str) and owner and owner != agent.id:
            return ""
        return str(body.get(_AGENT_SCIM_ID_KEY) or "")

    def delete_secret_best_effort(self, secret_id: str) -> None:
        """Delete a per-agent secret; an already-missing one is a success."""
        try:
            self._sm.delete_secret(SecretId=secret_id, ForceDeleteWithoutRecovery=True)
        except getattr(getattr(self._sm, "exceptions", None), "ResourceNotFoundException", ()):
            return
        except _SECRETS_FAULTS:
            logger.exception("[databricks_identity] delete_secret failed for a per-agent secret")
            raise
