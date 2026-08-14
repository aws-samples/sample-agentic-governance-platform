"""Guard: reference agents must never log token or JWT VALUES.

Regression cover for the E12 release gate closed in E34/T1 — the contact-center agent logged raw
Entra OBO bearer tokens and inbound user JWTs at INFO, so real unexpired user credentials reached
CloudWatch. The four reference agents are near-clones, so a single-file fix does not stay fixed;
this scans all of them.

Implementation note — why this parses instead of pattern-matching the raw text. The guard has to
draw one distinction: a token identifier in a VALUE position (``logger.info("%s", token)`` or
``f"...{access_token}"``) is a leak, while the same word inside literal prose is not. Every agent
carries a legitimate ``logger.warning("workload access token fetch failed …", wat_exc)``, so a
matcher run over raw call text flags all four files and the guard could never go green. So each
file is parsed, every string literal is blanked (f-string ``{...}`` bodies survive, because those
ARE evaluated), and only the surviving expression source is matched. Comments fall out for free —
they are not in the tree. Line numbers come from the AST, so failures point at the real line.

Two refinements were added after review found the first version both over- and under-firing, and
both are pinned by tests below so they cannot silently regress:

* a token COUNT is not a credential. ``prompt_tokens``/``total_tokens``/``max_tokens`` are numbers
  an LLM agent legitimately logs; only a single secret is an offence. See `TOKEN_IDENTIFIER`.
* a string used as a mapping KEY is not prose. ``headers["Authorization"]`` and
  ``payload.get("id_token")`` name the credential in the key, so the key survives blanking. See
  `_is_lookup_key`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

APPS_DIR = Path(__file__).resolve().parents[4] / "applications"

# Identifiers whose VALUE is a credential. Matched only against a logging call's evaluated
# expressions — never against literal prose (see the module docstring).
#
# THE RULE, in one sentence: a credential is ONE secret while an LLM usage metric is a NUMBER, so
# this flags the SINGULAR name (line 1) plus the few plurals whose qualifier can only mean a
# credential (line 2), and lets every other plural through as a count.
#
# So `prompt_tokens`/`total_tokens`/`max_tokens`/`cache_read_input_tokens` pass while
# `access_token`/`inbound_token`/`jwt`/`wat`/bare `token` fail; both directions are pinned by
# USAGE_METRIC_NAMES and CREDENTIAL_NAMES below. The asymmetry is deliberate: allow-listing the
# CREDENTIAL qualifiers means a metric name nobody predicted passes silently, whereas deny-listing
# the COUNTING qualifiers would turn every newly-invented metric name into a red release gate — and
# the cheapest apparent fix for that is loosening this regex, which disarms the guard entirely.
#
# The optional `\w+_` prefix on line 1 is what covers the whole family rather than a fixed list,
# including `inbound_token`, the raw inbound user JWT this guard exists because of; `wat` is here
# because in these agents that name holds the workload access token itself. Line 3 catches the
# credential in its most common shape, `headers["Authorization"]` — the exact idiom the deleted
# block used — and is only reachable because string keys survive blanking (see `_is_lookup_key`).
#
# Deliberately still narrow, and both boundaries are load-bearing: the LEADING `\b` is what saves
# boto3's camelCase `nextToken` pagination cursor (there is no word boundary inside `nextToken`),
# and the TRAILING `\b` is what saves `tokenizer`, `token_count` and `token_usage`.
TOKEN_IDENTIFIER = re.compile(
    r"\b(?:\w+_)?(?:token|jwt|wat)\b"
    r"|\b(?:access|id|bearer|obo|raw|inbound|refresh|session|auth)_tokens\b"
    r"|\b(?:\w+_)?authorization\b",
    re.IGNORECASE,
)

# A logging call is `<something named like a logger>.<level>(...)`: covers `logger.info`,
# `logging.getLogger(__name__).warning`, and `self.logger.error`.
LOG_RECEIVERS = {"logger", "_logger", "log", "logging", "getlogger"}
LOG_LEVELS = {"debug", "info", "warning", "error", "critical", "exception", "log"}


# Wrappers whose RESULT cannot be the credential, only a fact about it. `bool(token)` is the
# breadcrumb this guard's own failure message tells you to log instead, so it must not be an
# offence; `len(token)` is the same shape.
SAFE_WRAPPERS = {"bool", "len"}

# A string used as a mapping KEY is not prose, so it must survive blanking (see
# `_BlankNonCredentialValues`). "Shaped like a key" means identifier-shaped — letters, digits,
# underscores, and the dots/hyphens real header names carry (`x-ms-token`). That shape requirement
# is what keeps a URL out: `requests.get("https://login.microsoftonline.com/…/oauth2/v2.0/token")`
# contains the word `token` but is prose to us, and it fails this pattern on the `:` and `/`.
_LOOKUP_KEY_SHAPE = re.compile(r"[A-Za-z_][\w.\-]*")


def _is_lookup_key(node: ast.AST) -> bool:
    """True for a string literal shaped like a mapping key: ``"access_token"``, ``"Authorization"``."""
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _LOOKUP_KEY_SHAPE.fullmatch(node.value) is not None
    )


class _BlankNonCredentialValues(ast.NodeTransformer):
    """Erase every subexpression that cannot carry a credential value, keeping line numbers.

    This is what separates prose and facts from values: after this pass the only text left in a
    call's unparsed arguments is code whose value could actually be a token. Three erasures:

    * string/bytes literals — prose. Inside an f-string only the literal chunks go; each
      ``{expr}`` is a ``FormattedValue`` holding a real expression, so it survives and stays
      matchable, which is how ``f"...{access_token}"`` is still caught.
    * ``bool(...)``/``len(...)`` — a presence check, the recommended replacement for a dump.
    * any comparison — its value is a bool, so ``token is None`` leaks nothing.

    And one exception to the string blanking, because blanking every literal made credentials
    reached through a string KEY invisible: ``headers["Authorization"]`` and
    ``payload.get("id_token")`` name the credential in the key, not in a variable, so blanking the
    key erased the only evidence and the leak passed. `visit_Subscript`/`visit_Call` therefore put
    the key back after the recursive pass. Prose in a message string is untouched by this — it is
    neither a subscript index nor a ``.get()`` lookup name.

    Comments need no handling at all: they are not in the tree.
    """

    def visit_Constant(self, node: ast.Constant) -> ast.Constant:
        if isinstance(node.value, str):
            return ast.copy_location(ast.Constant(value=""), node)
        if isinstance(node.value, bytes):
            return ast.copy_location(ast.Constant(value=b""), node)
        return node

    def visit_Subscript(self, node: ast.Subscript) -> ast.AST:
        """``headers["Authorization"]`` — a credential access, so keep the key readable."""
        key = node.slice if _is_lookup_key(node.slice) else None
        node = self.generic_visit(node)
        if key is not None:
            node.slice = key
        return node

    def visit_Call(self, node: ast.Call) -> ast.AST:
        if isinstance(node.func, ast.Name) and node.func.id in SAFE_WRAPPERS:
            return ast.copy_location(ast.Constant(value=True), node)
        # `payload.get("id_token")` is the same credential access spelled as a method call.
        key = (
            node.args[0]
            if isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and _is_lookup_key(node.args[0])
            else None
        )
        node = self.generic_visit(node)
        if key is not None:
            node.args[0] = key
        return node

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        return ast.copy_location(ast.Constant(value=True), node)


def _receiver_names(func: ast.Attribute) -> set[str]:
    """Lower-cased identifiers appearing in the call's receiver (everything left of `.<level>`)."""
    return {
        sub.id.lower() if isinstance(sub, ast.Name) else sub.attr.lower()
        for sub in ast.walk(func.value)
        if isinstance(sub, (ast.Name, ast.Attribute))
    }


