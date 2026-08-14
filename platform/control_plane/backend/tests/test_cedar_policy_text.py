"""Unit tests for the pure Cedar policy text generator + parser (Epic 8, Task T2).

``cedar_policy_text`` is a pure string helper (no boto3, no I/O): ``build_cedar_policy``
turns the friendly form (user oid + label + gateway ARN + optional tool) into a Cedar
``permit`` statement carrying an ``// agp:v1`` metadata header, and ``parse_cedar_policy``
reads that header back so the UI can render the friendly row from the Cedar text the
gateway returns (no local policy mirror).
"""

from __future__ import annotations

from services.cedar_policy_text import (
    PRINCIPAL_OID_TAG,
    build_cedar_policy,
    detect_effect,
    param_types_from_schema,
    parse_cedar_policy,
    validate_conditions,
)


def test_build_specific_tool_has_action_clause():
    """A specific tool emits an ``action == AgentCore::Action::"<tool>"`` clause plus the
    principal / resource / oid-tag constraints."""
    out = build_cedar_policy(
        principal_oid="eb3da",
        principal_label="lars@x",
        gateway_arn="arn:gw",
        tool_name="T___get_claim",
    )
    assert 'action == AgentCore::Action::"T___get_claim"' in out
    assert "principal is AgentCore::OAuthUser" in out
    assert 'resource == AgentCore::Gateway::"arn:gw"' in out
    assert 'principal.getTag("oid") == "eb3da"' in out


def test_build_all_tools_omits_action_constraint():
    """``tool_name=None`` (All tools) → a bare ``action,`` line and NO Action constraint,
    while still carrying the resource + oid-tag clauses."""
    out = build_cedar_policy(
        principal_oid="eb3da",
        principal_label="lars@x",
        gateway_arn="arn:gw",
        tool_name=None,
    )
    assert "AgentCore::Action::" not in out
    # a bare `action,` line exists
    assert any(line.strip() == "action," for line in out.splitlines())
    assert 'resource == AgentCore::Gateway::"arn:gw"' in out
    assert 'principal.getTag("oid") == "eb3da"' in out


def test_build_includes_agp_header():
    """The first line is the ``// agp:v2`` header carrying effect / oid / tool / base64 label."""
    out = build_cedar_policy(
        principal_oid="eb3da",
        principal_label="lars@x",
        gateway_arn="arn:gw",
        tool_name="T___get_claim",
    )
    header = out.splitlines()[0]
    assert header.startswith("// agp:v2 ")
    assert "effect=permit" in header
    assert "oid=eb3da" in header
    assert "tool=T___get_claim" in header
    assert "label=" in header
    label_token = next(t for t in header.split() if t.startswith("label="))
    assert label_token != "label="  # a base64 token follows

    all_tools = build_cedar_policy(
        principal_oid="eb3da",
        principal_label="lars@x",
        gateway_arn="arn:gw",
        tool_name=None,
    )
    assert "tool=*" in all_tools.splitlines()[0]


def test_build_label_is_base64_round_trip():
    """A label with spaces / ``@`` / ``,`` / ``<>`` round-trips through build → parse."""
    label = "Lars Svensson <lars@x>, EU"
    out = build_cedar_policy(
        principal_oid="eb3da",
        principal_label=label,
        gateway_arn="arn:gw",
        tool_name="T___get_claim",
    )
    parsed = parse_cedar_policy(out)
    assert parsed is not None
    assert parsed["user_label"] == label


def test_parse_round_trip_specific_tool():
    """A built specific-tool policy parses back to the friendly fields."""
    out = build_cedar_policy(
        principal_oid="eb3da",
        principal_label="lars@x",
        gateway_arn="arn:gw",
        tool_name="T___get_claim",
    )
    assert parse_cedar_policy(out) == {
        "user_oid": "eb3da",
        "user_label": "lars@x",
        "tool": "T___get_claim",
        "effect": "allow",
        "conditions": [],
    }


def test_parse_round_trip_all_tools():
    """A built all-tools policy parses back with ``tool is None`` (oid + label intact)."""
    out = build_cedar_policy(
        principal_oid="eb3da",
        principal_label="lars@x",
        gateway_arn="arn:gw",
        tool_name=None,
    )
    parsed = parse_cedar_policy(out)
    assert parsed is not None
    assert parsed["tool"] is None
    assert parsed["user_oid"] == "eb3da"
    assert parsed["user_label"] == "lars@x"


