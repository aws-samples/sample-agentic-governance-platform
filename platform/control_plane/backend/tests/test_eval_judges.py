"""SpanSource (CloudWatch ``aws/spans`` via Logs Insights) + judge_item (AgentCore Evaluate).

The logs and bedrock-agentcore clients are MagicMocks; ``sleep`` is injected so polling is
instant and its total is observable. Spans are hand-built OTel documents in the shape
``aws/spans`` stores (flat dotted keys under ``attributes``).
"""

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from models.eval import DatasetItem, ToolCall
from services import eval_judges
from services.eval_judges import SpanSource, TraceSummary, judge_item

SINCE = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
NS = 1_791_000_000_000_000_000


def _span(session="sess-1", name="invoke_agent", start=0, end=1_000_000, trace="t1", **attrs):
    return {
        "traceId": trace,
        "spanId": f"s-{name}-{start}",
        "name": name,
        "startTimeUnixNano": NS + start,
        "endTimeUnixNano": NS + end,
        "attributes": {"session.id": session, **attrs},
        "resource": {"attributes": {"service.name": "harness_claims_ab12.DEFAULT"}},
    }


def _tool_span(tool, args, start, session="sess-1"):
    return _span(
        session=session,
        name=f"execute_tool {tool}",
        start=start,
        end=start + 10,
        **{
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": tool,
            "gen_ai.tool.call.arguments": args,
        },
    )


def _rows(spans):
    return [[{"field": "@message", "value": json.dumps(s)}, {"field": "@ptr", "value": "x"}] for s in spans]


def _logs(*result_batches, status="Complete"):
    """A logs client whose successive queries return the given row batches."""
    logs = MagicMock()
    logs.start_query.side_effect = [{"queryId": f"q{i}"} for i in range(len(result_batches))]
    logs.get_query_results.side_effect = [{"status": status, "results": _rows(b)} for b in result_batches]
    return logs


OWN = "/aws/bedrock-agentcore/runtimes/harness_claims_dev-Rt9-DEFAULT"


def _event(span):
    return {
        "timeUnixNano": str(span["startTimeUnixNano"] + 1),
        "spanId": span["spanId"],
        "attributes": {"session.id": "sess-1"},
    }


def test_session_spans_query_filters_by_session_id_on_aws_spans():
    spans = [_span(), _tool_span("get_policy", "{}", 5)]
    logs = _logs(spans, spans)
    got = SpanSource(logs, sleep=MagicMock()).session_spans("sess-1", since=SINCE)
    assert got == spans
    kwargs = logs.start_query.call_args.kwargs
    assert kwargs["logGroupNames"] == ["aws/spans"]
    assert 'attributes.session.id = "sess-1"' in kwargs["queryString"]
    assert kwargs["startTime"] == int(SINCE.timestamp())


def test_session_spans_polls_until_non_empty_and_stable():
    sleep = MagicMock()
    logs = _logs([], [], [_span()], [_span()])
    got = SpanSource(logs, sleep=sleep).session_spans("sess-1", since=SINCE, wait_s=60, poll_s=15)
    assert len(got) == 1
    assert logs.start_query.call_count == 4
    assert [c.args[0] for c in sleep.call_args_list] == [15, 15, 15]


def test_session_spans_keeps_polling_while_spans_arrive_in_batches():
    sleep = MagicMock()
    a, b, c = _span(start=0), _tool_span("get_policy", "{}", 5), _span(name="chat", start=9)
    logs = _logs([a], [a, b], [a, b, c], [a, b, c])
    got = SpanSource(logs, sleep=sleep).session_spans("sess-1", since=SINCE, wait_s=180, poll_s=15)
    assert got == [a, b, c]
    assert logs.start_query.call_count == 4


def test_session_spans_returns_the_partial_set_when_wait_runs_out():
    sleep = MagicMock()
    a, b = _span(start=0), _tool_span("get_policy", "{}", 5)
    logs = _logs([a], [a, b])
    got = SpanSource(logs, sleep=sleep).session_spans("sess-1", since=SINCE, wait_s=15, poll_s=15)
    assert got == [a, b]
    assert sum(x.args[0] for x in sleep.call_args_list) <= 15


def test_log_groups_override_is_used_by_both_queries():
    groups = ["/aws/bedrock-agentcore/runtimes/harness_claims_ab12-x-DEFAULT"]
    span = _span()
    logs = _logs([span, _event(span)], [span, _event(span)], [span])
    src = SpanSource(logs, log_groups=groups, sleep=MagicMock())
    src.session_spans("sess-1", since=SINCE)
    src.recent_sessions("harness", since=SINCE)
    assert [c.kwargs["logGroupNames"] for c in logs.start_query.call_args_list] == [groups] * 3


