"""The infra-template catalog: the record model, its store and the seed catalog.

An agent template says what the agent's code is; an infra template says how that code is deployed:
which IaC module runs (``iac_type``, the buildspec branch key), whether the build produces a
container or consumes a config file (``build_kind``), and which platform the result lands on. Storing
those facts makes tenant compatibility a derived join on ``platform`` and keeps the deploy mechanism
out of the caller's hands. A record is a pointer, never a payload: ``repo_name`` names the org repo
the rollout ensures and ``module_source`` the S3 key of the staged module zip.

Persistence mirrors ``template_registry``: one partition of the projects table,
``pk="infra_template"`` with the composite ``sk="<connection_id>#<id>"``, so one connection's
catalog is a contiguous ``begins_with`` range; ``#`` is refused in ``connection_id`` and excluded
from ``id`` by its shape. Reads are strict: a store fault raises :class:`InfraTemplateRegistryError`
rather than reading as an empty catalog, because "no infra templates" would tell the console every
agent template is incompatible with every tenant. The module holds no secret.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import ClassVar, List, Literal, Optional

from botocore.exceptions import ClientError
from pydantic import Field, PrivateAttr, field_validator

from core.errors import AppError, Outcome
from core.state_store import StateStore
from models.base import WireModel

logger = logging.getLogger(__name__)

_PARTITION_KEY = "infra_template"
_SK_SEPARATOR = "#"
_ID_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")
_IAC_TYPE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")

SUPPORTED_PLATFORMS = ("aws", "databricks")
SUPPORTED_BUILD_KINDS = ("container", "config")


class InfraTemplateRegistryError(AppError):
    """The infra template catalog could not be read or written: a store fault the caller cannot
    fix by retrying the same request, rendered as the retryable 503. The message is a fixed
    literal of this module, never a store value; the route shows its own fixed detail."""

    domain: ClassVar[str] = "infra_template"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "store_error": Outcome(503, "Infra template catalog is temporarily unavailable"),
    }


class InfraTemplateRegistryValidationError(InfraTemplateRegistryError):
    """The caller's input is malformed: an empty or ``#``-bearing key component. Permanent, so
    it renders the 422 where the parent renders the retryable 503; a subclass so a caller that
    only cares that the registry refused still catches the parent."""

    kinds: ClassVar[Mapping[str, Outcome]] = {
        "validation": Outcome(422, "Invalid infra template request"),
    }


class InfraTemplateRecord(WireModel):
    """One infra-template catalog entry: how a template's repo becomes a running agent.

    Field validation raises pydantic's ``ValidationError`` (a ``ValueError``) rather than the
    registry's exception types on purpose: this is model-shape validation that must fail at
    construction wherever a record is built, including from a DynamoDB item and from
    :data:`SEED_INFRA_TEMPLATES`, not only when it reaches the store.
    """

    id: str = Field(description="Kebab-case catalog key, matched with fullmatch so it is a legal sk half.")
    name: str
    description: str = ""
    platform: str = Field(
        description="One of SUPPORTED_PLATFORMS, the tenant-compatibility half of the join."
    )
    iac_type: str = Field(
        description="The buildspec branch key, snake_case; an open set checked for shape only."
    )
    build_kind: str = Field(
        description="One of SUPPORTED_BUILD_KINDS: a container build pushes an image, a config build none."
    )
    repo_name: str = Field(description="The org repository the rollout ensures for this module.")
    module_source: str = Field(description="The S3 key of the staged module zip; never served on the wire.")
    version: str = "1"
    connection_id: str
    created_at: str
    created_by: str = ""
    updated_at: str = ""
    artifact_kinds: List[Literal["agent", "mcp_server"]] = Field(
        default=["agent"],
        description=(
            "The artifact kinds this infra template can deploy, the constraint on a template's "
            "artifact_kind. Defaults to agents, what every infra template deployed before MCP "
            "servers existed."
        ),
    )
    _row_version: int | None = PrivateAttr(default=None)

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not _ID_RE.fullmatch(value):
            raise ValueError("infra template id must be kebab-case matching ^[a-z][a-z0-9-]{0,63}$")
        return value

    @field_validator("iac_type")
    @classmethod
    def _check_iac_type(cls, value: str) -> str:
        if not _IAC_TYPE_RE.fullmatch(value):
            raise ValueError("iac_type must be snake_case matching ^[a-z][a-z0-9_]{0,63}$")
        return value

    @field_validator("platform")
    @classmethod
    def _check_platform(cls, value: str) -> str:
        if value not in SUPPORTED_PLATFORMS:
            raise ValueError(f"platform must be one of {list(SUPPORTED_PLATFORMS)}, got {value!r}")
        return value

    @field_validator("build_kind")
    @classmethod
    def _check_build_kind(cls, value: str) -> str:
        if value not in SUPPORTED_BUILD_KINDS:
            raise ValueError(f"build_kind must be one of {list(SUPPORTED_BUILD_KINDS)}, got {value!r}")
        return value

    @field_validator("name", "repo_name", "module_source")
    @classmethod
    def _check_required_text(cls, value: str) -> str:
        """The stripped value is what gets stored, so the stored value is the validated one."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("name, repo_name and module_source are required")
        return stripped


