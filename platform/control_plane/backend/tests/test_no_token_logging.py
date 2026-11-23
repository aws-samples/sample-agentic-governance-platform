"""Guard: no agent and no backend module may log a credential value.

The contact-center agent once logged raw Entra OBO bearer tokens and inbound user JWTs at INFO, so
real unexpired user credentials reached CloudWatch. The four reference agents are near-clones, so a
single-file fix does not stay fixed; this scans all of them, the container agent templates every
tenant agent is generated from, and the backend's `src/`, which validates every inbound token and
holds the platform's own secrets.

Why this parses instead of pattern-matching the raw text: the guard has to draw one distinction. A
token identifier in a value position (``logger.info("%s", token)`` or ``f"...{access_token}"``) is a
leak, while the same word inside literal prose is not. Every agent carries a legitimate
``logger.warning("workload access token fetch failed …", wat_exc)``, so a matcher run over raw call
text flags all four files and the guard could never go green. So each file is parsed, every string
literal is blanked (f-string ``{...}`` bodies survive, because those are evaluated), and only the
surviving expression source is matched. Comments fall out for free, they are not in the tree. Line
numbers come from the AST, so failures point at the real line.

Two distinctions are pinned by the tests of the guard below, so neither can silently regress:

* a token count is not a credential. A credential is one secret while an LLM usage metric is a
  number, so `TOKEN_IDENTIFIER` flags the singular name under any ``<word>_`` prefix (which covers
  ``inbound_token``; ``wat`` is listed because in these agents it holds the workload access token),
  plus the few plurals whose qualifier can only mean a credential, and lets every other plural
  through as a count. The asymmetry is deliberate: allow-listing the credential qualifiers means a
  metric name nobody predicted passes silently, whereas deny-listing the counting qualifiers would
  turn every newly invented metric name into a red release gate, and the cheapest apparent fix for
  that is loosening the regex, which disarms the guard. Both word boundaries matter: the leading one
  saves boto3's camelCase ``nextToken`` cursor, the trailing one ``tokenizer``, ``token_count``,
  ``token_usage`` and ``secret_arn``, which names where a secret is stored, not the secret. Pinned by
  `USAGE_METRIC_NAMES`, `CREDENTIAL_NAMES` and the ``secret-arn-resource-name`` near miss.
* a string used as a mapping key is not prose. ``headers["Authorization"]`` and
  ``payload.get("id_token")`` name the credential in the key, so the key survives blanking. See
  `_is_lookup_key`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
APPS_DIR = REPO_ROOT / "applications"
TEMPLATES_DIR = REPO_ROOT / "platform" / "control_plane" / "agent-templates"
BACKEND_SRC = REPO_ROOT / "platform/control_plane/backend/src"

TOKEN_IDENTIFIER = re.compile(
    r"\b(?:\w+_)?(?:token|jwt|wat|secret|password|api_key|private_key|credentials?)\b"
    r"|\b(?:access|id|bearer|obo|raw|inbound|refresh|session|auth)_tokens\b"
    r"|\b(?:\w+_)?authorization\b",
    re.IGNORECASE,
)

LOG_RECEIVERS = {"logger", "_logger", "log", "logging", "getlogger"}
LOG_LEVELS = {"debug", "info", "warning", "error", "critical", "exception", "log"}

SAFE_WRAPPERS = {"bool", "len"}

_LOOKUP_KEY_SHAPE = re.compile(r"[A-Za-z_][\w.\-]*")


def _is_lookup_key(node: ast.AST) -> bool:
    """True for a string literal shaped like a mapping key: ``"access_token"``, ``"Authorization"``.

    Key-shaped means identifier-shaped: letters, digits, underscores, and the dots and hyphens real
    header names carry (``x-ms-token``). A URL such as ``…/oauth2/v2.0/token`` fails on ``:`` and
    ``/``, so it stays prose even as a ``.get()`` first argument.
    """
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
        self.generic_visit(node)
        if key is not None:
            node.slice = key
        return node

    def visit_Call(self, node: ast.Call) -> ast.AST:
        """``payload.get("id_token")`` is the credential access spelled as a method call; keep the key."""
        if isinstance(node.func, ast.Name) and node.func.id in SAFE_WRAPPERS:
            return ast.copy_location(ast.Constant(value=True), node)
        key = (
            node.args[0]
            if isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and _is_lookup_key(node.args[0])
            else None
        )
        self.generic_visit(node)
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
    """Yield (line_number, evaluated-argument-source) for every logging call in `source`.

    A logging call is ``<receiver naming a logger>.<level>(...)``: ``logger.info``,
    ``logging.getLogger(__name__).warning`` and ``self.logger.error`` all count.
    """
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


def _scanned_sources() -> list[Path]:
    """Dot-prefixed directories are skipped: each agent's `.bedrock_agentcore/` holds ~1,700 files
    of vendored third-party dependencies (git-ignored, `.gitignore:46`) that we neither own nor
    ship, and whose presence depends on whether anyone has run a local build.
    """
    return sorted(
        path
        for root in (APPS_DIR, TEMPLATES_DIR, BACKEND_SRC)
        for path in root.rglob("*.py")
        if not any(part.startswith(".") for part in path.relative_to(root).parts)
    )


def test_applications_dir_is_present():
    """Fail loudly rather than vacuously passing if the scan target moved."""
    assert APPS_DIR.is_dir(), f"expected reference agents at {APPS_DIR}"


def test_scan_covers_every_reference_agent():
    """The scan must still reach all four near-clone agents, or the guard is decorative."""
    scanned = {
        path.relative_to(APPS_DIR).parts[0] for path in _scanned_sources() if path.is_relative_to(APPS_DIR)
    }
    assert {
        "acme_contact_center_agent",
        "acme_fnol_agent",
        "acme_onboarding_agent",
        "acme_reference_agent",
    } <= scanned, f"guard is not scanning every reference agent; found {sorted(scanned)}"


def test_scan_covers_every_agent_template():
    """The container templates are what tenant agents are generated from, so a token log there ships
    to every agent built from them; the guard must reach each one."""
    assert TEMPLATES_DIR.is_dir(), f"expected agent templates at {TEMPLATES_DIR}"
    scanned = {
        path.relative_to(TEMPLATES_DIR).parts[0]
        for path in _scanned_sources()
        if path.is_relative_to(TEMPLATES_DIR)
    }
    assert {
        "crewai-agentcore",
        "langgraph-agentcore",
        "strands-agentcore",
    } <= scanned, f"guard is not scanning every agent template; found {sorted(scanned)}"


def test_scan_covers_the_backend_security_modules():
    """The backend validates Entra and GitHub OIDC tokens and brokers agent credentials, so a
    credential log there reaches the platform's own logs."""
    scanned = {
        path.relative_to(BACKEND_SRC).as_posix()
        for path in _scanned_sources()
        if path.is_relative_to(BACKEND_SRC)
    }
    missing = {
        "core/security_entra.py",
        "core/security_github_oidc.py",
        "services/agent_credential_service.py",
    } - scanned
    assert not missing, f"guard is not scanning these backend modules: {sorted(missing)}"


