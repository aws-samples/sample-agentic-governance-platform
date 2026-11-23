"""Shared contract test for EVERY agent-template scaffold.

The catalog holds several scaffolds: `strands-agentcore/`, LangGraph and CrewAI as container
templates, and `harness-agentcore/` as the first **config** template — a repo with no buildable
code at all. That makes "what must a scaffold directory contain?" a
question with two answers, and the answer is no longer readable from one hand-written test per
template.

So this test is parametrized over `agent-templates/*/` by DISCOVERY, not by an enumerated list.
A new scaffold directory is covered the moment it lands, with no edit here — which is the point:
the failure mode being guarded against is a template that ships missing its Dockerfile or its
workflow and is only found out when a customer materializes it.

**Discovery deliberately mirrors ROLLOUT's enumeration** (`_base_template_names()`: every
subdirectory, filtered only by rollout's own name regex) rather than keying on `template.json`
presence. Rollout treats a missing `template.json` as "this seed declares nothing" and pushes the
directory anyway, so a metadata-less scaffold is shippable — and a discovery rule that skipped it
would make exactly that case invisible. Whatever rollout would push, this test asserts on.

**The two kinds, distinguished structurally.** A scaffold's kind is read off its contents, not
its name or its `template.json`:

* **container** (no `harness.yaml`) — the image IS the deploy artifact, so it needs a
  `Dockerfile`, a `.github/workflows/build.yml` that pushes it, and an app that actually listens
  on 8080 (AgentCore invokes `/invocations` on that port; a scaffold that binds elsewhere builds
  clean and 502s in the runtime).
* **config** (`harness.yaml` present) — the YAML is the deploy artifact. It must have NO
  Dockerfile (a container here would be dead weight the harness never runs) and a
  `.github/workflows/deploy.yml` instead of a build workflow.

Reading the kind from the filesystem rather than from a declared `build_kind` is deliberate: the
declaration is what the registry serves to the platform, and a test that trusted it could not
catch the case where a template DECLARES `config` and still ships a Dockerfile. Structure first,
declaration checked against it.

**`infra_template_id` is required of EVERY scaffold, and must name a real seed infra template.**
The key sits in :data:`REQUIRED_METADATA_KEYS` with the rest of what registration reads, because
`_register` reads it exactly like the other six, with an `or DEFAULT_INFRA_TEMPLATE_ID` fold, and
because the failure it prevents is the worst of them: a seed with no id silently registers against
the DEFAULT infra template, so a config scaffold that forgot the key publishes a card the platform
will try to deploy with the CONTAINER module. An unknown id is the same defect louder — a template
nothing can materialize (no `iac_type`, no module).
"""

import ast
import json
import pathlib

import pytest
import yaml

from services.github_template_service import _NAME_RE as _TEMPLATE_NAME_RE

TEMPLATES_DIR = pathlib.Path(__file__).resolve().parents[2] / "agent-templates"

SEED_INFRA_TEMPLATE_IDS = {"agentcore-runtime", "agentcore-harness", "agentcore-gateway"}

REQUIRED_METADATA_KEYS = (
    "id",
    "name",
    "description",
    "framework",
    "aws_services",
    "tags",
    "version",
    "infra_template_id",
)


def _template_dirs() -> list[pathlib.Path]:
    """Every scaffold directory, enumerated THE WAY ROLLOUT ENUMERATES THEM.

    This mirrors `template_rollout_service._base_template_names()`, which is
    `sorted(p.name for p in agent_templates_dir.iterdir() if p.is_dir())` — every subdirectory,
    with no `template.json` requirement anywhere in it. That matters: a metadata-less directory is
    still ROLLOUT-ELIGIBLE (`_seed_metadata` treats an absent `template.json` as "this seed
    declares nothing" and registers the repo with empty metadata), so keying discovery on the
    metadata file — as this test first did — made exactly the broken case invisible: a scaffold
    that ships to a customer's org as an unregistered, un-described repo would be discovered by
    rollout and by nothing here.

    So: discover every directory, and let `test_scaffold_declares_metadata` FAIL on one without a
    `template.json` rather than silently skipping it.

    The one filter applied is rollout's own name regex, imported above. A directory rollout would
    refuse to push (`__pycache__`, `.ruff_cache`, `.DS_Store` — none match `^[a-z][a-z0-9-]{0,63}$`)
    cannot reach a customer, so holding it to a template contract would fail the suite over a build
    artifact. Anything rollout WOULD push is in scope, no exceptions.
    """
    if not TEMPLATES_DIR.is_dir():
        return []
    return sorted(p for p in TEMPLATES_DIR.iterdir() if p.is_dir() and _TEMPLATE_NAME_RE.match(p.name))


TEMPLATE_DIRS = _template_dirs()


def _ids(dirs: list[pathlib.Path]) -> list[str]:
    return [p.name for p in dirs]


@pytest.fixture(params=TEMPLATE_DIRS, ids=_ids(TEMPLATE_DIRS))
def scaffold(request) -> pathlib.Path:
    return request.param


