"""Tests for the printed output block — the script's only product.

The script writes nothing to disk, so this text IS the deliverable: the operator
copies it into `platform/control_plane/infrastructure/secrets.auto.tfvars` and
`platform/control_plane/frontend/.env`. That makes every key name a contract with
two config files and one deploy script, and a typo in one of them is not a cosmetic
bug — `deploy-full.sh` refuses to deploy when a `VITE_ENTRA_*` value is missing,
and the backend answers a bare 500 to every authenticated request when a
`entra_*` variable is empty. So the key names are asserted here byte for byte,
transcribed from `docs/entra-setup.md` ("Where every value goes") rather than read out of the module.

The other three things this file pins are the ones an operator loses if the text is
merely nearly right:

- the client secret appears exactly ONCE, flagged as un-recoverable, because
  Microsoft Entra discloses it only in the response that created it;
- the two post-deploy values are named as post-deploy, together with the
  redirect-URI registration in Microsoft Entra, which is the step that silently
  breaks sign-in when it is skipped;
- what the run did for the person who ran it, so an operator who is not sure
  whether they can sign in does not have to go and look.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# The script is a standalone single file, not an installed package: put its
# directory on the import path so the tests run the same way regardless of the
# working directory pytest was invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import setup_entra  # noqa: E402  (import follows the sys.path bootstrap above)


# The five Terraform variable names, transcribed from `docs/entra-setup.md`'s backend table
# rather than referenced from the module: two independent copies of a contract are
# what make a rename in either place visible instead of self-consistent.
TFVARS_KEYS = (
    "entra_tenant_id",
    "entra_audience",
    "entra_backend_client_id",
    "entra_backend_client_secret",
    "entra_spa_client_id",
)

# The five frontend keys, from the same doc's frontend table. The other two the SPA declares —
# `VITE_API_URL` and `VITE_ENTRA_SPA_REDIRECT_URI` — do not exist until after the
# first deploy and are deliberately NOT printed as values.
VITE_KEYS = (
    "VITE_AUTH_PROVIDER",
    "VITE_ENTRA_TENANT_ID",
    "VITE_ENTRA_TENANT_DOMAIN",
    "VITE_ENTRA_SPA_CLIENT_ID",
    "VITE_ENTRA_SPA_SCOPE",
)

# The two files the two blocks are pasted into, transcribed whole rather than by
# their basenames: a heading that names the right file in the wrong directory sends
# the operator to create a second `secrets.auto.tfvars` that Terraform never reads,
# and every value in it then looks unset for reasons nothing explains.
TFVARS_PATH = "platform/control_plane/infrastructure/secrets.auto.tfvars"
FRONTEND_ENV_PATH = "platform/control_plane/frontend/.env"

TENANT_ID = "11111111-1111-1111-1111-111111111111"
TENANT_DOMAIN = "contoso.onmicrosoft.com"
BACKEND_CLIENT_ID = "44444444-4444-4444-4444-444444444444"
SPA_CLIENT_ID = "55555555-5555-5555-5555-555555555555"
SECRET = "a-generated-client-secret-value"
UPN = "admin@contoso.onmicrosoft.com"


def render(**overrides) -> str:
    """The output of a successful fresh run, unless a keyword says otherwise."""
    arguments = {
        "tenant_id": TENANT_ID,
        "tenant_domain": TENANT_DOMAIN,
        "audience": setup_entra.DEFAULT_AUDIENCE,
        "backend_client_id": BACKEND_CLIENT_ID,
        "spa_client_id": SPA_CLIENT_ID,
        "secret": SECRET,
        "secret_outcome": "created",
        "me_upn": UPN,
        "admin_assigned": True,
        "dry_run": False,
    }
    arguments.update(overrides)
    return setup_entra.render_output(**arguments)


def assignment(key: str, value: str) -> re.Pattern[str]:
    """A Terraform assignment: the key at the start of a line, the value quoted.

    Whitespace around ``=`` is free (the block is column-aligned for reading), the
    key and the quoting are not — `secrets.auto.tfvars` is HCL, and an unquoted
    string is a parse error rather than a wrong value.
    """
    return re.compile(rf'^{re.escape(key)} *= "{re.escape(value)}"$', re.MULTILINE)


def env_line(key: str, value: str) -> re.Pattern[str]:
    """A dotenv assignment: no spaces, no quotes — Vite reads the value literally,
    so a quoted one arrives in the browser with the quotes still on it."""
    return re.compile(rf"^{re.escape(key)}={re.escape(value)}$", re.MULTILINE)


# ===========================================================================
# The two config blocks
# ===========================================================================
def test_every_terraform_key_is_present_and_quoted() -> None:
    """All five, spelled as `variables.tf` declares them, with the values a paste
    into `secrets.auto.tfvars` needs."""
    rendered = render()

    expected = {
        "entra_tenant_id": TENANT_ID,
        "entra_audience": setup_entra.DEFAULT_AUDIENCE,
        "entra_backend_client_id": BACKEND_CLIENT_ID,
        "entra_backend_client_secret": SECRET,
        "entra_spa_client_id": SPA_CLIENT_ID,
    }
    assert set(expected) == set(TFVARS_KEYS)
    for key, value in expected.items():
        assert assignment(key, value).search(rendered), f"{key} missing or misquoted"


def test_every_frontend_key_is_present_unquoted() -> None:
    """All five known ones, including the two the SPA never reads itself but
    `deploy-full.sh` refuses to deploy without."""
    rendered = render()

    expected = {
        "VITE_AUTH_PROVIDER": "entra",
        "VITE_ENTRA_TENANT_ID": TENANT_ID,
        "VITE_ENTRA_TENANT_DOMAIN": TENANT_DOMAIN,
        "VITE_ENTRA_SPA_CLIENT_ID": SPA_CLIENT_ID,
        "VITE_ENTRA_SPA_SCOPE": "api://agp/Access.Default",
    }
    assert set(expected) == set(VITE_KEYS)
    for key, value in expected.items():
        assert env_line(key, value).search(rendered), f"{key} missing or misformatted"


def test_the_scope_is_the_audience_joined_to_the_scope_name() -> None:
    """The one composed value in the block, and the one that fails as a bare
    `401 Invalid audience` on every API call when its halves disagree."""
    rendered = render(audience="api://agp-test")

    assert env_line(
        "VITE_ENTRA_SPA_SCOPE", f"api://agp-test/{setup_entra.SCOPE_NAME}"
    ).search(rendered)


def test_both_config_files_are_named_so_the_values_have_somewhere_to_go() -> None:
    """A block of keys with no destination is a puzzle, not an instruction.

    The whole path, not just the file name: `secrets.auto.tfvars` in the wrong
    directory is not read by Terraform and not reported as missing either, so every
    value in it appears unset for reasons nothing in the deployment explains.
    """
    rendered = render()

    assert TFVARS_PATH in rendered
    assert FRONTEND_ENV_PATH in rendered


# ===========================================================================
# The secret
# ===========================================================================
def test_the_secret_is_printed_once_and_flagged_as_un_recoverable() -> None:
    """Exactly once, on purpose.

    Microsoft Entra returns a client secret only in the response that creates it,
    so this print is the operator's single chance to capture it. Printing it twice
    would double the number of places it can be scrolled past, screenshotted, or
    pasted into the wrong window; printing it without saying it cannot be re-read
    is how it gets skipped and the registration has to be recreated.
    """
    rendered = render()

    assert rendered.count(SECRET) == 1
    lowered = rendered.lower()
    assert "only" in lowered and "cannot" in lowered


def test_a_reused_registration_says_the_existing_secret_cannot_be_read_back() -> None:
    """A re-run has no secret to print, and must not imply it does: the value in
    `secrets.auto.tfvars` is still the right one and must be left alone."""
    rendered = render(secret=None, secret_outcome="existing")

    assert assignment(
        "entra_backend_client_secret", "<existing secret — cannot be re-read>"
    ).search(rendered)
    assert SECRET not in rendered
    assert "--rotate-secret" in rendered


def test_a_dry_run_against_an_existing_backend_predicts_that_no_secret_is_coming() -> None:
    """The prediction is the point of a dry run, and this is the one it used to get
    backwards.

    A dry run against a tenant whose backend registration already exists has already
    established that no secret will be minted — by this run or by the real one after
    it. Printing `<created-on-real-run>` there tells the operator to wait for a value
    that is never going to arrive, and the real run that then prints "cannot be
    re-read" reads as a failure rather than as the correct answer.
    """
    rendered = render(secret=None, secret_outcome="existing", dry_run=True)

    assert assignment(
        "entra_backend_client_secret", "<existing secret — cannot be re-read>"
    ).search(rendered)
    assert "<created-on-real-run>" not in rendered
    assert "will NOT" in rendered
    assert "--rotate-secret" in rendered


def test_a_dry_run_with_rotation_predicts_a_second_secret() -> None:
    """The mirror image: with `--rotate-secret` a real run WOULD mint one, so the
    note must not say nothing is coming — and must say it is a second credential
    valid alongside the first, which is what makes a rotation deployable."""
    rendered = render(secret=None, secret_outcome="rotated", dry_run=True)

    assert assignment("entra_backend_client_secret", "<created-on-real-run>").search(
        rendered
    )
    lowered = rendered.lower()
    assert "--rotate-secret" in rendered
    assert "second secret" in lowered
    assert "alongside" in lowered


def test_a_dry_run_marks_the_values_it_cannot_know() -> None:
    """A plan is not a result. Every value that only a real run can produce says so,
    so that a dry-run block pasted into a config file fails loudly rather than
    deploying a stack with an angle-bracketed client id in it.
    """
    rendered = render(
        backend_client_id="<backend-app-id>",
        spa_client_id="<spa-app-id>",
        secret=None,
        dry_run=True,
    )

    assert assignment("entra_backend_client_id", "<created-on-real-run>").search(rendered)
    assert assignment("entra_spa_client_id", "<created-on-real-run>").search(rendered)
    assert assignment("entra_backend_client_secret", "<created-on-real-run>").search(
        rendered
    )
    # The tenant-level values ARE known in a dry run, and are printed for real.
    assert assignment("entra_tenant_id", TENANT_ID).search(rendered)
    assert env_line("VITE_ENTRA_TENANT_DOMAIN", TENANT_DOMAIN).search(rendered)


# ===========================================================================
# The after-you-deploy note
# ===========================================================================
def test_the_note_names_both_post_deploy_values_and_their_source() -> None:
    """Neither exists until the first apply, so neither is printed as a value — but
    an operator who is not told they are missing reads the block as complete."""
    rendered = render()

    assert "VITE_API_URL" in rendered
    assert "VITE_ENTRA_SPA_REDIRECT_URI" in rendered
    assert "terraform output" in rendered


def test_the_note_demands_the_redirect_uri_be_registered_in_entra_too() -> None:
    """The step that silently breaks sign-in when it is skipped.

    Setting `VITE_ENTRA_SPA_REDIRECT_URI` in the frontend is only half of it: the
    same URI has to be added to the frontend registration in Microsoft Entra, or
    sign-in fails with a redirect-URI mismatch that says nothing about a config
    file. This script deliberately does not do it (the address does not exist until
    the deployment does), which is exactly why it has to say so.
    """
    rendered = render()

    lowered = rendered.lower()
    assert "redirect" in lowered
    assert "entra" in lowered
    assert "register" in lowered


# ===========================================================================
# What the run did for the runner
# ===========================================================================
def test_the_block_reports_the_role_assignment_for_the_runner() -> None:
    """Named account, named role: it is the difference between an operator who can
    sign in and one who gets a token with no roles claim and no explanation."""
    rendered = render()

    assert re.search(rf"assigned {re.escape(UPN)} to Platform\.Admin", rendered)


def test_an_unassigned_runner_is_reported_as_unassigned() -> None:
    """When the role could not be assigned — a registration that predates it, say —
    the block must not claim otherwise; that is a sign-in failure with a misleading
    receipt."""
    rendered = render(admin_assigned=False)

    assert not re.search(rf"assigned {re.escape(UPN)} to Platform\.Admin", rendered)
    assert "Platform.Admin" in rendered
    assert "not assigned" in rendered.lower()


# ===========================================================================
# Order
# ===========================================================================
def test_the_five_parts_appear_in_the_documented_order() -> None:
    """Paste-ready first, caveats after: an operator works top to bottom, and the
    two blocks they have to copy are the reason they ran the script."""
    rendered = render()

    positions = [
        rendered.index("entra_tenant_id"),
        rendered.index("VITE_AUTH_PROVIDER"),
        rendered.index("VITE_API_URL"),
        rendered.index("Platform.Admin"),
    ]
    assert positions == sorted(positions), rendered


@pytest.mark.parametrize("key", TFVARS_KEYS + VITE_KEYS)
def test_no_key_is_printed_twice(key: str) -> None:
    """One assignment per key. Two would leave the operator choosing, and a
    half-pasted pair of blocks is a deployment that comes up unauthenticated.

    ``VITE_ENTRA_SPA_CLIENT_ID`` and ``entra_spa_client_id`` carry the same value
    deliberately (the backend accepts tokens whose audience is either the
    identifier URI or the client id), so this counts assignments, not values.
    """
    rendered = render()

    assert len(re.findall(rf"^{re.escape(key)} *=", rendered, re.MULTILINE)) == 1
