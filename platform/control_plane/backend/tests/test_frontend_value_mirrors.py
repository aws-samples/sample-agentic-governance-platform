"""Three values the frontend copies from the backend, read out of the TypeScript source and compared
to the value the backend enforces. Neither side imports the other, so an edit on one side alone lands
here, where the Python value lives; reading the .ts text means a test cannot pass by restating it."""

import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

OPERATIONS = Path(__file__).resolve().parents[2] / "frontend/src/components/operations"


def _mirrored(file_name: str, pattern: str) -> str:
    source = OPERATIONS / file_name
    assert source.is_file(), source
    (value,) = re.findall(pattern, source.read_text())
    return value


def test_the_link_redirect_url_ends_in_the_backends_callback_path():
    """GitHub refuses a redirect_uri that differs from the App's registered callback, and the backend
    compares the callback's path to ``LINK_CALLBACK_PATH`` exactly."""
    from services.github_user_link import LINK_CALLBACK_PATH

    mirrored = _mirrored(
        "githubLinkModel.ts",
        r"function buildLinkRedirectUrl\(origin: string\): string \{\s*return `\$\{origin\}([^`]*)`;",
    )
    assert mirrored == LINK_CALLBACK_PATH


def test_the_override_reason_cap_is_the_one_the_eval_gate_enforces():
    """The backend's cap is a bound inside the gate, not a named value, so it is measured: the
    frontend's maximum is accepted and one character more is refused."""
    from services.project_delivery import _eval_override
    from services.project_errors import ProjectError

    cap = int(_mirrored("promoteGate.ts", r"export const OVERRIDE_REASON_MAX = (\d+);"))
    svc = MagicMock()
    svc._evals.latest_run_for_build.return_value = None
    svc._now.return_value = datetime(2026, 11, 5, tzinfo=timezone.utc)

    def accepted(reason: str) -> bool:
        try:
            _eval_override(
                svc,
                MagicMock(id="r-1"),
                digest=None,
                tag="v1",
                promoted_by="owner-oid",
                override_reason=reason,
                promoted_by_name=None,
            )
        except ProjectError:
            return False
        return True

    assert accepted("x" * cap)
    assert not accepted("x" * (cap + 1))


def test_the_draft_file_limit_is_the_draft_routes_max_items():
    """The studio form allows exactly as many attached files as ``DraftRequest.files`` accepts."""
    from api.routes.studio import DraftRequest

    mirrored = int(_mirrored("studioModel.ts", r"export const DRAFT_MAX_FILES = (\d+);"))
    assert mirrored == DraftRequest.model_json_schema()["properties"]["files"]["maxItems"]
