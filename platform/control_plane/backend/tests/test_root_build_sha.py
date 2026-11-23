from contextlib import contextmanager

from fastapi.testclient import TestClient

from models.health import RootResponse

_RELOAD_PREFIXES = ("api.", "core.config")


@contextmanager
def _main_reloaded_with_stage_prefix(monkeypatch):
    import sys

    monkeypatch.setenv("ROOT_PATH", "/dev")

    def _evict():
        for key in [k for k in sys.modules if k == "main" or k.startswith(_RELOAD_PREFIXES)]:
            sys.modules.pop(key, None)

    _evict()
    try:
        import main

        yield main
    finally:
        _evict()


def _assert_root_body(body: dict, build_sha: str) -> None:
    assert body["build_sha"] == build_sha
    assert body["name"] == "Control Plane API"
    assert body["version"] == "1.0.0"
    assert body["status"] == "running"
    assert set(body) == {"name", "version", "status", "build_sha"}


def test_bare_root_reports_build_sha_from_env(monkeypatch):
    monkeypatch.setenv("BUILD_SHA", "abc123")
    with _main_reloaded_with_stage_prefix(monkeypatch) as main:
        response = TestClient(main.app).get("/")

    assert response.status_code == 200
    _assert_root_body(response.json(), "abc123")


def test_stage_prefixed_root_reports_build_sha_from_env(monkeypatch):
    monkeypatch.setenv("BUILD_SHA", "abc123")
    with _main_reloaded_with_stage_prefix(monkeypatch) as main:
        response = TestClient(main.app).get("/dev/")

    assert response.status_code == 200
    _assert_root_body(response.json(), "abc123")


def test_bare_root_reports_unknown_without_build_sha(monkeypatch):
    monkeypatch.delenv("BUILD_SHA", raising=False)
    with _main_reloaded_with_stage_prefix(monkeypatch) as main:
        response = TestClient(main.app).get("/")

    assert response.status_code == 200
    _assert_root_body(response.json(), "unknown")


def test_stage_prefixed_root_reports_unknown_without_build_sha(monkeypatch):
    monkeypatch.delenv("BUILD_SHA", raising=False)
    with _main_reloaded_with_stage_prefix(monkeypatch) as main:
        response = TestClient(main.app).get("/dev/")

    assert response.status_code == 200
    _assert_root_body(response.json(), "unknown")


def test_build_sha_is_read_at_request_time(monkeypatch):
    monkeypatch.setenv("BUILD_SHA", "first")
    with _main_reloaded_with_stage_prefix(monkeypatch) as main:
        client = TestClient(main.app)
        assert client.get("/").json()["build_sha"] == "first"
        monkeypatch.setenv("BUILD_SHA", "second")
        assert client.get("/").json()["build_sha"] == "second"
        assert client.get("/dev/").json()["build_sha"] == "second"


def test_both_roots_declare_root_response(monkeypatch):
    monkeypatch.setenv("BUILD_SHA", "abc123")
    with _main_reloaded_with_stage_prefix(monkeypatch) as main:
        document = main.app.openapi()
        body = TestClient(main.app).get("/").json()

    for path in ("/", "/dev/"):
        schema = document["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": "#/components/schemas/RootResponse"}, path
    assert RootResponse.model_validate(body).model_dump() == body
