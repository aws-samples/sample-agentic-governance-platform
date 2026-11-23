"""Tests for the Azure CLI token step and the Graph HTTP transport.

Fully offline: ``subprocess.run`` is mocked for the token tests and a fake
in-memory session is injected into ``GraphClient`` for the transport tests, so
this file never touches the network and never needs the Azure CLI installed.

The transport tests are the ones that matter most. The retry policy encodes
directory-replication behaviour that is expensive to rediscover by hand — a
freshly created application object is not immediately visible to the calls that
reference it, so Graph answers 400 or 404 for a while — and the two fail-fast
carve-outs (403, already-exists) are what keep a misconfigured run from hanging
for a minute before reporting a problem it knew about on the first response.
"""

from __future__ import annotations

import json
import subprocess
import sys
import traceback
from pathlib import Path
from unittest import mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import setup_entra


class FakeResponse:
    """Minimal stand-in for ``requests.Response``.

    ``body=None`` means "no JSON at all", which makes ``json()`` raise the same
    way a real empty 204 body does — that is the case the client has to survive
    when it reads an error message off a response that has none.
    """

    def __init__(self, status_code: int, body: dict | None = None, headers: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = "" if body is None else json.dumps(body)

    def json(self) -> dict:
        if self._body is None:
            raise ValueError("response body is not JSON")
        return self._body


class FakeSession:
    """Stand-in for ``requests.Session`` that replays queued responses.

    Records every call so a test can assert on the request COUNT — the retry
    tests are really about how many times the client goes back to the wire, not
    about what it sends. A queued *exception* stands for a transport failure: the
    real session raises instead of answering when the connection drops, times out
    or is refused by a proxy, and that is a shape the client has to classify too.
    """

    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self._responses:
            raise AssertionError("the client issued more requests than the test queued responses for")
        queued = self._responses.pop(0)
        if isinstance(queued, Exception):
            raise queued
        return queued


def graph_error_body(code: str = "Request_BadRequest", message: str = "boom") -> dict:
    """A Graph resource-endpoint error body, the shape errors are read out of."""
    return {"error": {"code": code, "message": message}}


def fake_token() -> setup_entra.AzToken:
    return setup_entra.AzToken(
        access_token="fake-access-token",
        tenant_id="11111111-1111-1111-1111-111111111111",
        expires_on=4102444800,
    )


@pytest.fixture
def recorded_sleeps(monkeypatch) -> list[float]:
    """Replace the retry sleep with a recorder so the tests stay instant."""
    sleeps: list[float] = []
    monkeypatch.setattr(setup_entra.time, "sleep", sleeps.append)
    return sleeps


def test_get_az_token_parses_the_cli_output() -> None:
    """The three fields the run needs come straight off the CLI's JSON.

    ``expires_on`` (POSIX, UTC) is read rather than ``expiresOn`` (local time),
    which is what the CLI documentation recommends for downstream consumers.
    """
    stdout = json.dumps(
        {
            "accessToken": "an-access-token",
            "expiresOn": "2026-08-13 10:00:00.000000",
            "expires_on": 1786000000,
            "subscription": "a-subscription-id",
            "tenant": "22222222-2222-2222-2222-222222222222",
            "tokenType": "Bearer",
        }
    )
    completed = mock.Mock(returncode=0, stdout=stdout, stderr="")

    with mock.patch.object(setup_entra.subprocess, "run", return_value=completed) as run:
        token = setup_entra.get_az_token()

    assert token.access_token == "an-access-token"
    assert token.tenant_id == "22222222-2222-2222-2222-222222222222"
    assert token.expires_on == 1786000000
    assert "ms-graph" in run.call_args.args[0]


def test_get_az_token_reports_a_missing_cli() -> None:
    """No ``az`` on PATH surfaces as a preflight failure naming the Azure CLI."""
    with mock.patch.object(setup_entra.subprocess, "run", side_effect=FileNotFoundError):
        with pytest.raises(setup_entra.PreflightError) as excinfo:
            setup_entra.get_az_token()

    assert "azure cli" in str(excinfo.value).lower()


def test_get_az_token_nonzero_exit_hints_at_signing_in() -> None:
    """The overwhelmingly common cause of a nonzero exit is not being signed in,
    so the message has to say how to fix it rather than echo a raw CLI error."""
    completed = mock.Mock(
        returncode=1,
        stdout="",
        stderr="ERROR: Please run 'az login' to setup account.",
    )

    with mock.patch.object(setup_entra.subprocess, "run", return_value=completed):
        with pytest.raises(setup_entra.PreflightError) as excinfo:
            setup_entra.get_az_token()

    assert "az login" in str(excinfo.value)


def test_get_az_token_rejects_unparseable_output() -> None:
    """Output that is not JSON is a preflight failure, not a crash mid-run."""
    completed = mock.Mock(returncode=0, stdout="not json at all", stderr="")

    with mock.patch.object(setup_entra.subprocess, "run", return_value=completed):
        with pytest.raises(setup_entra.PreflightError):
            setup_entra.get_az_token()


@pytest.mark.parametrize(
    "stdout",
    [
        pytest.param(
            '{"accessToken": "a-token", "tenant": "a-tenant", "expires_on": "soon"}', id="expiry-not-a-number"
        ),
        pytest.param(
            '{"accessToken": "a-token", "tenant": "a-tenant", "expires_on": "1786000000.0"}',
            id="expiry-not-an-integer",
        ),
        pytest.param("[1, 2, 3]", id="not-an-object"),
    ],
)
def test_get_az_token_rejects_a_token_response_it_cannot_read(stdout: str) -> None:
    """Every unusable shape of token response is classified, coercion included.

    An unguarded ``int()`` on the expiry, or a field read against something that
    is not an object, would escape as a bare traceback — from the one function
    whose whole job is turning the operator's environment into a message that says
    what to do about it.
    """
    completed = mock.Mock(returncode=0, stdout=stdout, stderr="")

    with mock.patch.object(setup_entra.subprocess, "run", return_value=completed):
        with pytest.raises(setup_entra.PreflightError):
            setup_entra.get_az_token()


def test_get_az_token_bounds_how_long_it_waits_for_the_cli() -> None:
    """The CLI call is time-boxed, and running out of time is a classified failure.

    Both of the CLI's streams are captured, so a CLI stalled on a wedged token
    cache or an unreachable login endpoint would otherwise show the operator a
    dead terminal with nothing printed and no indication of what to do.
    """
    timeout = subprocess.TimeoutExpired(cmd="az", timeout=60)

    with mock.patch.object(setup_entra.subprocess, "run", side_effect=timeout) as run:
        with pytest.raises(setup_entra.PreflightError) as excinfo:
            setup_entra.get_az_token()

    assert run.call_args.kwargs["timeout"] == 60
    assert "60 seconds" in str(excinfo.value)


def test_a_cli_timeout_leaves_the_partial_output_out_of_the_error() -> None:
    """``TimeoutExpired`` carries the stdout captured so far, the stream that holds the access token,
    so the PreflightError raised for it is not chained to it and no traceback can print it."""
    timeout = subprocess.TimeoutExpired(cmd="az", timeout=60, output='{"accessToken": "fake-access-token"}')

    with mock.patch.object(setup_entra.subprocess, "run", side_effect=timeout):
        with pytest.raises(setup_entra.PreflightError) as excinfo:
            setup_entra.get_az_token()

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    assert "fake-access-token" not in "".join(traceback.format_exception(excinfo.value))


@pytest.mark.parametrize("transient_status", [400, 404])
def test_transient_status_is_retried_then_succeeds(
    transient_status: int, recorded_sleeps: list[float]
) -> None:
    """400 and 404 are the two shapes a not-yet-replicated object produces.

    400 being retriable is the unusual half: creating a service principal for an
    application that has not replicated yet fails with 400, not 404, so a policy
    that retries only 404 flakes.
    """
    session = FakeSession(
        [
            FakeResponse(transient_status, graph_error_body(message="not replicated yet")),
            FakeResponse(201, {"id": "new-object-id"}),
        ]
    )
    client = setup_entra.GraphClient(fake_token(), session=session)

    result = client.post("/servicePrincipals", {"appId": "an-app-id"})

    assert result == {"id": "new-object-id"}
    assert len(session.calls) == 2
    assert recorded_sleeps == [1.0]


def test_throttling_honours_retry_after(recorded_sleeps: list[float]) -> None:
    """A 429 carrying ``Retry-After`` waits exactly as long as Graph asked."""
    session = FakeSession(
        [
            FakeResponse(
                429,
                graph_error_body(code="TooManyRequests", message="throttled"),
                headers={"Retry-After": "3"},
            ),
            FakeResponse(200, {"ok": True}),
        ]
    )
    client = setup_entra.GraphClient(fake_token(), session=session)

    assert client.post("/servicePrincipals/sp-id/appRoleAssignedTo", {}) == {"ok": True}
    assert recorded_sleeps == [3.0]


@pytest.mark.parametrize(
    ("retry_after", "expected_sleep"),
    [
        pytest.param("3600", 60.0, id="an-absurd-value-is-capped-at-the-retry-after-ceiling"),
        pytest.param("30", 30.0, id="a-plausible-value-is-obeyed-beyond-the-schedule-cap"),
        pytest.param("-5", 1.0, id="a-negative-value-takes-the-schedule-never-reaching-sleep"),
        pytest.param("xyz", 1.0, id="junk-takes-the-schedule"),
    ],
)
def test_retry_after_is_clamped_into_the_bounded_window(
    retry_after: str, expected_sleep: float, recorded_sleeps: list[float]
) -> None:
    """``Retry-After`` is a value the server chooses, so the policy has to survive
    every value it might choose — including a huge one and a nonsensical one."""
    session = FakeSession(
        [
            FakeResponse(
                429,
                graph_error_body(code="TooManyRequests", message="throttled"),
                headers={"Retry-After": retry_after},
            ),
            FakeResponse(200, {"ok": True}),
        ]
    )
    client = setup_entra.GraphClient(fake_token(), session=session)

    assert client.get("/me") == {"ok": True}
    assert recorded_sleeps == [expected_sleep]


def test_forbidden_fails_fast_with_directory_role_guidance(
    recorded_sleeps: list[float],
) -> None:
    """403 means a missing DIRECTORY ROLE, so it must never be retried.

    Retrying it would turn a clear permissions problem into a minute-long hang,
    and the guidance must point at directory roles — the caller already holds the
    right API permissions, otherwise the token would not have been issued.
    """
    session = FakeSession(
        [
            FakeResponse(
                403, graph_error_body(code="Authorization_RequestDenied", message="Insufficient privileges.")
            )
        ]
    )
    client = setup_entra.GraphClient(fake_token(), session=session)

    with pytest.raises(setup_entra.GraphAuthError) as excinfo:
        client.post("/applications", {"displayName": "an app"})

    assert len(session.calls) == 1
    assert recorded_sleeps == []
    assert excinfo.value.status == 403
    assert "directory role" in str(excinfo.value).lower()


@pytest.mark.parametrize("status", [400, 409])
@pytest.mark.parametrize(
    "message",
    [
        pytest.param(
            "A conflicting object with one or more of the specified property values already exists.",
            id="the-already-exists-phrase",
        ),
        pytest.param(
            "Permission entry already present for principal 33333333-3333-3333-3333-333333333333.",
            id="the-permission-entry-already-phrase-which-never-says-exists",
        ),
    ],
)
def test_already_exists_is_terminal_not_retried(
    status: int, message: str, recorded_sleeps: list[float]
) -> None:
    """A duplicate consent or assignment is the desired end state, not a failure.

    It arrives as a 400 with a generic ``Request_BadRequest`` code, so the message
    is the only discriminator — and the check has to run BEFORE the retry
    decision, or an already-satisfied grant gets retried for the full window.
    """
    matched = [phrase for phrase in setup_entra.ALREADY_EXISTS_SUBSTRINGS if phrase in message.lower()]
    assert len(matched) == 1

    session = FakeSession([FakeResponse(status, graph_error_body(message=message))])
    client = setup_entra.GraphClient(fake_token(), session=session)

    with pytest.raises(setup_entra.AlreadyExists):
        client.post("/oauth2PermissionGrants", {"clientId": "spa-sp-id"})

    assert len(session.calls) == 1
    assert recorded_sleeps == []


def test_retries_are_bounded_and_then_give_up(recorded_sleeps: list[float]) -> None:
    """Replication can lag for up to about a minute, but the window is finite.

    Eleven attempts with a per-attempt delay capped at eight seconds spends about
    52 seconds waiting and then reports the failure instead of hanging forever.
    """
    session = FakeSession([FakeResponse(400, graph_error_body(message="still not replicated"))] * 11)
    client = setup_entra.GraphClient(fake_token(), session=session)

    with pytest.raises(setup_entra.GraphError) as excinfo:
        client.post("/servicePrincipals", {"appId": "an-app-id"})

    assert type(excinfo.value) is setup_entra.GraphError
    assert len(session.calls) == 11
    assert recorded_sleeps == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 8.0, 8.0]
    assert sum(recorded_sleeps) == 52.0