def _metadata(scaffold: pathlib.Path) -> dict:
    path = scaffold / "template.json"
    assert path.is_file(), (
        f"{scaffold.name} has no template.json — rollout would still push this directory "
        "(`_seed_metadata` reads an absent file as 'declares nothing'), so it would reach a "
        "customer's org as a repo with a blank catalog card"
    )
    parsed = json.loads(path.read_text())
    assert isinstance(parsed, dict), f"{scaffold.name}/template.json must be a JSON object"
    return parsed


def _is_config_template(scaffold: pathlib.Path) -> bool:
    """Structural kind check — see the module docstring. `harness.yaml` present ⇒ config.
    `gateway.yaml` (the MCP-server gateway template) is the same kind: a config artifact,
    no image, a deploy workflow."""
    return (scaffold / "harness.yaml").is_file() or (scaffold / "gateway.yaml").is_file()


def test_discovery_covers_every_rollout_eligible_directory():
    """A guard on the DISCOVERY itself, stated as an EQUALITY so it maintains itself.

    Every other test here is parametrized over `TEMPLATE_DIRS`, so an empty or half-populated list
    makes the whole file pass by testing nothing — the classic way a discovery-parametrized suite
    goes green while the thing it guards is broken (a moved `agent-templates/`, a `parents[2]` that
    stops resolving, or a filter that quietly excludes a real scaffold).

    Asserted against the on-disk truth rather than a hand-maintained name list. A hardcoded list of
    known ids is a floor that rots: it needs an edit per new scaffold, and the edit that is skipped
    is invisible (the list still passes, it just stops covering the newest template — which is the
    one most likely to be wrong). The equality below cannot rot: any rollout-eligible directory
    that discovery misses fails here, and nothing needs updating when a scaffold is added.

    `expected` recomputes rollout's enumeration inline — deliberately duplicating
    `_template_dirs()`'s logic rather than calling it, since a test that asked the function under
    test for its own expected value would pass no matter what that function did.
    """
    assert TEMPLATES_DIR.is_dir(), f"agent-templates/ did not resolve to a directory: {TEMPLATES_DIR}"
    expected = {p.name for p in TEMPLATES_DIR.iterdir() if p.is_dir() and _TEMPLATE_NAME_RE.match(p.name)}
    assert set(_ids(TEMPLATE_DIRS)) == expected

    assert {
        "strands-agentcore",
        "langgraph-agentcore",
        "crewai-agentcore",
        "harness-agentcore",
    } <= expected, f"scaffold discovery under {TEMPLATES_DIR} found only {sorted(expected)}"


def test_scaffold_declares_metadata(scaffold: pathlib.Path):
    """Every discovered (rollout-eligible) directory must carry a readable `template.json`.

    Separate from the shape test below because the failure is different in kind: a shape defect
    publishes a card with a wrong field, while a MISSING file publishes an unregistered repo with
    no card at all — and rollout does the latter silently, treating absence as "declares nothing".
    """
    _metadata(scaffold)


def test_metadata_declares_the_seed_shape(scaffold: pathlib.Path):
    meta = _metadata(scaffold)
    missing = [k for k in REQUIRED_METADATA_KEYS if k not in meta]
    assert not missing, f"{scaffold.name}/template.json is missing {missing}"

    assert meta["id"] == scaffold.name, f"{scaffold.name}/template.json declares id={meta['id']!r}"

    assert isinstance(meta["name"], str) and meta["name"], f"{scaffold.name}: name must be non-empty"
    assert isinstance(meta["framework"], str) and meta["framework"], (
        f"{scaffold.name}: framework must be non-empty — an agent's declared framework is validated "
        "against this value instead of a hardcoded constant"
    )
    assert isinstance(meta["description"], str)
    assert isinstance(meta["aws_services"], list)
    assert isinstance(meta["tags"], list)
    assert isinstance(meta["version"], str) and meta["version"], (
        f"{scaffold.name}: version must be a non-empty STRING — TemplateRecord.version is a str, "
        "and a numeric literal here would round-trip through the registry as '1.0'"
    )


def test_infra_template_id_names_a_seed_template(scaffold: pathlib.Path):
    """Every scaffold, no exemptions — PRESENCE is `test_metadata_declares_the_seed_shape`'s job
    (`REQUIRED_METADATA_KEYS`), and this is the value half.

    The narrowing this test used to skip against is deleted: all four scaffolds declare the field, so
    the id is a universal requirement and a wrong one is a template nothing can materialize."""
    declared = _metadata(scaffold).get("infra_template_id")
    assert declared in SEED_INFRA_TEMPLATE_IDS, (
        f"{scaffold.name} declares infra_template_id={declared!r}, "
        f"which is not one of the seeded ids {sorted(SEED_INFRA_TEMPLATE_IDS)}"
    )


