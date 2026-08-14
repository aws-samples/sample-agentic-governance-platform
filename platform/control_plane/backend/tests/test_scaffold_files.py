"""Unit tests for the scaffold fs-walk helper (E20, E28D/T2).

Guards the pure ``collect_scaffold_files`` reader after its old home
(``test_template_rollout_service.py``) was deleted: it must read a scaffold dir into
``{relpath: bytes}`` and skip build/cache artifacts (``__pycache__`` dirs, ``.pyc`` files).

E28D/T2 turns the module's own rule into a fence. The header says "anything the scaffold's
.gitignore lists must ALSO be named here" and nothing enforced it, which is how ``uv.lock``
(304 KB) came to ship into every materialized customer repo. The parity test below reads the
template's real ``.gitignore`` and fails the next time a line is added there without a matching
skip entry.
"""

from pathlib import Path

from services.scaffold_files import (
    _SKIP_DIR_NAMES,
    _SKIP_DIR_SUFFIXES,
    _SKIP_FILE_NAMES,
    _SKIP_SUFFIXES,
    collect_scaffold_files,
)

# Resolve relative to this test file so it works regardless of pytest's cwd:
# tests/ -> backend/ -> control_plane/ -> agent-templates/...
TEMPLATE_GITIGNORE = (
    Path(__file__).resolve().parents[2]
    / "agent-templates/strands-agentcore/.gitignore"
)


def test_collect_scaffold_files_reads_tree_and_skips_artifacts(tmp_path: Path):
    (tmp_path / "app.py").write_bytes(b"print('hi')\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "mod.py").write_bytes(b"x = 1\n")

    # Build/cache artifacts that must be skipped.
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "app.cpython-311.pyc").write_bytes(b"\x00cached")
    (tmp_path / "sub" / "mod.pyc").write_bytes(b"\x00cached")

    files = collect_scaffold_files(tmp_path)

    assert files == {
        "app.py": b"print('hi')\n",
        "sub/mod.py": b"x = 1\n",
    }
    # No skipped artifact leaked in.
    assert not any(k.endswith(".pyc") or "__pycache__" in k for k in files)


def test_packaging_output_dirs_are_pruned(tmp_path: Path):
    """A ``build/`` or ``dist/`` dir holds a STALE COPY of the agent source.

    Worse in kind than the E28B ``.ruff_cache`` leak: cache files are inert noise, but
    ``build/lib/main.py`` is plausible source code landing in a customer's repo, shadowing the
    real ``src/``. Any host that has ever run ``python -m build`` / ``uv build`` leaves one.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_bytes(b"REAL = 1\n")
    (tmp_path / "build").mkdir(parents=True)
    (tmp_path / "build" / "lib").mkdir()
    (tmp_path / "build" / "lib" / "main.py").write_bytes(b"STALE = 1\n")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "agent-0.1.0.tar.gz").write_bytes(b"\x1f\x8b")

    files = collect_scaffold_files(tmp_path)

    assert files == {"src/main.py": b"REAL = 1\n"}


def test_uv_lock_is_not_collected(tmp_path: Path):
    """``uv.lock`` is 304 KB nothing installs from — the image build runs ``pip install .``.

    Shipping it claims a lockfile discipline the platform elsewhere documents it does not have
    (the promotion surface records the build as non-reproducible: floating base image, ranged
    deps, no lockfile). Skipping it also makes the template's ``.gitignore`` and this module agree.
    """
    (tmp_path / "pyproject.toml").write_bytes(b"[project]\nname = 'agent'\n")
    (tmp_path / "uv.lock").write_bytes(b"version = 1\n")

    files = collect_scaffold_files(tmp_path)

    assert "uv.lock" not in files
    # The narrow skip must not take the sibling manifest the build actually reads.
    assert "pyproject.toml" in files


def _covered_as_dir(entry: str) -> bool:
    """Would the walk's DIR prune match a directory basename ``entry``?"""
    if entry.startswith("*"):
        return entry[1:] in _SKIP_DIR_SUFFIXES
    return entry in _SKIP_DIR_NAMES


def _covered_as_file(entry: str) -> bool:
    """Would the walk's inner-loop FILE check match a file basename ``entry``?"""
    if entry.startswith("*"):
        return entry[1:] in _SKIP_SUFFIXES
    return entry in _SKIP_FILE_NAMES


