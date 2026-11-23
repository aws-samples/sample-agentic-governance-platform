"""The projects <-> builds DI singletons must WIRE, not recurse.

The defect this pins was found live. Two route factories point at each other:
``api.routes.projects.get_project_service`` resolves the builds route's singleton so a governed
promote deploys through the SAME RuntimeBuildService as an OIDC build, and
``api.routes.builds.get_build_service`` resolves ProjectService to walk the infra chain
agent -> repo -> template -> infra template. Each factory published its module global only AFTER its
constructor returned, so resolving both eagerly recursed until ``RecursionError``, and because neither
global was ever assigned it recursed again on every request: a permanent 500 on ``GET /projects`` and
``GET /repositories``.

The whole suite was green through it because every other test patches ``_svc`` / ``_build_svc``
directly, so nothing ever constructed the real chain. This file does, in BOTH entry orders, because a
one-sided fix still recurses on whichever route is hit first.

The assertions are not just "no crash": both cross-links must be non-None. A build service left with
``_projects = None`` resolves no infra chain and ``_resolve_chain`` folds to the container path, so an
unattached singleton would deploy config (harness) agents as images instead of failing.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def routes(monkeypatch):
    """Both route modules, singletons reset, every AWS/Entra collaborator faked.

    Only the two factories under test stay real. No client construction reaches AWS. The agents
    route's singletons are patched on the agents module (builds.py imports them lazily, inside the
    factory) AND on the projects module (which imported them by name at import time, so the
    module-level alias is a separate binding). ``monkeypatch.setattr`` restores each global
    afterwards, so this file cannot leak a half-built singleton into the rest of the suite.
    """
    import boto3

    from api.routes import agents as agents_module
    from api.routes import builds as builds_module
    from api.routes import projects as projects_module

    monkeypatch.setattr(boto3, "client", lambda *a, **k: MagicMock())
    monkeypatch.setattr(boto3, "resource", lambda *a, **k: MagicMock())

    monkeypatch.setattr(agents_module, "get_service", lambda: MagicMock())
    monkeypatch.setattr(agents_module, "get_identity_service", lambda: MagicMock())
    monkeypatch.setattr(agents_module, "get_langfuse_service", lambda: MagicMock())
    monkeypatch.setattr(projects_module, "get_agent_registry", lambda: MagicMock())
    monkeypatch.setattr(projects_module, "get_identity_service", lambda: MagicMock())
    monkeypatch.setattr(projects_module, "get_langfuse_service", lambda: MagicMock())

    monkeypatch.setattr(projects_module, "_svc", None)
    monkeypatch.setattr(builds_module, "_build_svc", None)
    monkeypatch.setattr(builds_module, "_project_svc", None)
    monkeypatch.setattr(builds_module, "_conn_svc", None)
    return projects_module, builds_module


def _assert_wired_once(projects_module, builds_module):
    """One instance of each service, and BOTH directions of the link populated: ``_builds`` is the
    promote path (None means a governed promote cannot deploy), ``_projects`` the infra chain (None
    silently folds every config build to the container mechanism). The fix breaks the cycle by
    publish-then-attach ORDER, so neither factory may construct a second service while the other is
    mid-construction."""
    proj = projects_module._svc
    build = builds_module._build_svc
    assert proj is not None and build is not None

    assert proj._builds is build
    assert build._projects is proj
    assert builds_module._project_svc is proj
    assert projects_module.get_project_service() is proj
    assert builds_module.get_build_service() is build


def test_projects_first_wires_both_ways(routes):
    """The ``GET /projects`` entry order, the one that 500'd live."""
    projects_module, builds_module = routes

    svc = projects_module.get_project_service()

    assert svc is projects_module._svc
    _assert_wired_once(projects_module, builds_module)


def test_builds_first_wires_both_ways(routes):
    """The ``POST /builds/runtime`` entry order: a one-sided fix passes the test above and still
    recurses here."""
    projects_module, builds_module = routes

    build = builds_module.get_build_service()

    assert build is builds_module._build_svc
    _assert_wired_once(projects_module, builds_module)
