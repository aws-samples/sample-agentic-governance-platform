"""The portable repository-provider seam (E28B/T1 — D-B1).

WHY THIS EXISTS
---------------
The pre-E28B materialize path issued **six writes** to one brand-new repository —
generate-from-template, cut a branch, commit a config file, set repo vars, create two
environments, set env vars — and four separate defects came from those writes racing
GitHub's own asynchronous template copy (a clobbered ``agent.config.json``, a reset
``default_branch``, permanently divergent ``dev``/``main`` trees, a prod candidate
registered on a repo nobody had merged to). None of those were ordering mistakes that a
sixth reordering would have fixed: they were the consequence of having **more than one
writer**.

So the seam is defined by what a repository provider must be *able* to do, not by what
GitHub's REST surface happens to offer:

``create_repo``      make an empty repository exist.
``commit_files``     put N files on a branch in **exactly ONE commit**.
``set_ci_vars``      give the provider's CI the values a build needs.
``ensure_pipeline``  make the committed CI definition actually run.
``read_pr``          read the state of one pull request.

Materialize then becomes ``create_repo → commit_files → set_ci_vars`` — one tree write,
so there is no second writer left to race.

E28C/T1 — THREE READS, AND WHY THE NUMBER MOVED FROM FIVE TO EIGHT (D-C1)
-------------------------------------------------------------------------
E28B's five were all WRITES plus one PR read, which is a seam that can create a repository but
cannot look at one. That left AGP's registry and the org's actual repositories unable to be
compared, and left materialize shipping bytes from the control plane's own DISK — so a customer
who improved their template repo watched AGP ignore it. E28C inverts that: the template
REPOSITORY is the source of truth, and reading it needs three verbs:

``read_tree``        the bytes at one ref, as ``dict[path, bytes]``.
``read_repo``        does this repository exist, and what is its default branch's tip.
``list_repos``       the names of an org's repositories.

**The count is not a budget.** Five was locked with the warning that a sixth method is how a
provider-specific verb creeps back in, and that warning stands. The bar these three cleared is
that each is a portable NOUN-READ — implementable on GitHub, GitLab, Bitbucket and Azure DevOps
with no premium tier and no provider concept in the signature — and that two of them are exact
inverses of verbs already here (``read_tree`` of ``commit_files``, sharing its
``dict[path, bytes]``; ``read_repo`` of ``create_repo``). ``list_repos`` deliberately takes NO
filter: the unportable part of the ``list_template_repos`` E28B deleted was its ``is_template``
flag, a GitHub-only concept, and human judgment replaces it (D-C4). A ninth method needs this
same argument written down before it is added.

**Adapters pick their own mechanics, and the pins are what is portable.** GitHub can walk the
git-data API or pull a tarball; GitLab and Azure DevOps have an archive endpoint; Bitbucket walks
per file. What every adapter owes the caller is identical: a required ``ref`` (so a mid-read push
cannot yield a mixed tree), root-relative paths, ``bytes`` values, and — the E28B failure class
restated — a RAISE rather than a shortened answer whenever the provider says its reply was
truncated or the tree holds something that is not a plain file. A partial tree that looks
complete is how a customer's repository gets materialized with half a template.

WHY A ``Protocol`` AND NOT AN ABC (D-B1)
----------------------------------------
:class:`services.github_repo_service.GitHubRepoService` was a 981-line client with 40+ call
sites. A base class would have forced every one of them through a hierarchy change in the same
commit that introduced the seam. A Protocol is satisfied *structurally*, so the existing
service was conformed method by method and the callers moved in later tasks. (E28B/T7 then
deleted the GitHub-shaped predecessors those callers left behind, which is why the module is
now well under half that size — the Protocol did not have to change for that either.)

E28B ships exactly ONE adapter (GitHub). A seam with a single implementation is an
assumption rather than a proof — accepted deliberately: speculative GitLab/Bitbucket/Azure
DevOps adapters with no account to test against are worse than an adapter written later
against a real customer's provider.

THE ASYMMETRY IS DELIBERATE — ``ensure_pipeline`` RETURNING ``None`` IS SUCCESS
------------------------------------------------------------------------------
GitHub, GitLab and Bitbucket all register a pipeline from a committed YAML file on their
own; Azure DevOps does not — it needs the platform to create a pipeline object, keep its
id, and delete it when the repository goes. ``ensure_pipeline`` therefore returns
``str | None``: a pipeline id where the provider has one, ``None`` where the provider
needed nothing done.

**``None`` is the SUCCESS answer, not a failure.** A caller that treats a falsy return as
an error breaks GitHub, GitLab and Bitbucket while looking correct against Azure DevOps.
A failure is always an *exception*, never a ``None``.

TOKENS ARE PARAMETERS
---------------------
Every method takes ``token`` and none of them can obtain one. Credentials stay with the
caller that resolved them, which is what keeps a provider adapter unable to widen its own
authority. Implementations must never log a token nor fold one into an exception message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from models.repository import PullRequestView


@dataclass(frozen=True)
class RepoView:
    """One repository as a provider reports it — the reconcile probe's answer (E28C/T1, D-C1).

    A PROJECTION, following :class:`~models.repository.PullRequestView`'s idiom: only the two
    facts AGP acts on cross, so a provider body cannot reach a caller through this. Existence
    itself is NOT a field — it is the difference between a ``RepoView`` and the ``None`` that
    :meth:`RepoProvider.read_repo` returns for not-found, because a ``exists=False`` view would
    invite a caller to read the other fields of a repository that is not there.

    A stdlib frozen dataclass rather than a ``BaseModel``: this never crosses the HTTP boundary as
    a response body (the reconcile surface's wire shape is its own model), and the seam module
    deliberately holds no dependency it does not need. Frozen because this is evidence about a
    provider at a moment in time — a mutated probe result is a record that disagrees with what
    was actually observed.

    ``head_sha`` is the default branch's TIP, and it is the value a caller passes back as
    ``read_tree``'s ``ref``: resolve once, read at that sha, and a template author's push landing
    mid-materialize cannot produce a tree that was never a commit. It is EMPTY for a repository
    that exists but has no commit yet (a freshly created, never-pushed repo) — present with
    nothing to read, which reconcile must be able to see as distinct from absent.
    """

    default_branch: str
    head_sha: str


@runtime_checkable
class RepoProvider(Protocol):
    """What AGP needs from a repository provider. EIGHT methods, no more (D-B1, amended by D-C1).

    Five WRITES/PR-read (D-B1) plus three portable noun-READS (D-C1, E28C/T1) — see the module
    docstring for the bar those three had to clear. **A NINTH IS STILL THE FAILURE MODE THE
    ORIGINAL WARNING NAMED**: it is how a provider-specific verb (``create_environment``, a
    GitHub-only concept) creeps back into a portable seam. The count moved because all three
    additions are implementable on GitHub, GitLab, Bitbucket and Azure DevOps with no premium
    tier and no provider concept in the signature, and that argument is recorded in D-C1 — not
    because eight is a budget with room in it. Anything further needs the same argument written
    down first.

    Structural: an implementation neither inherits from nor registers with this. The
    signatures below are the contract — :func:`test_repo_provider` compares them, method
    by method, against :class:`~services.github_repo_service.GitHubRepoService`, because a
    Protocol is not otherwise enforced at runtime and a silently drifted parameter name is
    exactly the change that would ship a "conforming" adapter that no caller can call.
    """

    def create_repo(self, org: str, name: str, *, private: bool, token: str,
                    base_url: str | None = None) -> str:
        """Make ``org/name`` exist and return the repository's canonical URL.

        Idempotent: an already-existing repository is a benign re-run (a retried
        materialize must converge), so the URL is read back rather than raised over.
        """
        ...

    def commit_files(self, org: str, repo: str, files: dict[str, bytes], *, branch: str,
                     message: str, token: str, base_url: str | None = None) -> str:
        """Put ``files`` on ``branch`` in **exactly ONE commit**; return the commit sha.

        ONE commit, whatever the provider's API costs internally — on GitHub that is
        blobs→tree→commit→ref, four calls the caller must never see. One commit means one
        CI trigger, so one ``[skip ci]`` in ``message`` suppresses the whole materialize
        build on every provider, with no branch filters to keep correct.

        The resulting tree is EXACTLY ``files`` — nothing pre-existing is carried forward
        and no layout is inspected, so a template author may move, add or restructure any
        path and it flows through untouched.

        **Idempotent by content.** Re-running with identical files is a no-op: the tree
        that content produces is the tree already on the branch, so no commit is written
        and the ref does not move. That is what makes a retried materialize safe rather
        than merely tolerable.
        """
        ...

    def set_ci_vars(self, org: str, repo: str, variables: dict[str, str], *, scope: str | None,
                    token: str, base_url: str | None = None) -> None:
        """Set the CI variables a build reads on ``org/repo``.

        ``scope`` is the provider's own grouping name (a GitHub Actions *environment*, a
        GitLab *environment scope*) or ``None`` for the repository-wide set. It is a
        PROVIDER currency, not a stage: AGP names no stages here, so a tenant with three
        stages needs no change to this method.

        **A named ``scope`` MUST ALREADY EXIST — this method does not create one.** GitHub
        answers 404 for a variable written under an absent environment, and an adapter must
        let that fail rather than provision the scope: creating it would be a SECOND write to
        a fresh repository, which is the property E28B exists to remove. E28B's materialize
        path therefore passes ``scope=None`` only.

        Idempotent: an existing variable is updated rather than rejected. An empty value is
        SKIPPED, so an unset optional variable is never written as an empty string — a
        build reading one cannot tell "" from "deliberately blank".
        """
        ...

    def ensure_pipeline(self, org: str, repo: str, *, yaml_path: str, token: str,
                        base_url: str | None = None) -> str | None:
        """Make the CI definition at ``yaml_path`` actually run. Returns a pipeline id, or
        ``None`` where the provider auto-registers it.

        **``None`` IS SUCCESS.** See the module docstring: three of the four providers need
        nothing done here, so ``None`` is the ordinary answer, and a caller that reads a
        falsy return as an error breaks all three. Failure raises.
        """
        ...

    def read_pr(self, org: str, repo: str, number: int, *, token: str,
                base_url: str | None = None) -> PullRequestView:
        """Read pull request ``number`` as the provider reports it, projected onto
        :class:`~models.repository.PullRequestView`.

        A PROJECTION, never a pass-through: only the fields AGP renders cross, so a
        provider body cannot reach a client through this.
        """
        ...

    # ------------------------------------------------------------------ #
    # E28C/T1 — the three portable NOUN-READS (D-C1). See the module docstring for why
    # the seam grew from five methods to eight and what bar these had to clear.
    # ------------------------------------------------------------------ #

    def read_tree(self, org: str, repo: str, *, ref: str, token: str,
                  base_url: str | None = None) -> "dict[str, bytes]":
        """Read the whole tree at ``ref`` as ``{path: content}``. The INVERSE of
        :meth:`commit_files`, in the same shape.

        ``ref`` IS REQUIRED, AND CALLERS PASS A HEAD SHA. Not a branch name and not a default:
        a caller resolves the tip once (:meth:`read_repo`) and reads AT that sha, so a template
        author pushing while a materialize is in flight cannot contribute half of an old tree
        and half of a new one — a tree that never existed as a commit. An adapter must refuse a
        blank ``ref`` rather than substitute one; silently reading "whatever HEAD is now" is the
        mixed-tree defect with the guard removed.

        **AGP MOVES BYTES AND NEVER PARSES THEM** (tenet 5). Values are ``bytes``, never ``str``
        — decoding would corrupt anything not UTF-8 and would presume a text file. Paths are
        ROOT-RELATIVE with NO leading slash and no archive-style top-level prefix, because they
        go straight back into :meth:`commit_files` and a prefix would silently nest a whole
        template one directory deep. NOTHING is filtered by meaning: no path is skipped for
        being a workflow, a lockfile or a dotfile. A git tree already holds only tracked files,
        so there is no build detritus to exclude and no judgment for this method to make.

        **RAISES, never returns a shortened answer** (the provider's own error convention —
        ``GitHubRepoError`` on the GitHub adapter, matching the five methods above):

        * the provider reports its listing was TRUNCATED or the archive was partial — a silent
          partial is exactly E28B's failure class, and half a template committed as if whole is
          worse than a failed step;
        * the tree holds a SYMLINK or a SUBMODULE entry. Neither is a blob AGP can carry: a
          symlink's content is a path, so writing it back as a regular file would replace a link
          with the text of its target, and a submodule is a pointer into a repository this seam
          cannot read at all. Raising says so; skipping would materialize a template that is
          quietly missing part of itself. A template author who needs either must be told, not
          guessed at.
        """
        ...

    def read_repo(self, org: str, repo: str, *, token: str,
                  base_url: str | None = None) -> Optional[RepoView]:
        """Does ``org/repo`` exist, and what is its default branch's tip? The reconcile probe.

        ``None`` MEANS NOT-FOUND, AND NOTHING ELSE. Every other failure — an expired token, a
        403, a 500, a DNS failure — RAISES. The distinction is the entire basis of the
        three-state reconcile (D-C3): ``None`` is what makes a registry row read
        ``registered_missing`` and offers an operator "re-create from seed". Folding an outage
        into ``None`` would tell a customer their templates are gone and invite them to
        overwrite iterated repositories with starter bytes. "AGP could not look" is not
        evidence of absence.

        A repository that exists but carries no commit yet answers a ``RepoView`` with an EMPTY
        ``head_sha`` — present with nothing to read. Not ``None`` (it exists, and a reconcile
        that called it missing would offer to create what is already there) and not a raise
        (nothing failed).

        Projection, like :meth:`read_pr`: the provider's repository body does not cross.
        """
        ...

    def list_repos(self, org: str, *, token: str,
                   base_url: str | None = None) -> "list[str]":
        """The NAMES of ``org``'s repositories. Pagination is the adapter's business.

        Names only — not descriptions, not URLs, not timestamps. The caller subtracts what AGP
        already accounts for (registered templates, materialized agent repos, the infra repo) and
        offers the remainder for adoption, and a name is all that needs.

        **NO FILTER PARAMETER, deliberately.** This is not the ``list_template_repos`` E28B
        deleted returning under a portable name: the unportable part of that method was its
        ``is_template`` filter — a GitHub-only flag with no equivalent on GitLab, Bitbucket or
        Azure DevOps — and re-admitting it as an argument would put a provider concept back in
        the seam's signature. What is a template is a HUMAN's statement, made by adopting a repo
        (D-C4), not a bit AGP reads off a provider.

        A provider that cannot serve the full list — a page fetch that fails, or a listing so
        long the adapter's own cap is reached — RAISES. A silently truncated list would make
        repositories look adoptable that already are, or hide ones an operator was looking for,
        which is the same silent-partial failure :meth:`read_tree` refuses.
        """
        ...