SEED_INFRA_TEMPLATES: List[dict] = [
    {
        "id": "agentcore-runtime",
        "name": "AgentCore Runtime",
        "description": (
            "Containerised agent on Bedrock AgentCore Runtime — the repo builds an image, "
            "the module provisions the runtime."
        ),
        "platform": "aws",
        "iac_type": "agentcore_runtime",
        "build_kind": "container",
        "repo_name": "agp-runtime-infra",
        "module_source": "runtime-module/agentcore_runtime.zip",
        "version": "1",
    },
    {
        "id": "agentcore-harness",
        "name": "AgentCore Harness",
        "description": (
            "Config-driven agent on Bedrock AgentCore Harness (preview) — the repo holds "
            "harness.yaml, there is no image."
        ),
        "platform": "aws",
        "iac_type": "agentcore_harness",
        "build_kind": "config",
        "repo_name": "agp-harness-infra",
        "module_source": "harness-module/agentcore_harness.zip",
        "version": "1",
    },
    {
        "id": "agentcore-gateway",
        "name": "AgentCore Gateway",
        "description": (
            "MCP server on Bedrock AgentCore Gateway — the repo holds gateway.yaml and an "
            "OpenAPI spec, there is no image."
        ),
        "platform": "aws",
        "iac_type": "agentcore_gateway",
        "build_kind": "config",
        "repo_name": "agp-gateway-infra",
        "module_source": "gateway-module/agentcore_gateway.zip",
        "version": "1",
        "artifact_kinds": ["mcp_server"],
    },
]


