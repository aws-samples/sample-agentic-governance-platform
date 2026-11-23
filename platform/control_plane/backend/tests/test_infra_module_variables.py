"""The root Terraform module neither shadows a module pin nor carries a variable nobody reads.

One module input's value can live in five places: the module default, the root default and the
three `env/*.tfvars`. Two of them disagree silently. A root default forwarded as `arg = var.x` wins
over the module default, so a pin bumped in the module stays frozen in the root: `17cc0681` moved
`modules/langfuse/ecr.tf` to ClickHouse `25.8.33.6` while `variables.tf` kept `25.8.28.1`, every root
apply re-pinned the older image, and `6db2f6d9` closed the gap 126 commits later (`11fb7be2` is its
description-only echo). A root variable referenced by nothing is still assigned in every env file:
`create_vpc` was declared at the tree's first commit and deleted by `10e239a2` seven months later
without ever being read. A pin-shaped module input the root never forwards cannot be set per
environment, the exposure behind `77c841bf`.

Text-level because `terraform validate` is silent on all three (proven on a synthetic root), the
backend job has no Terraform binary and `python-hcl2` is not a dependency. The scanner is standard
library: `mask()` blanks comment bodies, string contents and heredoc bodies while keeping `${...}`
interpolations visible, top-level blocks are found by brace matching over the masked text, depth-0
attributes are sliced from the original, nested `validation {}` blocks are skipped, `normalize()`
makes `{a="x"\\n b="y"}` equal `{a = "x", b = "y"}` and a quoted string equal its heredoc. Module
variables are read from every `*.tf` in the module directory because `ecr.tf` declares the one
this class is about.

Predicates. B1: for every module argument that is exactly `var.x`, where the root default is not
null and the module default is a non-empty literal (`""`, `{}`, `[]` and `null` mean the module has
no opinion), the two defaults are equal after normalisation. B2: every root variable is referenced
as `var.x` outside its own `variable` block in a root `*.tf`. B3: a module input named `*_version`,
`*_image` or `*_tag` of an instantiated module is forwarded from the root. B4: a module argument
of those shapes holds no non-empty string literal, no bare number and no `local.` reference, because
a pin written in the call cannot be set per environment either; `""`, `var.` and a bare `data.`
reference are allowed, while a string in a reference's index bracket or in `file(...)`, a non-empty
comparison operand and an empty heredoc are refused loud. B4 does not see a number inside an
interpolation, `tostring(...)` or `coalesce(...)`, a pin arriving through a `for_each` map literal
read as `each.value`, `module.<other>.<output>` or `terraform.workspace`.
Today's violations sit in the `KNOWN_*` lists with their remover; a list only shrinks.
Rung 2: rung 1 is one owner per default (delete the module defaults for forwarded inputs, or
`nullable = false` on the module side with a null root default), a `modules/**` change outside
this file's lane.

Held elsewhere, not here: an undeclared tfvars key and a root variable missing from an env file
(`test_tfvars_completeness.py`); a missing required input, an argument the module does not declare
and a nonexistent module output (`terraform validate` in the terraform quality job). No offline
predicate: an account-level precondition outside the stack, such as the Transaction Search setting
`a6724d53` enabled.
"""

import os
import re
import textwrap
from collections import namedtuple
from functools import cache
from pathlib import Path

REPO_ROOT = (
    Path(os.environ["AGP_REPO_ROOT"])
    if "AGP_REPO_ROOT" in os.environ
    else Path(__file__).resolve().parents[4]
)
INFRA = REPO_ROOT / "platform/control_plane/infrastructure"

KNOWN_SHADOWED_DEFAULTS: dict[tuple[str, str, str], str] = {
    ("langfuse_admin_email", "langfuse", "langfuse_init_user_email"): "product owner: root infrastructure",
}
KNOWN_UNREFERENCED_ROOT_VARIABLES: dict[str, str] = {
    "app_name": "product owner: root infrastructure",
}
KNOWN_UNFORWARDED_PINS: dict[tuple[str, str], str] = {
    ("langfuse", "langfuse_version"): "product owner: root infrastructure",
    ("langfuse", "postgres_version"): "product owner: root infrastructure",
}