def test_container_scaffold_ships_a_dockerfile_exposing_8080(scaffold: pathlib.Path):
    if _is_config_template(scaffold):
        pytest.skip(f"{scaffold.name} is a config template — a Dockerfile is forbidden, not required")
    dockerfile = scaffold / "Dockerfile"
    assert dockerfile.is_file(), f"{scaffold.name} is a container template with no Dockerfile"
    assert "EXPOSE 8080" in dockerfile.read_text(), (
        f"{scaffold.name}/Dockerfile must EXPOSE 8080 — AgentCore invokes /invocations on 8080"
    )


def _calls_a_run_method(source: str) -> bool:
    """True iff the module CONTAINS AN EXECUTABLE CALL to a `.run(...)` method.

    Parsed with `ast`, not searched as text, and that is the whole point of this helper. A
    substring check for `app.run(` is satisfied by a docstring or a comment — and it demonstrably
    would be here: `src/main.py` in both the Strands and LangGraph scaffolds mentions
    "`app.run()`" inside a comment explaining a telemetry trap. So a scaffold could DELETE its
    real `app.run()` (never binding a port, 502-ing in the runtime) and a text-matching test would
    still pass on the prose that describes it. The AST sees only code.

    Matched on the attribute NAME rather than on the receiver being `app`, so a scaffold that
    names its instance `application` or calls `BedrockAgentCoreApp().run()` inline still counts —
    the contract is "something starts a server", not "a variable is called app".
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "run"
        for node in ast.walk(tree)
    )


def test_container_scaffold_app_serves_on_8080(scaffold: pathlib.Path):
    """The app must actually START the server, not merely declare the port.

    `EXPOSE` is documentation the runtime does not enforce, so a scaffold can expose 8080 in its
    Dockerfile and never bind it — which is a clean build and a 502 on first invocation.
    `BedrockAgentCoreApp.run()` is what binds 8080 (the port is the SDK's default, so a correct
    scaffold contains no 8080 literal in `src/` at all), so an executable call to it is the
    evidence.

    There is deliberately NO `or "8080" in source` escape hatch. It was here, and it was worse
    than nothing: all three container scaffolds mention 8080 only in their module DOCSTRINGS (the
    "Local: … starts on http://localhost:8080" header), so the disjunct was permanently satisfied
    by prose and this assertion could not fail no matter what the code did. An escape hatch that
    nothing needs and that defeats the assertion is not flexibility. If a future template really
    serves the contract another way, widen `_calls_a_run_method` to recognise how it binds — in
    code — rather than re-adding a text match.
    """
    if _is_config_template(scaffold):
        pytest.skip(f"{scaffold.name} is a config template — it has no server")
    src = scaffold / "src"
    assert src.is_dir(), f"{scaffold.name} is a container template with no src/"
    modules = sorted(src.rglob("*.py"))
    assert modules, f"{scaffold.name}/src contains no Python modules"
    assert any(_calls_a_run_method(p.read_text()) for p in modules), (
        f"{scaffold.name}/src has no executable `.run(...)` call, so nothing binds 8080 — "
        "the image builds clean and every invocation 502s"
    )


def test_container_scaffold_has_a_build_workflow(scaffold: pathlib.Path):
    if _is_config_template(scaffold):
        pytest.skip(f"{scaffold.name} is a config template — it deploys, it does not build")
    workflow = scaffold / ".github/workflows/build.yml"
    assert workflow.is_file(), f"{scaffold.name} has no .github/workflows/build.yml"
    parsed = yaml.safe_load(workflow.read_text())
    assert isinstance(parsed, dict) and parsed.get("jobs"), (
        f"{scaffold.name}/.github/workflows/build.yml parses to no jobs"
    )


def test_config_scaffold_ships_no_dockerfile(scaffold: pathlib.Path):
    if not _is_config_template(scaffold):
        pytest.skip(f"{scaffold.name} is a container template")
    assert not (scaffold / "Dockerfile").exists(), (
        f"{scaffold.name} declares a harness.yaml AND ships a Dockerfile — the harness runtime "
        "never builds an image, so the Dockerfile is dead weight that will drift"
    )


def test_config_scaffold_has_a_deploy_workflow(scaffold: pathlib.Path):
    if not _is_config_template(scaffold):
        pytest.skip(f"{scaffold.name} is a container template")
    workflow = scaffold / ".github/workflows/deploy.yml"
    assert workflow.is_file(), f"{scaffold.name} has no .github/workflows/deploy.yml"
    parsed = yaml.safe_load(workflow.read_text())
    assert isinstance(parsed, dict) and parsed.get("jobs"), (
        f"{scaffold.name}/.github/workflows/deploy.yml parses to no jobs"
    )


def test_scaffold_ships_a_readme(scaffold: pathlib.Path):
    """The README is the only file a customer reliably reads — it ships into their repository."""
    readme = scaffold / "README.md"
    assert readme.is_file(), f"{scaffold.name} has no README.md"
    assert readme.read_text().strip(), f"{scaffold.name}/README.md is empty"
