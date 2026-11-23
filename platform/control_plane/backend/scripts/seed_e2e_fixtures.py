#!/usr/bin/env python
"""Seed and tear down the records the verify-agp write suite needs, through the product's own API.

HTTP only: the standard library plus ``httpx``, no import from ``src``, so the script runs against any
target. ``--api <url>`` names the API; auth comes from the environment: ``AGP_AUTH=dev-headers`` sends
``x-user-email: dev@example.com``, ``AGP_AUTH=bearer`` sends ``Authorization: Bearer $AGP_BEARER``,
anything else exits 2.

    seed_e2e_fixtures.py seed --run <id> [--tenant [--agent] [--project]] --api <url>
    seed_e2e_fixtures.py teardown --prefix <p> [--dry-run] --api <url>

A run's records are named ``<run>-<noun>``, the run id being ``e2e-<stamp>``: a run id outside it is
refused before any call, exit 2. ``seed`` creates what the target lacks: the AWS
tenant ``<run>-tenant`` with one dev stage on the allowlisted fake account id and no deploy role, so the
service makes no AWS call; with ``--agent`` the metadata-only agent ``<run>-agent`` and with
``--project`` the empty project ``<run>-project``, both under that tenant, so neither stands without
``--tenant``. It prints one ``created <kind> <name> <id>`` per record; an existing record of that name is
reused and reported the same way, so a second run creates nothing.

``teardown`` lists repos, projects, agents, MCP servers, guardrails and tenants, in that dependency
order, and deletes every record whose name starts with the prefix through the product's DELETE routes
(a repo through its delete-preview first). The prefix is the run id or the run id and ``-``; anything else
(empty, a bare ``e2e-``) is refused before any call, exit 2, because it would select records the run did
not create. A name that does not start with the prefix, or that is one of
``AGP_BASELINE_REPO``, ``AGP_SUITE_AGENT``, ``AGP_SUITE_PROJECT`` and ``AGP_SUITE_REPOS``, is ``refuse``;
``--dry-run`` turns every delete into ``skip``. Each deleted id is read back and must answer 404. A list
that does not answer 200 is one ``skip`` line naming its path, and the teardown goes on with the next
kind; a request that gets no answer at all (the API down, a timeout) ends the teardown with one
``skip teardown <prefix> stopped: <error>`` line after the decisions already made. One
``<action> <kind> <name> <reason>`` line per decision. Exit 0; 1 when a delete left its record in place, a
list failed or the teardown stopped; 2 on usage.

``agp seed`` / ``agp teardown`` and the write suite import this module by path and call ``seed`` and
``teardown`` with their own ``ApiClient``.
"""

import argparse
import functools
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

FAKE_ACCOUNT_ID = "111122223333"
FAKE_GROUP_ID = "11111111-1111-1111-1111-111111111111"
API = "/api/v1"
DEV_HEADERS = {"x-user-email": "dev@example.com"}
PROTECTED_VARIABLES = ("AGP_BASELINE_REPO", "AGP_SUITE_AGENT", "AGP_SUITE_PROJECT", "AGP_SUITE_REPOS")
RUN = re.compile(r"e2e-[A-Za-z0-9][A-Za-z0-9._-]*")
PREFIX = RUN
REPO_DELETE_ITEMS = frozenset({"record", "github", "image", "runtime", "identity"})
COLLECTIONS = (
    ("agent", f"{API}/agents"),
    ("mcp-server", f"{API}/mcp-servers"),
    ("guardrail", f"{API}/guardrails"),
    ("tenant", f"{API}/admin/tenants"),
)


class ApiClient(Protocol):
    def request(self, method: str, path: str, json: Any | None = None) -> tuple[int, Any]: ...


class HttpxApi(ApiClient):
    def __init__(
        self, base_url: str, headers: dict[str, str], transport: httpx.BaseTransport | None = None
    ) -> None:
        self._client = httpx.Client(base_url=base_url, headers=headers, transport=transport, timeout=30)

    def request(self, method: str, path: str, json: Any | None = None) -> tuple[int, Any]:
        response = self._client.request(method, path, json=json)
        if not response.content:
            return response.status_code, None
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, response.text


@dataclass
class Created:
    kind: str
    name: str
    id: str