PIN_SHAPES = ("_version", "_image", "_tag")
EMPTY_DEFAULTS = frozenset({"", "{}", "[]", "null"})
MODULE_BLOCK_FLOOR = 15
ROOT_VARIABLE_FLOOR = 40
MODULE_ARGUMENT_FLOOR = 150
MODULE_DIR_FLOOR = 18

Block = namedtuple("Block", "kind labels body line")
Variable = namedtuple("Variable", "name default nullable file line")
ModuleBlock = namedtuple("ModuleBlock", "name source arguments line")
Finding = namedtuple("Finding", "key message")

_HEREDOC_OPENER = re.compile(r"<<-?([A-Za-z_][A-Za-z0-9_]*)\r?\n")
_HEREDOC_WHOLE = re.compile(r"<<-?([A-Za-z_][A-Za-z0-9_]*)\r?\n(.*?)\n[ \t]*\1[ \t]*", re.DOTALL)
_HEADER = re.compile(r'^[ \t]*([A-Za-z_][A-Za-z0-9_-]*)((?:[ \t]+"[^"\n]*")*)[ \t]*\{', re.MULTILINE)
_ATTRIBUTE = re.compile(r"^[ \t]*([A-Za-z_][A-Za-z0-9_-]*)[ \t]*=(?!=)", re.MULTILINE)
_VAR_REFERENCE = re.compile(r"\bvar\.([A-Za-z_][A-Za-z0-9_-]*)")
_LOCAL_REFERENCE = re.compile(r"\blocal\.([A-Za-z_][A-Za-z0-9_-]*)")
_NUMBER_LITERAL = re.compile(r"-?[0-9]+(\.[0-9]+)?")
_BARE_LITERAL = re.compile(r"-?[0-9]+(\.[0-9]+)?|true|false|null|[\[{].*", re.DOTALL)
_NEWLINE_RUN = re.compile(r"[ \t]*\r?\n\s*")
_COMMA_AFTER_OPEN = re.compile(r"([\[{(,]),+")
_COMMA_BEFORE_CLOSE = re.compile(r",+([\]})])")
_AROUND_PUNCTUATION = re.compile(r"\s*([=,{}\[\]():?])\s*")
_WHITESPACE_RUN = re.compile(r"\s+")


Regions = list[tuple[int, int, str]]


def _comment_end(text: str, i: int) -> int | None:
    if text[i] == "#" or text.startswith("//", i):
        end = text.find("\n", i)
        return len(text) if end < 0 else end
    if text.startswith("/*", i):
        end = text.find("*/", i + 2)
        return len(text) if end < 0 else end + 2
    return None


def _scan_code(text: str, regions: Regions, i: int, until_brace: bool) -> int:
    depth = 0
    while i < len(text):
        char = text[i]
        comment_end = _comment_end(text, i)
        if comment_end is not None:
            regions.append((i, comment_end, "comment"))
            i = comment_end
        elif char == '"':
            i = _scan_string(text, regions, i)
        elif char == "<" and (opener := _HEREDOC_OPENER.match(text, i)):
            i = _scan_heredoc(text, regions, opener)
        elif until_brace and char in "{}":
            depth += 1 if char == "{" else -1
            if depth < 0:
                return i
            i += 1
        else:
            i += 1
    return i


def _scan_string(text: str, regions: Regions, i: int) -> int:
    start = i + 1
    i = start
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2
        elif char == '"':
            regions.append((start, i, "string"))
            return i + 1
        elif char in "$%" and text.startswith(char, i + 1):
            i += 2
        elif char in "$%" and text.startswith("{", i + 1):
            regions.append((start, i, "string"))
            i = _scan_code(text, regions, i + 2, until_brace=True) + 1
            start = i
        else:
            i += 1
    regions.append((start, len(text), "string"))
    return len(text)


