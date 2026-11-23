"""The OpenAPI document the backend serves is a reviewed snapshot, and the frontend's wire types
are generated from it.

What this pins. ``tests/fixtures/openapi.json`` is ``app.openapi()`` with the prose removed
(``description``, ``summary`` and ``title`` wherever the value is a string; a property that is
itself named ``description`` keeps its schema), sorted, two-space indented, regenerated only on
purpose with ``AGP_UPDATE_OPENAPI_SNAPSHOT=1``, so every path, schema, required set, enum and
response declaration that changes is a diff a reviewer reads in the merge request. The root path
is empty under the suite, because ``main.py`` registers every router a second time under
``ROOT_PATH`` and the snapshot carries one route set. Two checks then hold the document to the
wire's truth about defaults: every property of every schema reachable from a 2xx response is in
``required``, because the backend fills every default before it serializes, and a schema reachable
only from a request body keeps its defaulted properties optional, because a client may leave them
out; ``models.base.WireModel`` is what makes both true, and a class that derives from ``BaseModel``
directly fails the first by name. The second is also judged from the models: each schema a request
body reaches is mapped to its model through the routes' body parameters and requires only what
the model requires, which covers a ``default_factory`` field, whose schema carries no ``default``,
and a ``json_schema_mode_override`` that merges a model's two sides into one schema.

Where the frontend half lives. The snapshot is the generator's input: ``frontend/src/api/openapi.ts``
is ``openapi-typescript``'s output over this file, and ``frontend/src/api/openapi.test.ts`` holds
the file to the generator byte for byte and asserts that ``client.ts`` declares no interface and
no literal union; ``tsc -b`` is the rest of the enforcer.

Evidence. ``6a37fd0c`` added ``Repository.last_build_started_at`` to the backend model without
touching ``client.ts``; ``a5848f78`` caught up by hand. ``f2bff6eb`` dropped ``list_tenants``'s
response model, a weakening of the contract no review saw. Both would have been reviewed diffs of
this snapshot.
"""

import difflib
import json
import os
import re
from itertools import islice
from pathlib import Path
from typing import get_args

import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import AliasChoices, BaseModel, RootModel
from pydantic.fields import FieldInfo

SNAPSHOT = Path(__file__).parent / "fixtures/openapi.json"
UPDATE_ENV = "AGP_UPDATE_OPENAPI_SNAPSHOT"
PROSE_KEYS = ("description", "summary", "title")
REGENERATE = (
    f"Intended? Regenerate with {UPDATE_ENV}=1 ./venv/bin/python -m pytest "
    "tests/test_openapi_contract.py, review the diff in the merge request, then run "
    "npm run generate:api in platform/control_plane/frontend"
)


def strip_prose(node):
    if isinstance(node, dict):
        return {
            key: strip_prose(value)
            for key, value in node.items()
            if not (key in PROSE_KEYS and isinstance(value, str))
        }
    if isinstance(node, list):
        return [strip_prose(value) for value in node]
    return node