def uncovered_gitignore_entries(gitignore_text: str) -> "list[str]":
    """Return the ``.gitignore`` entries the scaffold walk would NOT skip.

    Two gitignore facts drive the parsing, and getting either wrong makes the fence lie:

    1. **The walk matches unanchored BASENAMES.** ``os.walk`` yields basenames, and the prune
       compares them to the skip sets at every level, so a bare ``build`` entry already covers
       the anchored ``/build`` and the explicit ``**/build`` forms — they are the same rule
       written three ways. Normalising them away prevents FALSE ALARMS (and prevents the
       message below from inviting a literal ``"/dist"`` into a skip set, which ``os.walk``
       could never match — a dead entry that would go green while the real coverage came from
       the pre-existing ``dist``).
    2. **A trailing ``/`` means directory-ONLY; no slash matches files AND dirs.** That
       distinction is the whole point of the two mechanisms, so it is read BEFORE it is
       stripped. ``uv.lock/`` therefore demands a DIR skip (which ``_SKIP_FILE_NAMES`` does not
       give), and only a dir set can satisfy it.

    Bare entries are kind-AMBIGUOUS, so either kind of coverage is accepted. That tolerance is
    deliberate and one-directional: the template's own ``uv.lock`` line is bare and is covered
    for files only, so demanding BOTH kinds would red-flag the real template and push the walk
    to widen for a directory nobody creates. The residual gap is narrow and known — a bare
    entry covered for one kind only still passes, e.g. a FILE named ``build``.

    Globs are honoured only in gitignore's ``*<suffix>`` form, which is what the walk's
    ``endswith`` checks express. Anything richer (``build-*.tar``, a path-scoped
    ``secrets/config.json``) is inexpressible here and is reported — the correct signal that the
    mechanism, not just the set, needs extending.
    """
    uncovered = []
    for raw in gitignore_text.splitlines():
        entry = raw.strip()
        if not entry or entry.startswith(("#", "!")):
            continue
        dir_only = entry.endswith("/")  # read the marker BEFORE normalising it away
        entry = entry.removeprefix("**/").lstrip("/").rstrip("/")
        if not entry:
            continue
        covered = (
            _covered_as_dir(entry)
            if dir_only
            else _covered_as_dir(entry) or _covered_as_file(entry)
        )
        if not covered:
            uncovered.append(f"{entry}/" if dir_only else entry)
    return uncovered


def test_template_gitignore_entries_are_all_covered():
    """The module header's rule, enforced: every template ``.gitignore`` entry is named here.

    Fails loudly the next time someone adds a line to the template's ``.gitignore`` without
    teaching the walk about it — the exact gap that let ``uv.lock`` slip through.
    """
    uncovered = uncovered_gitignore_entries(TEMPLATE_GITIGNORE.read_text())

    assert uncovered == [], (
        f"{TEMPLATE_GITIGNORE.name} names {uncovered}, which the scaffold walk would still ship "
        "into a materialized repo. The skip sets match unanchored BASENAMES, so add the bare "
        "name (never a leading '/' or '**/' — os.walk yields basenames, so such an entry would "
        "be dead) to services/scaffold_files.py: a trailing '/' means directory-only, so it "
        "needs _SKIP_DIR_NAMES (or _SKIP_DIR_SUFFIXES for a '*<suffix>' form); otherwise "
        "_SKIP_FILE_NAMES / _SKIP_SUFFIXES also satisfy it. A richer glob or a path-scoped "
        "entry cannot be expressed by these four sets at all and needs a new mechanism."
    )


def test_gitignore_parity_parser_matches_walk_semantics():
    """The fence's own parsing, pinned — it must not cry wolf, and must not miss a real gap.

    Both halves matter: a false alarm teaches devs to add dead skip entries, and a false
    negative is the very class of gap (``uv.lock``) this fence exists to catch.
    """
    # Already honoured by the walk — the same rule written three ways. Must NOT be flagged.
    assert uncovered_gitignore_entries("build\n/build\n**/build/\n/dist/\n") == []
    assert uncovered_gitignore_entries("**/__pycache__/\n*.egg-info/\n*.pyc\n") == []

    # Directory-ONLY entries need a DIR skip; uv.lock is a FILE skip, so `uv.lock/` is a gap.
    assert uncovered_gitignore_entries("uv.lock/\n") == ["uv.lock/"]
    assert uncovered_gitignore_entries("uv.lock\n") == []

    # Genuinely uncovered lines are reported; the *<suffix> glob form is the only glob honoured.
    assert uncovered_gitignore_entries("coverage.xml\n") == ["coverage.xml"]
    assert uncovered_gitignore_entries("*.log\n") == ["*.log"]
    assert uncovered_gitignore_entries("build-*.tar\n") == ["build-*.tar"]
    assert uncovered_gitignore_entries("secrets/config.json\n") == ["secrets/config.json"]

    # Noise lines carry no rule.
    assert uncovered_gitignore_entries("# a comment\n\n  \n!keep.me\n/\n") == []
