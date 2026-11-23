"""The wire shape of ``GET /users/me``'s membership list: ``UserTenant``, one entry per tenant the
caller belongs to, projected by ``api.routes.users`` from the ``Tenant`` record.

A membership carries the tenant's identity (``id``, ``name``, ``line_of_business``), its ``platform``
and ``binding_mode``, the derived per-verb ``readiness`` and its ``stages``. A stage is
projected per platform: an AWS stage crosses whole as ``TenantStageConfig``; a Databricks stage
crosses as ``UserDatabricksStage``, the four keys the route enumerates (``workspace_url``,
``workspace_id``, ``region`` and an always-empty ``account_id``), with ``cloud``, ``sp_client_id``
and ``sp_client_secret_arn`` left to the admin tenant route, so this model cannot carry a credential
pointer back onto the wire. The union resolves by the keys present, pydantic's smart mode preferring
the member that sets more fields: the Databricks projection sets all four of ``UserDatabricksStage``
against two of ``TenantStageConfig``, and an AWS stage has no ``workspace_url``. Every value is
reporting only: the invoke path re-reads the tenant record and never trusts a client's copy. Pure
models, no I/O.
"""

from __future__ import annotations

from typing import Dict, Union

from pydantic import Field

from models.base import WireModel
from models.tenant import TenantBindingMode, TenantPlatform, TenantStageConfig, VerbReadiness


class UserDatabricksStage(WireModel):
    workspace_url: str
    workspace_id: str
    region: str
    account_id: str = Field(
        description="Always empty here; the record's Databricks account UUID stays on the admin route"
    )


UserStage = Union[TenantStageConfig, UserDatabricksStage]


class UserTenant(WireModel):
    id: str
    name: str
    line_of_business: str
    platform: TenantPlatform
    binding_mode: TenantBindingMode
    readiness: Dict[str, VerbReadiness]
    stages: Dict[str, UserStage]
