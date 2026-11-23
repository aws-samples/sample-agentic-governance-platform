"""Microsoft Entra and Graph served from this process's memory, below ``GraphService``'s injected client.

``memory_graph_client(graph_base)`` answers an ``httpx.AsyncClient`` over ``MemoryGraphTransport``, which
opens no connection and serves two things. The Entra token endpoint, recognised by a path ending
``/oauth2/v2.0/token`` on any host with the tenant as its first segment, grants ``client_credentials`` to
``LOCAL_BACKEND_APP_ID`` in ``LOCAL_TENANT_ID`` alone and records the opaque token it mints; it refuses
every other grant, the On-Behalf-Of exchange included. Under the path of ``graph_base``, stripped before
routing, it serves exactly the Graph requests ``GraphService`` and ``graph_app_registration`` issue, each
carrying a minted bearer, in Graph's wire shapes: ``{"value": [...]}`` collections, ``{"error": {"code",
"message"}}`` refusals with Graph's statuses and codes, and the messages the adapter matches for a
duplicate identifier URI and an existing consent grant.

Every client shares the process's one ``MemoryDirectory``, built on the first call under a module lock:
the backend application and its principal, the four ``LOCAL_USERS`` and the group ``LOCAL_GROUP_ID``
whose members are the first three, then the applications, service principals, app-role assignments and
consent grants the backend creates. Deleting an application deletes its principals, and deleting a
principal deletes every assignment and grant naming it; the application outlives its principal, as in
Entra. One ``RLock`` guards every request.

The edge: a query in a form the backend does not issue answers 400 ``Request_UnsupportedQuery``, and any
other method and path 501 ``NOT_SERVED_CODE``; both messages name ``GRAPH_IN_MEMORY=true``, so the
adapter raises a ``GraphError`` that says which double refused. A ``$search`` in the backend's form sent
without the ``ConsistencyLevel: eventual`` header answers the same 400 with Graph's own message.

Decision: docs/adr/backend-graph-in-memory-behind-the-adapter.md
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Final, Iterable, NamedTuple
from urllib.parse import parse_qs, urlparse

import httpx

LOCAL_TENANT_ID: Final = "11111111-0000-0000-0000-000000000000"
LOCAL_BACKEND_APP_ID: Final = "11111111-0000-0000-0000-000000000001"
LOCAL_BACKEND_SP_ID: Final = "11111111-0000-0000-0000-000000000002"
LOCAL_GROUP_ID: Final = "11111111-1111-1111-1111-111111111111"
LOCAL_GROUP_NAME: Final = "Local tenant members"


class LocalUser(NamedTuple):
    oid: str
    display_name: str
    mail: str
    member: bool


LOCAL_USERS: Final = (
    LocalUser("11111111-0000-0000-0000-00000000000a", "Local Developer", "dev@example.com", True),
    LocalUser("11111111-0000-0000-0000-00000000000b", "Alex Example", "alex@example.com", True),
    LocalUser("11111111-0000-0000-0000-00000000000c", "Sam Example", "sam@example.com", True),
    LocalUser("11111111-0000-0000-0000-00000000000d", "Jordan Outsider", "jordan@example.com", False),
)
LOCAL_PRINCIPAL: Final = "Alex Example"
LOCAL_CLIENT_CREDENTIAL: Final = "in-memory-directory"
NOT_SERVED_CODE: Final = "Request_NotServedInMemory"

_BACKEND_APP_OBJECT_ID: Final = "11111111-0000-0000-0000-000000000003"
_BACKEND_DISPLAY_NAME: Final = "AGP backend (local)"
_OAUTH2_ENDPOINT_SUFFIX: Final = "/oauth2/v2.0/token"
_TOKEN_LIFETIME_SECONDS: Final = 3599
_QUERY_OPTIONS: Final = frozenset({"$filter", "$search", "$select", "$count"})
_SEARCH_PREFIX: Final = '"displayName:'
_DUPLICATE_URI: Final = "Another object with the same value for property identifierUris already exists."
_DUPLICATE_GRANT: Final = "Permission entry already exists."
_DUPLICATE_ASSIGNMENT: Final = "Permission being assigned already exists on the object"
_SEARCH_NEEDS_EVENTUAL: Final = (
    "Request with $search query parameter only works through MSGraph with a special request header: "
    "'ConsistencyLevel: eventual'"
)

Resource = dict[str, Any]


class MemoryDirectory:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.tokens: set[str] = set()
        self.users: dict[str, Resource] = {
            user.oid: {
                "id": user.oid,
                "displayName": user.display_name,
                "mail": user.mail,
                "userPrincipalName": user.mail,
            }
            for user in LOCAL_USERS
        }
        self.groups: dict[str, Resource] = {
            LOCAL_GROUP_ID: {"id": LOCAL_GROUP_ID, "displayName": LOCAL_GROUP_NAME}
        }
        self.memberships: dict[str, set[str]] = {
            LOCAL_GROUP_ID: {user.oid for user in LOCAL_USERS if user.member}
        }
        self.applications: dict[str, Resource] = {
            _BACKEND_APP_OBJECT_ID: {
                "id": _BACKEND_APP_OBJECT_ID,
                "appId": LOCAL_BACKEND_APP_ID,
                "displayName": _BACKEND_DISPLAY_NAME,
                "appRoles": [],
                "identifierUris": [],
            }
        }
        self.service_principals: dict[str, Resource] = {
            LOCAL_BACKEND_SP_ID: {
                "id": LOCAL_BACKEND_SP_ID,
                "appId": LOCAL_BACKEND_APP_ID,
                "displayName": _BACKEND_DISPLAY_NAME,
                "appRoles": [],
                "appRoleAssignmentRequired": False,
                "servicePrincipalType": "Application",
            }
        }
        self.assignments: dict[str, Resource] = {}
        self.grants: dict[str, Resource] = {}


class _Refusal(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class _Call(NamedTuple):
    path: str
    ids: list[str]
    body: Resource
    keep: Callable[[Resource], bool]
    select: list[str] | None
    count: bool


def _not_found(object_id: str) -> _Refusal:
    return _Refusal(
        404,
        "Request_ResourceNotFound",
        f"Resource '{object_id}' does not exist or one of its queried reference-property objects are "
        "not present.",
    )


def _bad_request(message: str) -> _Refusal:
    return _Refusal(400, "Request_BadRequest", message)


def _unsupported(option: str, value: str, path: str) -> _Refusal:
    return _Refusal(
        400,
        "Request_UnsupportedQuery",
        f"{option}={value} on {path} is not a query the in-memory directory (GRAPH_IN_MEMORY=true) parses; "
        "it parses the queries the backend issues",
    )


def _not_served(method: str, path: str) -> _Refusal:
    return _Refusal(
        501,
        NOT_SERVED_CODE,
        f"{method} {path} is not served by the in-memory directory (GRAPH_IN_MEMORY=true); it serves the "
        "Entra token endpoint and the Graph paths the backend issues",
    )


def _lookup(objects: dict[str, Resource], object_id: str) -> Resource:
    found = objects.get(object_id)
    if found is None:
        raise _not_found(object_id)
    return found


def _new_id() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _literal(text: str, start: int) -> tuple[str, int] | None:
    if not text.startswith("'", start):
        return None
    chars: list[str] = []
    position = start + 1
    while position < len(text):
        if text.startswith("''", position):
            chars.append("'")
            position += 2
        elif text[position] == "'":
            return "".join(chars), position + 1
        else:
            chars.append(text[position])
            position += 1
    return None


def _literals(text: str, prefix: str, *after_each: str) -> list[str] | None:
    if not text.startswith(prefix):
        return None
    position = len(prefix)
    values: list[str] = []
    for fixed in after_each:
        literal = _literal(text, position)
        if literal is None:
            return None
        value, position = literal
        if not text.startswith(fixed, position):
            return None
        position += len(fixed)
        values.append(value)
    return values if position == len(text) else None


def _filter(collection: str, text: str) -> Callable[[Resource], bool] | None:
    app_id = _literals(text, "appId eq ", "") if collection in ("applications", "servicePrincipals") else None
    if app_id is not None:
        return lambda obj: obj.get("appId") == app_id[0]
    uri = _literals(text, "identifierUris/any(u:u eq ", ")") if collection == "applications" else None
    if uri is not None:
        return lambda obj: uri[0] in obj.get("identifierUris", [])
    address = _literals(text, "mail eq ", " or userPrincipalName eq ", "") if collection == "users" else None
    if address is not None:
        mail, upn = (value.casefold() for value in address)
        return lambda obj: (
            str(obj.get("mail") or "").casefold() == mail
            or str(obj.get("userPrincipalName") or "").casefold() == upn
        )
    return None


def _search(collection: str, text: str) -> Callable[[Resource], bool] | None:
    if collection not in ("users", "groups") or not (text.startswith(_SEARCH_PREFIX) and text.endswith('"')):
        return None
    needle = text[len(_SEARCH_PREFIX) : -1]
    if '"' in needle:
        return None
    return lambda obj: needle.casefold() in str(obj.get("displayName") or "").casefold()


def _query(path: str, collection: str, options: dict[str, str], consistency: str | None) -> list[Callable]:
    unknown = sorted(set(options) - _QUERY_OPTIONS)
    if unknown:
        raise _unsupported(unknown[0], options[unknown[0]], path)
    predicates: list[Callable] = []
    for option, parse in (("$filter", _filter), ("$search", _search)):
        if option in options:
            predicate = parse(collection, options[option])
            if predicate is None:
                raise _unsupported(option, options[option], path)
            predicates.append(predicate)
    if "$search" in options and consistency != "eventual":
        raise _Refusal(400, "Request_UnsupportedQuery", _SEARCH_NEEDS_EVENTUAL)
    return predicates


def _body(content: bytes) -> Resource:
    if not content:
        return {}
    try:
        body = json.loads(content)
    except ValueError:
        raise _bad_request("Invalid JSON in the request body.") from None
    if not isinstance(body, dict):
        raise _bad_request("The request body must be a JSON object.")
    return body


def _project(obj: Resource, select: list[str] | None) -> Resource:
    if select is None:
        return obj
    return {key: obj[key] for key in ("id", *select) if key in obj}


def _collection(objects: Iterable[Resource], call: _Call) -> httpx.Response:
    value = [_project(obj, call.select) for obj in objects if call.keep(obj)]
    answer: Resource = {"value": value}
    if call.count:
        answer["@odata.count"] = len(value)
    return httpx.Response(200, json=answer)


def _token_refusal(status: int, error: str, code: int) -> httpx.Response:
    return httpx.Response(status, json={"error": error, "error_codes": [code]})


class MemoryGraphTransport(httpx.AsyncBaseTransport):
    def __init__(self, directory: MemoryDirectory, *, graph_path_prefix: str) -> None:
        self._directory = directory
        self._prefix = graph_path_prefix

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        content = await request.aread()
        with self._directory.lock:
            try:
                return self._handle(request, content)
            except _Refusal as refusal:
                return httpx.Response(
                    refusal.status, json={"error": {"code": refusal.code, "message": refusal.message}}
                )

    def _handle(self, request: httpx.Request, content: bytes) -> httpx.Response:
        method, path = request.method, request.url.path
        if method == "POST" and path.endswith(_OAUTH2_ENDPOINT_SUFFIX):
            return self._token(path, content)
        if not path.startswith(f"{self._prefix}/"):
            raise _not_served(method, path)
        self._authenticate(request.headers.get("authorization", ""))
        route = path[len(self._prefix) :]
        segments = tuple(route.strip("/").split("/"))
        handler, ids = _match(method, route, segments)
        collection = segments[0] if method == "GET" and len(segments) == 1 else ""
        options = dict(request.url.params.items())
        predicates = _query(route, collection, options, request.headers.get("consistencylevel"))
        selected = options.get("$select")
        call = _Call(
            path=route,
            ids=ids,
            body=_body(content),
            keep=lambda obj: all(predicate(obj) for predicate in predicates),
            select=[field.strip() for field in selected.split(",")] if selected is not None else None,
            count=options.get("$count") == "true",
        )
        return handler(self, call)

    def _token(self, path: str, content: bytes) -> httpx.Response:
        form = {key: values[0] for key, values in parse_qs(content.decode(errors="replace")).items()}
        if path.strip("/").split("/")[0] != LOCAL_TENANT_ID:
            return _token_refusal(400, "invalid_request", 90002)
        if form.get("grant_type") != "client_credentials":
            return _token_refusal(400, "unsupported_grant_type", 70003)
        if form.get("client_id") != LOCAL_BACKEND_APP_ID:
            return _token_refusal(401, "invalid_client", 700016)
        minted = uuid.uuid4().hex
        self._directory.tokens.add(minted)
        return httpx.Response(
            200, json={"token_type": "Bearer", "expires_in": _TOKEN_LIFETIME_SECONDS, "access_token": minted}
        )

    def _authenticate(self, authorization: str) -> None:
        if not authorization:
            raise _Refusal(401, "InvalidAuthenticationToken", "Access token is empty.")
        scheme, _, presented = authorization.partition(" ")
        if scheme != "Bearer" or presented not in self._directory.tokens:
            raise _Refusal(401, "InvalidAuthenticationToken", "Access token validation failure.")

    def _drop_principal(self, sp_id: str) -> None:
        directory = self._directory
        del directory.service_principals[sp_id]
        for key in [
            k for k, a in directory.assignments.items() if sp_id in (a["resourceId"], a["principalId"])
        ]:
            del directory.assignments[key]
        for key in [k for k, g in directory.grants.items() if sp_id in (g["clientId"], g["resourceId"])]:
            del directory.grants[key]

    def _principal_of(self, principal_id: str) -> tuple[str, Resource]:
        directory = self._directory
        for principal_type, objects in (
            ("User", directory.users),
            ("Group", directory.groups),
            ("ServicePrincipal", directory.service_principals),
        ):
            if principal_id in objects:
                return principal_type, objects[principal_id]
        raise _not_found(principal_id)

    def _create_application(self, call: _Call) -> httpx.Response:
        taken = {
            uri for app in self._directory.applications.values() for uri in app.get("identifierUris", [])
        }
        if taken.intersection(call.body.get("identifierUris") or []):
            raise _bad_request(_DUPLICATE_URI)
        application = {**call.body, "id": _new_id(), "appId": _new_id()}
        self._directory.applications[application["id"]] = application
        return httpx.Response(201, json=application)

    def _list_applications(self, call: _Call) -> httpx.Response:
        return _collection(self._directory.applications.values(), call)

    def _delete_application(self, call: _Call) -> httpx.Response:
        application = _lookup(self._directory.applications, call.ids[0])
        del self._directory.applications[application["id"]]
        for sp in [
            sp for sp in self._directory.service_principals.values() if sp["appId"] == application["appId"]
        ]:
            self._drop_principal(sp["id"])
        return httpx.Response(204)

    def _add_password(self, call: _Call) -> httpx.Response:
        _lookup(self._directory.applications, call.ids[0])
        credential = call.body.get("passwordCredential") or {}
        return httpx.Response(
            200,
            json={
                "secretText": uuid.uuid4().hex + uuid.uuid4().hex,
                "keyId": _new_id(),
                "displayName": credential.get("displayName"),
            },
        )

    def _create_principal(self, call: _Call) -> httpx.Response:
        app_id = call.body.get("appId")
        application = next((a for a in self._directory.applications.values() if a["appId"] == app_id), None)
        if application is None:
            raise _bad_request(
                f"The appId '{app_id}' of the service principal does not reference a valid application "
                "object."
            )
        if any(sp["appId"] == app_id for sp in self._directory.service_principals.values()):
            raise _bad_request(
                "The service principal cannot be created, updated, or restored because the service principal "
                f"name {app_id} is already in use."
            )
        principal = {
            "id": _new_id(),
            "appId": app_id,
            "displayName": application.get("displayName"),
            "appRoles": [dict(role) for role in application.get("appRoles", [])],
            "appRoleAssignmentRequired": False,
            "servicePrincipalType": "Application",
        }
        self._directory.service_principals[principal["id"]] = principal
        return httpx.Response(201, json=principal)

    def _list_principals(self, call: _Call) -> httpx.Response:
        return _collection(self._directory.service_principals.values(), call)

    def _patch_principal(self, call: _Call) -> httpx.Response:
        principal = _lookup(self._directory.service_principals, call.ids[0])
        if set(call.body) - {"appRoleAssignmentRequired"}:
            raise _not_served("PATCH", call.path)
        if "appRoleAssignmentRequired" in call.body:
            principal["appRoleAssignmentRequired"] = bool(call.body["appRoleAssignmentRequired"])
        return httpx.Response(204)

    def _delete_principal(self, call: _Call) -> httpx.Response:
        self._drop_principal(_lookup(self._directory.service_principals, call.ids[0])["id"])
        return httpx.Response(204)

    def _list_assigned_to(self, call: _Call) -> httpx.Response:
        sp_id = _lookup(self._directory.service_principals, call.ids[0])["id"]
        return _collection(
            (a for a in self._directory.assignments.values() if a["resourceId"] == sp_id), call
        )

    def _assign(self, call: _Call) -> httpx.Response:
        resource = _lookup(self._directory.service_principals, call.ids[0])
        if call.body.get("resourceId") != resource["id"]:
            raise _bad_request("The resourceId of the assignment must be the service principal of the path.")
        principal_id = str(call.body.get("principalId") or "")
        principal_type, principal = self._principal_of(principal_id)
        role_id = call.body.get("appRoleId")
        role = next(
            (r for r in resource["appRoles"] if r["id"] == role_id and r.get("isEnabled", True)), None
        )
        if role is None:
            raise _bad_request(
                f"Permission being assigned was not found on application '{resource['appId']}'."
            )
        member_type = "Application" if principal_type == "ServicePrincipal" else "User"
        if member_type not in role.get("allowedMemberTypes", []):
            raise _bad_request(
                f"The app role '{role.get('value')}' cannot be assigned to a {principal_type}."
            )
        if any(
            (a["principalId"], a["resourceId"], a["appRoleId"]) == (principal_id, resource["id"], role_id)
            for a in self._directory.assignments.values()
        ):
            raise _bad_request(_DUPLICATE_ASSIGNMENT)
        assignment = {
            "id": _new_id(),
            "principalId": principal_id,
            "principalDisplayName": principal.get("displayName"),
            "principalType": principal_type,
            "resourceId": resource["id"],
            "resourceDisplayName": resource.get("displayName"),
            "appRoleId": role_id,
            "createdDateTime": _now(),
            "deletedDateTime": None,
        }
        self._directory.assignments[assignment["id"]] = assignment
        return httpx.Response(201, json=assignment)

    def _unassign(self, call: _Call) -> httpx.Response:
        sp_id = _lookup(self._directory.service_principals, call.ids[0])["id"]
        assignment = self._directory.assignments.get(call.ids[1])
        if assignment is None or assignment["resourceId"] != sp_id:
            raise _not_found(call.ids[1])
        del self._directory.assignments[call.ids[1]]
        return httpx.Response(204)

    def _list_assignments_of(self, call: _Call) -> httpx.Response:
        sp_id = _lookup(self._directory.service_principals, call.ids[0])["id"]
        return _collection(
            (a for a in self._directory.assignments.values() if a["principalId"] == sp_id), call
        )

    def _list_principal_grants(self, call: _Call) -> httpx.Response:
        sp_id = _lookup(self._directory.service_principals, call.ids[0])["id"]
        return _collection((g for g in self._directory.grants.values() if g["clientId"] == sp_id), call)

    def _create_grant(self, call: _Call) -> httpx.Response:
        client_id, resource_id = call.body.get("clientId"), call.body.get("resourceId")
        for object_id in (client_id, resource_id):
            _lookup(self._directory.service_principals, str(object_id or ""))
        if any(
            (g["clientId"], g["resourceId"]) == (client_id, resource_id)
            for g in self._directory.grants.values()
        ):
            raise _bad_request(_DUPLICATE_GRANT)
        grant = {**call.body, "id": _new_id()}
        self._directory.grants[grant["id"]] = grant
        return httpx.Response(201, json=grant)

    def _delete_grant(self, call: _Call) -> httpx.Response:
        _lookup(self._directory.grants, call.ids[0])
        del self._directory.grants[call.ids[0]]
        return httpx.Response(204)

    def _list_users(self, call: _Call) -> httpx.Response:
        return _collection(self._directory.users.values(), call)

    def _get_user(self, call: _Call) -> httpx.Response:
        return httpx.Response(200, json=_project(_lookup(self._directory.users, call.ids[0]), call.select))

    def _user_groups(self, call: _Call) -> httpx.Response:
        oid = _lookup(self._directory.users, call.ids[0])["id"]
        memberships = self._directory.memberships
        return _collection(
            (g for g in self._directory.groups.values() if oid in memberships.get(g["id"], ())), call
        )

    def _list_groups(self, call: _Call) -> httpx.Response:
        return _collection(self._directory.groups.values(), call)

    def _get_group(self, call: _Call) -> httpx.Response:
        return httpx.Response(200, json=_project(_lookup(self._directory.groups, call.ids[0]), call.select))


_Handler = Callable[[MemoryGraphTransport, _Call], httpx.Response]

_ROUTES: Final[dict[tuple[str, tuple[str, ...]], _Handler]] = {
    ("POST", ("applications",)): MemoryGraphTransport._create_application,
    ("GET", ("applications",)): MemoryGraphTransport._list_applications,
    ("DELETE", ("applications", "*")): MemoryGraphTransport._delete_application,
    ("POST", ("applications", "*", "addPassword")): MemoryGraphTransport._add_password,
    ("POST", ("servicePrincipals",)): MemoryGraphTransport._create_principal,
    ("GET", ("servicePrincipals",)): MemoryGraphTransport._list_principals,
    ("PATCH", ("servicePrincipals", "*")): MemoryGraphTransport._patch_principal,
    ("DELETE", ("servicePrincipals", "*")): MemoryGraphTransport._delete_principal,
    ("GET", ("servicePrincipals", "*", "appRoleAssignedTo")): MemoryGraphTransport._list_assigned_to,
    ("POST", ("servicePrincipals", "*", "appRoleAssignedTo")): MemoryGraphTransport._assign,
    ("DELETE", ("servicePrincipals", "*", "appRoleAssignedTo", "*")): MemoryGraphTransport._unassign,
    ("GET", ("servicePrincipals", "*", "appRoleAssignments")): MemoryGraphTransport._list_assignments_of,
    (
        "GET",
        ("servicePrincipals", "*", "oauth2PermissionGrants"),
    ): MemoryGraphTransport._list_principal_grants,
    ("POST", ("oauth2PermissionGrants",)): MemoryGraphTransport._create_grant,
    ("DELETE", ("oauth2PermissionGrants", "*")): MemoryGraphTransport._delete_grant,
    ("GET", ("users",)): MemoryGraphTransport._list_users,
    ("GET", ("users", "*")): MemoryGraphTransport._get_user,
    ("GET", ("users", "*", "transitiveMemberOf", "microsoft.graph.group")): MemoryGraphTransport._user_groups,
    ("GET", ("groups",)): MemoryGraphTransport._list_groups,
    ("GET", ("groups", "*")): MemoryGraphTransport._get_group,
}


def _match(method: str, path: str, segments: tuple[str, ...]) -> tuple[_Handler, list[str]]:
    for (verb, pattern), handler in _ROUTES.items():
        if verb != method or len(pattern) != len(segments):
            continue
        pairs = list(zip(pattern, segments, strict=True))
        if all(fixed in ("*", segment) for fixed, segment in pairs):
            return handler, [segment for fixed, segment in pairs if fixed == "*"]
    raise _not_served(method, path)


_DIRECTORY: MemoryDirectory | None = None
_DIRECTORY_LOCK = threading.Lock()


def memory_graph_client(graph_base: str) -> httpx.AsyncClient:
    global _DIRECTORY
    with _DIRECTORY_LOCK:
        if _DIRECTORY is None:
            _DIRECTORY = MemoryDirectory()
        directory = _DIRECTORY
    prefix = urlparse(graph_base).path.rstrip("/")
    return httpx.AsyncClient(transport=MemoryGraphTransport(directory, graph_path_prefix=prefix))