def test_own_log_group_reads_its_spans_and_log_event_streams_and_aws_spans_stays_unfiltered():
    """One query per group; the agent's own group also holds runtime logs + EMF. Live 2026-10-06:
    Evaluate rejects spans without their gen_ai log events (LogEventMissingException), which sit in
    the agent group's ``otel-rt-logs`` stream."""
    own = "/aws/bedrock-agentcore/runtimes/harness_claims_dev-Rt9-DEFAULT"
    late, early = _span(start=9), _span(start=1)
    ev, ev2 = _event(early), _event(late)
    logs = _logs([late, ev, ev2], [early], [late, ev, ev2], [early])
    got = SpanSource(logs, log_groups=[own, "aws/spans"], sleep=MagicMock()).session_spans(
        "sess-1", since=SINCE
    )
    assert got == [early, ev, late, ev2]
    calls = [c.kwargs for c in logs.start_query.call_args_list]
    assert [c["logGroupNames"] for c in calls] == [[own], ["aws/spans"]] * 2
    assert '| filter @logStream in ["spans", "otel-rt-logs"]' in calls[0]["queryString"]
    assert "@logStream" not in calls[1]["queryString"]
    assert all(c["queryString"].startswith("fields @message") for c in calls)


def test_log_events_sort_by_their_time_among_spans():
    own = "/aws/bedrock-agentcore/runtimes/harness_claims_dev-Rt9-DEFAULT"
    span, event = _span(start=5), {"timeUnixNano": "3", "spanId": "s", "attributes": {"session.id": "sess-1"}}
    logs = _logs([span, event], [], [span, event], [])
    got = SpanSource(logs, log_groups=[own, "aws/spans"], sleep=MagicMock()).session_spans(
        "sess-1", since=SINCE
    )
    assert got == [event, span]


def test_the_poll_waits_for_the_agent_spans_log_event_that_lands_after_it():
    """Live 2026-10-06: judged too early, Evaluate said invoke_agent "is missing a corresponding log
    event". The fake answers (own group, aws/spans) per read: spans first, the event two reads later,
    then stable."""
    agent, tool = _span(start=5), _span(name="execute_tool", start=6)
    ev = _event(agent)
    logs = _logs([], [agent, tool], [], [agent, tool], [ev], [agent, tool], [ev], [agent, tool])
    got = SpanSource(logs, log_groups=[OWN, "aws/spans"], sleep=MagicMock()).session_spans(
        "sess-1", since=SINCE
    )
    assert got == [agent, ev, tool]
    assert logs.start_query.call_count == 8


def test_the_poll_waits_for_the_agent_span_when_log_events_land_first():
    agent = _span(start=5)
    ev = _event(agent)
    logs = _logs([ev], [], [ev], [], [ev], [agent], [ev], [agent])
    got = SpanSource(logs, log_groups=[OWN, "aws/spans"], sleep=MagicMock()).session_spans(
        "sess-1", since=SINCE
    )
    assert got == [agent, ev]


def test_aws_spans_alone_needs_no_log_events():
    agent = _span(start=5)
    logs = _logs([agent], [agent])
    got = SpanSource(logs, sleep=MagicMock()).session_spans("sess-1", since=SINCE)
    assert got == [agent]


def test_an_incomplete_session_is_returned_as_it_stands_when_the_wait_runs_out():
    agent = _span(start=5)
    logs = _logs(*([[], [agent]] * 50))
    got = SpanSource(logs, log_groups=[OWN, "aws/spans"], sleep=MagicMock()).session_spans(
        "sess-1", since=SINCE, wait_s=30, poll_s=5
    )
    assert got == [agent]


def test_a_failing_group_does_not_lose_the_other_groups_spans():
    logs = MagicMock()
    logs.start_query.side_effect = [RuntimeError("ResourceNotFoundException"), {"queryId": "q"}]
    logs.get_query_results.return_value = {"status": "Complete", "results": _rows([_span(session="s-1")])}
    got = SpanSource(
        logs, log_groups=["/aws/bedrock-agentcore/runtimes/gone", "aws/spans"], sleep=MagicMock()
    ).recent_sessions("harness_claims_dev.DEFAULT", since=SINCE)
    assert [t.session_id for t in got] == ["s-1"]


