"""``GitHubRepoError`` and ``GitHubRepoConflictError``, raised by ``services.github_repo_service`` and
``services.github_repo_paths``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from core.errors import AppError, Outcome
from services.repo_provider import RepoConflictError


class GitHubRepoError(AppError):
    """A GitHub read or write failed. The message is for the log and is safe (GitHub's validation
    text at most, never the token or a raw body); the client sees the fixed detail."""

    domain: ClassVar[str] = "github_repo"
    kinds: ClassVar[Mapping[str, Outcome]] = {"provider_error": Outcome(502, "Template rollout failed")}


class GitHubRepoConflictError(GitHubRepoError, RepoConflictError):
    """``commit_files_on_head`` or ``create_branch``: the branch is not at the expected head;
    nothing written."""
