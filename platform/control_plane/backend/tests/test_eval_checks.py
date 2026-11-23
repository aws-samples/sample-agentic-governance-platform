"""Deterministic eval checks: tool names, tool arguments, no-error."""

import pytest

from models.eval import ChecksConfig, DatasetItem, ExpectedTool, ItemResult, Score, ToolCall
from services.eval_checks import check_args, check_no_error, check_tools, run_checks, tool_name_matches


def _exp(*names, **args_by_name):
    return [ExpectedTool(name=n, args=args_by_name.get(n)) for n in names]


def _act(*calls):
    return [
        ToolCall(name=c, args={}) if isinstance(c, str) else ToolCall(name=c[0], args=c[1]) for c in calls
    ]


def _scored(score: Score | None) -> Score:
    assert score is not None
    return score


TOOLS_CASES = [
    ("exact", ["a", "b"], ["a", "b"], True),
    ("exact", ["a", "b"], ["b", "a"], False),
    ("exact", ["a", "b"], ["a", "b", "c"], False),
    ("exact", ["a", "b"], ["a"], False),
    ("exact", ["a"], ["a", "a"], False),
    ("in_order", ["a", "b"], ["a", "x", "b"], True),
    ("in_order", ["a", "b"], ["x", "a", "b", "y"], True),
    ("in_order", ["a", "b"], ["b", "a"], False),
    ("in_order", ["a", "b"], ["a"], False),
    ("in_order", ["a", "a"], ["a"], False),
    ("in_order", ["a", "a"], ["a", "x", "a"], True),
    ("any_order", ["a", "b"], ["b", "a"], True),
    ("any_order", ["a", "b"], ["b", "x", "a"], True),
    ("any_order", ["a", "a"], ["a", "b"], False),
    ("any_order", ["a", "a"], ["a", "b", "a"], True),
    ("any_order", ["a", "b"], ["a"], False),
]


@pytest.mark.parametrize("mode,expected,actual,passed", TOOLS_CASES)
def test_check_tools_modes(mode, expected, actual, passed):
    score = _scored(check_tools(_exp(*expected), _act(*actual), mode))
    assert score.evaluator == "tools"
    assert score.kind == "check"
    assert score.passed is passed
    assert score.value == (1.0 if passed else 0.0)
    assert score.error is None
    assert score.explanation


@pytest.mark.parametrize("mode", ["exact", "in_order", "any_order"])
def test_check_tools_not_scored_when_expected_empty(mode):
    assert check_tools([], _act("a"), mode) is None
    assert check_tools([], None, mode) is None


def test_check_tools_off_is_not_scored():
    assert check_tools(_exp("a"), _act("b"), "off") is None
    assert check_tools(_exp("a"), None, "off") is None


@pytest.mark.parametrize("mode", ["exact", "in_order", "any_order"])
def test_check_tools_no_data(mode):
    score = _scored(check_tools(_exp("a"), None, mode))
    assert score.error == "no tool-call data"
    assert score.passed is False
    assert score.value is None


def test_check_tools_empty_actual_is_data_not_missing():
    score = _scored(check_tools(_exp("a"), [], "in_order"))
    assert score.error is None
    assert score.passed is False


TOOLS_EXPLANATIONS = [
    (
        "in_order",
        ["get_policy", "open_claim"],
        ["get_policy"],
        "expected open_claim after get_policy; got [get_policy]",
    ),
    ("in_order", ["get_policy"], [], "expected get_policy first; got []"),
    (
        "exact",
        ["get_policy", "open_claim"],
        ["get_policy"],
        "expected open_claim after get_policy; got [get_policy]",
    ),
    (
        "exact",
        ["get_policy"],
        ["get_policy", "open_claim"],
        "unexpected open_claim after get_policy; got [get_policy, open_claim]",
    ),
    (
        "exact",
        ["get_policy", "open_claim"],
        ["open_claim", "get_policy"],
        "expected get_policy first, got open_claim; got [open_claim, get_policy]",
    ),
    ("exact", ["a", "b"], ["a", "c"], "expected b after a, got c; got [a, c]"),
    (
        "any_order",
        ["get_policy", "open_claim"],
        ["get_policy"],
        "expected open_claim (missing 1); got [get_policy]",
    ),
    ("any_order", ["a", "a"], ["a"], "expected a (missing 1); got [a]"),
]


@pytest.mark.parametrize("mode,expected,actual,explanation", TOOLS_EXPLANATIONS)
def test_check_tools_explanation_names_first_mismatch(mode, expected, actual, explanation):
    assert _scored(check_tools(_exp(*expected), _act(*actual), mode)).explanation == explanation


