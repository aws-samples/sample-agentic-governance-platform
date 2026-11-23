"""Parse and write the agent repo's eval files.

``evals/evals.yaml`` → ``EvalConfig`` and ``evals/dataset.jsonl`` → ``list[DatasetItem]``. The repo
is the source of truth, so a hand-written file must give the author a usable answer: every error
is ``"<file>:<line>: <message>"``, and the dataset parser collects ALL of them in one pass rather
than stopping at the first.

YAML is read with a ``SafeLoader`` subclass that only treats ``true``/``false`` as booleans. Plain
YAML 1.1 reads ``off`` / ``on`` / ``yes`` / ``no`` as booleans, which would turn the documented
``tools: off`` into ``False`` and fail validation.
"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from models.eval import DatasetItem, EvalConfig

CONFIG_FILE = "evals/evals.yaml"
DATASET_FILE = "evals/dataset.jsonl"
MAX_ITEMS = 50

_BOOL_TAG = "tag:yaml.org,2002:bool"


class _Loader(yaml.SafeLoader):
    pass


_Loader.yaml_implicit_resolvers = {
    first: [r for r in resolvers if r[0] != _BOOL_TAG]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Loader.add_implicit_resolver(_BOOL_TAG, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))


class EvalFileError(Exception):
    """One or more problems in an eval file. ``errors``: ``"<file>:<line>: <message>"`` each."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _field_errors(exc: ValidationError) -> list[tuple[tuple, str]]:
    return [(e["loc"], f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}") for e in exc.errors()]


def _line_of(root: yaml.Node | None, loc: tuple) -> int:
    """The 1-based line of the YAML node a pydantic ``loc`` points at; the nearest parent if gone."""
    node, line = root, 1
    for part in loc:
        if node is None:
            break
        line = node.start_mark.line + 1
        nxt = None
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                if key.value == part:
                    nxt = value
                    line = key.start_mark.line + 1
                    break
        elif isinstance(node, yaml.SequenceNode) and isinstance(part, int) and part < len(node.value):
            nxt = node.value[part]
        node = nxt
    if node is not None:
        line = node.start_mark.line + 1
    return line


def parse_config(text: str | None) -> EvalConfig:
    if not text:
        return EvalConfig()
    try:
        data = yaml.load(text, Loader=_Loader)  # noqa: S506 — _Loader is a SafeLoader subclass; expires 2027-10-23; added E49A-50A/T13
        root = yaml.compose(text, Loader=_Loader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
        line = mark.line + 1 if mark else 1
        problem = getattr(exc, "problem", None) or str(exc)
        raise EvalFileError([f"{CONFIG_FILE}:{line}: invalid YAML: {problem}"]) from None
    if data is None:
        return EvalConfig()
    if not isinstance(data, dict):
        raise EvalFileError([f"{CONFIG_FILE}:1: must be a mapping of settings"])
    try:
        return EvalConfig.model_validate(data)
    except ValidationError as exc:
        raise EvalFileError(
            [f"{CONFIG_FILE}:{_line_of(root, loc)}: {msg}" for loc, msg in _field_errors(exc)]
        ) from None


def parse_dataset(text: str | None) -> list[DatasetItem]:
    items: list[DatasetItem] = []
    errors: list[str] = []
    first_seen: dict[str, int] = {}
    count = 0
    for lineno, raw in enumerate((text or "").split("\n"), start=1):
        raw = raw.removesuffix("\r")
        if not raw.strip():
            continue
        count += 1
        if count == MAX_ITEMS + 1:
            errors.append(f"{DATASET_FILE}:{lineno}: more than {MAX_ITEMS} items (max {MAX_ITEMS})")
        where = f"{DATASET_FILE}:{lineno}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(f"{where}: invalid JSON: {exc.msg}")
            continue
        if not isinstance(data, dict):
            errors.append(f"{where}: must be a JSON object")
            continue
        try:
            item = DatasetItem.model_validate(data)
        except ValidationError as exc:
            errors.extend(f"{where}: {msg}" for _, msg in _field_errors(exc))
            continue
        if item.id in first_seen:
            errors.append(f"{where}: duplicate id '{item.id}' (first on line {first_seen[item.id]})")
            continue
        first_seen[item.id] = lineno
        items.append(item)
    if errors:
        raise EvalFileError(errors)
    return items


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9._/-]+", value)
        and yaml.load(value, Loader=_Loader) == value  # noqa: S506 — _Loader is a SafeLoader subclass; expires 2027-10-23; added E49A-50A/T13
    ):
        return value
    if isinstance(value, str):
        return json.dumps(value)
    return str(value)


def _commented(left: str, comment: str) -> str:
    return f"{left:<24} # {comment}"


def dump_config(cfg: EvalConfig) -> str:
    """``evals.yaml`` in the documented layout, with its explanatory comments."""
    judges_comment = "AgentCore Evaluations built-in ids; [] = checks only"
    lines = [
        f"version: {cfg.version}",
        f"dataset: {_scalar(cfg.dataset)}",
        "checks:",
        _commented(f"  tools: {cfg.checks.tools}", "exact | in_order | any_order | off   (tool names)"),
        _commented(f"  args: {cfg.checks.args}", "exact | subset | off                  (tool arguments)"),
        _commented(f"  no_error: {_scalar(cfg.checks.no_error)}", "the invoke must succeed"),
    ]
    if cfg.judges:
        lines.append(_commented("judges:", judges_comment))
        lines.extend(f"  - {_scalar(j)}" for j in cfg.judges)
    else:
        lines.append(_commented("judges: []", judges_comment))
    lines += [
        "gate:",
        _commented(f"  checks: {cfg.gate.checks}", "every item must pass every enabled check"),
        _commented(
            f"  judge_min_mean: {cfg.gate.judge_min_mean}",
            "every judge's mean score across items must be >= this",
        ),
    ]
    return "\n".join(lines) + "\n"


def _compact(model: BaseModel) -> str:
    return json.dumps(
        model.model_dump(mode="json", exclude_none=True), separators=(",", ":"), ensure_ascii=False
    )


def dump_dataset(items: list[DatasetItem]) -> str:
    """One compact JSON object per line, ``None`` fields omitted, trailing newline."""
    return "".join(_compact(item) + "\n" for item in items)