def test_server_errors_are_retried(recorded_sleeps: list[float]) -> None:
    """5xx is transient by definition; one retry is enough to prove it is in the set."""
    session = FakeSession(
        [
            FakeResponse(503, graph_error_body(code="serviceNotAvailable", message="busy")),
            FakeResponse(200, {"ok": True}),
        ]
    )
    client = setup_entra.GraphClient(fake_token(), session=session)

    assert client.get("/me") == {"ok": True}
    assert len(session.calls) == 2


def test_a_dropped_connection_is_retried_then_succeeds(
    recorded_sleeps: list[float],
) -> None:
    """A transport failure is the same transience as a 503 and gets the same window.

    On a laptop this is the most likely failure of all — a VPN reconnect, a
    corporate proxy, a captive portal — and it is the one shape that arrives as an
    exception rather than a status. Left uncaught it would abort a run that creates
    registrations, grants consent and assigns a role, leaving that half done.
    """
    session = FakeSession(
        [
            requests.exceptions.ConnectionError("connection reset by peer"),
            FakeResponse(201, {"id": "new-object-id"}),
        ]
    )
    client = setup_entra.GraphClient(fake_token(), session=session)

    assert client.post("/applications", {"displayName": "an app"}) == {"id": "new-object-id"}
    assert len(session.calls) == 2
    assert recorded_sleeps == [1.0]