def test_session_spans_waits_for_the_tool_span_the_invoke_reported():
    """Live 2026-10-07: invoke_agent landed before execute_tool and the judge scored 0.00."""
    sleep = MagicMock()
    a, t = _span(start=0), _tool_span("calculator", '{"expression": "17 * 23"}', 5)
    logs = _logs([a], [a], [a, t], [a, t])
    got = SpanSource(logs, sleep=sleep).session_spans(
        "sess-1", since=SINCE, wait_s=180, poll_s=15, min_tool_spans=1
    )
    assert got == [a, t]
    assert logs.start_query.call_count == 4


def test_session_spans_min_tool_spans_zero_keeps_todays_behaviour():
    a = _span(start=0)
    logs = _logs([a], [a])
    got = SpanSource(logs, sleep=MagicMock()).session_spans("sess-1", since=SINCE, min_tool_spans=0)
    assert got == [a]
    assert logs.start_query.call_count == 2


def test_complete_counts_tool_spans_by_operation_or_name():
    a = _span(start=0)
    op_attrs: dict[str, Any] = {"gen_ai.operation.name": "execute_tool"}
    by_op = _span(name="tool", start=5, **op_attrs)
    by_name = _span(name="execute_tool calculator", start=6)
    assert not eval_judges._complete([a], False, min_tool_spans=1)
    assert eval_judges._complete([a, by_op], False, min_tool_spans=1)
    assert eval_judges._complete([a, by_name], False, min_tool_spans=1)
    assert not eval_judges._complete([a, by_op], False, min_tool_spans=2)
    assert eval_judges._complete([a], False)


def test_session_spans_timeout_returns_empty():
    sleep = MagicMock()
    logs = MagicMock()
    logs.start_query.return_value = {"queryId": "q"}
    logs.get_query_results.return_value = {"status": "Complete", "results": []}
    got = SpanSource(logs, sleep=sleep).session_spans("sess-1", since=SINCE, wait_s=45, poll_s=15)
    assert got == []
    assert sum(c.args[0] for c in sleep.call_args_list) <= 45


def test_session_spans_waits_for_a_running_query():
    sleep = MagicMock()
    logs = MagicMock()
    logs.start_query.return_value = {"queryId": "q"}
    logs.get_query_results.side_effect = [
        {"status": "Running", "results": []},
        {"status": "Complete", "results": _rows([_span()])},
        {"status": "Complete", "results": _rows([_span()])},
    ]
    assert len(SpanSource(logs, sleep=sleep).session_spans("sess-1", since=SINCE)) == 1


def test_session_spans_client_error_returns_empty():
    logs = MagicMock()
    logs.start_query.side_effect = RuntimeError("ResourceNotFoundException: aws/spans")
    assert SpanSource(logs, sleep=MagicMock()).session_spans("sess-1", since=SINCE) == []


def test_session_spans_refuses_a_session_id_that_could_break_the_query():
    logs = MagicMock()
    assert SpanSource(logs, sleep=MagicMock()).session_spans('x" or 1=1', since=SINCE) == []
    logs.start_query.assert_not_called()


def test_session_spans_skips_rows_that_are_not_json_objects():
    logs = MagicMock()
    logs.start_query.return_value = {"queryId": "q"}
    logs.get_query_results.return_value = {
        "status": "Complete",
        "results": [
            [{"field": "@message", "value": "not json"}],
            [{"field": "@message", "value": "[1]"}],
            _rows([_span()])[0],
        ],
    }
    assert len(SpanSource(logs, sleep=MagicMock()).session_spans("sess-1", since=SINCE)) == 1


def test_tool_spans_become_calls_in_start_order():
    spans = [
        _span(),
        _tool_span("open_claim", '{"policy": "P-1"}', 50),
        _tool_span("get_policy", {"id": "P-1"}, 20),
    ]
    assert SpanSource.tool_calls_from_spans(spans) == [
        ToolCall(name="get_policy", args={"id": "P-1"}),
        ToolCall(name="open_claim", args={"policy": "P-1"}),
    ]


def test_tool_span_with_undecodable_args_gets_empty_args():
    assert SpanSource.tool_calls_from_spans([_tool_span("get_policy", "{broken", 1)]) == [
        ToolCall(name="get_policy", args={})
    ]


def test_unknown_spans_give_none():
    unknown: list[Any] = [_span(), {"weird": True}, "nope"]
    assert SpanSource.tool_calls_from_spans(unknown) is None
    assert SpanSource.tool_calls_from_spans([]) is None


