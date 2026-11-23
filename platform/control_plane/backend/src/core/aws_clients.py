"""The one constructor for every boto3 client, resource and table the backend touches.

`client(service, *, region=None, config=None, credentials=None)` returns
`boto3.client(service, region_name=region or settings.AWS_REGION, ...)` with the service as the first
positional argument, `config=` forwarded unchanged when given and the three `aws_*` keywords spelled
from `credentials` when given. `dynamodb_table(name, *, region=None)` returns `.Table(name)` on the
`boto3.resource("dynamodb", ...)` that `_dynamodb` builds, and refuses an empty name: an empty table
name selects the in-memory store, and that decision belongs to `core.state_store`.
`registry_control_client(region)` is `client(REGISTRY_CONTROL, region=region)`, the one provider the
registry writer and the bootstrap script obtain their client from; under `REGISTRY_IN_MEMORY=true` it answers
`core.registry_memory.memory_registry_client(region)`, the process's one in-memory registry, and builds
no boto3 client. `ambient_region()` is the default session's region, for the one script that reads the
profile rather than a setting. Every construction runs under one module lock, because boto3's default
session is not safe for concurrent client creation and the containment monitor thread constructs beside
request handlers. Nothing is cached: each holder keeps its client as an attribute.

boto3 is imported inside the functions, never at module scope, for two reasons the suite depends on:
the backend scripts' tests swap `sys.modules["boto3"]` per test and rely on a call-time import, and
two test files patch the global `boto3.client` and assert on its positional arguments.

Decision: docs/adr/backend-aws-clients-one-factory.md
Decision: docs/adr/backend-registry-in-memory-behind-the-provider.md
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from core.config import settings

if TYPE_CHECKING:
    from botocore.config import Config

REGISTRY_CONTROL: Final = "agent-registry-control"

_LOCK = threading.Lock()


@dataclass(frozen=True)
class AssumedCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str


def client(
    service: str,
    *,
    region: str | None = None,
    config: Config | None = None,
    credentials: AssumedCredentials | None = None,
) -> Any:
    import boto3

    kw: dict[str, Any] = {}
    if config is not None:
        kw["config"] = config
    if credentials is not None:
        kw["aws_access_key_id"] = credentials.access_key_id
        kw["aws_secret_access_key"] = credentials.secret_access_key
        kw["aws_session_token"] = credentials.session_token
    with _LOCK:
        return boto3.client(service, region_name=region or settings.AWS_REGION, **kw)


def _dynamodb(region: str) -> Any:
    import boto3

    with _LOCK:
        return boto3.resource("dynamodb", region_name=region)


def dynamodb_table(name: str, *, region: str | None = None) -> Any:
    if not name:
        raise ValueError(
            "a DynamoDB table needs a name; an empty name means the in-memory store, "
            "which core.state_store selects"
        )
    return _dynamodb(region or settings.AWS_REGION).Table(name)


def registry_control_client(region: str | None) -> Any:
    region = region or settings.AWS_REGION
    if settings.REGISTRY_IN_MEMORY:
        from core.registry_memory import memory_registry_client

        return memory_registry_client(region)
    return client(REGISTRY_CONTROL, region=region)


def ambient_region() -> str | None:
    import boto3

    with _LOCK:
        return boto3.Session().region_name
