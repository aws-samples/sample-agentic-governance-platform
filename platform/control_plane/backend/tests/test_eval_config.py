"""evals.yaml + dataset.jsonl parsing and dumping."""

import json

import pytest

from models.eval import ChecksConfig, DatasetItem, EvalConfig, ExpectedTool, GateConfig
from services.eval_config import (
    MAX_ITEMS,
    EvalFileError,
    dump_config,
    dump_dataset,
    parse_config,
    parse_dataset,
)

DESIGN_YAML = """\
version: 1
dataset: evals/dataset.jsonl
checks:
  tools: in_order        # exact | in_order | any_order | off   (tool names)
  args: subset           # exact | subset | off                  (tool arguments)
  no_error: true         # the invoke must succeed
judges:                  # AgentCore Evaluations built-in ids; [] = checks only
  - Builtin.GoalSuccessRate
  - Builtin.Correctness
gate:
  checks: all_pass       # every item must pass every enabled check
  judge_min_mean: 0.8    # every judge's mean score across items must be >= this
"""

DESIGN_ITEM = (
    '{"id":"fnol-001","input":"My car was hit on the A40, policy P-1001",'
    '"expected_output":"A claim is opened ...","assertions":["Opens a claim","Quotes the claim number"],'
    '"expected_tools":[{"name":"get_policy","args":{"policy_id":"P-1001"}},{"name":"open_claim"}],'
    '"source_trace_id":null,"tags":["happy-path"]}'
)


def _errors(fn, text):
    with pytest.raises(EvalFileError) as exc:
        fn(text)
    return exc.value.errors


@pytest.mark.parametrize("text", [None, ""])
def test_missing_config_is_the_defaults(text):
    cfg = parse_config(text)
    assert cfg == EvalConfig()
    assert cfg.version == 1
    assert cfg.dataset == "evals/dataset.jsonl"
    assert cfg.checks == ChecksConfig(tools="in_order", args="subset", no_error=True)
    assert cfg.judges == ["Builtin.GoalSuccessRate", "Builtin.Correctness"]
    assert cfg.gate == GateConfig(checks="all_pass", judge_min_mean=0.8)


def test_design_example_parses():
    assert parse_config(DESIGN_YAML) == EvalConfig()


def test_partial_config_fills_defaults():
    cfg = parse_config("checks:\n  tools: off\njudges: []\n")
    assert cfg.checks.tools == "off"
    assert cfg.checks.args == "subset"
    assert cfg.judges == []


def test_bad_yaml_carries_a_line_number():
    errs = _errors(parse_config, "version: 1\nchecks:\n  tools: [unclosed\n")
    assert len(errs) == 1
    assert errs[0].startswith("evals/evals.yaml:")


def test_non_mapping_yaml_is_refused():
    errs = _errors(parse_config, "- a\n- b\n")
    assert errs[0].startswith("evals/evals.yaml:1:")


def test_unknown_top_level_key_is_refused_with_its_line():
    errs = _errors(parse_config, "version: 1\njudgez: []\n")
    assert len(errs) == 1
    assert errs[0].startswith("evals/evals.yaml:2:")
    assert "judgez" in errs[0]


def test_unknown_nested_key_is_refused():
    errs = _errors(parse_config, "checks:\n  tool: exact\n")
    assert errs[0].startswith("evals/evals.yaml:2:")
    assert "tool" in errs[0]


@pytest.mark.parametrize("judge", ["GoalSuccessRate", "Builtin.", "Builtin.Goal-Rate", "Custom.X"])
def test_judge_id_pattern(judge):
    errs = _errors(parse_config, f"judges:\n  - {judge}\n")
    assert errs[0].startswith("evals/evals.yaml:2:")


def test_judge_id_valid():
    assert parse_config("judges: [Builtin.Helpfulness]\n").judges == ["Builtin.Helpfulness"]


@pytest.mark.parametrize("value", ["-0.1", "1.5"])
def test_threshold_out_of_range(value):
    errs = _errors(parse_config, f"gate:\n  judge_min_mean: {value}\n")
    assert errs[0].startswith("evals/evals.yaml:2:")


@pytest.mark.parametrize("value", ["0", "1", "0.5"])
def test_threshold_in_range(value):
    assert parse_config(f"gate:\n  judge_min_mean: {value}\n").gate.judge_min_mean == float(value)


def test_bad_enum_and_version():
    errs = _errors(parse_config, "version: 2\nchecks:\n  tools: sometimes\n")
    assert len(errs) == 2
    assert errs[0].startswith("evals/evals.yaml:1:")
    assert errs[1].startswith("evals/evals.yaml:3:")


def test_config_round_trips():
    cfg = EvalConfig(
        checks=ChecksConfig(tools="any_order", args="off", no_error=False),
        judges=["Builtin.Helpfulness"],
        gate=GateConfig(judge_min_mean=0.65),
    )
    assert parse_config(dump_config(cfg)) == cfg
    assert parse_config(dump_config(EvalConfig())) == EvalConfig()


def test_dumped_config_carries_the_design_comments():
    text = dump_config(EvalConfig())
    assert text == DESIGN_YAML
    empty = dump_config(EvalConfig(judges=[]))
    assert parse_config(empty).judges == []
    assert "# AgentCore Evaluations built-in ids" in empty