def test_recent_sessions_groups_spans_newest_first():
    older = [
        _span(
            session="s-old",
            start=0,
            end=2_000_000,
            trace="t-old",
            **{"gen_ai.prompt": "hi", "gen_ai.completion": "hello"},
        ),
    ]
    newer = [
        _span(session="s-new", start=10_000_000_000, end=10_500_000_000, trace="t-new"),
        _tool_span("get_policy", "{}", 10_100_000_000, session="s-new"),
    ]
    logs = _logs(newer + older)
    got = SpanSource(logs, sleep=MagicMock()).recent_sessions("harness_claims", since=SINCE, limit=50)
    assert [t.session_id for t in got] == ["s-new", "s-old"]
    assert isinstance(got[0], TraceSummary)
    assert got[0].trace_id == "t-new"
    assert got[0].latency_ms == 500
    assert got[0].tool_calls == [ToolCall(name="get_policy", args={})]
    assert got[1].input == "hi" and got[1].output == "hello"
    assert got[1].tool_calls is None
    assert got[1].started_at == datetime.fromtimestamp(NS / 1e9, tz=UTC)
    q = logs.start_query.call_args.kwargs["queryString"]
    assert "resource.attributes.service.name" in q and "harness_claims" in q


def test_recent_sessions_honours_limit():
    spans = [_span(session=f"s{i}", start=i * 1000) for i in range(5)]
    got = SpanSource(_logs(spans), sleep=MagicMock()).recent_sessions("harness", since=SINCE, limit=2)
    assert [t.session_id for t in got] == ["s4", "s3"]
    got0 = SpanSource(_logs(spans), sleep=MagicMock()).recent_sessions("harness", since=SINCE, limit=0)
    assert [t.session_id for t in got0] == ["s4"]


def test_session_id_with_a_trailing_newline_is_refused():
    logs = MagicMock()
    assert SpanSource(logs, sleep=MagicMock()).session_spans("sess-1\n", since=SINCE) == []
    logs.start_query.assert_not_called()


def test_recent_sessions_failure_or_bad_prefix_returns_empty():
    logs = MagicMock()
    logs.start_query.side_effect = RuntimeError("boom")
    assert SpanSource(logs, sleep=MagicMock()).recent_sessions("harness", since=SINCE) == []
    logs2 = MagicMock()
    assert SpanSource(logs2, sleep=MagicMock()).recent_sessions("bad/prefix", since=SINCE) == []
    logs2.start_query.assert_not_called()


def _item(**kw):
    return DatasetItem(id="i1", input="file a claim", **kw)


def test_judge_success_maps_value_label_explanation():
    client = MagicMock()
    client.evaluate.return_value = {
        "evaluationResults": [
            {
                "evaluatorId": "Builtin.Correctness",
                "value": 0.75,
                "label": "Mostly correct",
                "explanation": "missed the date",
            }
        ]
    }
    spans = [_span()]
    score = judge_item(client, "Builtin.Correctness", spans, _item())
    assert score.evaluator == "Builtin.Correctness"
    assert score.kind == "judge"
    assert score.value == 0.75 and score.passed is None
    assert score.label == "Mostly correct" and score.explanation == "missed the date"
    assert score.error is None
    kwargs = client.evaluate.call_args.kwargs
    assert kwargs["evaluatorId"] == "Builtin.Correctness"
    assert kwargs["evaluationInput"] == {"sessionSpans": spans}
    assert "evaluationReferenceInputs" not in kwargs


def test_judge_exception_gives_error_score():
    client = MagicMock()
    client.evaluate.side_effect = RuntimeError("AccessDeniedException")
    score = judge_item(client, "Builtin.GoalSuccessRate", [_span()], _item())
    assert score.kind == "judge" and score.value is None and score.passed is None
    assert score.error is not None
    assert "AccessDeniedException" in score.error


def test_judge_result_error_code_gives_error_score():
    client = MagicMock()
    client.evaluate.return_value = {
        "evaluationResults": [{"errorCode": "ValidationException", "errorMessage": "no trace"}]
    }
    score = judge_item(client, "Builtin.Correctness", [_span()], _item())
    assert score.error is not None
    assert score.value is None and "no trace" in score.error


def test_judge_without_spans_does_not_call_the_api():
    client = MagicMock()
    score = judge_item(client, "Builtin.Correctness", [], _item())
    assert score.error and score.value is None
    client.evaluate.assert_not_called()


