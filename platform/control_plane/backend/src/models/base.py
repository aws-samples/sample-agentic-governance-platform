"""The base of every Pydantic class that reaches the OpenAPI document.

``WireModel`` is ``BaseModel`` with ``json_schema_serialization_defaults_required`` set, and every
request body, response model and nested wire type under ``models``, the route modules and the
services inherits it; a subclass that sets its own ``model_config`` keeps its entries, because
Pydantic merges a parent's config under the child's. FastAPI renders a response model in
serialization mode and a request body in validation mode, so with this flag the response side
marks every defaulted property required, an ``Optional[...] = None`` field included, while the
request side keeps it optional, and a model used on both sides splits into ``-Input`` and
``-Output`` schemas. The document then says what the wire does: the backend fills every default
before it serializes, so a client reading a response never sees the property absent, while a
client building a request may leave it out.

Held by ``tests/test_openapi_contract.py::test_response_schemas_require_every_property``: every
property of every response schema is in ``required``, so a model that derives from ``BaseModel``
directly fails it, named with the schema and the property it leaves optional.

Decision: docs/adr/frontend-wire-types-are-generated.md
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class WireModel(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
