"""Find an AWS Agent Registry by name in the pages of ``ListRegistries``.

AWS mints the registry id, so the backend resolves ``AGENT_REGISTRY_NAME`` / ``MCP_REGISTRY_NAME`` to
an id from one paginated ``ListRegistries`` call, matched client-side on ``name`` (the API filters
only on ``status`` and ``discoveryConfiguration.authorizerType``, per the botocore model).
:func:`find_registry_by_name` is the one implementation: ``services.registry_writer`` passes the
pages of its own client at first use and ``scripts/ensure_registry.py`` the pages of the bootstrap's.

Three rules, each pinned in ``tests/test_registry_name_resolution.py``: a name that matches nothing
is loud (:class:`RegistryNotFoundError`, whose message is the remedy); two viable registries sharing
a name refuse rather than guess (:class:`AmbiguousRegistryNameError`), because records written into
the wrong registry fail silently; a registry whose status is in ``_NON_VIABLE_STATUSES`` is skipped
and logged, so an interrupted bootstrap's leftovers never make a healthy twin ambiguous, while every
other status, known or future, is viable. Importing this module makes no AWS call, reads no settings
and constructs no client: the function takes the pages, never a client.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any, ClassVar

from core.errors import AppError, Outcome

logger = logging.getLogger(__name__)

_NON_VIABLE_STATUSES = frozenset({"CREATE_FAILED", "DELETING", "DELETE_FAILED"})


class RegistryNotFoundError(AppError):
    """No usable registry with the requested name exists in the account and region, or every match
    is in a non-viable state. The message names the registry, the region and the commands that
    create or repair it, composed from the configured name and region, the ids and statuses of the
    registries the list call returned, and this module's own text, never a provider error body.
    Fatal on purpose: an empty registry would render an inert UI with no error anywhere."""

    domain = "registry"
    kinds: ClassVar = {"not_found": Outcome(503, None)}


class RegistryNotConfiguredError(AppError):
    """A registry service was given neither a registry id nor a registry name. Raised on first use
    rather than in the constructor, so constructing a service never fails on configuration alone.
    The message names the environment variables to set and nothing from a provider."""

    domain = "registry"
    kinds: ClassVar = {"not_configured": Outcome(503, None)}


class AmbiguousRegistryNameError(AppError):
    """Two or more viable registries share the requested name; the message names their ids so the
    operator can delete the stray one instead of the platform guessing. It carries the requested
    name and those identifiers, never a provider error body."""

    domain = "registry"
    kinds: ClassVar = {"ambiguous_name": Outcome(503, None)}


def find_registry_by_name(pages: Iterable[Mapping[str, Any]], name: str) -> tuple[str | None, str | None]:
    """Return ``(registryId, registryArn)`` for the viable registry named ``name`` in the ``ListRegistries``
    ``pages``, or ``(None, None)``.

    List items carry a real ``registryId`` member, so nothing is inferred and no ARN is parsed.
    Absence is an ordinary answer because ``scripts/ensure_registry.py`` finds-or-creates through this
    function; the registry writer turns it into :class:`RegistryNotFoundError`. A registry whose
    status is in ``_NON_VIABLE_STATUSES`` is skipped and logged at INFO, so a ``CREATE_FAILED`` corpse
    beside a ``READY`` twin neither counts as ambiguous nor hides the diagnosis; ``CREATING`` stays
    viable because the bootstrap adopts a match and waits on the ``registry_ready`` waiter, and
    ``UPDATE_FAILED`` stays viable because that registry still holds every record. The status filter is
    client-side on purpose: filtering server-side would make "exists but broken" indistinguishable from
    "does not exist". Raises :class:`AmbiguousRegistryNameError` when more than one viable registry
    carries the name; the scan is completed rather than short-circuited so the duplicate can be seen.
    """
    matches: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    for page in pages:
        for item in page.get("registries", []):
            status = item.get("status")
            logger.debug(
                "  registry name=%r id=%r status=%r",
                item.get("name"),
                item.get("registryId"),
                status,
            )
            if item.get("name") != name:
                continue
            if status in _NON_VIABLE_STATUSES:
                logger.info(
                    "Ignoring registry %r (id %s) — status is %s, which is not a usable "
                    "state. It will not be resolved and does not count towards the "
                    "duplicate-name check.",
                    name,
                    item.get("registryId"),
                    status,
                )
                skipped.append((item.get("registryId"), status))
                continue
            matches.append((item.get("registryId"), item.get("registryArn")))

    if len(matches) > 1:
        raise AmbiguousRegistryNameError(
            f"{len(matches)} registries are named {name!r} "
            f"(ids: {', '.join(str(rid) for rid, _arn in matches)}). AWS does not enforce "
            "unique registry names, and picking one would write agent/MCP records into a "
            "registry nobody selected — silently, since records would land somewhere and "
            "nothing would error. Delete the registry that should not exist (or point the "
            "*_REGISTRY_ID setting at the one you want, which bypasses this lookup "
            "entirely), then retry."
        )
    if matches:
        return matches[0]
    if skipped:
        raise RegistryNotFoundError(
            f"A registry named {name!r} exists but none of the "
            f"{len(skipped)} match(es) is in a usable state "
            f"({', '.join(f'{rid} is {st}' for rid, st in skipped)}). This is what an "
            "interrupted bootstrap leaves behind. Delete the unusable registry, then re-run "
            "`terraform apply` in platform/control_plane/infrastructure (or "
            f"`PYTHONPATH=src venv/bin/python scripts/ensure_registry.py --name {name}`) to "
            "create a healthy one. A registry mid-create (CREATING) is NOT what this "
            "message is about — that state is waited for, not skipped."
        )
    return None, None
