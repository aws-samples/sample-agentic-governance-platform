"""Tests for the eval Langfuse mirror.

``mirror_run`` pushes an eval run's dataset items and per-item scores into the agent's own
Langfuse project. It is an OPTIONAL mirror: it returns ``False`` (never raises) when the agent
has no Langfuse key or any call fails.

ALL HTTP is mocked: an ``httpx.Client`` over an ``httpx.MockTransport`` records every request
(the ``test_langfuse_metrics_service.py`` idiom, sync flavour). The per-agent key loader is
monkeypatched, so no Secrets Manager / AWS is touched.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from models.eval import DatasetItem, EvalConfig, EvalRun, ExpectedTool, ItemResult, Score
from services import eval_langfuse
from services.eval_langfuse import mirror_run

HOST = "https://langfuse.example.com"


@pytest.fixture(autouse=True)
def _host_and_keys(monkeypatch):
    monkeypatch.setattr(eval_langfuse.settings, "LANGFUSE_HOST", HOST)
    loaded: list[str] = []

    def fake_load(secret_name):
        loaded.append(secret_name)
        return "pk-test", "sk-test"

    monkeypatch.setattr(eval_langfuse, "_load_keys", fake_load)
    return loaded


def _recorder(status_for=lambda req: 200):
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(status_for(req), json={"id": "x"})

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def _agent(key_ref: str | None = "langfuse-agent-a1-keys"):
    return SimpleNamespace(id="a1", langfuse_key_secret_name=key_ref)


def _run():
    return EvalRun(
        run_id="run-1",
        repo_id="repo-1",
        agent_id="a1",
        project_id="p1",
        trigger="manual",
        started_by="user-1",
        started_at=datetime(2026, 10, 6, tzinfo=timezone.utc),
        config=EvalConfig(),
    )


def _items():
    return [
        DatasetItem(
            id="claim-1",
            input="Open a claim for policy P-1",
            expected_output="Claim opened",
            assertions=["opens a claim"],
            expected_tools=[ExpectedTool(name="open_claim", args={"policy": "P-1"})],
            tags=["smoke"],
        ),
        DatasetItem(id="claim-2", input="Hello"),
    ]


def _results():
    return [
        ItemResult(
            item_id="claim-1",
            session_id="sess-1",
            scores=[
                Score(evaluator="tools", kind="check", value=1.0, passed=True),
                Score(
                    evaluator="Builtin.Correctness",
                    kind="judge",
                    value=0.75,
                    passed=None,
                    explanation="mostly right",
                ),
                Score(evaluator="args", kind="check", value=None, passed=False, error="no tool-call data"),
            ],
        ),
        ItemResult(
            item_id="claim-2",
            session_id=None,
            scores=[Score(evaluator="no_error", kind="check", value=0.0, passed=False)],
        ),
    ]


def _body(req: httpx.Request) -> dict:
    return json.loads(req.content)


def test_no_key_returns_false_without_calls(_host_and_keys):
    http, calls = _recorder()
    assert mirror_run(_agent(key_ref=None), _run(), _items(), _results(), http=http) is False
    assert calls == []
    assert _host_and_keys == []


def test_no_host_returns_false_without_calls(monkeypatch):
    monkeypatch.setattr(eval_langfuse.settings, "LANGFUSE_HOST", "")
    http, calls = _recorder()
    assert mirror_run(_agent(), _run(), _items(), _results(), http=http) is False
    assert calls == []


def test_happy_path_call_sequence_and_payloads():
    http, calls = _recorder()
    assert mirror_run(_agent(), _run(), _items(), _results(), http=http) is True

    paths = [c.url.path for c in calls]
    assert paths == [
        "/api/public/v2/datasets",
        "/api/public/dataset-items",
        "/api/public/dataset-items",
        "/api/public/scores",
        "/api/public/scores",
    ]
    assert all(c.method == "POST" for c in calls)
    assert all(str(c.url).startswith(HOST) for c in calls)
    assert all(c.headers["authorization"].startswith("Basic ") for c in calls)

    assert _body(calls[0])["name"] == "agp-repo-1"

    item = _body(calls[1])
    assert item == {
        "datasetName": "agp-repo-1",
        "id": "repo-1:claim-1",
        "input": "Open a claim for policy P-1",
        "expectedOutput": "Claim opened",
        "metadata": {
            "assertions": ["opens a claim"],
            "expected_tools": [{"name": "open_claim", "args": {"policy": "P-1"}}],
            "tags": ["smoke"],
        },
    }
    assert _body(calls[2])["id"] == "repo-1:claim-2"
    assert _body(calls[2])["expectedOutput"] is None

    s1, s2 = _body(calls[3]), _body(calls[4])
    assert s1 == {
        "sessionId": "sess-1",
        "name": "tools",
        "value": 1.0,
        "comment": None,
        "dataType": "NUMERIC",
    }
    assert s2["name"] == "Builtin.Correctness"
    assert s2["value"] == 0.75
    assert s2["comment"] == "mostly right"


def test_dataset_already_exists_is_fine():
    http, calls = _recorder(lambda req: 409 if req.url.path.endswith("/datasets") else 200)
    assert mirror_run(_agent(), _run(), _items(), _results(), http=http) is True
    assert len(calls) == 5


def test_score_500_returns_false_and_does_not_raise():
    http, _ = _recorder(lambda req: 500 if req.url.path == "/api/public/scores" else 200)
    assert mirror_run(_agent(), _run(), _items(), _results(), http=http) is False


def test_transport_error_returns_false():
    def boom(req):
        raise httpx.ConnectError("down", request=req)

    http = httpx.Client(transport=httpx.MockTransport(boom))
    assert mirror_run(_agent(), _run(), _items(), _results(), http=http) is False


def test_key_load_failure_returns_false(monkeypatch):
    def fail(_):
        raise RuntimeError("secret missing")

    monkeypatch.setattr(eval_langfuse, "_load_keys", fail)
    http, calls = _recorder()
    assert mirror_run(_agent(), _run(), _items(), _results(), http=http) is False
    assert calls == []