def test_judge_reference_inputs_split_session_assertions_from_per_trace_expected_output():
    client = MagicMock()
    client.evaluate.return_value = {"evaluationResults": [{"value": 1.0}]}
    spans = [_span(trace="t1"), _span(name="chat", start=5, trace="t2")]
    judge_item(
        client,
        "Builtin.GoalSuccessRate",
        spans,
        _item(expected_output="Claim opened", assertions=["asks for date"]),
    )
    assert client.evaluate.call_args.kwargs["evaluationReferenceInputs"] == [
        {"context": {"spanContext": {"sessionId": "sess-1"}}, "assertions": [{"text": "asks for date"}]},
        {
            "context": {"spanContext": {"sessionId": "sess-1", "traceId": "t2"}},
            "expectedResponse": {"text": "Claim opened"},
        },
    ]


def test_judge_surfaces_ignored_reference_fields_in_explanation():
    client = MagicMock()
    client.evaluate.return_value = {
        "evaluationResults": [
            {"value": 0.5, "explanation": "ok", "ignoredReferenceInputFields": ["expectedResponse"]}
        ]
    }
    score = judge_item(client, "Builtin.GoalSuccessRate", [_span()], _item(expected_output="x"))
    assert score.value == 0.5
    assert score.explanation == "ok (ignored reference inputs: expectedResponse)"


def test_judge_surfaces_ignored_reference_fields_in_error():
    client = MagicMock()
    client.evaluate.return_value = {
        "evaluationResults": [
            {
                "errorCode": "ValidationException",
                "errorMessage": "bad",
                "ignoredReferenceInputFields": ["assertions"],
            }
        ]
    }
    score = judge_item(client, "Builtin.Correctness", [_span()], _item(assertions=["a"]))
    assert score.error is not None
    assert "ignored reference inputs: assertions" in score.error


def test_judge_reference_inputs_omit_absent_fields():
    client = MagicMock()
    client.evaluate.return_value = {"evaluationResults": [{"value": 1.0}]}
    judge_item(client, "Builtin.GoalSuccessRate", [_span()], _item(assertions=["asks for date"]))
    (ref,) = client.evaluate.call_args.kwargs["evaluationReferenceInputs"]
    assert "traceId" not in ref["context"]["spanContext"]
    assert "expectedResponse" not in ref and ref["assertions"] == [{"text": "asks for date"}]


def test_attribute_names_live_in_one_constants_block():
    assert eval_judges.SPANS_LOG_GROUP == "aws/spans"
    assert eval_judges.SPANS_STREAMS == ("spans", "otel-rt-logs")
    assert eval_judges.ATTR_SESSION_ID == "session.id"
    assert eval_judges.ATTR_TOOL_NAME == "gen_ai.tool.name"
    assert eval_judges.RECENT_WAIT_S == 20


SKILL = "---\nname: fnol\ndescription: claims\n---\n# FNOL skill"
PROMPT = "I had a rear-end collision yesterday"


@pytest.mark.parametrize(
    "value",
    [
        json.dumps(
            [
                {"role": "user", "content": [{"text": SKILL}]},
                {"role": "assistant", "content": [{"text": "ok"}]},
                {"role": "user", "content": [{"text": PROMPT}]},
                {"role": "user", "content": [{"toolResult": {"content": [{"text": "policy POL-1"}]}}]},
            ]
        ),
        [{"role": "system", "content": SKILL}, {"role": "user", "content": PROMPT}],
        json.dumps(
            [
                {
                    "role": "user",
                    "parts": [{"type": "text", "content": SKILL}, {"type": "text", "content": PROMPT}],
                }
            ]
        ),
    ],
)
def test_user_text_is_the_last_user_prompt(value):
    assert eval_judges._user_text(value) == PROMPT


@pytest.mark.parametrize(
    "value,want",
    [
        ("hi", "hi"),
        (None, None),
        (json.dumps({"text": "x"}), "x"),
        (json.dumps([{"role": "assistant", "content": [{"text": "a"}]}]), "a"),
    ],
)
def test_user_text_falls_back_to_the_first_text(value, want):
    assert eval_judges._user_text(value) == want


def test_recent_sessions_input_is_the_prompt_and_output_unchanged():
    messages = json.dumps(
        [{"role": "user", "content": [{"text": SKILL}]}, {"role": "user", "content": [{"text": PROMPT}]}]
    )
    attrs: dict[str, Any] = {"gen_ai.input.messages": messages, "gen_ai.completion": "done"}
    spans = [_span(session="s-1", **attrs)]
    got = SpanSource(_logs(spans), sleep=MagicMock()).recent_sessions("harness", since=SINCE)
    assert got[0].input == PROMPT and got[0].output == "done"
    assert eval_judges._text(messages) == SKILL