def test_parse_returns_none_for_foreign_policy():
    """A raw, hand-authored Cedar statement with NO ``// agp:v1`` header → None."""
    foreign = "permit( principal, action, resource );\n"
    assert parse_cedar_policy(foreign) is None


def test_parse_tolerates_extra_whitespace():
    """A header line with extra (double) spaces between tokens still parses correctly."""
    out = build_cedar_policy(
        principal_oid="eb3da",
        principal_label="lars@x",
        gateway_arn="arn:gw",
        tool_name="T___get_claim",
    )
    header, _, body = out.partition("\n")
    spaced_header = header.replace(" ", "  ")
    statement = f"{spaced_header}\n{body}"
    parsed = parse_cedar_policy(statement)
    assert parsed is not None
    assert parsed["user_oid"] == "eb3da"
    assert parsed["tool"] == "T___get_claim"
    assert parsed["user_label"] == "lars@x"


def test_build_is_deterministic():
    """Same inputs twice → byte-identical output (no timestamps / randomness)."""
    kwargs = dict(
        principal_oid="eb3da",
        principal_label="lars@x",
        gateway_arn="arn:gw",
        tool_name="T___get_claim",
    )
    assert build_cedar_policy(**kwargs) == build_cedar_policy(**kwargs)


def test_build_rejects_quote_or_newline_in_interpolated_fields():
    """principal_oid / tool_name / gateway_arn containing ``"`` / newline / space → ValueError.
    principal_label is exempt (base64-encoded) and must still be accepted."""
    import pytest

    # (a) tool_name with a double-quote
    with pytest.raises(ValueError):
        build_cedar_policy(
            principal_oid="eb3da",
            principal_label="lars@x",
            gateway_arn="arn:gw",
            tool_name='bad"tool',
        )

    # (b) gateway_arn with a double-quote
    with pytest.raises(ValueError):
        build_cedar_policy(
            principal_oid="eb3da",
            principal_label="lars@x",
            gateway_arn='arn:"bad"',
            tool_name="T___get_claim",
        )

    # (c) principal_oid with a space (whitespace breaks header tokenization)
    with pytest.raises(ValueError):
        build_cedar_policy(
            principal_oid="eb 3da",
            principal_label="lars@x",
            gateway_arn="arn:gw",
            tool_name="T___get_claim",
        )

    # (d) principal_oid with a newline
    with pytest.raises(ValueError):
        build_cedar_policy(
            principal_oid="eb3da\nevil",
            principal_label="lars@x",
            gateway_arn="arn:gw",
            tool_name="T___get_claim",
        )

    # Normal call must still succeed.
    out = build_cedar_policy(
        principal_oid="eb3da",
        principal_label="lars@x",
        gateway_arn="arn:gw",
        tool_name="T___get_claim",
    )
    assert out  # non-empty

    # principal_label with spaces / @ / , / quotes is ACCEPTED (base64-protected).
    out2 = build_cedar_policy(
        principal_oid="eb3da",
        principal_label='Lars "The Builder" Svensson <lars@x>, EU',
        gateway_arn="arn:gw",
        tool_name="T___get_claim",
    )
    assert out2  # must not raise


def test_parse_malformed_base64_label_yields_empty_label():
    """A corrupted ``label=`` token produces ``user_label == ""`` — never None, never a raise."""
    out = build_cedar_policy(
        principal_oid="eb3da",
        principal_label="lars@x",
        gateway_arn="arn:gw",
        tool_name="T___get_claim",
    )
    # Corrupt the label= token to non-base64 garbage.
    corrupted = "\n".join(
        "label=@@@notb64@@@" if token.startswith("label=") else token
        for token in out.replace(" ", "\n").split("\n")
    )
    # Reconstruct as a single-line header by un-splitting on spaces won't work here;
    # easier: just replace the label token directly in the header line.
    lines = out.splitlines()
    header_tokens = lines[0].split()
    header_tokens = [
        "label=@@@notb64@@@" if t.startswith("label=") else t
        for t in header_tokens
    ]
    lines[0] = " ".join(header_tokens)
    corrupted_statement = "\n".join(lines)

    parsed = parse_cedar_policy(corrupted_statement)
    assert parsed is not None
    assert parsed["user_oid"] == "eb3da"
    assert parsed["user_label"] == ""


