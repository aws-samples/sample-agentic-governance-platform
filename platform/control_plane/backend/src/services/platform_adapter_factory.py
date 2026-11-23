"""Build the platform adapters once, and dispatch to one by platform.

Kept apart from ``services/platform_adapter.py`` so a consumer of the protocol (a type annotation,
a test, a route that only needs ``AdapterError``) does not import both platforms' service graphs.
``adapter_for`` is an allowlist with no default arm: an unknown platform, or one this deployment
wired no collaborator for, is refused with ``platform_unsupported`` rather than handed whichever
adapter happens to be first in the dict. The DI layer constructs the dict once per process.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from models.tenant import TenantPlatform
from services.platform_adapter import AdapterError, PlatformAdapter
from services.platform_adapters.aws import AwsPlatformAdapter
from services.platform_adapters.databricks import DatabricksPlatformAdapter

if TYPE_CHECKING:
    from models.tenant import Tenant
    from services.agent_identity_service import AgentIdentityService
    from services.databricks_identity_service import DatabricksIdentityService
    from services.tenant_service import TenantService


def build_adapters(
    *,
    identity: AgentIdentityService,
    databricks_identity: DatabricksIdentityService | None,
    tenant_service: TenantService,
) -> dict[TenantPlatform, PlatformAdapter]:
    """One adapter per platform AGP can act on, keyed by ``TenantPlatform``.

    Exhaustive over ``TenantPlatform`` when everything is wired (the test asserts set equality, so
    a third platform fails here rather than as a ``KeyError`` in production). A platform with no
    collaborator is omitted, not half-built: ``adapter_for`` then refuses it with
    ``platform_unsupported``, the one place a caller can ask "is this platform available here?".
    """
    adapters: dict[TenantPlatform, PlatformAdapter] = {
        TenantPlatform.AWS: AwsPlatformAdapter(identity=identity),
    }
    if databricks_identity is not None:
        adapters[TenantPlatform.DATABRICKS] = DatabricksPlatformAdapter(
            databricks_identity, probe=_tenant_probe(tenant_service)
        )
    return adapters


def _tenant_probe(tenant_service: TenantService):
    """Adapt ``TenantService._probe`` (which takes a stage map, mid-create, before a ``Tenant``
    exists) to the seam's ``(tenant, admin_creds)`` shape. The one place that private is named.

    ``previous`` is read from the store, not from the passed ``tenant``: at create the passed
    record has empty ``capabilities`` and ``binding_mode == ""``, so using it would make the
    carry-over arm return the AWS answer for a Databricks tenant. ``_get(tenant.id)`` is ``None``
    before the record is persisted and the stored record afterwards, the right ``previous`` for
    each path, and it keeps a re-probe of an uncredentialable tenant from zeroing its flags.
    """

    def probe(tenant: Tenant, admin_creds) -> tuple[dict, str]:
        previous = tenant_service._get(tenant.id)
        admin_unresolved = bool(tenant.account_admin_secret_arn) and not all(admin_creds or (None, None))
        return tenant_service._probe(
            tenant.stages, admin_creds, previous=previous, admin_unresolved=admin_unresolved
        )

    return probe


def adapter_for(
    adapters: dict[TenantPlatform, PlatformAdapter], platform: TenantPlatform | str | None
) -> PlatformAdapter:
    """The adapter for one platform, by enum or by stored wire string. Raises
    ``AdapterError(kind="platform_unsupported")`` for an unknown value, ``None``, or a platform this
    deployment wired no collaborator for; the platform value is never echoed."""
    key: TenantPlatform | None
    try:
        key = TenantPlatform(platform)
    except ValueError:
        key = None
    adapter = adapters.get(key) if key is not None else None
    if adapter is None:
        raise AdapterError("AGP has no adapter for that platform", kind="platform_unsupported")
    return adapter