def _scan_heredoc(text: str, regions: Regions, opener: re.Match) -> int:
    marker = opener.group(1)
    i = opener.end()
    while i < len(text):
        end = text.find("\n", i)
        end = len(text) if end < 0 else end
        line = text[i:end]
        if line.strip() == marker:
            regions.append((opener.end() - 1, i + len(line) - len(line.lstrip()), "heredoc"))
            return end
        i = end + 1
    regions.append((opener.end() - 1, len(text), "heredoc"))
    return len(text)


@cache
def _regions(text: str) -> tuple[tuple[int, int, str], ...]:
    """Spans of comment text, string contents (quotes and interpolations excluded) and heredoc bodies."""
    regions: Regions = []
    _scan_code(text, regions, 0, until_brace=False)
    return tuple(regions)


def mask(text: str) -> str:
    pieces: list[str] = []
    position = 0
    for start, end, kind in _regions(text):
        pieces.append(text[position:start])
        blanked = text[start:end]
        pieces.append(" " * len(blanked) if kind == "heredoc" else re.sub(r"[^\n]", " ", blanked))
        position = end
    pieces.append(text[position:])
    return "".join(pieces)


def _without_comments(text: str) -> str:
    pieces: list[str] = []
    position = 0
    for start, end, kind in _regions(text):
        if kind == "comment":
            pieces.append(text[position:start])
            position = end
    pieces.append(text[position:])
    return "".join(pieces)


def _squeeze_code(code: str) -> str:
    code = _NEWLINE_RUN.sub(",", code)
    code = _COMMA_AFTER_OPEN.sub(r"\1", code)
    code = _COMMA_BEFORE_CLOSE.sub(r"\1", code)
    code = _AROUND_PUNCTUATION.sub(r"\1", code)
    return _WHITESPACE_RUN.sub(" ", code)


def normalize(expr: str) -> str:
    text = _without_comments(expr).strip()
    heredoc = _HEREDOC_WHOLE.fullmatch(text)
    if heredoc:
        return textwrap.dedent(heredoc.group(2)).strip()
    pieces: list[str] = []
    position = 0
    for start, end, _kind in _regions(text):
        pieces.append(_squeeze_code(text[position:start]))
        pieces.append(text[start:end])
        position = end
    pieces.append(_squeeze_code(text[position:]))
    text = "".join(pieces).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'" and text[0] not in text[1:-1]:
        return text[1:-1]
    return text