@pytest.mark.parametrize("text", [None, ""])
def test_missing_dataset_is_empty(text):
    assert parse_dataset(text) == []


def test_design_item_parses():
    [item] = parse_dataset(DESIGN_ITEM + "\n")
    assert item.id == "fnol-001"
    assert item.expected_tools == [
        ExpectedTool(name="get_policy", args={"policy_id": "P-1001"}),
        ExpectedTool(name="open_claim"),
    ]
    assert item.source_trace_id is None
    assert item.tags == ["happy-path"]


def test_blank_lines_are_skipped():
    text = '\n{"id":"a","input":"x"}\n\n   \n{"id":"b","input":"y"}\n\n'
    assert [i.id for i in parse_dataset(text)] == ["a", "b"]


def test_all_dataset_errors_collected_with_line_numbers():
    text = "\n".join(
        [
            '{"id":"a","input":"x"}',
            '{"id":"a","input":"y"}',
            '{"id":"b", nope',
            '{"id":"c"}',
            '{"id":"d","input":""}',
            '{"id":"bad id!","input":"x"}',
            '{"id":"e","input":"x","extra":1}',
            "[1, 2]",
        ]
    )
    errs = _errors(parse_dataset, text)
    lines = [e.split(":")[1] for e in errs]
    assert lines == ["2", "3", "4", "5", "6", "7", "8"]
    assert all(e.startswith("evals/dataset.jsonl:") for e in errs)
    assert "duplicate id 'a'" in errs[0]
    assert "input" in errs[2]
    assert "extra" in errs[5]


def test_input_too_long():
    errs = _errors(parse_dataset, json.dumps({"id": "a", "input": "x" * 8001}))
    assert errs[0].startswith("evals/dataset.jsonl:1:")
    assert parse_dataset(json.dumps({"id": "a", "input": "x" * 8000}))[0].input == "x" * 8000


def test_more_than_max_items_refused_and_other_errors_kept():
    assert MAX_ITEMS == 50
    lines = [json.dumps({"id": f"i{n}", "input": "x"}) for n in range(MAX_ITEMS + 1)]
    lines[3] = "not json"
    errs = _errors(parse_dataset, "\n".join(lines))
    assert len(errs) == 2
    assert errs[0].startswith("evals/dataset.jsonl:4:")
    assert errs[1].startswith("evals/dataset.jsonl:51:")
    assert "50" in errs[1]


def test_exactly_max_items_is_fine():
    lines = [json.dumps({"id": f"i{n}", "input": "x"}) for n in range(MAX_ITEMS)]
    assert len(parse_dataset("\n".join(lines))) == MAX_ITEMS


def test_dataset_round_trips_compact_without_nones():
    items = [
        DatasetItem(id="a", input="hi"),
        DatasetItem(
            id="b",
            input="claim",
            expected_output="ok",
            assertions=["Opens a claim"],
            expected_tools=[
                ExpectedTool(name="get_policy", args={"policy_id": "P-1"}),
                ExpectedTool(name="open_claim"),
            ],
            source_trace_id="t-1",
            tags=["happy-path"],
        ),
    ]
    text = dump_dataset(items)
    assert text.endswith("\n")
    rows = text.splitlines()
    assert len(rows) == 2
    assert rows[0] == '{"id":"a","input":"hi","assertions":[],"expected_tools":[],"tags":[]}'
    assert "null" not in text
    assert parse_dataset(text) == items


def test_dump_empty_dataset():
    assert parse_dataset(dump_dataset([])) == []


def test_dataset_round_trips_unicode_line_separators():
    items = [
        DatasetItem(id="a", input="one\u2028two\u2029three\u0085four", expected_output="x\ry"),
        DatasetItem(id="b", input="plain"),
    ]
    assert parse_dataset(dump_dataset(items)) == items


def test_crlf_dataset_parses():
    assert [i.id for i in parse_dataset('{"id":"a","input":"x"}\r\n{"id":"b","input":"y"}\r\n')] == ["a", "b"]


def test_expected_tool_typo_is_refused():
    errs = _errors(parse_dataset, '{"id":"a","input":"x","expected_tools":[{"name":"t","arg":{"k":1}}]}')
    assert errs[0].startswith("evals/dataset.jsonl:1:")
    assert "arg" in errs[0]


@pytest.mark.parametrize("dataset", ["123", "true", "off", "null", "1.5", "evals/my data.jsonl", "a:b"])
def test_dataset_values_that_would_need_quoting_are_refused_by_the_model(dataset):
    """``dataset`` is ``evals/<name>.jsonl`` only (AGP commits to it), so none of
    the YAML-ambiguous strings the quoting in ``_scalar`` guards against can reach it any more."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EvalConfig(dataset=dataset)


@pytest.mark.parametrize("dataset", ["evals/golden.jsonl", "evals/v1.2_set-a.jsonl"])
def test_dump_config_round_trips_a_valid_dataset_path(dataset):
    cfg = EvalConfig(dataset=dataset)
    assert parse_config(dump_config(cfg)) == cfg
