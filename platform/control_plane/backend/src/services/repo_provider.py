"""The portable repository-provider seam.

A materialize is ``create_repo → commit_files → set_ci_vars``: one tree write, so no second writer
is left to race the provider's own asynchronous work (six writes to one new repository produced
four separate defects). The seam is defined by what a provider must be able to do, not by what one
REST surface offers: every method is a portable noun or verb implementable on GitHub, GitLab,
Bitbucket and Azure DevOps with no premium tier and no provider concept in the signature, and a
new method needs that argument written down first. ``list_repos`` takes no filter because the one
unportable thing about its predecessor was an ``is_template`` flag; what is a template is a human's
statement, made by adopting a repository.

Adapters pick their own mechanics; what every adapter owes the caller is identical: a required
``ref`` on the reads (a mid-read push cannot yield a mixed tree), root-relative paths, ``bytes``
values, and a raise rather than a shortened answer whenever the provider reports a truncated reply
or a tree entry that is not a plain file. ``ensure_pipeline`` returning ``None`` is success: three
of four providers register a pipeline from the committed YAML on their own, and a failure is always
an exception. Every method takes ``token`` and none can obtain one, which keeps an adapter unable
to widen its own authority; a token never reaches a log line or an exception message.

A ``Protocol`` rather than an ABC: it is satisfied structurally, so the existing GitHub client was
conformed method by method, and ``test_repo_provider`` compares the signatures parameter by
parameter because a Protocol is not otherwise enforced at runtime.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


class RepoConflictError(Exception):
    """The branch moved since the caller read it: nothing was written; re-read and retry.
    Adapters raise a subclass of their own error type and of this, so a caller can catch the
    conflict without knowing the provider."""


@dataclass(frozen=True)
class RepoView:
    """One repository as a provider reports it: the reconcile probe's answer.

    A projection, so a provider body cannot reach a caller through it. Existence is not a field:
    it is the difference between a ``RepoView`` and the ``None`` :meth:`RepoProvider.read_repo`
    returns for not-found. ``head_sha`` is the default branch's tip, the value a caller passes back
    as ``read_tree``'s ``ref``; it is empty for a repository that exists but has no commit yet.
    Frozen because a mutated probe result is a record that disagrees with what was observed.
    """

    default_branch: str
    head_sha: str


@runtime_checkable
class RepoProvider(Protocol):
    """What AGP needs from a repository provider; the methods below are the whole contract.

    Structural: an implementation neither inherits from nor registers with this. The signatures
    are compared method by method against the GitHub adapter by ``test_repo_provider``, because a
    silently drifted parameter name would ship a "conforming" adapter no caller can call.
    """

    def create_repo(
        self, org: str, name: str, *, private: bool, token: str, base_url: str | None = None
    ) -> str:
        """Make ``org/name`` exist and return the repository's canonical URL. Idempotent: an
        already-existing repository is a benign re-run (a retried materialize must converge), so
        the URL is read back rather than raised over."""
        ...

    def commit_files(
        self,
        org: str,
        repo: str,
        files: dict[str, bytes],
        *,
        branch: str,
        message: str,
        token: str,
        base_url: str | None = None,
    ) -> str:
        """Put ``files`` on ``branch`` in exactly one commit; return the commit sha.

        One commit whatever the provider's API costs internally, so one ``[skip ci]`` in
        ``message`` suppresses the whole materialize build. The resulting tree is exactly
        ``files``: nothing pre-existing is carried forward and no layout is inspected. Idempotent
        by content: identical files produce the tree already on the branch, so no commit is
        written and the ref does not move.
        """
        ...

    def commit_files_on_head(
        self,
        org: str,
        repo: str,
        files: dict[str, bytes],
        *,
        branch: str,
        expected_head_sha: str,
        message: str,
        token: str,
        base_url: str | None = None,
    ) -> str:
        """Overlay ``files`` onto ``branch``'s tip in one commit; return the commit sha.

        The edit-a-live-repo counterpart of :meth:`commit_files`: every other path and its mode is
        carried forward, the commit's parent is ``expected_head_sha`` and the branch only moves as
        a fast-forward. If the tip is no longer ``expected_head_sha``, nothing is written and
        :class:`RepoConflictError` is raised. Identical content is a no-op returning
        ``expected_head_sha``.
        """
        ...

    def create_branch(
        self,
        org: str,
        repo: str,
        *,
        branch: str,
        from_branch: str | None,
        token: str,
        base_url: str | None = None,
    ) -> str:
        """Create ``branch`` at ``from_branch``'s tip and return that tip sha; ``from_branch=None``
        is the repository's default branch, read from the provider. Idempotent when ``branch``
        already points at the source tip; at any other sha it raises :class:`RepoConflictError`
        rather than move a branch that holds commits this call did not write."""
        ...

    def read_file(
        self, org: str, repo: str, path: str, *, ref: str, token: str, base_url: str | None = None
    ) -> Optional[bytes]:
        """One file's bytes at ``ref`` (a sha), or ``None`` when the path does not exist there.
        Any other failure raises, as :meth:`read_tree` does."""
        ...

    def set_ci_vars(
        self,
        org: str,
        repo: str,
        variables: Mapping[str, str | None],
        *,
        scope: str | None,
        token: str,
        base_url: str | None = None,
    ) -> None:
        """Set the CI variables a build reads on ``org/repo``.

        ``scope`` is the provider's own grouping name (a GitHub Actions environment, a GitLab
        environment scope) or ``None`` for the repository-wide set; AGP names no stage here. A
        named scope must already exist: creating it would be a second write to a fresh
        repository, so the materialize path passes ``scope=None`` only. Idempotent: an existing
        variable is updated. An empty value is skipped, so an unset optional variable is never
        written as an empty string a build cannot tell from a deliberate blank.
        """
        ...

    def ensure_pipeline(
        self, org: str, repo: str, *, yaml_path: str, token: str, base_url: str | None = None
    ) -> str | None:
        """Make the CI definition at ``yaml_path`` run. Returns a pipeline id where the provider
        has one (Azure DevOps) and ``None`` where the provider registers it itself; ``None`` is
        success, and a failure raises."""
        ...

    def read_tree(
        self, org: str, repo: str, *, ref: str, token: str, base_url: str | None = None
    ) -> dict[str, bytes]:
        """The whole tree at ``ref`` as ``{path: content}``: the inverse of :meth:`commit_files`.

        ``ref`` is required and callers pass a head sha: resolve the tip once with
        :meth:`read_repo` and read at it, so a push landing mid-read cannot produce a tree that
        was never a commit; an adapter refuses a blank ``ref`` rather than substitute one. Values
        are ``bytes`` and paths are root-relative with no archive prefix; nothing is filtered by
        meaning. Raises, never returns a shortened answer, when the provider reports a truncated
        listing or the tree holds a symlink or a submodule entry.
        """
        ...

    def read_repo(
        self, org: str, repo: str, *, token: str, base_url: str | None = None
    ) -> Optional[RepoView]:
        """Does ``org/repo`` exist, and what is its default branch's tip?

        ``None`` means not-found and nothing else; an expired token, a 403, a 500 or a transport
        failure raises, because folding an outage into ``None`` would tell a customer their
        templates are gone and offer to overwrite them with starter bytes. A repository that exists
        with no commit yet answers a ``RepoView`` with an empty ``head_sha``.
        """
        ...

    def list_repos(self, org: str, *, token: str, base_url: str | None = None) -> list[str]:
        """The names of ``org``'s repositories, paginated by the adapter; no filter parameter. A
        page fetch that fails or a listing past the adapter's own cap raises rather than returning
        a silently truncated list."""
        ...