def test_an_unreachable_graph_gives_up_with_a_classified_failure(
    recorded_sleeps: list[float],
) -> None:
    """When the network never comes back, the run ends with a message, not a stack.

    The failure names the exception CLASS — which is what tells the operator
    whether to look at the network, the proxy or the certificate chain — and
    nothing else about the request.
    """
    session = FakeSession([requests.exceptions.Timeout("timed out")] * 11)
    client = setup_entra.GraphClient(fake_token(), session=session)

    with pytest.raises(setup_entra.GraphError) as excinfo:
        client.post("/applications", {"displayName": "an app"})

    assert type(excinfo.value) is setup_entra.GraphError
    assert len(session.calls) == 11
    assert recorded_sleeps == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 8.0, 8.0]
    assert "Timeout" in str(excinfo.value)


def test_get_sends_bearer_and_query_parameters() -> None:
    """Every call is a bearer-authenticated request against the versioned base URL."""
    session = FakeSession([FakeResponse(200, {"value": []})])
    client = setup_entra.GraphClient(fake_token(), session=session)

    client.get("/servicePrincipals", params={"$select": "id,appRoles"})

    call = session.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == f"{setup_entra.GRAPH_BASE}/servicePrincipals"
    assert call["headers"]["Authorization"] == "Bearer fake-access-token"
    assert call["params"] == {"$select": "id,appRoles"}


