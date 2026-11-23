"""Scaffold filesystem helper.

Reads an on-disk agent-template scaffold directory into ``{relative_path: content}``, skipping the
build and cache artifacts a fresh repository must not carry. A pure filesystem walk: no boto3, no
credentials. The skip sets below are the only filter; the scaffold's own ``.gitignore`` is not
consulted, so ``tests/test_scaffold_files.py`` holds the two in parity.
"""

from __future__ import annotations

import os
from pathlib import Path

_SKIP_DIR_NAMES = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    ".omc",
    ".ruff_cache",
    ".venv",
    ".mypy_cache",
    "node_modules",
    "build",
    "dist",
}
_SKIP_SUFFIXES = (".pyc",)
_SKIP_DIR_SUFFIXES = (".egg-info",)
_SKIP_FILE_NAMES = frozenset({"uv.lock"})


def collect_scaffold_files(scaffold_dir: Path) -> dict[str, bytes]:
    """Read a scaffold directory into ``{relative_path: content}``, pruning the skip directories in
    place so the walk never descends into them and dropping the skip files and suffixes."""
    files: dict[str, bytes] = {}
    for root, dirs, names in os.walk(scaffold_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIR_NAMES and not d.endswith(_SKIP_DIR_SUFFIXES)]
        for name in names:
            if name in _SKIP_FILE_NAMES or name.endswith(_SKIP_SUFFIXES):
                continue
            abs_path = Path(root) / name
            rel_path = abs_path.relative_to(scaffold_dir).as_posix()
            files[rel_path] = abs_path.read_bytes()
    return files