POLICY = {"policy_id": "P-1001"}
NESTED = {"claim": {"type": "auto", "parties": [{"name": "A"}, {"name": "B"}]}}

ARGS_CASES = [
    ("subset", [("get_policy", POLICY)], [("get_policy", POLICY)], True),
    ("subset", [("get_policy", POLICY)], [("get_policy", {**POLICY, "lang": "en"})], True),
    ("subset", [("get_policy", POLICY)], [("get_policy", {"policy_id": "P-2"})], False),
    ("subset", [("get_policy", POLICY)], [("get_policy", {})], False),
    ("subset", [("get_policy", POLICY)], [("open_claim", POLICY)], False),
    ("exact", [("get_policy", POLICY)], [("get_policy", POLICY)], True),
    ("exact", [("get_policy", POLICY)], [("get_policy", {**POLICY, "lang": "en"})], False),
    (
        "exact",
        [("open_claim", NESTED)],
        [("open_claim", {"claim": {"type": "auto", "parties": [{"name": "A"}, {"name": "B"}]}})],
        True,
    ),
    (
        "exact",
        [("open_claim", NESTED)],
        [("open_claim", {"claim": {"type": "auto", "parties": [{"name": "A"}]}})],
        False,
    ),
    ("subset", [("open_claim", NESTED)], [("open_claim", {**NESTED, "x": 1})], True),
    (
        "subset",
        [("open_claim", NESTED)],
        [("open_claim", {"claim": {**NESTED["claim"], "extra": 1}})],
        False,
    ),
    (
        "subset",
        [("get_policy", {"policy_id": "1"}), ("get_policy", {"policy_id": "2"})],
        [("get_policy", {"policy_id": "1"}), ("get_policy", {"policy_id": "2"})],
        True,
    ),
    (
        "subset",
        [("get_policy", {"policy_id": "1"}), ("get_policy", {"policy_id": "2"})],
        [("get_policy", {"policy_id": "1"})],
        False,
    ),
    (
        "subset",
        [("get_policy", {"policy_id": "2"})],
        [("get_policy", {"policy_id": "1"}), ("get_policy", {"policy_id": "2"})],
        False,
    ),
    (
        "subset",
        [("get_policy", None), ("get_policy", {"policy_id": "2"})],
        [("get_policy", {"policy_id": "1"}), ("get_policy", {"policy_id": "2"})],
        True,
    ),
]


@pytest.mark.parametrize("mode,expected,actual,passed", ARGS_CASES)
def test_check_args_modes(mode, expected, actual, passed):
    exp = [ExpectedTool(name=n, args=a) for n, a in expected]
    score = _scored(check_args(exp, _act(*actual), mode))
    assert score.evaluator == "args"
    assert score.kind == "check"
    assert score.passed is passed
    assert score.value == (1.0 if passed else 0.0)
    assert score.explanation


def test_check_args_off_and_empty_are_not_scored():
    assert check_args([ExpectedTool(name="a", args={"x": 1})], _act("a"), "off") is None
    assert check_args([], _act("a"), "subset") is None
    assert check_args([], None, "exact") is None


def test_check_args_not_scored_when_no_expected_tool_has_args():
    assert check_args(_exp("a", "b"), _act("a", "b"), "subset") is None


@pytest.mark.parametrize("mode", ["exact", "subset"])
def test_check_args_no_data(mode):
    score = _scored(check_args([ExpectedTool(name="a", args={"x": 1})], None, mode))
    assert score.error == "no tool-call data"
    assert score.passed is False
    assert score.value is None


ARGS_EXPLANATIONS = [
    (
        "subset",
        [("get_policy", POLICY)],
        [("get_policy", {"policy_id": "P-2"})],
        "get_policy.policy_id: expected 'P-1001', got 'P-2'",
    ),
    (
        "subset",
        [("get_policy", POLICY)],
        [("get_policy", {})],
        "get_policy.policy_id: expected 'P-1001', got nothing",
    ),
    ("subset", [("get_policy", POLICY)], [], "no get_policy call to check arguments against"),
    (
        "exact",
        [("get_policy", POLICY)],
        [("get_policy", {**POLICY, "lang": "en"})],
        "get_policy: expected args {'policy_id': 'P-1001'}, got {'policy_id': 'P-1001', 'lang': 'en'}",
    ),
]


