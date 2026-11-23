"""Pure Cedar policy text generation and parse-back for gateway authorization.

No boto3, no I/O. ``build_cedar_policy`` turns the friendly form (user oid, label, gateway ARN,
optional tool, effect, parameter conditions) into a Cedar ``permit`` or ``forbid`` statement
carrying an ``// agp:v2`` metadata header; ``parse_cedar_policy`` reads that header back (``agp:v2``
and the legacy ``agp:v1``) so the UI can render the friendly row from the Cedar text the gateway
returns, with no local policy mirror. Two vocabularies: the Cedar statement and header say
``permit`` / ``forbid``; the API, service and UI say ``allow`` / ``deny``; the builder accepts the
API words and the parser returns them.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Literal, Optional, Sequence

_HEADER_PREFIX = "// agp:v1 "
_HEADER_PREFIX_V2 = "// agp:v2 "

PRINCIPAL_OID_TAG = "oid"
"""The Cedar principal tag the gateway matches the Entra ``oid`` claim against; AgentCore surfaces
the claim under this exact name, and renaming it silently default-denies every user, so it is a
named constant with a pinning test."""

PRINCIPAL_AZP_TAG = "azp"
"""The Cedar principal tag carrying the calling agent's Entra app id (the ``azp`` claim). Always
``hasTag``-guarded: a forbid whose ``getTag`` errors is skipped by Cedar, which fails open."""

CedarEffect = Literal["allow", "deny"]

_EFFECT_API_TO_CEDAR: dict[CedarEffect, str] = {"allow": "permit", "deny": "forbid"}
_EFFECT_CEDAR_TO_API: dict[str, CedarEffect] = {"permit": "allow", "forbid": "deny"}

_OP_TO_CEDAR = {"=": "==", "!=": "!=", "<": "<", "<=": "<=", ">": ">", ">=": ">="}
_NUMERIC_OPS = frozenset({"=", "!=", "<", "<=", ">", ">="})
_STRING_OPS = frozenset({"=", "!="})

_PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_FORBIDDEN_IN_INTERPOLATED = ('"', "\\", "\n", "\r")

_HEADER_MARKERS = ("agp:v1", "agp:v2")


def _check_interpolated(value: str, field: str) -> None:
    """Reject values that would corrupt the generated Cedar text or the single-line header.

    ``principal_oid``, ``tool_name``, ``gateway_arn`` and string condition values are interpolated
    verbatim into Cedar string literals and the header, so they must not contain double quotes,
    backslashes or line breaks (a trailing backslash would escape the closing quote and allow
    clause injection). ``principal_oid`` and ``agent_app_id`` are whitespace-tokenized on parse, so
    whitespace would silently truncate them. ``principal_label`` is exempt: it is base64-encoded.
    """
    for ch in _FORBIDDEN_IN_INTERPOLATED:
        if ch in value:
            raise ValueError(
                f"invalid character in {field}: must not contain quotes, backslashes, or newlines"
            )
    if field in ("principal_oid", "agent_app_id") and any(c.isspace() for c in value):
        raise ValueError(f"invalid character in {field}: must not contain whitespace")


def param_types_from_schema(input_schema: Optional[dict]) -> dict[str, str]:
    """Map a tool's top-level JSON-schema ``properties`` to a coarse Cedar-facing type:
    ``integer`` / ``number`` to ``"number"``, ``string`` to ``"string"``, anything else (and a
    property with no or an unknown type) to ``"other"``. An empty or ``None`` schema gives ``{}``."""
    properties = (input_schema or {}).get("properties") or {}
    result: dict[str, str] = {}
    for name, prop in properties.items():
        prop_type = (prop or {}).get("type") if isinstance(prop, dict) else None
        if prop_type in ("integer", "number"):
            result[name] = "number"
        elif prop_type == "string":
            result[name] = "string"
        else:
            result[name] = "other"
    return result


def validate_conditions(conditions: Sequence[dict], input_schema: Optional[dict]) -> list[dict]:
    """Validate and normalise parameter conditions against a tool's input schema.

    For each ``{param, op, value, type}``: ``param`` must be a known top-level property; the
    schema-derived type must be ``number`` or ``string``; ``op`` must be legal for that type; a
    ``number`` value must parse as an integer and a ``string`` value must pass the interpolation
    guard. Returns the conditions with the server-derived type (the client's is overridden), in
    order. Raises ``ValueError`` on any violation.
    """
    schema_types = param_types_from_schema(input_schema)
    normalized: list[dict] = []
    for cond in conditions:
        param = cond["param"]
        op = cond["op"]
        value = cond["value"]
        if param not in schema_types:
            raise ValueError(f"unknown parameter {param!r}")
        derived = schema_types[param]
        if derived not in ("number", "string"):
            raise ValueError(f"unsupported parameter type for {param!r}: {derived}")
        legal_ops = _NUMERIC_OPS if derived == "number" else _STRING_OPS
        if op not in legal_ops:
            raise ValueError(f"operator {op!r} is not legal for parameter {param!r} of type {derived}")
        if derived == "number":
            try:
                int(value)
            except (TypeError, ValueError) as err:
                raise ValueError(
                    f"numeric parameter {param!r} requires an integer value, got {value!r}"
                ) from err
        else:
            _check_interpolated(value, f"condition value for {param}")
        normalized.append({"param": param, "op": op, "value": value, "type": derived})
    return normalized


def _render_condition_clause(cond: dict) -> str:
    """One condition as its ``has``-guarded Cedar clause, re-guarding the parameter name, the
    operator and the value (an integer for ``number``, an interpolation-checked quoted string
    otherwise). Raises ``ValueError`` on any violation."""
    param = cond["param"]
    op = cond["op"]
    value = cond["value"]
    ctype = cond.get("type")
    if not isinstance(param, str) or not _PARAM_NAME_RE.match(param):
        raise ValueError(f"invalid condition parameter name: {param!r}")
    if op not in _OP_TO_CEDAR:
        raise ValueError(f"invalid condition operator: {op!r}")
    cedar_op = _OP_TO_CEDAR[op]
    if ctype == "number":
        try:
            int(value)
        except (TypeError, ValueError) as err:
            raise ValueError(f"numeric condition requires an integer value, got {value!r}") from err
        rendered = str(int(value))
    else:
        _check_interpolated(str(value), f"condition value for {param}")
        rendered = f'"{value}"'
    return f"context.input has {param} && context.input.{param} {cedar_op} {rendered}"


def build_cedar_policy(
    *,
    principal_oid: Optional[str],
    principal_label: str,
    gateway_arn: str,
    tool_name: Optional[str],
    effect: CedarEffect = "allow",
    conditions: Sequence[dict] = (),
) -> str:
    """A Cedar ``permit`` or ``forbid`` statement carrying an ``// agp:v2`` header. Deterministic.

    Raises ``ValueError`` unless ``effect`` is ``allow`` or ``deny``, an allow names a
    ``principal_oid`` (no all-users permit), conditions come with a ``tool_name``, and an all-users
    deny carries at least one condition (it would otherwise block the whole tool or gateway);
    ``gateway_arn``, ``tool_name`` and ``principal_oid`` pass the interpolation guard and every
    condition is re-guarded as rendered. ``tool_name=None`` is all tools (a bare ``action,``
    clause). The conditions are stored as given in the ``cond`` header token; schema validation is
    the caller's (:func:`validate_conditions`).
    """
    if effect not in _EFFECT_API_TO_CEDAR:
        raise ValueError(f"invalid effect {effect!r}: must be 'allow' or 'deny'")
    conds = list(conditions)
    if effect == "allow" and principal_oid is None:
        raise ValueError("an allow policy requires a principal_oid (no all-users permit)")
    if conds and tool_name is None:
        raise ValueError("conditions require a specific tool_name")
    if effect == "deny" and principal_oid is None and not conds:
        raise ValueError("an all-users deny with no conditions would block the whole tool/gateway")

    _check_interpolated(gateway_arn, "gateway_arn")
    if tool_name is not None:
        _check_interpolated(tool_name, "tool_name")
    if principal_oid is not None:
        _check_interpolated(principal_oid, "principal_oid")

    keyword = _EFFECT_API_TO_CEDAR[effect]
    label_b64 = base64.b64encode(principal_label.encode("utf-8")).decode("ascii")
    tool_token = tool_name if tool_name else "*"
    oid_token = principal_oid if principal_oid is not None else "-"
    cond_json = json.dumps(conds, separators=(",", ":"))
    cond_b64 = base64.b64encode(cond_json.encode("utf-8")).decode("ascii")
    header = (
        f"{_HEADER_PREFIX_V2}effect={keyword} oid={oid_token} "
        f"tool={tool_token} label={label_b64} cond={cond_b64}"
    )
    action_line = f'  action == AgentCore::Action::"{tool_name}",\n' if tool_name else "  action,\n"

    clauses: list[str] = []
    if principal_oid is not None:
        clauses.append(
            f'principal.hasTag("{PRINCIPAL_OID_TAG}") '
            f'&& principal.getTag("{PRINCIPAL_OID_TAG}") == "{principal_oid}"'
        )
    for cond in conds:
        clauses.append(_render_condition_clause(cond))

    when_line = ""
    if clauses:
        when_line = f"when {{ {' && '.join(clauses)} }};\n"

    return (
        f"{header}\n"
        f"{keyword}(\n"
        f"  principal is AgentCore::OAuthUser,\n"
        f"{action_line}"
        f'  resource == AgentCore::Gateway::"{gateway_arn}"\n'
        f")\n"
        f"{when_line}"
    )


def build_agent_forbid(*, agent_app_id: str, agent_label: str, gateway_arn: str) -> str:
    """A ``forbid`` on every tool of the gateway for one agent, matched on the ``PRINCIPAL_AZP_TAG``
    principal tag (``hasTag``-guarded first). The header carries ``kind=contain agent=<app id>`` so
    :func:`parse_cedar_policy` tells it from a user policy. Raises ``ValueError`` on an empty app id
    or a bad interpolated value. Deterministic."""
    if not agent_app_id:
        raise ValueError("agent_app_id is required")
    _check_interpolated(agent_app_id, "agent_app_id")
    _check_interpolated(gateway_arn, "gateway_arn")
    label_b64 = base64.b64encode(agent_label.encode("utf-8")).decode("ascii")
    cond_b64 = base64.b64encode(b"[]").decode("ascii")
    header = (
        f"{_HEADER_PREFIX_V2}effect=forbid oid=- tool=* label={label_b64} "
        f"cond={cond_b64} kind=contain agent={agent_app_id}"
    )
    return (
        f"{header}\n"
        f"forbid(\n"
        f"  principal is AgentCore::OAuthUser,\n"
        f"  action,\n"
        f'  resource == AgentCore::Gateway::"{gateway_arn}"\n'
        f")\n"
        f'when {{ principal.hasTag("{PRINCIPAL_AZP_TAG}") '
        f'&& principal.getTag("{PRINCIPAL_AZP_TAG}") == "{agent_app_id}" }};\n'
    )


def detect_effect(statement: str) -> CedarEffect:
    """``deny`` when the statement contains ``forbid(`` or a line whose stripped text starts with
    ``forbid``, else ``allow``. For foreign rows, whose header carries no effect."""
    if "forbid(" in statement:
        return "deny"
    for line in statement.splitlines():
        if line.strip().startswith("forbid"):
            return "deny"
    return "allow"


def _header(statement: str) -> Optional[tuple[str, str]]:
    """The marker version and the remainder of the first ``// agp:vN`` header line, split on any
    run of whitespace so doubled spaces anywhere in the line still parse; ``None`` when the
    statement carries no header."""
    for line in statement.splitlines():
        tokens = line.strip().split()
        if len(tokens) >= 2 and tokens[0] == "//" and tokens[1] in _HEADER_MARKERS:
            return tokens[1], " ".join(tokens[2:])
    return None


def _header_fields(remainder: str) -> dict[str, str]:
    """The ``key=value`` tokens of a header remainder; a repeated key keeps its last value."""
    fields: dict[str, str] = {}
    for token in remainder.split():
        if "=" in token:
            key, _, value = token.partition("=")
            fields[key] = value
    return fields


def _decode_label(value: str) -> str:
    try:
        return base64.b64decode(value.encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return ""


def _decode_conditions(value: Optional[str]) -> list:
    """The ``cond`` token's base64 JSON list; anything malformed is ``[]``, never an error."""
    if not value:
        return []
    try:
        parsed = json.loads(base64.b64decode(value.encode("ascii")).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def parse_cedar_policy(statement: str) -> Optional[dict]:
    """The friendly row of an AGP-generated statement from its header, or ``None`` for a headerless
    or foreign statement (the effect of a foreign row is :func:`detect_effect`'s job).

    Recognises ``// agp:v2`` and the legacy ``// agp:v1``. Returns ``{user_oid, user_label, tool,
    effect, conditions, kind, agent_app_id}``: ``kind`` and ``agent_app_id`` come from the
    ``kind=`` / ``agent=`` tokens of :func:`build_agent_forbid` and are ``None`` for every other
    row; a v1 header is ``effect="allow"`` with no conditions; a v2 header takes the effect from its
    keyword, ``oid=-`` is ``user_oid=None``, a malformed ``cond`` is ``[]`` and a malformed
    ``label`` is ``""``. A header without an ``oid`` token is treated as foreign.
    """
    header = _header(statement)
    if header is None:
        return None
    version, remainder = header
    fields = _header_fields(remainder)

    oid_token = fields.get("oid")
    if oid_token is None:
        return None
    tool_token = fields.get("tool", "*")
    tool = None if tool_token == "*" else tool_token  # noqa: S105 — a Cedar header token, not a credential; expires 2027-10-23; added E49A-50A/T6
    label = _decode_label(fields.get("label", ""))
    kind = fields.get("kind") or None
    agent_app_id = fields.get("agent") or None

    if version == "agp:v1":
        return {
            "user_oid": oid_token,
            "user_label": label,
            "tool": tool,
            "effect": "allow",
            "conditions": [],
            "kind": kind,
            "agent_app_id": agent_app_id,
        }

    user_oid = None if oid_token == "-" else oid_token  # noqa: S105 — a Cedar header token, not a credential; expires 2027-10-23; added E49A-50A/T6
    return {
        "user_oid": user_oid,
        "user_label": label,
        "tool": tool,
        "effect": _EFFECT_CEDAR_TO_API.get(fields.get("effect", "permit"), "allow"),
        "conditions": _decode_conditions(fields.get("cond")),
        "kind": kind,
        "agent_app_id": agent_app_id,
    }
