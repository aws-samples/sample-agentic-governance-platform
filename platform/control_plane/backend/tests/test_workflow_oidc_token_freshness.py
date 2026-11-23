"""The GitHub OIDC token used to POST `/builds/runtime` is minted inside the 409 retry loop, never
once before it.

The retry loop (`builds.py`'s start_runtime_build 409 contract) can legitimately poll for up to
`maxWaitMs` (20 minutes) while a prior build for the same agent+stage finishes. GitHub OIDC tokens
are short-lived (about 5 min) and the backend checks expiry with a 60 s leeway
(`core/security_github_oidc.py`), so a token fetched once before the loop goes stale and the retry's
own POST starts 401ing well before the wait is over.

Checked on all four templates: the three container builds (strands/langgraph/crewai) and the
config-only harness deploy.
"""

import pathlib

import pytest

TEMPLATES_DIR = pathlib.Path(__file__).resolve().parents[2] / "agent-templates"

WORKFLOWS = [
    TEMPLATES_DIR / "strands-agentcore/.github/workflows/build.yml",
    TEMPLATES_DIR / "langgraph-agentcore/.github/workflows/build.yml",
    TEMPLATES_DIR / "crewai-agentcore/.github/workflows/build.yml",
    TEMPLATES_DIR / "harness-agentcore/.github/workflows/deploy.yml",
]


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.parts[-4])
def test_oidc_token_is_minted_exactly_once_and_inside_the_retry_loop(workflow):
    """The mint sits after the loop opens and before the fetch that uses it, so every iteration
    carries a fresh token instead of one hoisted above the loop and read once."""
    text = workflow.read_text()

    assert text.count("core.getIDToken('agp-runtime-build')") == 1, (
        "expected exactly one token mint — one stale pre-loop fetch plus one in-loop fetch "
        "would both match a bare substring count and hide the bug"
    )

    while_idx = text.index("while (true) {")
    token_idx = text.index("core.getIDToken('agp-runtime-build')")
    fetch_idx = text.index("await fetch(")

    assert while_idx < token_idx < fetch_idx