def _log_call_arguments(source: str):
    """Yield (line_number, evaluated-argument-source) for every logging call in `source`."""
    tree = _BlankNonCredentialValues().visit(ast.parse(source))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr.lower() not in LOG_LEVELS:
            continue
        if not _receiver_names(node.func) & LOG_RECEIVERS:
            continue
        arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
        yield node.lineno, ", ".join(ast.unparse(argument) for argument in arguments)


def _agent_sources() -> list[Path]:
    """Every tracked Python file under `applications/`.

    Dot-prefixed directories are skipped: each agent's `.bedrock_agentcore/` holds ~1,700 files
    of vendored third-party dependencies (git-ignored, `.gitignore:46`) that we neither own nor
    ship, and whose presence depends on whether anyone has run a local build.
    """
    return sorted(
        path
        for path in APPS_DIR.rglob("*.py")
        if not any(part.startswith(".") for part in path.relative_to(APPS_DIR).parts)
    )


def test_applications_dir_is_present():
    """Fail loudly rather than vacuously passing if the scan target moved."""
    assert APPS_DIR.is_dir(), f"expected reference agents at {APPS_DIR}"


def test_scan_covers_every_reference_agent():
    """The scan must still reach all four near-clone agents, or the guard is decorative."""
    scanned = {path.relative_to(APPS_DIR).parts[0] for path in _agent_sources()}
    assert {
        "acme_contact_center_agent",
        "acme_fnol_agent",
        "acme_onboarding_agent",
        "acme_reference_agent",
    } <= scanned, f"guard is not scanning every reference agent; found {sorted(scanned)}"


