"""Runtime resource-policy levers (deny / restore invokes) and runtime_service_name."""

import json
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from services import containment_native
from services.containment_native import RuntimeLevers, make_client_factory, runtime_service_name

RT = "arn:aws:bedrock-agentcore:us-east-1:<acct>:runtime/fraud_triage_dev-AbC123"
HARNESS = "arn:aws:bedrock-agentcore:us-east-1:<acct>:harness/fnol_e40_dev-XyZ789"
AGENT = SimpleNamespace(id="agent1")


def _not_found(op):
    return ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "no policy"}}, op)


class _FakeControl:
    def __init__(self, policy=None, get_error=None, put_error=None):
        self.policy = policy
        self.get_error = get_error
        self.put_error = put_error
        self.calls = []

    def get_resource_policy(self, resourceArn):
        self.calls.append(("get", resourceArn))
        if self.get_error:
            raise self.get_error
        return {"policy": self.policy} if self.policy is not None else {}

    def put_resource_policy(self, resourceArn, policy):
        self.calls.append(("put", resourceArn, policy))
        if self.put_error:
            raise self.put_error
        self.policy = policy
        return {"policy": policy}

    def delete_resource_policy(self, resourceArn):
        self.calls.append(("delete", resourceArn))
        self.policy = None
        return {}


def _levers(client):
    seen = []

    def factory(service, agent):
        seen.append((service, agent.id))
        return client

    return RuntimeLevers(client_factory=factory), seen


@pytest.mark.parametrize(
    "arn,name",
    [
        (RT, "fraud_triage_dev"),
        (RT + "/runtime-endpoint/DEFAULT", "fraud_triage_dev"),
        (RT + "/runtime-endpoint/DEFAULT:DEFAULT", "fraud_triage_dev"),
        (
            "arn:aws:bedrock-agentcore:us-east-1:<acct>:runtime/claims_fnol_prod-Q1w2E3r4T5",
            "claims_fnol_prod",
        ),
        ("arn:aws:bedrock-agentcore:us-east-1:<acct>:runtime/a_b_c-x-9Zz", "a_b_c-x"),
    ],
)
def test_runtime_service_name(arn, name):
    assert runtime_service_name(arn) == name


def test_deny_reads_old_policy_then_puts_a_single_deny():
    old = json.dumps({"Version": "2012-10-17", "Statement": [{"Sid": "Mine", "Effect": "Allow"}]})
    client = _FakeControl(policy=old)
    levers, seen = _levers(client)
    out = levers.deny_invokes(AGENT, RT)
    assert out == {"status": "ok", "before": old}
    assert seen == [("bedrock-agentcore-control", "agent1")]
    assert [c[0] for c in client.calls] == ["get", "put"]
    put = json.loads(client.calls[1][2])
    assert client.calls[1][1] == RT
    assert put["Statement"] == [
        {
            "Sid": "AgpContainDenyInvoke",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "bedrock-agentcore:InvokeAgentRuntime",
            "Resource": RT,
        }
    ]


def test_deny_with_no_policy_returns_before_none():
    levers, _ = _levers(_FakeControl())
    assert levers.deny_invokes(AGENT, RT) == {"status": "ok", "before": None}


def test_deny_over_our_own_deny_returns_before_none():
    own = json.dumps(
        {"Version": "2012-10-17", "Statement": [{"Sid": "AgpContainDenyInvoke", "Effect": "Deny"}]}
    )
    levers, _ = _levers(_FakeControl(policy=own))
    assert levers.deny_invokes(AGENT, RT) == {"status": "ok", "before": None}


def test_deny_over_a_mixed_policy_strips_our_statement_from_before():
    theirs = {"Sid": "Theirs", "Effect": "Allow"}
    mixed = json.dumps({"Version": "2012-10-17", "Statement": [{"Sid": "AgpContainDenyInvoke"}, theirs]})
    levers, _ = _levers(_FakeControl(policy=mixed))
    before = levers.deny_invokes(AGENT, RT)["before"]
    assert json.loads(before) == {"Version": "2012-10-17", "Statement": [theirs]}


def test_deny_with_not_found_on_get_returns_before_none():
    client = _FakeControl(get_error=_not_found("GetResourcePolicy"))
    levers, _ = _levers(client)
    assert levers.deny_invokes(AGENT, RT) == {"status": "ok", "before": None}
    assert client.calls[-1][0] == "put"


def test_deny_failure_on_get_does_not_put():
    client = _FakeControl(
        get_error=ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "Get")
    )
    levers, _ = _levers(client)
    out = levers.deny_invokes(AGENT, RT)
    assert out["status"] == "failed" and "AccessDeniedException" in out["detail"]
    assert [c[0] for c in client.calls] == ["get"]


def test_deny_failure_on_put_is_failed():
    client = _FakeControl(
        put_error=ClientError({"Error": {"Code": "ValidationException", "Message": "bad"}}, "Put")
    )
    levers, _ = _levers(client)
    out = levers.deny_invokes(AGENT, RT)
    assert out["status"] == "failed" and "ValidationException" in out["detail"]


def test_restore_puts_back_before():
    old = '{"Version":"2012-10-17","Statement":[]}'
    client = _FakeControl(policy="deny")
    levers, _ = _levers(client)
    assert levers.restore_invokes(AGENT, RT, old) == {"status": "ok"}
    assert client.calls == [("put", RT, old)]


def test_restore_deletes_when_before_is_none():
    client = _FakeControl(policy="deny")
    levers, _ = _levers(client)
    assert levers.restore_invokes(AGENT, RT, None) == {"status": "ok"}
    assert client.calls == [("delete", RT)]


def test_restore_delete_already_gone_is_ok():
    class Gone(_FakeControl):
        def delete_resource_policy(self, resourceArn):
            raise _not_found("DeleteResourcePolicy")

    levers, _ = _levers(Gone())
    assert levers.restore_invokes(AGENT, RT, None) == {"status": "ok"}


def test_restore_failure_is_failed():
    client = _FakeControl(
        put_error=ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "Put")
    )
    levers, _ = _levers(client)
    out = levers.restore_invokes(AGENT, RT, "{}")
    assert out["status"] == "failed" and "ThrottlingException" in out["detail"]


def test_harness_arn_is_skipped_for_both():
    client = _FakeControl()
    levers, seen = _levers(client)
    assert levers.deny_invokes(AGENT, HARNESS) == {"status": "skipped", "detail": "not a runtime"}
    assert levers.restore_invokes(AGENT, HARNESS, None) == {"status": "skipped", "detail": "not a runtime"}
    assert client.calls == [] and seen == []


def test_client_factory_failure_is_failed():
    def boom(service, agent):
        raise RuntimeError("assume role failed")

    out = RuntimeLevers(client_factory=boom).deny_invokes(AGENT, RT)
    assert out["status"] == "failed" and "RuntimeError" in out["detail"]


def test_make_client_factory_wraps_stage_client(monkeypatch):
    calls = []
    monkeypatch.setattr(
        containment_native,
        "stage_client",
        lambda service, cfg, *, session_suffix: calls.append((service, cfg, session_suffix)),
    )
    factory = make_client_factory(lambda agent: "cfg-" + agent.id)
    factory("logs", AGENT)
    make_client_factory()("bedrock-agentcore", AGENT)
    assert calls == [("logs", "cfg-agent1", "contain-agent1"), ("bedrock-agentcore", None, "contain-agent1")]