@pytest.mark.parametrize("mode,expected,actual,explanation", ARGS_EXPLANATIONS)
def test_check_args_explanation_names_first_mismatch(mode, expected, actual, explanation):
    exp = [ExpectedTool(name=n, args=a) for n, a in expected]
    assert _scored(check_args(exp, _act(*actual), mode)).explanation == explanation


def test_check_no_error_passes_without_error():
    score = check_no_error(ItemResult(item_id="i1", output="ok"))
    assert (score.evaluator, score.kind, score.value, score.passed) == ("no_error", "check", 1.0, True)


def test_check_no_error_fails_with_error_text_as_explanation():
    score = check_no_error(ItemResult(item_id="i1", error="invoke timed out"))
    assert (score.value, score.passed) == (0.0, False)
    assert score.explanation == "invoke timed out"


def _item(**kw):
    return DatasetItem(id="i1", input="hi", **kw)


def test_run_checks_all_enabled():
    item = _item(
        expected_tools=[ExpectedTool(name="get_policy", args=POLICY), ExpectedTool(name="open_claim")]
    )
    result = ItemResult(item_id="i1", tool_calls=_act(("get_policy", POLICY), "open_claim"))
    scores = run_checks(item, result, ChecksConfig())
    assert [(s.evaluator, s.passed) for s in scores] == [("tools", True), ("args", True), ("no_error", True)]


def test_run_checks_skips_unscored():
    result = ItemResult(item_id="i1", tool_calls=None)
    assert [s.evaluator for s in run_checks(_item(), result, ChecksConfig())] == ["no_error"]
    assert run_checks(_item(), result, ChecksConfig(no_error=False)) == []
    item = _item(expected_tools=[ExpectedTool(name="a", args={"x": 1})])
    assert run_checks(item, result, ChecksConfig(tools="off", args="off", no_error=False)) == []


def test_run_checks_missing_tool_data_fails_both_checks():
    item = _item(expected_tools=[ExpectedTool(name="a", args={"x": 1})])
    scores = run_checks(item, ItemResult(item_id="i1", error="boom"), ChecksConfig())
    assert [(s.evaluator, s.passed, s.error) for s in scores] == [
        ("tools", False, "no tool-call data"),
        ("args", False, "no tool-call data"),
        ("no_error", False, None),
    ]


GP, OC = "claims_walk__api___getPolicy", "claims_api__api___openClaim"


@pytest.mark.parametrize(
    "expected,actual,ok",
    [
        ("api___getPolicy", "api___getPolicy", True),
        ("api___getPolicy", GP, True),
        ("getPolicy", GP, True),
        ("claims_walk__api___getPolicy", GP, True),
        ("api___getPolicy", "claims_walk__api___getPolicyX", False),
        ("Policy", GP, False),
        ("other__api___getPolicy", GP, False),
        ("getPolicy", "getPolicy", True),
    ],
)
def test_tool_name_matches(expected, actual, ok):
    assert tool_name_matches(expected, actual) is ok


@pytest.mark.parametrize(
    "mode,expected,actual,passed",
    [
        ("exact", ["api___getPolicy", "openClaim"], [GP, OC], True),
        ("exact", ["getPolicy", "openClaim"], [OC, GP], False),
        ("in_order", ["api___getPolicy", "api___openClaim"], ["skills", GP, OC], True),
        ("in_order", ["openClaim", "getPolicy"], [GP, OC], False),
        ("any_order", ["openClaim", "getPolicy"], ["skills", GP, OC], True),
        ("any_order", ["getPolicy", "getPolicy"], [GP, OC], False),
    ],
)
def test_check_tools_accepts_the_mcp_prefix(mode, expected, actual, passed):
    score = _scored(check_tools(_exp(*expected), _act(*actual), mode))
    assert score.passed is passed


def test_any_order_prefixed_missing_message():
    score = _scored(check_tools(_exp("getPolicy", "getPolicy"), _act(GP, OC), "any_order"))
    assert score.explanation is not None
    assert score.explanation.startswith("expected getPolicy (missing 1)")


@pytest.mark.parametrize("mode", ["exact", "subset"])
def test_check_args_pairs_prefixed_calls(mode):
    expected = [ExpectedTool(name="api___openClaim", args={"policyId": "POL-1002"})]
    good = _scored(check_args(expected, _act(("skills", {}), (OC, {"policyId": "POL-1002"})), mode))
    bad = _scored(check_args(expected, _act((OC, {"policyId": "POL-9"})), mode))
    assert good.passed is True
    assert bad.explanation is not None
    assert bad.passed is False and "api___openClaim" in bad.explanation