@pytest.mark.parametrize(
    "path", _agent_sources(), ids=lambda p: str(p.relative_to(APPS_DIR))
)
def test_no_token_values_in_log_calls(path):
    offenders = [
        (line, args.strip())
        for line, args in _log_call_arguments(path.read_text(encoding="utf-8"))
        if TOKEN_IDENTIFIER.search(args)
    ]
    assert not offenders, (
        f"{path.relative_to(APPS_DIR)} logs credential values: {offenders}. "
        "Log a boolean presence check or a claim like 'aud'/'exp' instead — never the token."
    )


# ---------------------------------------------------------------------------
# Tests OF the guard.
#
# The scan above passes both when the tree is clean and when the detector is broken, so the
# detector needs its own cover. Without it, narrowing the regex or the literal-blanking silently
# disarms the release gate and every test still reports green — which is precisely the failure
# mode that let the original block live for two months.
# ---------------------------------------------------------------------------

def _flags(snippet: str) -> bool:
    return any(
        TOKEN_IDENTIFIER.search(args) for _, args in _log_call_arguments(snippet)
    )


@pytest.mark.parametrize(
    "snippet",
    [
        pytest.param('logger.info("obo token: %s", token)', id="positional-arg"),
        pytest.param('logger.info(f"bearer {access_token}")', id="f-string-interpolation"),
        pytest.param('logger.info("inbound: %s", inbound_token)', id="inbound-user-jwt"),
        pytest.param('logger.debug("wat: %s", wat)', id="workload-access-token"),
        pytest.param('logger.error("x", extra={"t": id_token})', id="keyword-argument"),
        pytest.param('self.logger.warning("x %s", obo_token)', id="attribute-receiver"),
        pytest.param('logging.getLogger(__name__).info("x %s", jwt)', id="getlogger-chain"),
        pytest.param('logger.info("done) %s", token)', id="close-paren-in-message"),
        # The plan forbids these two as substitutes for deletion, so neither may pass as a fix.
        pytest.param('logger.info("prefix %s", token[:12])', id="truncated-token-prefix"),
        pytest.param(
            'logger.info("h %s", sha256(bearer_token.encode()).hexdigest())', id="hashed-token"
        ),
        pytest.param('logger.info("%s", bool(x) and raw_token)', id="beside-a-safe-wrapper"),
        # Credentials reached through a string KEY. Blanking every literal made these invisible;
        # the third form is nearly what the deleted block did, so this was a live gap.
        pytest.param(
            'logger.info("%s", headers["Authorization"])', id="subscript-string-key"
        ),
        pytest.param(
            "logger.info(f'{claims[\"access_token\"]}')", id="subscript-key-inside-f-string"
        ),
        pytest.param(
            'logger.info("token: %s", payload.get("id_token"))', id="get-with-string-key"
        ),
        pytest.param(
            'logger.info("%s", headers.get("Authorization"))', id="get-authorization-header"
        ),
        pytest.param('logger.info("%s", os.environ["OBO_TOKEN"])', id="environ-string-key"),
    ],
)
def test_guard_flags_credential_values(snippet):
    assert _flags(snippet), f"guard failed to flag a credential leak: {snippet}"


