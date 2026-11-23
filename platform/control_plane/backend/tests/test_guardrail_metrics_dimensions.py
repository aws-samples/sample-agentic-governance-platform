"""Guardrail metrics read what CloudWatch actually publishes (demo-seeding finding, 2026-10-07).

AWS/Bedrock/Guardrails has `Invocations` and `InvocationsIntervened` keyed by
`GuardrailArn` + `GuardrailVersion`. There is no `GuardrailId` dimension and no
`InvocationsBlocked` metric, so the old query read 0 for every guardrail.

The read goes through GetMetricData: the ECS task role grants only
`cloudwatch:GetMetricData`, so GetMetricStatistics was AccessDenied and read 0 too.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from models.guardrail import GuardrailMetrics
from services.guardrail_service import GuardrailService

ARN = "arn:aws:bedrock:us-east-1:111122223333:guardrail/gr-abc"
TS1 = datetime(2026, 10, 7, 1, tzinfo=timezone.utc)
TS2 = datetime(2026, 10, 7, 2, tzinfo=timezone.utc)


def _svc(pages):
    svc = GuardrailService()
    cw = MagicMock()
    cw.get_metric_data.side_effect = pages
    svc._cloudwatch_client = cw
    return svc, cw


def test_metrics_query_the_published_metric_names_and_dimensions():
    svc, cw = _svc(
        [
            {
                "MetricDataResults": [
                    {"Id": "invocations", "Timestamps": [TS1], "Values": [40.0]},
                    {"Id": "intervened", "Timestamps": [TS1], "Values": [10.0]},
                ]
            }
        ]
    )
    m = svc.get_metrics("gr-abc", guardrail_arn=ARN, guardrail_version="DRAFT")
    cw.get_metric_statistics.assert_not_called()
    queries = cw.get_metric_data.call_args.kwargs["MetricDataQueries"]
    assert {q["MetricStat"]["Metric"]["MetricName"] for q in queries} == {
        "Invocations",
        "InvocationsIntervened",
    }
    for q in queries:
        stat = q["MetricStat"]
        assert stat["Metric"]["Namespace"] == "AWS/Bedrock/Guardrails"
        assert stat["Metric"]["Dimensions"] == [
            {"Name": "GuardrailArn", "Value": ARN},
            {"Name": "GuardrailVersion", "Value": "DRAFT"},
        ]
        assert (stat["Period"], stat["Stat"]) == (3600, "Sum")
    assert (m.total_invocations, m.blocked_count, m.allowed_count, m.block_rate) == (40, 10, 30, 25.0)
    assert m.model_dump(mode="json")["time_series"] == [{"timestamp": TS1.isoformat(), "invocations": 40}]


def test_metrics_follow_next_token_and_sort_the_series():
    svc, cw = _svc(
        [
            {
                "MetricDataResults": [{"Id": "invocations", "Timestamps": [TS2], "Values": [5.0]}],
                "NextToken": "t1",
            },
            {
                "MetricDataResults": [
                    {"Id": "invocations", "Timestamps": [TS1], "Values": [3.0]},
                    {"Id": "intervened", "Timestamps": [TS1, TS2], "Values": [1.0, 1.0]},
                ]
            },
        ]
    )
    m = svc.get_metrics("gr-abc", guardrail_arn=ARN)
    assert cw.get_metric_data.call_count == 2
    assert cw.get_metric_data.call_args_list[1].kwargs["NextToken"] == "t1"
    assert (m.total_invocations, m.blocked_count, m.allowed_count) == (8, 2, 6)
    assert [p["timestamp"] for p in m.model_dump(mode="json")["time_series"]] == [
        TS1.isoformat(),
        TS2.isoformat(),
    ]


def test_metrics_read_zero_when_cloudwatch_denies():
    svc, _ = _svc(Exception("AccessDenied"))
    m = svc.get_metrics("gr-abc", guardrail_arn=ARN)
    assert (m.total_invocations, m.blocked_count, m.block_rate) == (0, 0, 0)


def test_a_time_series_point_carries_its_timestamp_and_invocations():
    point = {"timestamp": TS1.isoformat(), "invocations": 3}
    metrics = GuardrailMetrics.model_validate({"guardrail_id": "gr-abc", "time_series": [point]})
    assert metrics.model_dump(mode="json")["time_series"] == [point]
    with pytest.raises(ValidationError):
        GuardrailMetrics.model_validate(
            {"guardrail_id": "gr-abc", "time_series": [{"timestamp": TS1.isoformat()}]}
        )
