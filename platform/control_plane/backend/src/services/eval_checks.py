"""Deterministic eval checks: tool names, tool arguments, no-error.

Pure functions, no LLM, no I/O. Each check returns a ``Score`` (``kind="check"``, value 1.0/0.0)
or ``None`` when it is not scored: the mode is ``off``, or the item expects no tools (args: no
expected tool carries ``args``). ``actual is None`` means no tool-call source produced data — that
is an errored score (``passed=False``, ``value=None``), not a pass. ``actual == []`` is real data:
the agent called nothing.

``explanation`` names the first mismatch so the Evaluations tab can show it as-is.

Tool names are compared with :func:`tool_name_matches`: the runtime namespaces gateway tools as
``<mcp_label>__<target>___<op>`` (``claims_walk__api___getPolicy``), while a dataset names them
``api___getPolicy`` or just ``getPolicy``.
"""

from __future__ import annotations

from models.eval import (
    ArgsMatch,
    ChecksConfig,
    DatasetItem,
    ExpectedTool,
    ItemResult,
    Score,
    ToolCall,
    ToolMatch,
)

NO_DATA = "no tool-call data"


def tool_name_matches(expected: str, actual: str) -> bool:
    """``actual`` is ``expected``, or ``expected`` behind the runtime's MCP prefix: ``actual``
    ends with ``"__" + expected``, or (``expected`` has no ``__``) with ``"___" + expected``."""
    if actual == expected or actual.endswith("__" + expected):
        return True
    return "__" not in expected and actual.endswith("___" + expected)


def _score(evaluator: str, passed: bool, explanation: str) -> Score:
    return Score(
        evaluator=evaluator,
        kind="check",
        value=1.0 if passed else 0.0,
        passed=passed,
        explanation=explanation,
    )


def _no_data(evaluator: str) -> Score:
    return Score(evaluator=evaluator, kind="check", value=None, passed=False, error=NO_DATA)


def _where(names: list[str], i: int) -> str:
    return "first" if i == 0 else f"after {names[i - 1]}"


def _exact_mismatch(exp: list[str], act: list[str]) -> str | None:
    for i in range(max(len(exp), len(act))):
        if i >= len(act):
            return f"expected {exp[i]} {_where(exp, i)}"
        if i >= len(exp):
            return f"unexpected {act[i]} {_where(exp, i)}"
        if not tool_name_matches(exp[i], act[i]):
            return f"expected {exp[i]} {_where(exp, i)}, got {act[i]}"
    return None


def _in_order_mismatch(exp: list[str], act: list[str]) -> str | None:
    """One iterator over the actual calls: each expected name consumes the calls up to and including
    its match, so a name found behind an earlier match counts as missing."""
    rest = iter(act)
    for i, name in enumerate(exp):
        if not any(tool_name_matches(name, a) for a in rest):
            return f"expected {name} {_where(exp, i)}"
    return None


def _any_order_mismatch(exp: list[str], act: list[str]) -> str | None:
    """Every expected name takes its own actual call, the first unused match, so a repeated
    expectation needs a repeated call."""
    used: set[int] = set()
    missing: dict[str, int] = {}
    for name in exp:
        idx = next((i for i, a in enumerate(act) if i not in used and tool_name_matches(name, a)), None)
        if idx is None:
            missing[name] = missing.get(name, 0) + 1
        else:
            used.add(idx)
    if missing:
        name, count = next(iter(missing.items()))
        return f"expected {name} (missing {count})"
    return None


def _first_tools_mismatch(exp: list[str], act: list[str], mode: ToolMatch) -> str | None:
    if mode == "exact":
        return _exact_mismatch(exp, act)
    if mode == "in_order":
        return _in_order_mismatch(exp, act)
    return _any_order_mismatch(exp, act)


def check_tools(expected: list[ExpectedTool], actual: list[ToolCall] | None, mode: ToolMatch) -> Score | None:
    if mode == "off" or not expected:
        return None
    if actual is None:
        return _no_data("tools")
    exp = [t.name for t in expected]
    act = [c.name for c in actual]
    got = f"got [{', '.join(act)}]"
    mismatch = _first_tools_mismatch(exp, act, mode)
    if mismatch:
        return _score("tools", False, f"{mismatch}; {got}")
    return _score("tools", True, f"tools match ({mode}); {got}")


def _args_mismatch(name: str, want: dict, got: dict, mode: ArgsMatch) -> str | None:
    if mode == "exact":
        return None if got == want else f"{name}: expected args {want!r}, got {got!r}"
    for key, value in want.items():
        if key not in got:
            return f"{name}.{key}: expected {value!r}, got nothing"
        if got[key] != value:
            return f"{name}.{key}: expected {value!r}, got {got[key]!r}"
    return None


def check_args(expected: list[ExpectedTool], actual: list[ToolCall] | None, mode: ArgsMatch) -> Score | None:
    """Each expected tool takes the first unused actual call with the same name, in order, even when
    it carries no ``args``, so a later same-name expectation lines up with its own call. An expected
    tool without a call is the tools check's finding; here it fails only when it carries ``args``."""
    if mode == "off" or not any(t.args is not None for t in expected):
        return None
    if actual is None:
        return _no_data("args")
    used: set[int] = set()
    for tool in expected:
        idx = next(
            (i for i, c in enumerate(actual) if i not in used and tool_name_matches(tool.name, c.name)), None
        )
        if idx is None:
            if tool.args is None:
                continue
            return _score("args", False, f"no {tool.name} call to check arguments against")
        used.add(idx)
        if tool.args is None:
            continue
        mismatch = _args_mismatch(tool.name, tool.args, actual[idx].args, mode)
        if mismatch:
            return _score("args", False, mismatch)
    return _score("args", True, f"arguments match ({mode})")


def check_no_error(result: ItemResult) -> Score:
    if result.error:
        return _score("no_error", False, result.error)
    return _score("no_error", True, "invoke succeeded")


def run_checks(item: DatasetItem, result: ItemResult, cfg: ChecksConfig) -> list[Score]:
    scores = [
        check_tools(item.expected_tools, result.tool_calls, cfg.tools),
        check_args(item.expected_tools, result.tool_calls, cfg.args),
        check_no_error(result) if cfg.no_error else None,
    ]
    return [s for s in scores if s is not None]