# The counts-vs-credentials rule, pinned in both directions. The first version of this guard flagged
# every name in CREDENTIAL_NAMES *and* every name in USAGE_METRIC_NAMES, which is the dangerous
# half: an author logging `prompt_tokens` hits a red security gate for a non-issue, and the cheapest
# apparent fix is loosening TOKEN_IDENTIFIER — disarming the gate. Both lists must stay pinned so
# neither direction can drift.
USAGE_METRIC_NAMES = [
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "max_tokens",
    "input_tokens",
    "output_tokens",
    "num_tokens",
    "cache_read_input_tokens",
    "token_count",
    "token_usage",
    "tokens_used",
    "usage",
]

CREDENTIAL_NAMES = [
    "token",
    "access_token",
    "id_token",
    "bearer_token",
    "obo_token",
    "raw_token",
    "inbound_token",
    "jwt",
    "wat",
    "access_tokens",  # plural, but a LIST of secrets is still secrets
    "authorization",
]


@pytest.mark.parametrize("name", USAGE_METRIC_NAMES)
def test_token_counts_are_not_credentials(name):
    """Logging how many tokens a model used is legitimate and must never trip the gate."""
    assert not _flags(
        f'logger.info("usage: %s", {name})'
    ), f"guard false-positived on an LLM usage metric: {name}"


@pytest.mark.parametrize("name", CREDENTIAL_NAMES)
def test_credential_names_are_flagged(name):
    """Every name that can only hold the secret itself stays an offence."""
    assert _flags(
        f'logger.info("x: %s", {name})'
    ), f"guard failed to flag a credential identifier: {name}"


@pytest.mark.parametrize(
    "snippet",
    [
        # The real WAT warning all four agents carry: "token" is prose, the arg is an exception.
        pytest.param(
            'logger.warning("workload access token fetch failed: %r", wat_exc)',
            id="token-word-in-prose",
        ),
        pytest.param('logger.info("ok")  # dumps the access_token', id="token-word-in-comment"),
        # The remediations this guard's own failure message recommends.
        pytest.param('logger.info("have token: %s", bool(token))', id="boolean-presence-check"),
        pytest.param('logger.info("len %s", len(access_token))', id="length-only"),
        pytest.param('logger.info("absent: %s", token is None)', id="none-comparison"),
        pytest.param('logger.info("aud=%s", claims.get("aud"))', id="decoded-aud-claim"),
        # Near-miss identifiers that are not credentials.
        pytest.param('logger.info("page %s", nextToken)', id="boto3-pagination-cursor"),
        pytest.param('logger.info("t %s", tokenizer)', id="tokenizer-object"),
        pytest.param('logger.info("n %s", token_count)', id="token-count-metric"),
        pytest.param('send_token("x", access_token)', id="not-a-logging-call"),
        # Keys survive blanking, so a metric read through one must still be a count, not a leak.
        pytest.param('logger.info("n %s", usage["total_tokens"])', id="metric-via-string-key"),
        # A URL is prose even in a `.get()` first argument — `_is_lookup_key` rejects it on `:`/`/`.
        pytest.param(
            'logger.info("%s", session.get("https://login.microsoftonline.com/t/oauth2/v2.0/token").status_code)',
            id="url-argument-to-http-get",
        ),
    ],
)
def test_guard_ignores_non_credential_logging(snippet):
    assert not _flags(snippet), f"guard false-positived on a safe log: {snippet}"