def _matching_brace(masked: str, open_index: int) -> int:
    depth = 0
    for i in range(open_index, len(masked)):
        if masked[i] == "{":
            depth += 1
        elif masked[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError(f"unbalanced braces from offset {open_index}")


def _expression_end(masked: str, start: int) -> int:
    depth = 0
    for i in range(start, len(masked)):
        char = masked[i]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "\n" and depth <= 0:
            return i
    return len(masked)


def _next_line(masked: str, position: int) -> int:
    newline = masked.find("\n", position)
    return len(masked) if newline < 0 else newline + 1


def top_level_blocks(text: str) -> list[Block]:
    masked = mask(text)
    blocks: list[Block] = []
    position = 0
    while header := _HEADER.search(masked, position):
        open_index = header.end() - 1
        close_index = _matching_brace(masked, open_index)
        labels = tuple(re.findall(r'"([^"]*)"', text[header.start(2) : header.end(2)]))
        line = text.count("\n", 0, header.start()) + 1
        blocks.append(Block(header.group(1), labels, text[open_index + 1 : close_index], line))
        position = close_index + 1
    return blocks


def _attribute_spans(body: str) -> dict[str, tuple[int, int]]:
    masked = mask(body)
    spans: dict[str, tuple[int, int]] = {}
    position = 0
    while position < len(masked):
        header = _HEADER.match(masked, position)
        attribute = _ATTRIBUTE.match(masked, position)
        if header:
            position = _matching_brace(masked, header.end() - 1)
        elif attribute:
            start = attribute.end()
            end = _expression_end(masked, start)
            spans[attribute.group(1)] = (start, start + len(masked[start:end].rstrip()))
            position = end
        position = _next_line(masked, position)
    return spans


def attributes(body: str) -> dict[str, str]:
    return {name: body[start:end].strip() for name, (start, end) in _attribute_spans(body).items()}


def _variables_in(path: Path) -> dict[str, Variable]:
    variables: dict[str, Variable] = {}
    for block in top_level_blocks(path.read_text()):
        if block.kind != "variable" or len(block.labels) != 1:
            continue
        spans = _attribute_spans(block.body)
        default = None
        line = block.line
        if "default" in spans:
            start, end = spans["default"]
            default = normalize(block.body[start:end])
            line += block.body.count("\n", 0, start)
        nullable = True
        if "nullable" in spans:
            start, end = spans["nullable"]
            nullable = normalize(block.body[start:end]) != "false"
        variables[block.labels[0]] = Variable(block.labels[0], default, nullable, path, line)
    return variables


def root_variables(infra: Path = INFRA) -> dict[str, Variable]:
    variables: dict[str, Variable] = {}
    for path in sorted(infra.glob("*.tf")):
        variables.update(_variables_in(path))
    return variables


def module_variables(module_dir: Path) -> dict[str, Variable]:
    variables: dict[str, Variable] = {}
    for path in sorted(module_dir.glob("*.tf")):
        variables.update(_variables_in(path))
    return variables


def module_blocks(infra: Path = INFRA) -> list[ModuleBlock]:
    blocks: list[ModuleBlock] = []
    for path in sorted(infra.glob("*.tf")):
        for block in top_level_blocks(path.read_text()):
            if block.kind != "module" or len(block.labels) != 1:
                continue
            arguments = attributes(block.body)
            source = normalize(arguments["source"]) if "source" in arguments else ""
            blocks.append(ModuleBlock(block.labels[0], source, arguments, block.line))
    return blocks


def _line_offset(text: str, line: int) -> int:
    offset = 0
    for _ in range(line - 1):
        offset = text.index("\n", offset) + 1
    return offset


def var_references(infra: Path = INFRA) -> set[str]:
    references: set[str] = set()
    for path in sorted(infra.glob("*.tf")):
        text = path.read_text()
        masked = mask(text)
        for block in top_level_blocks(text):
            if block.kind == "variable":
                body_start = masked.index("{", _line_offset(text, block.line)) + 1
                masked = masked[:body_start] + " " * len(block.body) + masked[body_start + len(block.body) :]
        references.update(_VAR_REFERENCE.findall(masked))
    return references


def _module_dir(infra: Path, block: ModuleBlock) -> Path | None:
    if not block.source.startswith("."):
        return None
    module_dir = Path(os.path.normpath(infra / block.source))
    return module_dir if module_dir.is_dir() else None


def _where(infra: Path, variable: Variable) -> str:
    return f"{variable.file.relative_to(infra).as_posix()}:{variable.line}"


def _hcl(value: str) -> str:
    return value if _BARE_LITERAL.fullmatch(value) else f'"{value}"'


def shadowed_defaults(infra: Path = INFRA) -> list[Finding]:
    roots = root_variables(infra)
    findings: list[Finding] = []
    for block in module_blocks(infra):
        module_dir = _module_dir(infra, block)
        if module_dir is None:
            continue
        inputs = module_variables(module_dir)
        for name, expr in block.arguments.items():
            forwarded = _VAR_REFERENCE.fullmatch(expr)
            root = roots.get(forwarded.group(1)) if forwarded else None
            module_input = inputs.get(name)
            if root is None or module_input is None:
                continue
            if root.default in (None, "null") or module_input.default in (None, *EMPTY_DEFAULTS):
                continue
            if root.default == module_input.default:
                continue
            message = (
                f"{_where(infra, root)}: root `{root.name}` default {_hcl(root.default)} is forwarded to "
                f'module "{block.name}" input `{name}` whose default is {_hcl(module_input.default)} '
                f"({_where(infra, module_input)}); set the root default to {_hcl(module_input.default)} "
                "(the module owns the pin) or delete the root default so the value comes from env/*.tfvars"
            )
            if not module_input.nullable:
                message += ", or set it to null (the module input is nullable = false)"
            findings.append(Finding((root.name, block.name, name), message))
    return findings


def unreferenced_root_variables(infra: Path = INFRA) -> list[Finding]:
    references = var_references(infra)
    env_files = ", ".join(p.relative_to(infra).as_posix() for p in sorted(infra.glob("env/*.tfvars")))
    return [
        Finding(
            variable.name,
            f"{_where(infra, variable)}: root variable `{variable.name}` is referenced by nothing in the "
            f"root files; delete it and its line in {env_files or 'env/*.tfvars'}, or wire it",
        )
        for variable in root_variables(infra).values()
        if variable.name not in references
    ]


def unforwarded_pins(infra: Path = INFRA) -> list[Finding]:
    findings: list[Finding] = []
    for block in module_blocks(infra):
        module_dir = _module_dir(infra, block)
        if module_dir is None:
            continue
        for name, module_input in module_variables(module_dir).items():
            if not name.endswith(PIN_SHAPES) or name in block.arguments:
                continue
            default = "required" if module_input.default is None else f"default {_hcl(module_input.default)}"
            findings.append(
                Finding(
                    (block.name, name),
                    f'module "{block.name}" (root line {block.line}) leaves input `{name}` ({default}, '
                    f"{_where(infra, module_input)}) to the module; forward it from a root variable so "
                    "env/*.tfvars can pin it per environment",
                )
            )
    return findings


def _literals_and_locals(expr: str) -> list[str]:
    masked = mask(expr)
    held: list[str] = []
    if any(end > start for start, end, kind in _regions(expr) if kind != "comment"):
        held.append("a string literal")
    if _NUMBER_LITERAL.fullmatch(masked.strip()):
        held.append("a number literal")
    held.extend(f"`local.{name}`" for name in dict.fromkeys(_LOCAL_REFERENCE.findall(masked)))
    return held


def pinned_by_a_literal(infra: Path = INFRA) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(infra.glob("*.tf")):
        for block in top_level_blocks(path.read_text()):
            if block.kind != "module" or len(block.labels) != 1:
                continue
            for name, (start, end) in _attribute_spans(block.body).items():
                if not name.endswith(PIN_SHAPES):
                    continue
                held = _literals_and_locals(block.body[start:end])
                if not held:
                    continue
                line = block.line + block.body.count("\n", 0, start)
                findings.append(
                    Finding(
                        (block.labels[0], name),
                        f'{path.relative_to(infra).as_posix()}:{line}: module "{block.labels[0]}" argument '
                        f"`{name}` is set by {' and '.join(held)}; forward it from a root variable or read "
                        "it from a data source, so the pin is set per environment and not in the module call",
                    )
                )
    return findings


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"))


def test_root_defaults_never_shadow_a_module_pin():
    fresh = [f for f in shadowed_defaults() if f.key not in KNOWN_SHADOWED_DEFAULTS]
    assert fresh == [], "\n".join(f.message for f in fresh) + (
        "\nIf this is today's debt, add the key to KNOWN_SHADOWED_DEFAULTS with its remover, "
        "in a reviewed diff."
    )


def test_shadowed_defaults_known_list_only_shrinks():
    live = {f.key for f in shadowed_defaults()}
    stale = sorted(k for k in KNOWN_SHADOWED_DEFAULTS if k not in live)
    assert stale == [], f"no longer a violation; delete it from KNOWN_SHADOWED_DEFAULTS: {stale}"


def test_every_root_variable_is_referenced():
    fresh = [f for f in unreferenced_root_variables() if f.key not in KNOWN_UNREFERENCED_ROOT_VARIABLES]
    assert fresh == [], "\n".join(f.message for f in fresh) + (
        "\nIf this is today's debt, add the key to KNOWN_UNREFERENCED_ROOT_VARIABLES with its remover, "
        "in a reviewed diff."
    )


def test_unreferenced_root_variables_known_list_only_shrinks():
    live = {f.key for f in unreferenced_root_variables()}
    stale = sorted(k for k in KNOWN_UNREFERENCED_ROOT_VARIABLES if k not in live)
    assert stale == [], f"no longer a violation; delete it from KNOWN_UNREFERENCED_ROOT_VARIABLES: {stale}"


def test_pin_shaped_module_inputs_are_forwarded():
    fresh = [f for f in unforwarded_pins() if f.key not in KNOWN_UNFORWARDED_PINS]
    assert fresh == [], "\n".join(f.message for f in fresh) + (
        "\nIf this is today's debt, add the key to KNOWN_UNFORWARDED_PINS with its remover, "
        "in a reviewed diff."
    )


def test_unforwarded_pins_known_list_only_shrinks():
    live = {f.key for f in unforwarded_pins()}
    stale = sorted(k for k in KNOWN_UNFORWARDED_PINS if k not in live)
    assert stale == [], f"no longer a violation; delete it from KNOWN_UNFORWARDED_PINS: {stale}"


def test_no_module_pin_is_set_by_a_literal_or_a_local():
    findings = pinned_by_a_literal()
    assert findings == [], "\n".join(f.message for f in findings)


def test_the_scan_has_something_to_scan():
    blocks = module_blocks()
    assert len(blocks) >= MODULE_BLOCK_FLOOR, [b.name for b in blocks]
    assert len(root_variables()) >= ROOT_VARIABLE_FLOOR
    assert sum(len(b.arguments) for b in blocks) >= MODULE_ARGUMENT_FLOOR
    module_dirs = [p for p in (INFRA / "modules").iterdir() if p.is_dir()]
    assert len(module_dirs) >= MODULE_DIR_FLOOR, sorted(p.name for p in module_dirs)


def test_scanner_reads_heredoc_map_and_list_defaults_and_skips_validation_blocks(tmp_path):
    _write(
        tmp_path,
        "variables.tf",
        """
        variable "doc" {
          description = <<-EOT
            First line # not a comment
            second line
          EOT
          type        = string
          default     = <<-EOT
            25.8.28.1
          EOT
          validation {
            condition     = length(var.doc) > 0
            error_message = "must not be empty"
          }
        }

        variable "tags" {
          type = map(string)
          default = {
            Project   = "agp"
            ManagedBy = "terraform"
          }
        }

        variable "zones" {
          type    = list(string)
          default = ["us-east-1a", "us-east-1b"]
        }
        """,
    )
    blocks = top_level_blocks((tmp_path / "variables.tf").read_text())
    assert [(b.kind, b.labels, b.line) for b in blocks] == [
        ("variable", ("doc",), 1),
        ("variable", ("tags",), 16),
        ("variable", ("zones",), 24),
    ]
    doc = attributes(blocks[0].body)
    assert set(doc) == {"description", "type", "default"}
    assert normalize(doc["description"]) == "First line # not a comment\nsecond line"
    assert normalize(doc["default"]) == "25.8.28.1" == normalize('"25.8.28.1"') == normalize("'25.8.28.1'")
    variables = root_variables(tmp_path)
    assert variables["doc"].default == "25.8.28.1"
    assert variables["doc"].line == 7
    assert variables["tags"].default == normalize('{ Project = "agp", ManagedBy = "terraform" }')
    assert variables["tags"].default == '{Project="agp",ManagedBy="terraform"}'
    assert variables["zones"].default == '["us-east-1a","us-east-1b"]'
    assert variables["zones"].line == 26


def test_scanner_ignores_hash_inside_strings_and_drops_trailing_comments(tmp_path):
    _write(
        tmp_path,
        "variables.tf",
        """
        variable "instances" {
          description = "instances # not a comment // nor this"
          type        = number
          default     = 2 # Default to 2 instances
        }

        variable "name" {
          type    = string
          default = "a#b" // keeps the hash
          nullable = false
        }
        """,
    )
    text = (tmp_path / "variables.tf").read_text()
    masked = mask(text)
    assert len(masked) == len(text)
    lines = masked.splitlines()
    assert lines[1] == '  description = "' + " " * len("instances # not a comment // nor this") + '"'
    assert lines[3] == "  default     = 2 " + " " * len("# Default to 2 instances")
    variables = root_variables(tmp_path)
    assert variables["instances"].default == "2"
    assert normalize(attributes(top_level_blocks(text)[0].body)["description"]) == (
        "instances # not a comment // nor this"
    )
    assert variables["name"].default == "a#b"
    assert variables["name"].nullable is False
    assert variables["instances"].nullable is True


def test_scanner_finds_a_variable_declared_outside_variables_tf(tmp_path):
    module_dir = tmp_path / "modules" / "langfuse"
    _write(
        module_dir,
        "variables.tf",
        """
        variable "name" {
          description = "no default here"
          type        = string
        }
        """,
    )
    _write(
        module_dir,
        "ecr.tf",
        """
        # Bump these on a schedule.
        variable "langfuse_version" {
          description = "Langfuse image version to pull and push"
          type        = string
          default     = "3.225.11"
        }
        """,
    )
    variables = module_variables(module_dir)
    assert set(variables) == {"name", "langfuse_version"}
    assert variables["name"].default is None
    assert variables["name"].line == 1
    assert variables["name"].file == module_dir / "variables.tf"
    assert variables["langfuse_version"].default == "3.225.11"
    assert variables["langfuse_version"].file == module_dir / "ecr.tf"
    assert variables["langfuse_version"].line == 5


def test_scanner_reads_for_each_module_blocks_and_the_providers_meta_argument(tmp_path):
    _write(
        tmp_path,
        "main.tf",
        """
        module "agent_registry" {
          source = "./modules/agent_registry"

          # Two instances, one per registry.
          for_each = {
            agents = { name = var.agent_registry_name, read_region = var.agent_registry_region }
            mcp    = { name = var.mcp_registry_name, read_region = var.mcp_registry_region }
          }

          name        = each.value.name
          region      = var.agent_registry_region
          read_region = each.value.read_region
        }

        module "langfuse" {
          source = "./modules/langfuse"
          count  = var.create_langfuse ? 1 : 0

          providers = {
            aws           = aws
            aws.us_east_1 = aws.us_east_1
          }

          name               = "${local.name_prefix}-langfuse"
          clickhouse_version = var.clickhouse_version
        }
        """,
    )
    blocks = module_blocks(tmp_path)
    assert [(b.name, b.source, b.line) for b in blocks] == [
        ("agent_registry", "./modules/agent_registry", 1),
        ("langfuse", "./modules/langfuse", 15),
    ]
    registry, langfuse = blocks[0].arguments, blocks[1].arguments
    assert set(registry) == {"source", "for_each", "name", "region", "read_region"}
    assert registry["name"] == "each.value.name"
    assert registry["region"] == "var.agent_registry_region"
    assert registry["read_region"] == "each.value.read_region"
    assert normalize(registry["for_each"]) == normalize(
        "{ agents = { name = var.agent_registry_name, read_region = var.agent_registry_region }, "
        "mcp = { name = var.mcp_registry_name, read_region = var.mcp_registry_region } }"
    )
    assert set(langfuse) == {"source", "count", "providers", "name", "clickhouse_version"}
    assert langfuse["count"] == "var.create_langfuse ? 1 : 0"
    assert normalize(langfuse["providers"]) == "{aws=aws,aws.us_east_1=aws.us_east_1}"
    assert langfuse["name"] == '"${local.name_prefix}-langfuse"'
    assert langfuse["clickhouse_version"] == "var.clickhouse_version"


def test_var_references_ignore_comments_and_see_interpolations(tmp_path):
    _write(
        tmp_path,
        "main.tf",
        """
        # var.in_comment is named here only
        locals {
          a = "${var.in_interpolation}-x"
          b = "var.in_string"
          c = var.plain
          /* var.in_block_comment */
        }
        """,
    )
    _write(
        tmp_path,
        "variables.tf",
        """
        variable "self" {
          type = string
          validation {
            condition     = length(var.self) > 0
            error_message = "must not be empty"
          }
        }
        """,
    )
    assert var_references(tmp_path) == {"in_interpolation", "plain"}


def _clickhouse_tree(root: Path, root_default: str, module_default: str, nullable: str = "") -> None:
    _write(
        root,
        "variables.tf",
        f'''
        variable "clickhouse_version" {{
          description = "ClickHouse image version for the Langfuse module"
          type        = string
          default     = "{root_default}"
        }}
        ''',
    )
    _write(
        root,
        "main.tf",
        """
        module "langfuse" {
          source = "./modules/langfuse"

          clickhouse_version = var.clickhouse_version
        }
        """,
    )
    _write(
        root,
        "modules/langfuse/ecr.tf",
        f'''
        variable "clickhouse_version" {{
          description = "ClickHouse image version to pull and push"
          type        = string
          default     = "{module_default}"{nullable}
        }}
        ''',
    )


def test_shadowed_default_message_suggests_null_only_for_nullable_false(tmp_path):
    _clickhouse_tree(tmp_path / "nullable", "25.8.28.1", "25.8.33.6", "\n          nullable    = false")
    _clickhouse_tree(tmp_path / "plain", "25.8.28.1", "25.8.33.6")
    (with_null,) = shadowed_defaults(tmp_path / "nullable")
    (without,) = shadowed_defaults(tmp_path / "plain")
    assert with_null.message.endswith(", or set it to null (the module input is nullable = false)")
    assert "null" not in without.message


def test_reproduction_of_the_clickhouse_pin(tmp_path):
    _clickhouse_tree(tmp_path, "25.8.28.1", "25.8.33.6")
    assert shadowed_defaults(tmp_path) == [
        Finding(
            ("clickhouse_version", "langfuse", "clickhouse_version"),
            'variables.tf:4: root `clickhouse_version` default "25.8.28.1" is forwarded to module "langfuse" '
            'input `clickhouse_version` whose default is "25.8.33.6" (modules/langfuse/ecr.tf:4); '
            'set the root default to "25.8.33.6" (the module owns the pin) or delete the root default '
            "so the value comes from env/*.tfvars",
        )
    ]
    _clickhouse_tree(tmp_path, "25.8.33.6", "25.8.33.6")
    assert shadowed_defaults(tmp_path) == []


def test_a_module_pin_set_by_a_literal_or_a_local_is_named_with_its_file_and_line(tmp_path):
    _write(
        tmp_path,
        "main.tf",
        """
        module "langfuse" {
          source = "./modules/langfuse"

          clickhouse_version = "24.3"
          postgres_version   = 16
          langfuse_version   = /* was "3.0", local.v3 */ var.langfuse_version
          redis_tag          = "${var.redis_tag}"
          redis_image        = "${local.registry}/redis:${var.redis_tag}"
        }

        module "ecs" {
          source = "./modules/ecs"

          container_image = local.zz_pin
          sidecar_image   = var.sidecar_image != "" ? var.sidecar_image : data.aws_ssm_parameter.sidecar.value
          proxy_image     = ""
          name            = "agp-ecs"
        }
        """,
    )
    remedy = (
        "; forward it from a root variable or read it from a data source, so the pin is set per "
        "environment and not in the module call"
    )
    assert pinned_by_a_literal(tmp_path) == [
        Finding(
            ("langfuse", "clickhouse_version"),
            'main.tf:4: module "langfuse" argument `clickhouse_version` is set by a string literal' + remedy,
        ),
        Finding(
            ("langfuse", "postgres_version"),
            'main.tf:5: module "langfuse" argument `postgres_version` is set by a number literal' + remedy,
        ),
        Finding(
            ("langfuse", "redis_image"),
            'main.tf:8: module "langfuse" argument `redis_image` is set by a string literal and '
            "`local.registry`" + remedy,
        ),
        Finding(
            ("ecs", "container_image"),
            'main.tf:14: module "ecs" argument `container_image` is set by `local.zz_pin`' + remedy,
        ),
    ]
