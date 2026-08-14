"""Resolve an AWS Agent Registry NAME to its registryId, lazily and at first use (E32).

WHY THIS MODULE EXISTS — the id cannot be chosen, so it must be discovered
-------------------------------------------------------------------------
AWS mints the registryId. ``RegistryIdentifier`` accepts only an ARN or a generated
12-16 char id (verified pattern: ``(arn:aws...:registry/)?[a-zA-Z0-9]{12,16}``) — never a
name. Combined with the fact that there is NO Terraform resource for the ``agent-registry``
namespace (``modules/agent_registry`` creates registries through a ``local-exec``
provisioner, and a provisioner has no channel to return a value), that meant the id had to
round-trip through a capture file that Terraform read during the PLAN walk — i.e. BEFORE
the provisioner that writes it had run. The consequence was a from-zero deploy that needed
``terraform apply`` TWICE: pass 1 baked ``AGENT_REGISTRY_ID=""`` into the ECS task
definition, pass 2 filled in the real id.

The name, by contrast, is a static configuration value known to everyone at plan time
(``AGENT_REGISTRY_NAME = "agp-agents"``, ``MCP_REGISTRY_NAME = "agp-mcp-servers"`` in
``core/config.py``, mirrored by the ``agent_registry_name`` / ``mcp_registry_name`` tfvars).
Resolving name -> id is ONE ``ListRegistries`` call. Doing that resolution in the backend,
at first use, removes the capture file, the guarded plan-time read, the second apply and
the null-route guards in one move.

This mirrors an existing precedent in ``infrastructure/main.tf``: the GitHub OIDC provider
and the shared push role are deliberately NOT Terraform objects, because they cannot be
clean ones — the platform bootstraps them instead. Same reasoning applies here.

WHY THE MATCH IS CLIENT-SIDE
----------------------------
``ListRegistries``' ``filters`` cannot filter on name. Verified against the
``agent-registry-control`` botocore model: the only filterable names are ``status`` and
``discoveryConfiguration.authorizerType``. So the paginated pages have to be walked and
matched on ``item["name"]`` — which is exactly what ``scripts/ensure_registry.py`` has
always done, and this module is where that logic now lives so the script and the two
registry services share ONE implementation. List items carry a real ``registryId`` member
alongside ``name``/``registryArn``/``status``, so nothing is inferred and no ARN is parsed.

WHY A DUPLICATE NAME IS A HARD ERROR, NOT "FIRST MATCH WINS"
------------------------------------------------------------
AWS does not guarantee name uniqueness across registries in an account+region, so two
registries CAN legitimately share a name (a half-finished bootstrap against an interrupted
apply is the realistic route). Picking one silently means the platform writes agent and
MCP-server records into a registry nobody intended, splits the catalog across two
registries depending on which process resolved first, and reports success the whole time —
records land somewhere, so nothing errors. That is unrecoverable by inspection: from the
outside, "half my agents disappeared" and "records went to the other registry" look the
same. Refusing to guess turns an ambiguous account state into one loud message naming both
ids, which an operator fixes in a minute by deleting the stray registry. There is no
tie-break rule available that would be better than a guess either — creation order is not
exposed in a stable, meaningful way for this purpose, and "oldest wins" would still be
arbitrary when the newer one is the intended one.

IMPORT-TIME PURITY IS A CONTRACT
--------------------------------
Importing this module makes no AWS call, reads no settings and constructs no client: boto3
is imported inside ``_client`` and every entry point takes an injectable client. The
backend has an existing convention here (``tests/test_api_properties.py`` deliberately
bypasses ``src/__init__.py`` to avoid triggering ``Settings()`` validation), and the two
registry services resolve on FIRST USE, memoised on the instance — never at import, never
in ``Settings()``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# The Registry's namespace since the 2026-08-06 split. The old `bedrock-agentcore-control`
# client is unreachable from accounts created after it and shuts down 2026-09-17.
SERVICE_NAME = "agent-registry-control"

# Registry statuses a registry CANNOT usefully be resolved to. A closed denylist, taken from
# the ``RegistryStatus`` enum in the pinned botocore model (verified offline; the full set is
# CREATING, READY, UPDATING, CREATE_FAILED, UPDATE_FAILED, DELETING, DELETE_FAILED).
#
# WHY A DENYLIST AND NOT AN ALLOWLIST OF {READY}. The two failure directions are not
# symmetric. Treating an unknown/new status as non-viable would take down a HEALTHY registry
# the moment AWS adds a status value — an outage caused by our own optimism. Treating one as
# viable, at worst, addresses a registry that then errors on its own terms, which is loud and
# local. So anything not explicitly known to be unusable is allowed through, and the
# duplicate-name hard error below remains the backstop if a future failure status ever
# co-exists with a healthy twin.
#
# WHY EACH OF THE THREE IS HERE:
#   CREATE_FAILED  never became usable; it holds no records and never will.
#   DELETING       its records are going away; writing into it loses them silently.
#   DELETE_FAILED  the operator's intent was deletion. Adopting it would write governance
#                  records into something someone is actively trying to remove.
#
# AND WHY THE OTHER FOUR ARE DELIBERATELY *NOT*:
#   READY          the ordinary case.
#   CREATING       LOAD-BEARING that this stays viable. ``scripts/ensure_registry.py`` adopts
#                  a match and then waits on the ``registry_ready`` waiter, so a registry
#                  found mid-create is fine — it is waited for. Excluding it would make the
#                  bootstrap CREATE A SECOND registry beside one that is already coming up,
#                  manufacturing exactly the duplicate-name state that script exists to avoid.
#   UPDATING       transient, and the records are all still there.
#   UPDATE_FAILED  the non-obvious one, and deliberate: unlike CREATE_FAILED this describes a
#                  registry that WAS ready and still holds every record — only a configuration
#                  change failed. Refusing it would be an outage we inflicted over a failed
#                  update, on a registry that reads and writes perfectly well.
_NON_VIABLE_STATUSES = frozenset({"CREATE_FAILED", "DELETING", "DELETE_FAILED"})


class RegistryNotFoundError(Exception):
    """No registry with the requested NAME exists, in a usable state, in the account+region.

    Deliberately fatal rather than "treat it as an empty registry". An empty result renders
    an inert UI — zero agents, zero MCP servers, no error anywhere — which is precisely the
    failure mode the name-resolution change exists to kill: the operator sees a working
    control plane with nothing in it and no clue why. The message therefore names the
    registry, the region, and the two commands that create it.

    Also raised when the name DOES match but every match is in a non-viable state (see
    ``_NON_VIABLE_STATUSES``) — a distinct message, because "it does not exist" and "it
    exists but is broken" have different remedies and the second is otherwise very hard to
    diagnose from the outside.
    """


class RegistryNotConfiguredError(Exception):
    """A registry service was given neither a registry id nor a registry name.

    Raised on FIRST USE rather than in the constructor, so constructing a service (which
    several scripts and tests do before deciding what to do with it) never fails on config
    alone — but the first call that needs a registry says so plainly instead of addressing
    the empty-string registryId that the old required-``registry_id`` signature allowed.
    """


class AmbiguousRegistryNameError(Exception):
    """Two or more registries share the requested NAME — refuse to guess which one.

    See the module docstring: silently choosing one writes governance records into a
    registry nobody selected, and that is invisible until the catalog looks halved.
    """


def _client(region: str):
    """Return an ``agent-registry-control`` client for ``region``.

    boto3 is imported lazily so importing this module stays side-effect-free. Tests
    monkeypatch this function (or, more usually, inject a client and never reach it).
    """
    import boto3

    return boto3.client(SERVICE_NAME, region_name=region)


def find_registry_by_name(ctl, name: str):
    """Return ``(registryId, registryArn)`` for the VIABLE registry named ``name``.

    Returns ``(None, None)`` when no such registry exists — the find-or-CREATE caller
    (``scripts/ensure_registry.py``) needs absence to be an ordinary answer, so absence is
    NOT an exception here; the read-only callers turn it into ``RegistryNotFoundError``
    themselves via :func:`resolve_registry_id`.

    STATUS IS PART OF THE MATCH, NOT JUST THE NAME
    ----------------------------------------------
    Matching on name alone created a state that poisons its own remedy. The realistic route
    to a duplicate name is an interrupted bootstrap: a first attempt leaves a registry in
    ``CREATE_FAILED``, a retry creates a second that reaches ``READY``, and the account now
    holds two registries called ``agp-agents``. Name-only matching then finds BOTH, raises
    :class:`AmbiguousRegistryNameError`, and the platform hard-errors on every registry
    request — permanently, until a human deletes the corpse. Worse, this is the SAME function
    ``ensure_registry.py`` uses to find-or-create, so the operator's most natural remedy
    (re-run the bootstrap) refuses too instead of adopting the healthy registry. A
    self-healing state became an outage.

    Registries in a non-viable state are therefore skipped outright (see
    ``_NON_VIABLE_STATUSES`` for the exact set and the reasoning per status, including why
    ``CREATING`` and ``UPDATE_FAILED`` are deliberately kept viable). A skipped match is
    logged at INFO, not silently dropped: "the registry is there but broken" must be visible.

    WHY THE FILTER IS CLIENT-SIDE. ``ListRegistries``' ``filters`` genuinely CAN filter on
    ``status`` server-side (it is one of only two filterable fields, ``name`` not being one of
    them) and doing so would be marginally cheaper. It is not used, on purpose: filtering
    server-side makes "a CREATE_FAILED registry by this name exists" indistinguishable from
    "nothing by this name exists at all", which throws away the diagnosis this change exists
    to produce. The message that tells an operator "it exists but is in CREATE_FAILED, delete
    it and re-run" is worth more than one skipped field comparison per item.

    Raises :class:`AmbiguousRegistryNameError` when more than one VIABLE registry carries the
    name (see the module docstring for why that is not a first-match-wins case). Counting
    viable matches only is the whole point of the change — two genuinely usable registries
    sharing a name is still ambiguous and still refuses. The scan is completed rather than
    short-circuited on the first hit precisely so the duplicate can be detected at all — a
    short-circuit cannot see the second one.

    Uses the registered ``list_registries`` paginator and matches on ``item["name"]``; list
    items expose ``registryId``, ``registryArn`` and ``status`` directly.
    """
    matches: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    for page in ctl.get_paginator("list_registries").paginate():
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
                # INFO, not DEBUG: this is the line that explains an otherwise baffling
                # "registry not found" for a name the operator can plainly see in the console.
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
        # The name matched, but nothing usable. Distinguished from plain absence because the
        # remedy is different (delete the broken one and re-run, vs. create one) and because
        # "not found" for a registry the operator can see in the console is the single most
        # confusing message this resolver could emit.
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


def resolve_registry_id(name: str, region: str, *, ctl=None) -> str:
    """Return the registryId of the registry named ``name`` in ``region``.

    Raises :class:`RegistryNotFoundError` when the name matches nothing and
    :class:`AmbiguousRegistryNameError` when it matches more than once. AWS-side failures
    (missing credentials, throttling, a bad region, an endpoint the SDK does not know)
    propagate UNCHANGED — they are transient or environmental, and swallowing them here
    would turn "AWS is briefly unavailable" into "the registry does not exist", sending an
    operator to create a registry that already exists.

    Pass ``ctl`` to inject a client; that keyword is what keeps every caller testable
    offline.
    """
    ctl = ctl if ctl is not None else _client(region)
    rid, _arn = find_registry_by_name(ctl, name)
    if not rid:
        raise RegistryNotFoundError(
            f"No AWS Agent Registry named {name!r} exists in region {region!r}. The "
            "control plane resolves the registry by NAME at first use, so nothing can be "
            "read or written until it exists. Create it with `terraform apply` in "
            "platform/control_plane/infrastructure (it creates both registries), or "
            "directly with `PYTHONPATH=src venv/bin/python scripts/ensure_registry.py "
            f"--name {name} --region {region}`. If the registry lives elsewhere, set "
            "AGENT_REGISTRY_NAME/MCP_REGISTRY_NAME (or pin the id outright with "
            "AGENT_REGISTRY_ID/MCP_REGISTRY_ID)."
        )
    return rid
