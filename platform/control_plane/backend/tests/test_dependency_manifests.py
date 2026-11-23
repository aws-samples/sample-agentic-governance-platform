"""Every vendored manifest the bounds module excludes is tracked, every manifest it discovers under
an application or a template is one its ceiling and constraints checks read, every template install
reads its constraints file, and every pin in a constraints file satisfies the specifier it pins.

The mistake this pins: an application's second requirements file the ceiling check compares with
nothing, a ``pyproject.toml`` nested in a template whose constraints file and pip-audit ignores no
check reads, a Dockerfile whose ``pip install`` drops ``-c constraints.txt`` and a ``pyproject.toml``
specifier moved past its pin each pass ``tests/test_dependency_bounds.py``. The manifests, the
templates and the applications are the ones that module discovers, loaded through
``importlib.util`` so the discovery has one copy. A ``VENDORED_MANIFESTS`` entry there is tracked
unless nothing under its top-level directory is, as in the public snapshot, which carries no
``.claude/``. A Dockerfile is read through its ``RUN``, ``CMD``, ``ENTRYPOINT``, ``SHELL`` and
``HEALTHCHECK CMD`` instructions, ``ONBUILD`` ones included, and a workflow through every ``run:``
value in the document, nothing else; an install a template reaches through a script file, a
``PIP_*`` variable, or a command name held in a variable or built by command substitution is not
read. A pin is judged by ``packaging.specifiers``, a prerelease pin admitted as pip admits an
explicit one.
"""

import functools
import importlib.util
import json
import os
import re
import shlex
from pathlib import Path

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet

BOUNDS_PY = Path(__file__).with_name("test_dependency_bounds.py")

_PIP = re.compile(r"(?:-[A-Za-z]*m)?pip(?:3(?:\.\d+)?)?")
_INSTALLER = re.compile(r"pip|\b(?:uv|poetry)\b")
_INSTALL_WORD = re.compile(r"\b(?:install|sync)\b")
_OTHER_INSTALLERS = {"uv": {"sync"}, "poetry": {"install", "sync"}, "pipenv": {"install", "sync"}}
_SHELL_OPERATOR = re.compile(r"^[;&|<>()]+$")
_REDIRECT = re.compile(r"&?[<>]+[&|]?")
_COMMAND_INSTRUCTION = re.compile(
    r"^\s*(?:ONBUILD\s+)?(?:RUN|CMD|ENTRYPOINT|SHELL|HEALTHCHECK\s+(?:--\S+\s+)*CMD)"
    r"\s+(?:--\S+\s+)*(?P<body>.*)$",
    re.IGNORECASE,
)
_HEREDOC = re.compile(r"(?<!<)<<-?\s*([\"']?)(?P<word>[^\s\"'<>|&;()]+)\1")
_VALUE_OPTIONS = frozenset({"-c", "--constraint", "-r", "--requirement", "-e", "--editable"})
_EXTRAS = re.compile(r"\[[^\]]*\]$")


