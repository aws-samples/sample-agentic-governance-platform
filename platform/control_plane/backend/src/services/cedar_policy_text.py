"""Pure Cedar policy text generation + parse-back for E8/E10 gateway authorization.

No boto3 / no I/O. build_cedar_policy() turns the friendly form (user oid + label +
gateway ARN + optional tool + effect + parameter conditions) into a Cedar `permit` or
`forbid` statement carrying an `// agp:v2` metadata header; parse_cedar_policy() reads
that header back (both `agp:v2` AND the legacy `agp:v1`) so the UI can render the friendly
row from the Cedar text the gateway returns (no local policy mirror).

Two vocabularies (pinned): the Cedar statement + header use `permit`/`forbid`; the
API/service/UI use `allow`/`deny`. build_cedar_policy() accepts `effect="allow"|"deny"`
and maps internally; the header stores `effect=permit|forbid`; parse_cedar_policy() returns
the API vocabulary (`effect: "allow"|"deny"`).
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Optional, Sequence

_HEADER_PREFIX = "// agp:v1 "  # legacy E8 marker — parsed forever, NEVER emitted anymore
_HEADER_PREFIX_V2 = "// agp:v2 "  # the new generator emits this

# The Cedar principal tag the gateway matches the Entra `oid` claim against. Pinned: AgentCore
# surfaces the claim under this exact tag name (verified live once, at E8 LOG_ONLY→ENFORCE
# shakeout). Renaming it silently default-denies every user, so it is a named constant with a
# pinning test rather than a literal scattered through the generator.
PRINCIPAL_OID_TAG = "oid"

_EFFECT_API_TO_CEDAR = {"allow": "permit", "deny": "forbid"}
_EFFECT_CEDAR_TO_API = {"permit": "allow", "forbid": "deny"}

_OP_TO_CEDAR = {"=": "==", "!=": "!=", "<": "<", "<=": "<=", ">": ">", ">=": ">="}
_NUMERIC_OPS = frozenset({"=", "!=", "<", "<=", ">", ">="})
_STRING_OPS = frozenset({"=", "!="})

_PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_FORBIDDEN_IN_INTERPOLATED = ('"', "\\", "\n", "\r")


def _check_interpolated(value: str, field: str) -> None:
    """Reject values that would corrupt the generated Cedar text or the single-line header.

    principal_oid, tool_name, gateway_arn, and string condition values are interpolated
    verbatim into Cedar string literals and/or into the header; they must not contain
    double-quotes, backslashes, newlines, or carriage returns (a trailing backslash would
    escape the closing Cedar quote, enabling clause injection). principal_oid is also
    whitespace-tokenized on header parse, so any whitespace character would silently
    truncate it. principal_label is exempt — it is base64-encoded.
    """
    for ch in _FORBIDDEN_IN_INTERPOLATED:
        if ch in value:
            raise ValueError(
                f"invalid character in {field}: must not contain quotes, backslashes, or newlines"
            )
    if field == "principal_oid" and any(c.isspace() for c in value):
        raise ValueError(
            "invalid character in principal_oid: must not contain whitespace"
        )


def param_types_from_schema(input_schema: Optional[dict]) -> dict[str, str]:
    """Map a tool's top-level JSON-schema `properties` to a coarse Cedar-facing type.

    JSON-schema `integer`/`number` → `"number"`; `string` → `"string"`; anything else (and
    a property with no/unknown type) → `"other"`. An empty/None schema → `{}`. Pure.
    """
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


def validate_conditions(
    conditions: Sequence[dict], input_schema: Optional[dict]
) -> list[dict]:
    """Validate + normalize parameter conditions against a tool's input schema (pure).

    For each `{param, op, value, type}`:
      1. `param` must be a known top-level property (else ValueError "unknown parameter …");
      2. the schema-derived type must be `number` or `string`
         (else ValueError "unsupported parameter type …");
      3. `op` must be legal for the derived type (_NUMERIC_OPS / _STRING_OPS), else ValueError;
      4. derived `number` → `int(value)` must parse; derived `string` → value must pass the
         interpolation guard;
      5. returns the NORMALIZED condition `{param, op, value, type:<derived>}` — the
         server-derived type WINS over the client's. Order is preserved.

    Raises ValueError on any violation.
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
            raise ValueError(
                f"operator {op!r} is not legal for parameter {param!r} of type {derived}"
            )
        if derived == "number":
            try:
                int(value)
            except (TypeError, ValueError):
                raise ValueError(
                    f"numeric parameter {param!r} requires an integer value, got {value!r}"
                )
        else:  # string
            _check_interpolated(value, f"condition value for {param}")
        normalized.append({"param": param, "op": op, "value": value, "type": derived})
    return normalized