# --- E10: v2 generator/parser + conditions + deny -----------------------------


def test_build_v2_permit_no_conditions_matches_e8_body():
    """A per-user, specific-tool, no-condition build emits the v2 header but the E8 body."""
    out = build_cedar_policy(
        principal_oid="eb3da",
        principal_label="lars@x",
        gateway_arn="arn:gw",
        tool_name="T___get_claim",
        effect="allow",
        conditions=[],
    )
    header = out.splitlines()[0]
    assert header.startswith("// agp:v2 effect=permit")
    assert "permit(" in out
    assert 'action == AgentCore::Action::"T___get_claim"' in out
    assert 'resource == AgentCore::Gateway::"arn:gw"' in out
    assert 'principal.getTag("oid") == "eb3da"' in out


def test_build_deny_emits_forbid():
    """``effect="deny"`` → the policy keyword is ``forbid(`` and the header records ``forbid``."""
    out = build_cedar_policy(
        principal_oid="eb3da",
        principal_label="lars@x",
        gateway_arn="arn:gw",
        tool_name="T___get_claim",
        effect="deny",
    )
    assert "forbid(" in out
    assert "effect=forbid" in out.splitlines()[0]


def test_build_numeric_condition_renders_has_guard_and_bare_value():
    """A numeric condition emits the ``has`` guard and renders the integer value bare."""
    out = build_cedar_policy(
        principal_oid="eb3da",
        principal_label="lars@x",
        gateway_arn="arn:gw",
        tool_name="transfer",
        effect="allow",
        conditions=[{"param": "amount", "op": "<", "value": "1000", "type": "number"}],
    )
    assert "context.input has amount && context.input.amount < 1000" in out


def test_build_string_condition_renders_quoted_value():
    """A string condition renders the value as a quoted Cedar literal."""
    out = build_cedar_policy(
        principal_oid="eb3da",
        principal_label="lars@x",
        gateway_arn="arn:gw",
        tool_name="get_client",
        effect="allow",
        conditions=[{"param": "client_id", "op": "=", "value": "id1", "type": "string"}],
    )
    assert (
        'context.input has client_id && context.input.client_id == "id1"' in out
    )


def test_build_multiple_conditions_anded():
    """Two conditions both appear joined by `` && ``, with the oid clause present and first."""
    out = build_cedar_policy(
        principal_oid="eb3da",
        principal_label="lars@x",
        gateway_arn="arn:gw",
        tool_name="transfer",
        effect="allow",
        conditions=[
            {"param": "amount", "op": "<", "value": "1000", "type": "number"},
            {"param": "client_id", "op": "=", "value": "id1", "type": "string"},
        ],
    )
    when_line = next(line for line in out.splitlines() if line.strip().startswith("when {"))
    oid_idx = when_line.index('principal.getTag("oid") == "eb3da"')
    amount_idx = when_line.index("context.input.amount < 1000")
    client_idx = when_line.index('context.input.client_id == "id1"')
    assert oid_idx < amount_idx < client_idx
    assert " && " in when_line


def test_build_all_users_forbid_omits_principal_clause():
    """An all-users forbid (principal_oid=None) omits the oid clause; header ``oid=-``."""
    out = build_cedar_policy(
        principal_oid=None,
        principal_label="Everyone",
        gateway_arn="arn:gw",
        tool_name="transfer",
        effect="deny",
        conditions=[{"param": "amount", "op": ">", "value": "10000", "type": "number"}],
    )
    assert "forbid(" in out
    assert 'principal.getTag("oid")' not in out
    assert "oid=-" in out.splitlines()[0]
    assert "context.input has amount && context.input.amount > 10000" in out


def test_build_allow_without_oid_raises():
    """An allow with no principal_oid is rejected (no all-users permit)."""
    import pytest

    with pytest.raises(ValueError):
        build_cedar_policy(
            principal_oid=None,
            principal_label="Everyone",
            gateway_arn="arn:gw",
            tool_name="transfer",
            effect="allow",
        )


def test_build_conditions_require_tool_raises():
    """Conditions with no tool_name are rejected (conditions need a known tool)."""
    import pytest

    with pytest.raises(ValueError):
        build_cedar_policy(
            principal_oid="eb3da",
            principal_label="lars@x",
            gateway_arn="arn:gw",
            tool_name=None,
            effect="allow",
            conditions=[{"param": "amount", "op": "<", "value": "1000", "type": "number"}],
        )


