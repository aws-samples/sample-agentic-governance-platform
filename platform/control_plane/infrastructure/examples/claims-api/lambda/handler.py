"""Claims API demo fixture: one Lambda behind a REST API Gateway.

Serves getPolicy, openClaim and getClaimStatus from in-memory sample data.
The same function is the API's REQUEST authorizer: it checks the x-api-key
header against the key stored in Secrets Manager (SECRET_ARN).
"""

import hmac
import json
import os
import uuid

import boto3

POLICIES = {
    "POL-1001": {
        "policyId": "POL-1001",
        "holder": "Lars Svensson",
        "product": "Home Comfort",
        "status": "active",
        "coverage": {"buildings": 750000, "contents": 120000, "currency": "EUR"},
    },
    "POL-1002": {
        "policyId": "POL-1002",
        "holder": "Marie Dubois",
        "product": "Motor Plus",
        "status": "active",
        "coverage": {"thirdParty": 1000000, "ownDamage": 40000, "currency": "EUR"},
    },
    "POL-1003": {
        "policyId": "POL-1003",
        "holder": "Jonas Weber",
        "product": "Travel Basic",
        "status": "lapsed",
        "coverage": {"medical": 50000, "currency": "EUR"},
    },
}

CLAIMS = {
    "CLM-2026-0001": {
        "claimId": "CLM-2026-0001",
        "policyId": "POL-1001",
        "description": "Water damage in the kitchen",
        "incidentDate": "2026-09-12",
        "status": "in_review",
    },
}

_REQUIRED_CLAIM_FIELDS = ("policyId", "description")
_api_key_cache = None


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _not_found(message):
    return _response(404, {"message": message})


def _get_policy(event):
    policy_id = (event.get("pathParameters") or {}).get("policyId", "")
    policy = POLICIES.get(policy_id)
    return _response(200, policy) if policy else _not_found(f"Policy {policy_id} not found")


def _open_claim(event):
    try:
        body = json.loads(event.get("body") or "")
    except ValueError:
        return _response(400, {"message": "Body must be JSON"})
    if not isinstance(body, dict):
        return _response(400, {"message": "Body must be a JSON object"})
    missing = [f for f in _REQUIRED_CLAIM_FIELDS if not isinstance(body.get(f), str) or not body[f]]
    if missing:
        return _response(400, {"message": f"Missing or empty fields: {', '.join(missing)}"})
    if "incidentDate" in body and not isinstance(body["incidentDate"], str):
        return _response(400, {"message": "incidentDate must be a string (YYYY-MM-DD)"})
    if body["policyId"] not in POLICIES:
        return _not_found(f"Policy {body['policyId']} not found")
    claim = {
        "claimId": f"CLM-{uuid.uuid4().hex[:8].upper()}",
        "policyId": body["policyId"],
        "description": body["description"],
        "incidentDate": body.get("incidentDate"),
        "status": "received",
    }
    CLAIMS[claim["claimId"]] = claim  # lives only as long as this warm container
    return _response(201, claim)


def _get_claim_status(event):
    claim_id = (event.get("pathParameters") or {}).get("claimId", "")
    claim = CLAIMS.get(claim_id)
    if not claim:
        return _not_found(f"Claim {claim_id} not found")
    return _response(200, {"claimId": claim_id, "status": claim["status"]})


ROUTES = {
    ("GET", "/policies/{policyId}"): _get_policy,
    ("POST", "/claims"): _open_claim,
    ("GET", "/claims/{claimId}"): _get_claim_status,
}


def _expected_api_key():
    global _api_key_cache
    if _api_key_cache is None:
        secret = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["SECRET_ARN"])
        _api_key_cache = json.loads(secret["SecretString"])["api_key"]
    return _api_key_cache


def _authorize(event):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    supplied = headers.get("x-api-key") or ""
    effect = "Allow" if hmac.compare_digest(supplied.encode(), _expected_api_key().encode()) else "Deny"
    return {
        "principalId": "claims-api-client",
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [{"Action": "execute-api:Invoke", "Effect": effect, "Resource": event["methodArn"]}],
        },
    }


def handler(event, _context):
    if event.get("type") == "REQUEST" and "methodArn" in event:
        return _authorize(event)
    route = ROUTES.get((event.get("httpMethod"), event.get("resource")))
    return route(event) if route else _not_found("No such route")