def test_patch_returns_nothing_for_an_empty_response_body() -> None:
    """Graph answers a successful PATCH with 204 and no body, so there is nothing
    to hand back — the caller must not have to guess whether a dict is coming."""
    session = FakeSession([FakeResponse(204)])
    client = setup_entra.GraphClient(fake_token(), session=session)

    assert client.patch("/servicePrincipals/sp-id", {"appRoleAssignmentRequired": True}) is None
    assert session.calls[0]["method"] == "PATCH"


def test_the_request_body_reaches_the_wire_as_a_json_body() -> None:
    """The dict the caller passed is the dict that gets sent, as JSON.

    This is the seam between the payload builders and the transport, and nothing
    else in the suite crosses it: a client that sent no body, or form-encoded the
    dict instead of serialising it, would satisfy every other test here while
    creating an app registration with none of its fields — the one call that is
    never repaired afterwards.
    """
    create_body = setup_entra.spa_app_payload(
        name="AGP - Frontend",
        backend_app_id="bbbbbbbb-0000-0000-0000-000000000002",
        scope_id="aaaaaaaa-0000-0000-0000-000000000001",
    )
    patch_body = {"appRoleAssignmentRequired": True}
    session = FakeSession([FakeResponse(201, {"id": "new-object-id"}), FakeResponse(204)])
    client = setup_entra.GraphClient(fake_token(), session=session)

    client.post("/applications", create_body)
    client.patch("/servicePrincipals/sp-id", patch_body)

    assert session.calls[0]["json"] == create_body
    assert session.calls[1]["json"] == patch_body