def test_build_unconditional_all_users_deny_raises():
    """A deny with no user AND no conditions would block everything → rejected."""
    import pytest

    with pytest.raises(ValueError):
        build_cedar_policy(
            principal_oid=None,
            principal_label="Everyone",
            gateway_arn="arn:gw",
            tool_name="transfer",
            effect="deny",
            conditions=[],
        )


def test_build_rejects_bad_param_name():
    """A condition param failing the identifier charset is rejected."""
    import pytest

    with pytest.raises(ValueError):
        build_cedar_policy(
            principal_oid="eb3da",
            principal_label="lars@x",
            gateway_arn="arn:gw",
            tool_name="transfer",
            effect="allow",
            conditions=[{"param": "bad-name", "op": "<", "value": "1000", "type": "number"}],
        )


def test_build_rejects_quote_in_string_condition_value():
    """A string condition value containing a double-quote is rejected (injection guard)."""
    import pytest

    with pytest.raises(ValueError):
        build_cedar_policy(
            principal_oid="eb3da",
            principal_label="lars@x",
            gateway_arn="arn:gw",
            tool_name="get_client",
            effect="allow",
            conditions=[{"param": "client_id", "op": "=", "value": 'ev"il', "type": "string"}],
        )


def test_build_rejects_non_integer_numeric_value():
    """A numeric condition value that is not an integer is rejected."""
    import pytest

    with pytest.raises(ValueError):
        build_cedar_policy(
            principal_oid="eb3da",
            principal_label="lars@x",
            gateway_arn="arn:gw",
            tool_name="transfer",
            effect="allow",
            conditions=[{"param": "amount", "op": "<", "value": "1.5", "type": "number"}],
        )


def test_parse_v2_round_trip_conditions():
    """A v2 policy with a numeric + a string condition round-trips through build → parse."""
    conds = [
        {"param": "amount", "op": "<", "value": "1000", "type": "number"},
        {"param": "client_id", "op": "=", "value": "id1", "type": "string"},
    ]
    out = build_cedar_policy(
        principal_oid="eb3da",
        principal_label="lars@x",
        gateway_arn="arn:gw",
        tool_name="transfer",
        effect="allow",
        conditions=conds,
    )
    parsed = parse_cedar_policy(out)
    assert parsed is not None
    assert parsed["effect"] == "allow"
    assert parsed["user_oid"] == "eb3da"
    assert parsed["tool"] == "transfer"
    assert parsed["conditions"] == conds


def test_parse_v2_all_users_forbid():
    """An all-users v2 forbid parses → user_oid None, effect deny, label Everyone."""
    conds = [{"param": "amount", "op": ">", "value": "10000", "type": "number"}]
    out = build_cedar_policy(
        principal_oid=None,
        principal_label="Everyone",
        gateway_arn="arn:gw",
        tool_name="transfer",
        effect="deny",
        conditions=conds,
    )
    parsed = parse_cedar_policy(out)
    assert parsed is not None
    assert parsed["user_oid"] is None
    assert parsed["effect"] == "deny"
    assert parsed["user_label"] == "Everyone"
    assert parsed["conditions"] == conds


def test_parse_v1_backcompat_is_allow_no_conditions():
    """A live (hardcoded) v1 statement parses → effect allow, conditions empty."""
    v1 = (
        "// agp:v1 oid=eb3da tool=T___get_claim label=bGFyc0B4\n"
        "permit(\n  principal is AgentCore::OAuthUser,\n"
        '  action == AgentCore::Action::"T___get_claim",\n'
        '  resource == AgentCore::Gateway::"arn:gw"\n)\n'
        'when { principal.hasTag("oid") && principal.getTag("oid") == "eb3da" };\n'
    )
    assert parse_cedar_policy(v1) == {
        "user_oid": "eb3da",
        "user_label": "lars@x",
        "tool": "T___get_claim",
        "effect": "allow",
        "conditions": [],
    }


def test_parse_foreign_still_none():
    """A headerless foreign forbid statement → None (UNCHANGED contract)."""
    foreign = "forbid( principal, action, resource );\n"
    assert parse_cedar_policy(foreign) is None


def test_detect_effect():
    """detect_effect peeks the Cedar keyword: forbid → deny, permit → allow."""
    assert detect_effect("forbid(\n  principal,\n  action,\n  resource\n);\n") == "deny"
    assert detect_effect("permit(\n  principal,\n  action,\n  resource\n);\n") == "allow"