@pytest.mark.parametrize("path", _scanned_sources(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_token_values_in_log_calls(path):
    offenders = [
        (line, args.strip())
        for line, args in _log_call_arguments(path.read_text(encoding="utf-8"))
        if TOKEN_IDENTIFIER.search(args)
    ]
    assert not offenders, (
        f"{path.relative_to(REPO_ROOT)} logs credential values: {offenders}. "
        "Log a boolean presence check or a claim like 'aud'/'exp' instead — never the token."
    )


def _flags(snippet: str) -> bool:
    """Whether the guard flags `snippet`; the scan passes on a clean tree and on a broken detector
    alike, so the detector carries its own cover below."""
    return any(TOKEN_IDENTIFIER.search(args) for _, args in _log_call_arguments(snippet))


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
        pytest.param('logger.info("prefix %s", token[:12])', id="truncated-token-prefix"),
        pytest.param('logger.info("h %s", sha256(bearer_token.encode()).hexdigest())', id="hashed-token"),
        pytest.param('logger.info("%s", bool(x) and raw_token)', id="beside-a-safe-wrapper"),
        pytest.param('logger.info("%s", headers["Authorization"])', id="subscript-string-key"),
        pytest.param("logger.info(f'{claims[\"access_token\"]}')", id="subscript-key-inside-f-string"),
        pytest.param('logger.info("token: %s", payload.get("id_token"))', id="get-with-string-key"),
        pytest.param('logger.info("%s", headers.get("Authorization"))', id="get-authorization-header"),
        pytest.param('logger.info("%s", os.environ["OBO_TOKEN"])', id="environ-string-key"),
        pytest.param(
            'logger.info("entra app: %s", settings.ENTRA_BACKEND_CLIENT_SECRET)', id="client-secret-setting"
        ),
        pytest.param('logger.debug(f"calling with key {api_key}")', id="api-key-f-string"),
    ],
)
def test_guard_flags_credential_values(snippet):
    """Every shape of a credential value in a logging call is an offence: a truncated or hashed token
    (neither is an accepted substitute for deletion) and a credential reached through a string key
    included."""
    assert _flags(snippet), f"guard failed to flag a credential leak: {snippet}"


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
    "access_tokens",
    "authorization",
    "secret",
    "client_secret",
    "password",
    "api_key",
    "private_key",
    "credential",
    "credentials",
]