@dataclass
class Decision:
    action: Literal["delete", "skip", "refuse"]
    kind: str
    name: str
    reason: str
    ok: bool


def tenant_body(run: str) -> dict[str, Any]:
    return {
        "name": f"{run}-tenant",
        "line_of_business": "e2e",
        "entra_group_ids": [FAKE_GROUP_ID],
        "platform": "aws",
        "stages": {"dev": {"account_id": FAKE_ACCOUNT_ID, "region": "us-east-1"}},
    }


def run_error(run: str) -> str | None:
    if RUN.fullmatch(run):
        return None
    return f"run id must start with e2e- and carry only letters, digits, '.', '_' and '-', got {run!r}"


def seed_error(run: str, *, tenant: bool, agent: bool, project: bool) -> str | None:
    error = run_error(run)
    if error is None and (agent or project) and not tenant:
        return "--agent and --project need --tenant"
    return error


def seed(
    api: ApiClient, run: str, *, tenant: bool, agent: bool = False, project: bool = False
) -> list[Created]:
    error = seed_error(run, tenant=tenant, agent=agent, project=project)
    if error:
        raise ValueError(error)
    if not tenant:
        return []
    created = [_ensure(api, "tenant", f"{API}/admin/tenants", tenant_body(run))]
    tenant_id = created[0].id
    if agent:
        body = {
            "name": f"{run}-agent",
            "purpose": "verify-agp proof",
            "tenant_id": tenant_id,
            "platform": "aws_bedrock",
        }
        created.append(_ensure(api, "agent", f"{API}/agents", body))
    if project:
        body = {
            "name": f"{run}-project",
            "connection_id": "",
            "tenant_id": tenant_id,
            "description": "verify-agp proof",
        }
        created.append(_ensure(api, "project", f"{API}/projects", body))
    return created


def _ensure(api: ApiClient, kind: str, collection: str, body: dict[str, Any]) -> Created:
    existing = [r for r in _listed(api, collection) if r.get("name") == body["name"]]
    if existing:
        return Created(kind, body["name"], existing[0]["id"])
    status, record = api.request("POST", collection, json=body)
    if status != 201:
        raise RuntimeError(f"POST {collection} answered {status}: {record}")
    return Created(kind, body["name"], record["id"])


def prefix_error(prefix: str) -> str | None:
    if PREFIX.fullmatch(prefix):
        return None
    return f"--prefix must be the run id (e2e-<stamp>) or e2e-<stamp>-, got {prefix!r}"


def teardown(api: ApiClient, prefix: str, *, dry_run: bool, protected: frozenset[str]) -> list[Decision]:
    """Decide and delete over every record named ``<prefix>…``; ``prefix`` is the run id or the run id
    and ``-`` (``PREFIX``), anything else raises ``ValueError`` before a single request."""
    error = prefix_error(prefix)
    if error:
        raise ValueError(error)
    decide = functools.partial(_decide, api, prefix=prefix, protected=protected, dry_run=dry_run)
    decisions: list[Decision] = []
    read = functools.partial(_read, api, decisions)
    try:
        for project in read("project", f"{API}/projects") or ():
            project_path = f"{API}/projects/{project['id']}"
            detail = read("repo", project_path)
            if detail is None:
                continue
            for repo in detail["repositories"]:
                repo_path = f"{project_path}/repos/{repo['id']}"
                delete = functools.partial(_delete_repo, api, repo_path)
                decisions.append(decide("repo", repo["name"], f"{repo_path}/status", delete))
            delete = functools.partial(_delete, api, project_path)
            decisions.append(decide("project", project["name"], project_path, delete))
        for kind, collection in COLLECTIONS:
            for record in read(kind, collection) or ():
                path = f"{collection}/{record['id']}"
                decisions.append(decide(kind, record["name"], path, functools.partial(_delete, api, path)))
    except httpx.HTTPError as error:
        reason = f"stopped: {type(error).__name__}: {error}"
        decisions.append(Decision("skip", "teardown", prefix, reason, False))
    return decisions


