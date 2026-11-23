"""Unit tests for the Databricks invoke leg: the ``workspace_url`` check through httpx's parser, the
``bound_stage`` resolution, the module's public surface and the invoke refusal table's Databricks
rows. The route contract (every code, every status, the hostile-input suites and the
validate-then-mint sequence) lives in ``tests/test_invoke_route.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from models.agent import Agent, AgentAuthType, LifecycleState, Platform
from services.databricks_invoke import _resolve_databricks_workspace, invoke_databricks
from services.platform_adapter import InvokeError

APP_HANDLE = "https://claims-triage-1234.aws.databricksapps.com"
GOOD_WORKSPACE = "https://dbc-test.cloud.databricks.com"

HOSTILE_WORKSPACE_URLS = [
    "https://dbc-test.cloud.databricks.com/x\r\nX-Evil: 1",
    "https://dbc\xad-test.cloud.databricks.com",
    "https://xn--.cloud.databricks.com",
    "https://xn--a.cloud.databricks.com",
    "https://xn--evil.cloud.databricks.com",
]

ACTIONABLE_KINDS = ("federation_unavailable", "sp_secret_disabled", "workspace_stage_unresolved")


def _agent(**overrides) -> Agent:
    now = datetime.now(UTC)
    base: dict[str, Any] = dict(
        id="rec-db-1",
        name="claims-triage-db",
        purpose="Triage claims on Databricks",
        lifecycle_state=LifecycleState.APPROVED,
        platform=Platform.DATABRICKS,
        auth_type=AgentAuthType.ENTRA,
        tenant_id="ten-db",
        runtime_handle=APP_HANDLE,
        runtime_kind="app",
        created_at=now,
        updated_at=now,
        created_by="maria.bauer@example.com",
    )
    base.update(overrides)
    return Agent(**base)


def _tenant(*workspace_urls: str):
    """A duck-typed tenant: the resolver reads ``.stages`` and each stage's ``workspace_url`` through
    ``getattr``, so the hostile values a validated model would refuse to hold stay testable."""
    return SimpleNamespace(
        stages={
            name: SimpleNamespace(workspace_url=url)
            for name, url in zip(("dev", "prod", "qa"), workspace_urls, strict=False)
        }
    )


@pytest.mark.parametrize("workspace_url", HOSTILE_WORKSPACE_URLS)
def test_a_workspace_url_httpx_cannot_use_is_refused(workspace_url):
    """A CRLF in the path, a soft hyphen in the host and an empty or invalid punycode label each
    pass ``urlparse`` and fail only inside httpx, after a token would have been spent. The refusal
    is a 502 with the code and never echoes the URL, which names the customer's workspace."""
    with pytest.raises(InvokeError) as excinfo:
        _resolve_databricks_workspace(_agent(), _tenant(workspace_url))

    assert excinfo.value.status_code == 502
    assert excinfo.value.kind == "workspace_url_invalid"
    assert excinfo.value.detail == "the agent could not be invoked [workspace_url_invalid]"
    assert "databricks.com" not in excinfo.value.detail


@pytest.mark.parametrize(
    "workspace_url",
    [
        GOOD_WORKSPACE,
        "https://DBC-Test.Cloud.Databricks.Com",
        "https://dbc-test.cloud.databricks.com:443",
        "https://dbc-test.ß.databricks.com",
        "https://claims.azuredatabricks.net",
        "https://dbc-test.cloud.databricks.com/",
    ],
)
def test_a_usable_workspace_url_is_returned_unchanged(workspace_url):
    """Host case, an explicit port, a legitimate internationalised label, an Azure host and a
    trailing slash are all accepted, and the string the token call uses is the one validated."""
    assert _resolve_databricks_workspace(_agent(), _tenant(workspace_url)) == workspace_url


def test_only_the_accepted_candidate_is_validated():
    """A hostile stage that loses the prefix match must not refuse the invoke: its only role is to
    fail a ``startswith``, and validating it would let an unrelated stage's typo take down a
    working agent."""
    tenant = _tenant(APP_HANDLE.rsplit("-", 1)[0], "https://xn--.cloud.databricks.com")
    assert _resolve_databricks_workspace(_agent(), tenant) == APP_HANDLE.rsplit("-", 1)[0]


def test_the_multi_workspace_refusal_still_precedes_the_url_check():
    """An unresolvable stage is ``workspace_stage_unresolved``, not a URL verdict: the two codes
    point an operator at different fixes."""
    tenant = _tenant(GOOD_WORKSPACE, "https://dbc-prod.cloud.databricks.com")
    with pytest.raises(InvokeError) as excinfo:
        _resolve_databricks_workspace(_agent(), tenant)
    assert excinfo.value.kind == "workspace_stage_unresolved"
    assert "workspace_stage_unresolved" in excinfo.value.detail


def test_the_bound_stage_is_preferred_over_the_heuristic():
    """A record provisioning bound in a named stage resolves directly to that stage's workspace,
    even on a multi-workspace tenant the prefix heuristic could never disambiguate."""
    tenant = _tenant(GOOD_WORKSPACE, "https://dbc-prod.cloud.databricks.com")
    agent = _agent(bound_stage="prod")
    assert _resolve_databricks_workspace(agent, tenant) == "https://dbc-prod.cloud.databricks.com"


def test_a_legacy_record_without_a_bound_stage_falls_back_to_the_heuristic():
    tenant = _tenant(APP_HANDLE.rsplit("-", 1)[0], "https://dbc-prod.cloud.databricks.com")
    assert _resolve_databricks_workspace(_agent(bound_stage=None), tenant) == APP_HANDLE.rsplit("-", 1)[0]


def test_a_bound_stage_naming_no_live_workspace_falls_back_to_the_heuristic():
    tenant = _tenant(APP_HANDLE.rsplit("-", 1)[0])
    assert _resolve_databricks_workspace(_agent(bound_stage="prod"), tenant) == APP_HANDLE.rsplit("-", 1)[0]


def test_invoke_databricks_is_the_modules_only_public_name():
    """One export: ``DatabricksPlatformAdapter.invoke`` is the only caller, and a second public
    name would be a second way into a path whose order (validate, then mint) is its security
    property."""
    import services.databricks_invoke as leg

    public = {
        name
        for name in vars(leg)
        if not name.startswith("_") and getattr(getattr(leg, name), "__module__", None) == leg.__name__
    }
    assert public == {"invoke_databricks"}


@pytest.mark.parametrize("kind", ACTIONABLE_KINDS)
def test_the_actionable_kinds_name_a_fix_and_leak_nothing(kind):
    """The three operator-actionable codes carry a sentence composed from fixed text, ending in
    the code; never a workspace URL or a host."""
    detail = InvokeError.kinds[kind].detail
    assert isinstance(detail, str) and detail.endswith(f" [{kind}]")
    assert len(detail) > len(f"the agent could not be invoked [{kind}]")
    assert "databricks.com" not in detail
    assert "https://" not in detail


def test_every_other_databricks_kind_is_the_bare_code_sentence():
    bare = {
        "invalid_runtime_handle",
        "tenant_unresolved",
        "workspace_url_invalid",
        "sp_credential_unavailable",
        "federation_exchange_failed",
        "token_mint_failed",
        "binding_mode_unresolved",
    }
    for kind in bare:
        assert InvokeError.kinds[kind] == (502, f"the agent could not be invoked [{kind}]")


def test_the_leg_is_reachable_and_async():
    import inspect

    assert inspect.iscoroutinefunction(invoke_databricks)