def _load_bounds():
    spec = importlib.util.spec_from_file_location("agp_dependency_bounds", BOUNDS_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bounds = _load_bounds()
REPO_ROOT: Path = bounds.REPO_ROOT
VENDORED_MANIFESTS: dict[str, str] = bounds.VENDORED_MANIFESTS


def _under(manifest: str, roots: set[Path]) -> bool:
    return any(root in Path(manifest).parents for root in roots)


@functools.cache
def templates() -> tuple[str, ...]:
    roots = {Path(template).parent for template in bounds.TEMPLATES}
    found = {
        str(Path(manifest).parent)
        for manifest in bounds.SHIPPED_MANIFESTS
        if Path(manifest).name == "pyproject.toml" and _under(manifest, roots)
    }
    return tuple(sorted(found | set(bounds.TEMPLATES)))


def _commands(line: str) -> list[list[str]]:
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    commands: list[list[str]] = [[]]
    nested: list[list[str]] = []
    tokens = iter(lexer)
    for token in tokens:
        if _REDIRECT.fullmatch(token):
            if commands[-1] and commands[-1][-1].isdigit():
                commands[-1].pop()
            next(tokens, None)
        elif _SHELL_OPERATOR.match(token):
            commands.append([])
        else:
            commands[-1].append(token)
            if _INSTALLER.search(token) and any(character.isspace() for character in token):
                nested += _commands(bounds._COMMENT.sub("", token))
    return commands + nested


def pip_installs(line: str) -> list[list[str]]:
    return [
        command[command.index("install", index + 1) + 1 :]
        for command in _commands(line)
        for index, token in enumerate(command)
        if _PIP.fullmatch(Path(token).name) and "install" in command[index + 1 :]
    ]


def other_installs(line: str) -> list[str]:
    return [
        shlex.join(command)
        for command in _commands(line)
        if any(
            _OTHER_INSTALLERS.get(Path(token).name, set()) & set(command[index + 1 :])
            for index, token in enumerate(command)
        )
    ]


def _split(tokens: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    options: list[tuple[str, str]] = []
    positional: list[str] = []
    rest = iter(tokens)
    for token in rest:
        name, equals, value = token.partition("=")
        if token.startswith("--") and equals:
            options.append((name, value))
        elif token in _VALUE_OPTIONS:
            options.append((token, next(rest, "")))
        elif token.startswith(("-c", "-r", "-e")) and not token.startswith("--"):
            options.append((token[:2], token[2:]))
        elif token.startswith("-"):
            options.append((token, ""))
        else:
            positional.append(token)
    return options, positional


def _installs_only_build_tools(options: list[tuple[str, str]], positional: list[str]) -> bool:
    if any(option in ("-r", "--requirement") for option, _ in options):
        return False
    for token in positional:
        match = bounds._NAME.match(token.split(";", 1)[0].strip())
        if (
            not match
            or bounds.normalize(match.group(1)) not in bounds.BUILD_TOOLS
            or (match.group(3) and not bounds._OPERATOR.match(match.group(3)))
        ):
            return False
    return True


def _run_line(body: str) -> str:
    try:
        array = json.loads(body) if body.lstrip().startswith("[") else None
    except json.JSONDecodeError:
        array = None
    if isinstance(array, list) and all(isinstance(argument, str) for argument in array):
        return shlex.join(array)
    return bounds._COMMENT.sub("", body)


def _dockerfile_lines(text: str) -> list[str]:
    kept = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    logical = bounds._logical_lines("\n".join(kept))
    lines: list[str] = []
    index = 0
    while index < len(logical):
        instruction = _COMMAND_INSTRUCTION.match(logical[index])
        index += 1
        if not instruction:
            continue
        lines.append(_run_line(instruction.group("body")))
        for heredoc in _HEREDOC.finditer(instruction.group("body")):
            words = [inner.strip() for inner in logical[index:]]
            if heredoc.group("word") in words:
                end = index + words.index(heredoc.group("word"))
                lines += [bounds._COMMENT.sub("", inner) for inner in logical[index:end]]
                index = end + 1
    return lines


def _run_values(node: object) -> list[str]:
    if isinstance(node, dict):
        runs = [value for key, value in node.items() if key == "run" and isinstance(value, str)]
        return runs + [run for value in node.values() for run in _run_values(value)]
    if isinstance(node, list):
        return [run for value in node for run in _run_values(value)]
    return []


def _workflow_lines(text: str) -> list[str]:
    return [
        bounds._COMMENT.sub("", line)
        for run in _run_values(yaml.safe_load(text))
        for line in bounds._logical_lines(run)
    ]


def _is_project(target: str) -> bool:
    path = _EXTRAS.sub("", target)
    return path != "" and os.path.normpath(path) == "."


def judge_install(path: str, arguments: list[str]) -> tuple[bool, list[str]]:
    options, positional = _split(arguments)
    targets = positional + [value for option, value in options if option in ("-e", "--editable")]
    command = shlex.join(["pip", "install", *arguments])
    if not any(_is_project(target) for target in targets):
        if targets and _installs_only_build_tools(options, targets):
            return False, []
        return False, [
            f"{path}: `{command}` installs something other than the project or "
            f"{sorted(bounds.BUILD_TOOLS)}; install it through the project and its {bounds.CONSTRAINTS}"
        ]
    findings: list[str] = []
    constraints = [value for option, value in options if option in ("-c", "--constraint")]
    if bounds.CONSTRAINTS not in {os.path.normpath(value) for value in constraints}:
        findings.append(
            f"{path}: `{command}` installs the project without -c {bounds.CONSTRAINTS}, "
            "so a new major of any dependency reaches the build unpinned"
        )
    if not _installs_only_build_tools(options, [t for t in targets if not _is_project(t)]):
        findings.append(
            f"{path}: `{command}` installs something beside the project other than "
            f"{sorted(bounds.BUILD_TOOLS)}, and -c pins only the names {bounds.CONSTRAINTS} lists; "
            "declare it in pyproject.toml and recompile"
        )
    return True, findings


def install_findings(path: str, needs_project: bool = True) -> list[str]:
    if not (REPO_ROOT / path).is_file():
        return [f"{path} is missing; the template builds through it"]
    text = (REPO_ROOT / path).read_text()
    try:
        bodies = _dockerfile_lines(text) if Path(path).name == "Dockerfile" else _workflow_lines(text)
    except yaml.YAMLError:
        return [f"{path} does not parse as YAML, so no step's run value can be read"]
    findings: list[str] = []
    projects = 0
    for body in bodies:
        try:
            installs = pip_installs(body)
            others = other_installs(body)
        except ValueError:
            if _INSTALLER.search(body) and _INSTALL_WORD.search(body):
                findings.append(f"{path}: `{body.strip()}` does not split as a shell line")
            continue
        findings += [
            f"{path}: `{command}` installs through another installer, which never reads "
            f"{bounds.CONSTRAINTS}; install the project with pip and -c {bounds.CONSTRAINTS}"
            for command in others
        ]
        for arguments in installs:
            project, judged = judge_install(path, arguments)
            projects += project
            findings += judged
    if needs_project and projects == 0:
        findings.append(f"{path}: no `pip install` of the project (`.` or -e) with -c {bounds.CONSTRAINTS}")
    return findings


def test_the_manifest_lists_are_every_tracked_manifest():
    tracked = bounds.tracked_files(REPO_ROOT)
    findings = [
        f"{manifest} is in VENDORED_MANIFESTS but not tracked, while {Path(manifest).parts[0]}/ is; "
        "delete the entry"
        for manifest in sorted(VENDORED_MANIFESTS)
        if manifest not in tracked and any(path.startswith(f"{Path(manifest).parts[0]}/") for path in tracked)
    ]
    application_roots = {Path(application).parent.parent for application in bounds.APPLICATIONS}
    applications = {manifest for manifest in bounds.SHIPPED_MANIFESTS if _under(manifest, application_roots)}
    findings += [
        f"{manifest} is an application manifest outside APPLICATIONS, so the ceiling check "
        "compares it with nothing"
        for manifest in sorted(applications - set(bounds.APPLICATIONS))
    ]
    findings += [
        f"{template} is a template outside TEMPLATES, so neither its constraints file nor its "
        "pip-audit ignores are read"
        for template in templates()
        if template not in bounds.TEMPLATES
    ]
    assert findings == [], "\n".join(findings)


def test_every_template_install_reads_its_constraints():
    findings: list[str] = []
    for template in templates():
        constraints = f"{template}/{bounds.CONSTRAINTS}"
        if not (REPO_ROOT / constraints).is_file():
            findings.append(f"{constraints} is missing; run `{bounds.COMPILE_COMMAND}` in the template")
        for path in (f"{template}/Dockerfile", f"{template}/{bounds.WORKFLOW}"):
            findings += install_findings(path)
        workflows = sorted((REPO_ROOT / template / bounds.WORKFLOW).parent.glob("*.y*ml"))
        for workflow in workflows:
            path = str(workflow.relative_to(REPO_ROOT))
            if path != f"{template}/{bounds.WORKFLOW}":
                findings += install_findings(path, needs_project=False)
    assert findings == [], "\n".join(findings)


def test_every_pin_satisfies_its_specifier():
    findings: list[str] = []
    for template in templates():
        constraints = f"{template}/{bounds.CONSTRAINTS}"
        if not (REPO_ROOT / constraints).is_file():
            findings.append(f"{constraints} is missing; run `{bounds.COMPILE_COMMAND}` in the template")
            continue
        pins: dict[str, list[str]] = {}
        for entry in bounds.parse_requirements((REPO_ROOT / constraints).read_text(), constraints):
            if bounds._PIN.match(entry.specifier):
                pins.setdefault(entry.name, []).append(entry.specifier[2:])
        manifest = f"{template}/pyproject.toml"
        for requirement in bounds.parse_pyproject((REPO_ROOT / manifest).read_text(), manifest):
            where = f"{manifest} [{requirement.group}] {requirement.raw}"
            try:
                specifier = SpecifierSet(requirement.specifier)
            except InvalidSpecifier:
                findings.append(
                    f"{where} is a direct reference or no version specifier, which no pin in "
                    f"{constraints} can hold; depend on a released version"
                )
                continue
            if requirement.name not in pins:
                findings.append(f"{constraints} pins no {requirement.name}== for {where}")
            findings += [
                f"{constraints} pins {requirement.name}=={pin}, outside {where}; run "
                f"`{bounds.COMPILE_COMMAND}` in the template"
                for pin in pins.get(requirement.name, [])
                if not specifier.contains(pin, prereleases=True)
            ]
    assert findings == [], "\n".join(findings)