@pytest.mark.parametrize("name", USAGE_METRIC_NAMES)
def test_token_counts_are_not_credentials(name):
    """Logging how many tokens a model used is legitimate and must never trip the gate."""
    assert not _flags(f'logger.info("usage: %s", {name})'), (
        f"guard false-positived on an LLM usage metric: {name}"
    )


@pytest.mark.parametrize("name", CREDENTIAL_NAMES)
def test_credential_names_are_flagged(name):
    """Every name that can only hold the secret itself stays an offence, `access_tokens` included: a
    list of secrets is still secrets."""
    assert _flags(f'logger.info("x: %s", {name})'), f"guard failed to flag a credential identifier: {name}"


@pytest.mark.parametrize(
    "snippet",
    [
        pytest.param(
            'logger.warning("workload access token fetch failed: %r", wat_exc)',
            id="token-word-in-prose",
        ),
        pytest.param('logger.info("ok")  # dumps the access_token', id="token-word-in-comment"),
        pytest.param('logger.info("have token: %s", bool(token))', id="boolean-presence-check"),
        pytest.param('logger.info("len %s", len(access_token))', id="length-only"),
        pytest.param('logger.info("absent: %s", token is None)', id="none-comparison"),
        pytest.param('logger.info("aud=%s", claims.get("aud"))', id="decoded-aud-claim"),
        pytest.param('logger.info("page %s", nextToken)', id="boto3-pagination-cursor"),
        pytest.param('logger.info("t %s", tokenizer)', id="tokenizer-object"),
        pytest.param('logger.info("n %s", token_count)', id="token-count-metric"),
        pytest.param('send_token("x", access_token)', id="not-a-logging-call"),
        pytest.param('logger.info("n %s", usage["total_tokens"])', id="metric-via-string-key"),
        pytest.param('logger.info("stored at %s", secret_arn)', id="secret-arn-resource-name"),
        pytest.param(
            'logger.info("%s", session.get("https://login.microsoftonline.com/t/oauth2/v2.0/token").status_code)',
            id="url-argument-to-http-get",
        ),
    ],
)
def test_guard_ignores_non_credential_logging(snippet):
    """Prose, comments, presence checks, decoded claims, near-miss identifiers, metrics behind a string
    key and URLs never trip the gate."""
    assert not _flags(snippet), f"guard false-positived on a safe log: {snippet}"


def test_the_safe_wrappers_are_exactly_the_listed_ones():
    """A listed wrapper blanks its whole call out of the scan, whatever it returns, so the set changes
    only with this literal."""
    assert SAFE_WRAPPERS == frozenset({"bool", "len"})