def _decide(
    api: ApiClient,
    kind: str,
    name: str,
    read_path: str,
    delete: Callable[[], int],
    *,
    prefix: str,
    protected: frozenset[str],
    dry_run: bool,
) -> Decision:
    if not name.startswith(prefix):
        return Decision("refuse", kind, name, f"name does not start with {prefix}", True)
    if name in protected:
        return Decision("refuse", kind, name, "protected", True)
    if dry_run:
        return Decision("skip", kind, name, "dry-run", True)
    status = delete()
    gone = api.request("GET", read_path)[0] == 404
    reason = "deleted" if gone else f"delete answered {status}; still present"
    return Decision("delete", kind, name, reason, gone)


def _listed(api: ApiClient, path: str) -> list[dict[str, Any]]:
    status, body = api.request("GET", path)
    if status != 200:
        raise RuntimeError(f"GET {path} answered {status}: {body}")
    return body


def _read(api: ApiClient, decisions: list[Decision], kind: str, path: str) -> Any:
    status, body = api.request("GET", path)
    if status != 200:
        decisions.append(Decision("skip", kind, path, f"list failed: {status}", False))
        return None
    return body


def _delete(api: ApiClient, path: str) -> int:
    return api.request("DELETE", path)[0]


def _delete_repo(api: ApiClient, repo_path: str) -> int:
    status, preview = api.request("GET", f"{repo_path}/delete-preview")
    selection = None
    if status == 200:
        selection = {
            item["item"]: item["state"] != "gone"
            for item in preview["items"]
            if item["item"] in REPO_DELETE_ITEMS
        }
    return api.request("DELETE", repo_path, json=selection)[0]


def auth_headers(env: Mapping[str, str]) -> dict[str, str] | None:
    mode = env.get("AGP_AUTH", "")
    if mode == "dev-headers":
        return dict(DEV_HEADERS)
    if mode == "bearer" and env.get("AGP_BEARER"):
        return {"Authorization": f"Bearer {env['AGP_BEARER']}"}
    return None


def protected_names(env: Mapping[str, str]) -> frozenset[str]:
    names: set[str] = set()
    for variable in PROTECTED_VARIABLES:
        names.update(env.get(variable, "").split())
    return frozenset(names)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="seed_e2e_fixtures.py",
        description="Seed and tear down verify-agp fixtures through the AGP API.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    seed_parser = commands.add_parser("seed", help="create the records a run needs and the target lacks")
    seed_parser.add_argument(
        "--run", required=True, help="the run id e2e-<stamp>; records are named <run>-<noun>"
    )
    seed_parser.add_argument("--tenant", action="store_true", help="create the AWS tenant <run>-tenant")
    seed_parser.add_argument(
        "--agent", action="store_true", help="create the agent <run>-agent; needs --tenant"
    )
    seed_parser.add_argument(
        "--project", action="store_true", help="create the project <run>-project; needs --tenant"
    )
    teardown_parser = commands.add_parser(
        "teardown", help="delete every record whose name starts with the prefix"
    )
    teardown_parser.add_argument("--prefix", required=True, help="the name prefix: <run> or <run>-")
    teardown_parser.add_argument("--dry-run", action="store_true", help="decide and print, delete nothing")
    for command in (seed_parser, teardown_parser):
        command.add_argument("--api", required=True, help="the API base URL (the harness passes $AGP_API)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "teardown":
        error = prefix_error(args.prefix)
    else:
        error = seed_error(args.run, tenant=args.tenant, agent=args.agent, project=args.project)
    if error:
        print(error, file=sys.stderr)
        return 2
    headers = auth_headers(os.environ)
    if headers is None:
        print("set AGP_AUTH=dev-headers, or AGP_AUTH=bearer with AGP_BEARER", file=sys.stderr)
        return 2
    api = HttpxApi(args.api, headers)
    try:
        if args.command == "seed":
            for record in seed(api, args.run, tenant=args.tenant, agent=args.agent, project=args.project):
                print(f"created {record.kind} {record.name} {record.id}")
            return 0
        decisions = teardown(api, args.prefix, dry_run=args.dry_run, protected=protected_names(os.environ))
    except (RuntimeError, httpx.HTTPError) as err:
        print(f"{args.command} failed: {type(err).__name__}: {err}", file=sys.stderr)
        return 1
    for decision in decisions:
        print(f"{decision.action} {decision.kind} {decision.name} {decision.reason}")
    return 0 if all(decision.ok for decision in decisions) else 1


if __name__ == "__main__":
    sys.exit(main())
