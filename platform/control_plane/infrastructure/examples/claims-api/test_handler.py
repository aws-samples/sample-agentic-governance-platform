"""Handler tests for the Claims API demo fixture.

Run from this directory with the backend venv:
    <main-checkout>/platform/control_plane/backend/venv/bin/python -m pytest test_handler.py -q
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lambda"))

import handler


def _event(method, resource, path_params=None, body=None):
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path_params,
        "body": body,
    }


def _call(event):
    resp = handler.handler(event, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_get_policy_returns_sample_policy():
    status, body = _call(_event("GET", "/policies/{policyId}", {"policyId": "POL-1001"}))
    assert status == 200
    assert body["policyId"] == "POL-1001"
    assert body["status"] == "active"


def test_get_policy_unknown_is_404():
    status, body = _call(_event("GET", "/policies/{policyId}", {"policyId": "POL-0000"}))
    assert status == 404
    assert "POL-0000" in body["message"]


def test_open_claim_returns_received_claim():
    payload = {"policyId": "POL-1001", "description": "Hail damage", "incidentDate": "2026-09-30"}
    status, body = _call(_event("POST", "/claims", body=json.dumps(payload)))
    assert status == 201
    assert body["claimId"].startswith("CLM-")
    assert body["status"] == "received"
    assert body["policyId"] == "POL-1001"


def test_open_claim_without_incident_date():
    payload = {"policyId": "POL-1002", "description": "Parking scrape"}
    status, body = _call(_event("POST", "/claims", body=json.dumps(payload)))
    assert status == 201
    assert body["status"] == "received"
    assert body["incidentDate"] is None


def test_open_claim_unknown_policy_is_404():
    payload = {"policyId": "POL-0000", "description": "x", "incidentDate": "2026-09-30"}
    status, _ = _call(_event("POST", "/claims", body=json.dumps(payload)))
    assert status == 404


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "not json",
        json.dumps(["a list"]),
        json.dumps({"policyId": "POL-1001"}),
        json.dumps({"policyId": "POL-1001", "description": "x", "incidentDate": 20260930}),
        json.dumps({"policyId": "POL-1001", "description": "", "incidentDate": "2026-09-30"}),
    ],
)
def test_open_claim_bad_body_is_400(raw):
    status, body = _call(_event("POST", "/claims", body=raw))
    assert status == 400
    assert body["message"]


def test_get_claim_status_sample_claim():
    status, body = _call(_event("GET", "/claims/{claimId}", {"claimId": "CLM-2026-0001"}))
    assert status == 200
    assert body["claimId"] == "CLM-2026-0001"
    assert body["status"]


def test_get_claim_status_unknown_is_404():
    status, _ = _call(_event("GET", "/claims/{claimId}", {"claimId": "CLM-NOPE"}))
    assert status == 404


def test_unknown_route_is_404():
    status, _ = _call(_event("DELETE", "/claims/{claimId}", {"claimId": "CLM-2026-0001"}))
    assert status == 404


def test_authorizer_allows_matching_key(monkeypatch):
    monkeypatch.setattr(handler, "_expected_api_key", lambda: "s3cret")
    event = {
        "type": "REQUEST",
        "methodArn": "arn:aws:execute-api:eu-central-1:111111111111:abc/v1/GET/claims/x",
        "headers": {"x-api-key": "s3cret"},
    }
    policy = handler.handler(event, None)["policyDocument"]
    assert policy["Statement"][0]["Effect"] == "Allow"


def test_authorizer_denies_wrong_key(monkeypatch):
    monkeypatch.setattr(handler, "_expected_api_key", lambda: "s3cret")
    event = {
        "type": "REQUEST",
        "methodArn": "arn:aws:execute-api:eu-central-1:111111111111:abc/v1/GET/claims/x",
        "headers": {"X-Api-Key": "wrong"},
    }
    policy = handler.handler(event, None)["policyDocument"]
    assert policy["Statement"][0]["Effect"] == "Deny"