def _render_condition_clause(cond: dict) -> str:
    """Render one condition to its `has`-guarded Cedar clause. Re-guards param/op/value.

    param via _PARAM_NAME_RE; op via _OP_TO_CEDAR; numeric value via int(value); string
    value via _check_interpolated (rendered quoted). Raises ValueError on any violation.
    """
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
        except (TypeError, ValueError):
            raise ValueError(f"numeric condition requires an integer value, got {value!r}")
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
    effect: str = "allow",
    conditions: Sequence[dict] = (),
) -> str:
    """Generate a Cedar `permit`/`forbid` statement carrying an `// agp:v2` header.

    Preconditions (raise ValueError):
      - `effect ∈ {"allow","deny"}`;
      - `effect == "allow"` ⇒ `principal_oid` is not None (no all-users permit);
      - `conditions` non-empty ⇒ `tool_name` is not None (conditions need a known tool);
      - `effect == "deny"` AND `principal_oid is None` AND no conditions ⇒ rejected (would
        block the whole tool/gateway unconditionally);
      - `_check_interpolated` on gateway_arn, tool_name (when not None), principal_oid (when
        not None); each condition re-guarded as it is rendered.

    The body for a no-condition per-user permit is byte-identical to E8's except the header
    version line. tool_name=None → "All tools" (a bare `action,` clause). Deterministic.

    The builder TRUSTS the caller-provided conditions for rendering (it has no schema) and
    only re-guards param/op/value syntactically; the conditions are stored as-is (param, op,
    value, type) in the `cond` header token. Type/schema validation is the caller's job
    (services.validate_conditions).
    """
    if effect not in _EFFECT_API_TO_CEDAR:
        raise ValueError(f"invalid effect {effect!r}: must be 'allow' or 'deny'")
    conds = list(conditions)
    if effect == "allow" and principal_oid is None:
        raise ValueError("an allow policy requires a principal_oid (no all-users permit)")
    if conds and tool_name is None:
        raise ValueError("conditions require a specific tool_name")
    if effect == "deny" and principal_oid is None and not conds:
        raise ValueError(
            "an all-users deny with no conditions would block the whole tool/gateway"
        )

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
    action_line = (
        f'  action == AgentCore::Action::"{tool_name}",\n' if tool_name else "  action,\n"
    )

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


def detect_effect(statement: str) -> str:
    """Peek the first non-comment Cedar keyword: 'deny' if the statement contains a
    `forbid(` or a line whose stripped text starts with `forbid`, else 'allow'. Pure."""
    if "forbid(" in statement:
        return "deny"
    for line in statement.splitlines():
        if line.strip().startswith("forbid"):
            return "deny"
    return "allow"


def parse_cedar_policy(statement: str) -> Optional[dict]:
    """Parse an AGP-generated statement's header → the friendly row dict, or None for a
    headerless/foreign statement (UNCHANGED contract — do not infer effect for foreign rows;
    that is detect_effect's job).

    Recognizes BOTH `// agp:v2` and the legacy `// agp:v1` markers. Returns:
        {user_oid: <oid|None>, user_label: <str>, tool: <str|None>,
         effect: "allow"|"deny", conditions: [{param, op, value, type}, ...]}

    - v1 header → effect="allow", conditions=[]; oid/tool/label exactly as E8 parsed.
    - v2 header → effect from the keyword; oid token "-" → user_oid=None; cond base64→JSON→
      conditions (malformed cond → [], never raises); label base64-decoded (malformed → "").
    Tolerant of extra whitespace in the header line (base64 label/cond carry no spaces).
    """
    remainder: Optional[str] = None
    version: Optional[str] = None
    for line in statement.splitlines():
        # Split on any run of whitespace so doubled spaces anywhere in the header
        # (including within the `// agp:vN` marker) still parse.
        tokens = line.strip().split()
        if len(tokens) >= 2 and tokens[0] == "//" and tokens[1] in ("agp:v1", "agp:v2"):
            version = tokens[1]
            remainder = " ".join(tokens[2:])
            break
    if remainder is None or version is None:
        return None

    oid_token: Optional[str] = None
    label = ""
    tool_token = "*"
    effect_token = "permit"
    cond_token: Optional[str] = None
    for token in remainder.split():
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        if key == "oid":
            oid_token = value
        elif key == "tool":
            tool_token = value
        elif key == "effect":
            effect_token = value
        elif key == "cond":
            cond_token = value
        elif key == "label":
            try:
                label = base64.b64decode(value.encode("ascii")).decode("utf-8")
            except (binascii.Error, ValueError, UnicodeDecodeError):
                label = ""

    if oid_token is None:
        # header present but malformed → treat as foreign
        return None

    tool = None if tool_token == "*" else tool_token

    if version == "agp:v1":
        return {
            "user_oid": oid_token,
            "user_label": label,
            "tool": tool,
            "effect": "allow",
            "conditions": [],
        }

    # agp:v2
    user_oid = None if oid_token == "-" else oid_token
    effect = _EFFECT_CEDAR_TO_API.get(effect_token, "allow")
    conditions: list = []
    if cond_token:
        try:
            decoded = base64.b64decode(cond_token.encode("ascii")).decode("utf-8")
            parsed_conds = json.loads(decoded)
            if isinstance(parsed_conds, list):
                conditions = parsed_conds
        except (binascii.Error, ValueError, UnicodeDecodeError):
            conditions = []
    return {
        "user_oid": user_oid,
        "user_label": label,
        "tool": tool,
        "effect": effect,
        "conditions": conditions,
    }