class InfraTemplateRegistry:
    """The :class:`InfraTemplateRecord` store: the ``pk="infra_template"`` partition of the
    projects table, written through :class:`StateStore` (in memory when ``table_name`` is empty)."""

    def __init__(
        self,
        table_name: str = "",
        region: str | None = None,
        *,
        now=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.table_name = table_name
        self.region = region
        self._now = now
        self._store = StateStore(table_name, region=region)

    def list(self, connection_id: str) -> List[InfraTemplateRecord]:
        """One connection's infra catalog, a ``begins_with`` range read. A store failure raises
        rather than returning ``[]``: an empty catalog and an unreadable one are opposite answers."""
        self._validate_ids(connection_id)
        prefix = f"{connection_id}{_SK_SEPARATOR}"
        return [self._from_item(i) for i in self._query(sk_prefix=prefix)]

    def get(
        self, connection_id: str, infra_template_id: str, *, consistent: bool = False
    ) -> Optional[InfraTemplateRecord]:
        """One infra template, or ``None``. A store failure raises, so the compatibility join never
        reads an unreadable partition as an unknown platform."""
        self._validate_ids(connection_id, infra_template_id)
        sk = _sort_key(connection_id, infra_template_id)
        try:
            item = self._store.get((_PARTITION_KEY, sk), consistent=consistent)
        except ClientError:
            logger.exception("[infra-template-registry] failed to read infra template %s", sk)
            raise InfraTemplateRegistryError("Could not read the infra template catalog") from None
        return self._from_item(item) if item else None

    def create(self, record: InfraTemplateRecord) -> bool:
        """Write ``record`` as a new infra template at ``row_version`` 1, stamping its ``updated_at``,
        and return whether it landed: ``False`` means the (connection, id) is already in the catalog
        and nothing was written."""
        self._validate_ids(record.connection_id, record.id)
        record.updated_at = self._now().isoformat()
        try:
            created = self._store.put_new({**self._to_item(record), "row_version": 1})
        except ClientError:
            logger.exception(
                "[infra-template-registry] failed to write infra template %s",
                _sort_key(record.connection_id, record.id),
            )
            raise InfraTemplateRegistryError("Could not write the infra template catalog") from None
        if created:
            record._row_version = 1
        return created

    def save(self, record: InfraTemplateRecord) -> InfraTemplateRecord:
        """Replace the infra template ``record`` was read from, stamping ``updated_at``, and return
        it at the ``row_version`` it was written at. The write lands only while the row is at the
        ``row_version`` the record carries, ``None`` matching only a row that carries none; an absent
        row or one another write moved raises ``core.state_store.VersionMismatch`` and writes
        nothing."""
        self._validate_ids(record.connection_id, record.id)
        stamped = record.model_copy(update={"updated_at": self._now().isoformat()})
        expected = stamped._row_version
        written = (expected or 0) + 1
        try:
            self._store.put_if_version(
                {**self._to_item(stamped), "row_version": written}, "row_version", expected
            )
        except ClientError:
            logger.exception(
                "[infra-template-registry] failed to write infra template %s",
                _sort_key(stamped.connection_id, stamped.id),
            )
            raise InfraTemplateRegistryError("Could not write the infra template catalog") from None
        stamped._row_version = written
        return stamped

    def delete(self, connection_id: str, infra_template_id: str) -> bool:
        """Deregister one infra template and report whether an entry was removed: the pointer only,
        never the infra repo or the staged zip. Idempotent: already gone is ``False``, not an
        error; the store's delete returns the old row, which is how absence is told apart."""
        self._validate_ids(connection_id, infra_template_id)
        sk = _sort_key(connection_id, infra_template_id)
        try:
            removed = self._store.delete((_PARTITION_KEY, sk))
        except ClientError:
            logger.exception("[infra-template-registry] failed to delete infra template %s", sk)
            raise InfraTemplateRegistryError("Could not update the infra template catalog") from None
        return removed is not None

    @staticmethod
    def _validate_ids(connection_id: str, infra_template_id: Optional[str] = None) -> None:
        """Refuse an empty or ``#``-bearing key half: ``("a", "b#c")`` and ``("a#b", "c")`` both
        encode to ``"a#b#c"``, so the second write would silently overwrite the first. The id half
        is re-checked because :meth:`get` and :meth:`delete` take a bare string, not a record.
        Raises the validation subclass, never the store-fault parent."""
        if not connection_id:
            raise InfraTemplateRegistryValidationError("connection_id is required")
        if _SK_SEPARATOR in connection_id:
            raise InfraTemplateRegistryValidationError("connection_id must not contain '#'")
        if infra_template_id is not None:
            if not infra_template_id:
                raise InfraTemplateRegistryValidationError("infra template id is required")
            if _SK_SEPARATOR in infra_template_id:
                raise InfraTemplateRegistryValidationError("infra template id must not contain '#'")

    def _query(self, *, sk_prefix: str) -> List[dict]:
        try:
            return self._store.query(_PARTITION_KEY, sk_prefix=sk_prefix)
        except ClientError:
            logger.exception(
                "[infra-template-registry] failed to list the infra_template partition for %s",
                sk_prefix,
            )
            raise InfraTemplateRegistryError("Could not read the infra template catalog") from None

    @staticmethod
    def _to_item(record: InfraTemplateRecord) -> dict:
        return {
            "pk": _PARTITION_KEY,
            "sk": _sort_key(record.connection_id, record.id),
            **json.loads(record.model_dump_json()),
        }

    @staticmethod
    def _from_item(item: dict) -> InfraTemplateRecord:
        clean = {k: v for k, v in item.items() if k not in ("pk", "sk")}
        record = InfraTemplateRecord.model_validate(clean)
        version = item.get("row_version")
        record._row_version = None if version is None else int(version)
        return record


def _sort_key(connection_id: str, infra_template_id: str) -> str:
    return f"{connection_id}{_SK_SEPARATOR}{infra_template_id}"