def test_every_request_is_time_boxed_and_never_weakens_tls(
    recorded_sleeps: list[float],
) -> None:
    """Two deliberate safety guards, both invisible in a passing run.

    Without a timeout a wedged connection hangs the script silently instead of
    failing and retrying; with TLS verification off the bearer token would be
    handed to whatever presented itself as Microsoft Graph. Either could be
    dropped in an edit or a merge resolution and nothing else here would notice, so
    the invariant is asserted over every request the client makes — the retried
    ones included, since the retry path builds its own way to the wire.
    """
    session = FakeSession(
        [
            FakeResponse(200, {"value": []}),
            FakeResponse(201, {"id": "new-object-id"}),
            FakeResponse(400, graph_error_body(message="not replicated yet")),
            FakeResponse(204),
        ]
    )
    client = setup_entra.GraphClient(fake_token(), session=session)

    client.get("/servicePrincipals")
    client.post("/applications", {"displayName": "an app"})
    client.patch("/servicePrincipals/sp-id", {"appRoleAssignmentRequired": True})

    assert len(session.calls) == 4
    for call in session.calls:
        assert call["timeout"] == 30
        assert call.get("verify", True) is True


def test_failures_never_put_the_access_token_in_their_message(
    recorded_sleeps: list[float],
) -> None:
    """The script's loudest security claim, made executable.

    The two failures that carry the most detail are the ones checked: a refusal,
    which quotes Graph's own words back, and an unreachable network, where the
    underlying exception is the one object that holds the prepared request and
    therefore the bearer header. The transport failure below is deliberately given
    a message containing the token, so a client that echoed the exception it caught
    would fail this test.
    """
    auth_session = FakeSession([FakeResponse(403, graph_error_body(message="Insufficient privileges."))])
    with pytest.raises(setup_entra.GraphAuthError) as auth_failure:
        setup_entra.GraphClient(fake_token(), session=auth_session).post(
            "/applications", {"displayName": "an app"}
        )

    transport_session = FakeSession(
        [requests.exceptions.ConnectionError("refused: Bearer fake-access-token")] * 11
    )
    with pytest.raises(setup_entra.GraphError) as transport_failure:
        setup_entra.GraphClient(fake_token(), session=transport_session).post(
            "/applications", {"displayName": "an app"}
        )

    for failure in (auth_failure, transport_failure):
        assert "fake-access-token" not in str(failure.value)
        assert "fake-access-token" not in repr(failure.value)