def test_param_types_from_schema():
    """Top-level properties map to number/string/other; empty or None schema → {}."""
    schema = {
        "properties": {
            "amount": {"type": "number"},
            "client_id": {"type": "string"},
            "meta": {"type": "object"},
        }
    }
    assert param_types_from_schema(schema) == {
        "amount": "number",
        "client_id": "string",
        "meta": "other",
    }
    assert param_types_from_schema({}) == {}
    assert param_types_from_schema(None) == {}


def test_validate_conditions_unknown_param_raises():
    """A condition on a param absent from the schema is rejected."""
    import pytest

    schema = {"properties": {"amount": {"type": "number"}}}
    with pytest.raises(ValueError):
        validate_conditions(
            [{"param": "nope", "op": "<", "value": "1", "type": "number"}], schema
        )


def test_validate_conditions_type_mismatch_raises():
    """An operator illegal for the schema-derived type (`<` on a string) is rejected."""
    import pytest

    schema = {"properties": {"client_id": {"type": "string"}}}
    with pytest.raises(ValueError):
        validate_conditions(
            [{"param": "client_id", "op": "<", "value": "1", "type": "string"}], schema
        )


def test_validate_conditions_non_integer_numeric_raises():
    """A non-integer numeric value is rejected during validation."""
    import pytest

    schema = {"properties": {"amount": {"type": "number"}}}
    with pytest.raises(ValueError):
        validate_conditions(
            [{"param": "amount", "op": "<", "value": "1.5", "type": "number"}], schema
        )


def test_validate_conditions_normalizes_type_from_schema():
    """The server-derived type wins over the client's; an illegal op then raises."""
    import pytest

    schema = {"properties": {"amount": {"type": "number"}}}
    # Client lies that amount is a string, but with a legal numeric op it normalizes.
    normalized = validate_conditions(
        [{"param": "amount", "op": "<", "value": "1000", "type": "string"}], schema
    )
    assert normalized == [
        {"param": "amount", "op": "<", "value": "1000", "type": "number"}
    ]
    # A string-only op combined with the schema's number type would still be legal for
    # `=`/`!=`, so force an op that is illegal once normalized to number? `<` is legal for
    # number; instead prove the inverse: a numeric op on a schema-string param raises.
    str_schema = {"properties": {"client_id": {"type": "string"}}}
    with pytest.raises(ValueError):
        validate_conditions(
            [{"param": "client_id", "op": ">", "value": "1", "type": "number"}], str_schema
        )


def test_build_rejects_backslash_in_string_condition_value():
    """A string condition value ending in a backslash is rejected (would escape the Cedar
    closing quote and allow clause injection)."""
    import pytest

    # "id1\\" is the 4-char Python string ending in one backslash.
    with pytest.raises(ValueError):
        build_cedar_policy(
            principal_oid="eb3da",
            principal_label="x",
            gateway_arn="arn:gw",
            tool_name="T",
            effect="allow",
            conditions=[{"param": "client_id", "op": "=", "value": "id1\\", "type": "string"}],
        )


def test_build_rejects_backslash_in_gateway_arn():
    """A backslash in gateway_arn is rejected by the shared interpolation guard."""
    import pytest

    with pytest.raises(ValueError):
        build_cedar_policy(
            principal_oid="eb3da",
            principal_label="x",
            gateway_arn="arn:\\bad",
            tool_name="T",
        )


def test_principal_oid_tag_is_pinned_to_oid():
    """The Cedar principal tag name is the constant ``"oid"`` and the generator interpolates it.

    AgentCore surfaces the Entra ``oid`` claim under this exact tag name (verified live once, at
    the E8 LOG_ONLY→ENFORCE shakeout; not provable offline). A rename would silently
    default-deny every user, so both the constant and the emitted text are pinned here.
    """
    assert PRINCIPAL_OID_TAG == "oid"
    out = build_cedar_policy(
        principal_oid="eb3da",
        principal_label="lars@x",
        gateway_arn="arn:gw",
        tool_name="T___get_claim",
    )
    assert 'principal.hasTag("oid")' in out
    assert 'principal.getTag("oid")' in out
    assert f'principal.getTag("{PRINCIPAL_OID_TAG}")' in out