def prose_paths(node, at: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in PROSE_KEYS and isinstance(value, str):
                found.append(f"{at}/{key}")
            found.extend(prose_paths(value, f"{at}/{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(prose_paths(value, f"{at}/{index}"))
    return found


def openapi_document() -> dict:
    from main import app

    document = strip_prose(app.openapi())
    assert isinstance(document, dict)
    return document


def dump(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True, indent=2) + "\n"


def _schema_refs(node) -> set[str]:
    found: set[str] = set()

    def walk(value) -> None:
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                found.add(ref.rsplit("/", 1)[1])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return found


def _reachable(doc: dict, roots: set[str]) -> set[str]:
    schemas = doc["components"]["schemas"]
    reached: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached.add(name)
        pending.extend(_schema_refs(schemas[name]))
    return reached


def _operations(doc: dict):
    for path_item in doc["paths"].values():
        for operation in path_item.values():
            if isinstance(operation, dict) and "responses" in operation:
                yield operation


def response_schemas(doc: dict) -> set[str]:
    roots: set[str] = set()
    for operation in _operations(doc):
        for status, response in operation["responses"].items():
            if status.startswith("2"):
                roots |= _schema_refs(response)
    return _reachable(doc, roots)


def request_only_schemas(doc: dict) -> set[str]:
    requested: set[str] = set()
    responded: set[str] = set()
    for operation in _operations(doc):
        requested |= _schema_refs(operation.get("requestBody", {}))
        responded |= _schema_refs(operation["responses"])
    return _reachable(doc, requested) - _reachable(doc, responded)


def _defaulted_properties(doc: dict, name: str) -> list[str]:
    schema = doc["components"]["schemas"][name]
    members = set(schema.get("required") or [])
    return [
        f"{name}.{prop}"
        for prop, value in sorted((schema.get("properties") or {}).items())
        if isinstance(value, dict) and "default" in value and prop in members
    ]


def test_openapi_document_matches_snapshot():
    expected = dump(openapi_document())
    if os.environ.get(UPDATE_ENV) == "1":
        SNAPSHOT.write_text(expected)
    assert SNAPSHOT.is_file(), f"{SNAPSHOT.name} is missing. {REGENERATE}"
    actual = SNAPSHOT.read_text()
    if actual == expected:
        return
    diff = difflib.unified_diff(
        actual.splitlines(),
        expected.splitlines(),
        "tests/fixtures/openapi.json",
        "app.openapi()",
        n=2,
        lineterm="",
    )
    pytest.fail("\n".join(islice(diff, 40)) + f"\n{REGENERATE}")


def test_root_path_is_empty_in_the_test_environment():
    from core.config import settings

    assert settings.ROOT_PATH == "", (
        f"ROOT_PATH is {settings.ROOT_PATH!r}: main.py registers every router a second time under "
        "it, so the document would carry a doubled route set. Unset it (a developer's .env) before "
        "running the suite; the snapshot is generated with no root path."
    )


def test_snapshot_is_prose_free_sorted_two_space_with_trailing_newline():
    text = SNAPSHOT.read_text()
    doc = json.loads(text)
    assert dump(doc) == text, (
        "tests/fixtures/openapi.json is not json.dumps(sort_keys=True, indent=2) plus a trailing "
        f"newline. {REGENERATE}"
    )
    assert prose_paths(doc) == [], (
        f"prose survives in the snapshot at {prose_paths(doc)[:10]}; openapi_document() removes "
        f"{PROSE_KEYS} wherever the value is a string. {REGENERATE}"
    )


def test_response_schemas_require_every_property():
    doc = openapi_document()
    schemas = doc["components"]["schemas"]
    optional: list[str] = []
    for name in sorted(response_schemas(doc)):
        required = set(schemas[name].get("required") or [])
        optional += [
            f"response schema {name} leaves {prop} optional; its model derives from BaseModel directly: "
            "inherit models.base.WireModel"
            for prop in sorted(schemas[name].get("properties") or {})
            if prop not in required
        ]
    assert optional == [], "\n".join(optional)


def test_request_schemas_keep_their_defaulted_properties_optional():
    doc = openapi_document()
    required = [
        found for name in sorted(request_only_schemas(doc)) for found in _defaulted_properties(doc, name)
    ]
    assert required == [], (
        "a request-only schema requires a property it defaults: "
        + ", ".join(required)
        + ". A payload that omits it is valid, so the validation-mode schema keeps it optional; "
        "models.base.WireModel only tightens the serialization side."
    )


def _models(annotation) -> set[type[BaseModel]]:
    found = {annotation} if isinstance(annotation, type) and issubclass(annotation, BaseModel) else set()
    for argument in get_args(annotation):
        found |= _models(argument)
    return found


def _base_name(schema: str) -> str:
    return schema.removesuffix("-Input").removesuffix("-Output").rsplit("__", 1)[-1]


def _request_key(name: str, field: FieldInfo) -> str:
    alias = field.validation_alias
    if isinstance(alias, AliasChoices):
        paths = alias.convert_to_aliases()
        return next((path[0] for path in paths if len(path) == 1 and isinstance(path[0], str)), name)
    return alias if isinstance(alias, str) else field.alias or name


def _pair(refs: set[str], models: set[type[BaseModel]]) -> tuple[list[tuple[str, type[BaseModel]]], set[str]]:
    if len(refs) == 1 and len(models) == 1:
        return [(next(iter(refs)), next(iter(models)))], set()
    pairs = [
        (ref, model)
        for ref in refs
        for model in models
        if re.sub(r"[^a-zA-Z0-9.\-_]", "_", model.__name__) == _base_name(ref)
    ]
    return pairs, refs - {ref for ref, _ in pairs}


def request_models(doc: dict) -> tuple[dict[str, set[type[BaseModel]]], set[str]]:
    from main import app

    schemas = doc["components"]["schemas"]
    pending: list[tuple[str, type[BaseModel]]] = []
    unpaired: set[str] = set()
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if not isinstance(route, APIRoute) or not route.dependant.body_params or route.body_field is None:
            continue
        operations = doc["paths"].get(context.path_format, {})
        for method in context.methods or ():
            body = operations.get(method.lower(), {}).get("requestBody", {})
            pairs, missed = _pair(_schema_refs(body), _models(route.body_field.field_info.annotation))
            pending += pairs
            unpaired |= missed
    mapped: dict[str, set[type[BaseModel]]] = {}
    while pending:
        name, model = pending.pop()
        if model in mapped.setdefault(name, set()):
            continue
        mapped[name].add(model)
        properties = schemas[name].get("properties") or {}
        for field_name, field in model.model_fields.items():
            node = (
                schemas[name]
                if issubclass(model, RootModel)
                else properties.get(_request_key(field_name, field), {})
            )
            pairs, missed = _pair(_schema_refs(node), _models(field.annotation))
            pending += pairs
            unpaired |= missed
    return mapped, unpaired


def test_request_schemas_require_only_the_models_required_fields():
    doc = openapi_document()
    schemas = doc["components"]["schemas"]
    mapped, unpaired = request_models(doc)
    reached = {
        name
        for name in request_only_schemas(doc) | unpaired
        if schemas[name].get("properties") or schemas[name].get("required") or _schema_refs(schemas[name])
    }
    findings: list[str] = []
    for name in sorted(reached | set(mapped)):
        if name not in mapped:
            findings.append(f"{name} maps to no model through the routes' body parameters")
            continue
        required = set(schemas[name].get("required") or [])
        for model in sorted(mapped[name], key=lambda model: f"{model.__module__}.{model.__qualname__}"):
            fields = {
                _request_key(field_name, field): field for field_name, field in model.model_fields.items()
            }
            findings += [
                f"{name}.{prop} is required in the schema, but {model.__module__}.{model.__qualname__} "
                + ("has no such field" if prop not in fields else "does not require it")
                for prop in sorted(required)
                if prop not in fields or not fields[prop].is_required()
            ]
    assert findings == [], (
        "a request schema requires what its model does not: "
        + "; ".join(findings)
        + ". A payload that omits the field is valid, so the request schema keeps it optional; "
        "a json_schema_mode_override or a hand-written json_schema_extra is the usual cause."
    )


CLOSED_WIRE_PROPERTIES = (
    ("DriftEntry", "kind"),
    ("PrincipalHit", "type"),
    ("ReconcileItemView", "origin"),
    ("ReconcileItemView", "state"),
    ("DiscoveredAgents", "platform"),
    ("MarketplaceMetrics", "by_type"),
    ("MarketplaceMetrics", "top_products"),
    ("GuardrailMetrics", "time_series"),
)


def _is_closed(schemas: dict, node: dict) -> bool:
    ref = node.get("$ref")
    if isinstance(ref, str):
        node = schemas[ref.rsplit("/", 1)[1]]
    if "enum" in node:
        return True
    properties = set(node.get("properties") or {})
    return (
        bool(properties)
        and set(node.get("required") or []) == properties
        and "additionalProperties" not in node
    )


@pytest.mark.parametrize(
    ("schema", "prop"),
    CLOSED_WIRE_PROPERTIES,
    ids=[f"{schema}.{prop}" for schema, prop in CLOSED_WIRE_PROPERTIES],
)
def test_the_snapshot_closes_every_wire_string_the_backend_computes(schema, prop):
    schemas = json.loads(SNAPSHOT.read_text())["components"]["schemas"]
    node = schemas[schema]["properties"][prop]
    node = node.get("items", node)
    assert _is_closed(schemas, node), (
        f"{schema}.{prop} is open in the snapshot ({node}): type it with a Literal, an enum or a "
        f"WireModel whose fields are all required. {REGENERATE}"
    )


def test_the_snapshot_closes_the_metrics_scope_query():
    operation = json.loads(SNAPSHOT.read_text())["paths"]["/api/v1/observability/metrics"]["get"]
    (scope,) = [parameter["schema"] for parameter in operation["parameters"] if parameter["name"] == "scope"]
    assert scope["enum"] == ["platform", "tenant", "project"]
