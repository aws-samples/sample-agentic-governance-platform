"""Scaffold filesystem helper (E20).

Reads an on-disk agent-template scaffold dir into ``{relative_path: content}``,
skipping build/cache artifacts so a fresh repo/zip carries only source. Pure fs-walk
— no I/O beyond reading the scaffold, no boto3, no credentials. Reused by the Ops
template seed path (``ops_template_service.seed_from_disk``).
"""

from __future__ import annotations

import os
from pathlib import Path

# Scaffold artifacts that must never be pushed into a fresh repo.
#
# THIS LIST IS THE ONLY FILTER — `.gitignore` is NOT consulted. Found live during the E28B test:
# the template's own `.gitignore` names `.venv/` and `uv.lock`, and `.ruff_cache/` was not listed
# anywhere, so a materialized repo received four `.ruff_cache/` files (and, from any workspace
# holding one, a `.venv/` — 4074 files / 107MB on a dev laptop). Being git-ignored keeps a file
# out of OUR repo, never out of a materialized one, because this walks the filesystem.
# So anything the scaffold's .gitignore lists must ALSO be named here. That rule is no longer
# just prose: ``test_scaffold_files.py`` reads the template's real `.gitignore` and fails if an
# entry is not covered by one of the four sets below (E28D/T2) — because `uv.lock` slipped
# through exactly that gap and shipped into every materialized repo.
_SKIP_DIR_NAMES = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    ".omc",
    ".ruff_cache",  # lint cache — whatever happened to sit on the image-build host
    ".venv",  # a local virtualenv is not template content
    ".mypy_cache",
    "node_modules",
    # Packaging output. Not hygiene — a correctness fix: `build/lib/` holds a STALE COPY of the
    # agent source, so shipping it puts plausible-looking code in a customer's repo next to the
    # real `src/`. Any host that ever ran `python -m build` / `uv build` leaves both behind.
    "build",
    "dist",
}
_SKIP_SUFFIXES = (".pyc",)
_SKIP_DIR_SUFFIXES = (".egg-info",)
# Exact FILE names — the mechanism `_SKIP_SUFFIXES` could not express (`uv.lock` has no
# skippable suffix). Nothing installs from the lockfile: the scaffold's Dockerfile runs
# `pip install .` off `pyproject.toml`, and no workflow calls `uv sync --frozen`. Shipping
# 304 KB would claim a lockfile discipline the platform documents it does not maintain.
_SKIP_FILE_NAMES = frozenset({"uv.lock"})


def collect_scaffold_files(scaffold_dir: Path) -> "dict[str, bytes]":
    """Read a scaffold dir into ``{relative_path: content}``, skipping build/cache
    artifacts so a fresh repo carries only source."""
    files: dict[str, bytes] = {}
    for root, dirs, names in os.walk(scaffold_dir):
        # Prune skip dirs in place so os.walk doesn't descend into them.
        dirs[:] = [
            d
            for d in dirs
            if d not in _SKIP_DIR_NAMES and not d.endswith(_SKIP_DIR_SUFFIXES)
        ]
        for name in names:
            if name in _SKIP_FILE_NAMES or name.endswith(_SKIP_SUFFIXES):
                continue
            abs_path = Path(root) / name
            rel_path = abs_path.relative_to(scaffold_dir).as_posix()
            files[rel_path] = abs_path.read_bytes()
    return files
