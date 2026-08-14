"""The org RECONCILE service (E22/T5 → rebuilt in E28C/T3, design D-C3).

WHAT THIS SERVICE IS FOR NOW
----------------------------
It is the ONE place AGP's template registry and the connected org's actual repositories are
compared. Every row it returns carries the STATE of that comparison, and each state has exactly
one honest action behind it:

===========================  ===============================  ============================
state                        meaning                          the operator's one action
===========================  ===============================  ============================
``registered_present``       in sync                          nothing / re-push the seed
``registered_missing``       the repo behind the record is gone  re-create from seed / deregister
``unregistered_present``     a repo AGP does not know about   **adopt**
``seed_absent``              a seed with nothing in the org   create
===========================  ===============================  ============================

WHY THE BOOLEAN HAD TO GO (and why this is not a rename)
--------------------------------------------------------
E28B/T2 answered "is this template already there?" from AGP's own CATALOG (``name in existing``).
A DDB row is evidence about AGP's store, never about the org, so the answer was wrong in both
directions and each direction cost something real:

* a REGISTERED template whose repo had been deleted read as *present*, so the surface offered
  nothing and the operator's only route back was a hand delete of the record;
* a repo that already carried a seed's name read as *absent*, so rollout POSTed
  ``/orgs/{org}/repos``, GitHub 422'd, and the route flattened that into a 502 "Template rollout
  failed" inside "connecting failed" — which is how the 2026-08-04 live test ended in a manual
  repo delete.

So ``RolloutItem.exists_in_org`` is DELETED rather than aliased, and ``read_repo`` (E28C/T1) —
whose ``None`` means not-found AND NOTHING ELSE — is the probe. ``kind`` and ``selectable`` are
gone too: the template/infra distinction is now STRUCTURAL (:class:`ReconcileView` has two
fields), so "forced, never a choice" cannot be turned into a choice by flipping a flag.

**AND THE THREE-STATE TRUTH BINDS THE VERBS, NOT JUST THE VIEW.** The first cut of this rebuild
got ``reconcile`` right and left both write verbs deciding from the registry alone, which the
E28C/T3 review caught by execution: a rollout REPLACED an unregistered pre-existing repo's whole
tree (reported as ``"created"``), and ``adopt`` accepted the infra repo and live agent repos that
the picker hides. A state the surface computes but the verbs cannot see is decoration. So
``_rollout_template`` dispatches on :func:`_state_for` (refusing ``unregistered_present``
outright) and :meth:`adopt` enforces the picker's whole subtraction server-side.

DESTRUCTIVE OVERWRITE IS DELETED, NOT IMPROVED
----------------------------------------------
The old "override" path called ``delete_repo`` and then ``create_repo_from_zip`` — it destroyed a
customer's repository, its history and its issues to push starter bytes, and it only existed
because ``POST /orgs/{org}/repos`` 422s on an existing name. Since materialize no longer requires
disk == repo (D-C2), a re-push is now what it should always have been: ``create_repo`` (idempotent)
then ``commit_files`` of the seed bytes — ONE commit on top, history preserved, and idempotent by
content so a re-run that changes nothing writes no commit and fires no build. The vestigial
dict→zip→dict round trip went with it, which is why there is no ``ZipService`` collaborator any
more; the S3 infra archive is unpacked here instead.

THE COST MODEL IS RULED (D-C3): NO READ SURFACE PAYS FOR A PROVIDER CALL
------------------------------------------------------------------------
The rule is about which SURFACES cost provider calls, not about which methods make them. NO
list/page route gains one: the Templates page stays registry-only and instant, there is no polling
and no cache, and Bitbucket's 1,000 req/h — the binding budget — is never approached on a page load.

:meth:`reconcile` is the only READ that talks to a provider: one paginated ``list_repos``, one
registry read, and a ``read_repo`` per SEED-or-REGISTERED row (org-origin rows are already known
present from the listing, so they are not probed again).

The WRITE verbs probe too, and they must: :meth:`adopt` reads the repo it is about to register, and
``_rollout_template``/``_ensure_infra``/``_push`` each read state before deciding what to do with
it. That is not a violation of the cost model — those are POSTs an operator explicitly clicked, and
the E28C/T3 review showed what the alternative costs: a verb that decided from the registry alone
replaced an unregistered repository's whole tree.

Collaborators mirror ``project_service``: a ``RepoProvider`` (the E28C/T1 seam — two writes, three
reads), a ``ConnectionService`` (resolves org / base_url / bearer token), an S3 client for the
staged runtime module, a ``TemplateRegistry`` (the catalog), and a ``known_repo_names`` callable
that names the materialized AGENT repos AGP already accounts for. That last one is REQUIRED for
:meth:`reconcile`: without it AGP cannot subtract its own agent repos, and offering one for
adoption would invite an operator to register a live agent repo as a template.

SECURITY: the connection credential is read via ``ConnectionService.get_bearer_token`` (a stored
PAT or a freshly minted GitHub App installation token) and flows ONLY into the provider seam — it
is never logged, never returned, never put on a read model, and never reaches the registry.
``RolloutError`` carries a SAFE message + a ``.kind`` the route maps to a fixed HTTP status
(never ``str(exc)``).
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from services.github_repo_service import GitHubRepoError

# The ONE authority on what a legal template/repo name is — imported, never re-typed, so the
# rollout path cannot drift from the catalog's answer (requirement 4; the same aliasing idiom
# ``project_service`` uses).
from services.github_template_service import _NAME_RE as _TEMPLATE_NAME_RE
from services.github_template_service import TemplateView
from services.scaffold_files import collect_scaffold_files
from services.template_registry import (
    TemplateRecord,
    TemplateRegistryError,
    TemplateRegistryValidationError,
    template_id_for,
)

logger = logging.getLogger(__name__)

# The FORCED per-org runtime-infra repo. Always ensured on rollout; never selectable (it is its own
# field on ``ReconcileView``, not a flagged row), never registered, and never adoptable — that last
# one is enforced by a GUARD in ``adopt`` rather than by the picker's omission, because the review
# showed a hand-made POST could register it and make it appear as both a template row and infra_repo.
INFRA_REPO_NAME = "agp-runtime-infra"

# Each seed dir may carry this file: the catalog metadata a rolled-out card renders (design 4e).
# Read from DISK, which is allowed precisely because it IS the seed — not repo content.
SEED_METADATA_FILE = "template.json"

# The push message. ``[skip ci]`` is load-bearing: ONE commit means one CI trigger, so one marker
# suppresses the whole seed push on every provider with no branch filters to keep correct.
_SEED_PUSH_MESSAGE = "chore: seed template contents from AGP [skip ci]"
_INFRA_PUSH_MESSAGE = "chore: seed runtime-infra module from AGP [skip ci]"

# The branch a push targets when the repo does not exist yet. ``create_repo``'s ``auto_init``
# names the seeded branch from the ORG's default-branch setting, so this is only ever the
# fallback for a repo whose default branch could not be read — never an override of one.
_FALLBACK_BRANCH = "main"


class RolloutError(Exception):
    """A rollout/reconcile/adopt operation failed. Carries a SAFE message + a ``.kind`` hint the
    route maps to a fixed HTTP status/detail — never ``str(exc)`` (which could carry a store or
    provider message).

    The kinds, and why each is its own:

    * ``not_found`` (404) — no such SEED on disk. The caller named a template AGP does not ship.
    * ``repo_not_found`` (404) — no such REPO in the org (adopt). A different fact from the above,
      so it gets a different detail literal: telling an operator "Unknown base template" when
      their repo name was simply mistyped points them at the wrong thing.
    * ``conflict`` (409) — already registered (adopt). Added in E28C/T3: there was no 409 kind, so
      "this is already a template" had nowhere honest to land.
    * ``validation`` (422) — malformed caller input. PERMANENT, so it must not share the retryable
      502 a genuine fault gets (the reconcile route used to flatten it there).
    * ``rollout_error`` (502) — a genuine store/provider fault. The only retryable kind.
    """

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


@dataclass
class ReconcileItem:
    """One row of a :class:`ReconcileView` — the registry-vs-org comparison for ONE name.

    ``origin`` says WHY this row exists: ``"seed"`` (AGP ships a scaffold under this name),
    ``"registry"`` (AGP has a record but no seed — an uploaded or adopted template) or ``"org"``
    (a repository AGP found and does not account for). Seed wins over registry when both apply,
    because seed-ness is the CAPABILITY signal: only a seed row can offer "re-push" or "re-create
    from seed".

    ``state`` is the comparison (see the module docstring). ``default_branch``/``head_sha`` are
    what ``read_repo`` found, and both are ``None`` when there was nothing to find — an absent
    repo, an org-origin row (known present from the listing, deliberately not re-probed), or a
    repository that exists with NO COMMIT yet. That last case is why ``head_sha`` is
    ``Optional[str]`` and not ``str``: the seam reports it as ``""``, and passing an empty string
    on as a ``read_tree`` ref would be a request to read "whatever HEAD is now" — the mixed-tree
    defect T1 exists to refuse. ``None`` says "present, nothing to read" instead.

    There is deliberately no ``exists_in_org`` and no ``selectable``. See the module docstring.
    """

    name: str
    origin: str   # "seed" | "org" | "registry"
    state: str    # "registered_present" | "registered_missing" | "unregistered_present" | "seed_absent"
    default_branch: Optional[str] = None
    head_sha: Optional[str] = None


@dataclass
class ReconcileView:
    """The reconcile surface's whole answer.

    ``infra_repo`` is its OWN field rather than a flagged row in ``templates``: that is how
    "FORCED — always ensured, never a choice" is expressed now that ``selectable`` is gone. A
    structural separation cannot be flipped, so the infra repo is never OFFERED as an adopt row and
    can never be left out of a rollout by deselecting it.

    That is a statement about this VIEW only. It does not stop a client POSTing the infra name to
    the adopt route, so :meth:`RolloutService.adopt` refuses it explicitly — the shape of a read
    model is not an authorization boundary (E28C/T3 review, F2).
    """

    templates: List[ReconcileItem]
    infra_repo: ReconcileItem


@dataclass
class RolloutResultItem:
    name: str
    # "created" | "overwritten" | "recreated" | "skipped" | "adopted".
    #
    # EVERY WORD IS DERIVED FROM THE OBSERVED STATE (:data:`_ACTION_FOR_STATE`), never from the
    # registry alone — the E28C/T3 review found "created" reported for a repository that already
    # existed, and that string is what would have made the silent tree replacement visible.
    #
    # "overwritten" KEEPS ITS NAME and no longer means destruction: it is a re-push, an idempotent
    # ``commit_files`` of the seed bytes onto a template AGP registered. "recreated" is the
    # ``registered_missing`` answer (D-C3's "re-create from seed") — distinct from "created"
    # because a record already existed, and from "overwritten" because there was no repo to write
    # over. "adopted" is the register-as-is; :meth:`adopt` is a single-repo verb with its own route
    # returning a ``TemplateView``, so no rollout BATCH emits it today — the vocabulary is pinned
    # here because this is the type that names it.
    action: str
    reason: Optional[str] = None


@dataclass
class RolloutResult:
    items: List[RolloutResultItem] = field(default_factory=list)


class RolloutService:
    def __init__(
        self,
        repo_provider,
        connection_service,
        *,
        agent_templates_dir: str,
        s3_client,
        runtime_module_bucket: str,
        runtime_module_key: str,
        template_registry,
        known_repo_names=None,
        now=lambda: datetime.now(timezone.utc).isoformat(),
    ) -> None:
        # The E28C/T1 seam: ``create_repo``/``commit_files`` for the seed push,
        # ``read_repo``/``list_repos`` for the comparison. NOT a GitHub-shaped client any more —
        # ``delete_repo`` and ``create_repo_from_zip`` are no longer called by anything here.
        self._provider = repo_provider
        self._conn = connection_service
        self._agent_templates_dir = Path(agent_templates_dir)
        # The catalog. A pushed scaffold becomes a template by being REGISTERED here — the
        # is_template flip it used to get is a GitHub-only flag (E28B/T2).
        self._registry = template_registry
        self._now = now
        # The agentcore_runtime module is staged in S3 (uploaded by terraform apply); the backend
        # image ships no infrastructure/ tree, so the infra rollout DOWNLOADS the module zip from
        # here rather than reading a local dir. Base templates stay local.
        self._s3 = s3_client
        self._runtime_module_bucket = runtime_module_bucket
        self._runtime_module_key = runtime_module_key
        # ``(connection_id) -> set[str]``: the names of the MATERIALIZED AGENT repos AGP already
        # accounts for. Required by :meth:`reconcile` (it fails closed without it) and unused by
        # every other verb — which is why it is optional in the signature: the E25B migration
        # script constructs this service to force the infra repo and never reconciles.
        self._known_repo_names = known_repo_names

    # ===================================================================== #
    # Public API
    # ===================================================================== #

    def reconcile(self, connection_id: str) -> ReconcileView:
        """Compare AGP's registry against the org's repositories; return one row per name.

        THE ROW UNIVERSE is seed names ∪ registered names ∪ (``list_repos(org)`` minus what AGP
        already accounts for). That last subtraction is the adopt picker's whole correctness: the
        registered templates (they have their own rows), this connection's materialized AGENT
        repos, and the forced infra repo are all things AGP knows about, and offering any of them
        for adoption would invite an operator to register a live agent repo as a template.

        Seed and registered rows are probed with ``read_repo``. Org rows are NOT re-probed — the
        listing already proved them present — which is what keeps this to one paginated call plus
        a bounded handful of probes (the ruled cost model, D-C3).

        FAILS CLOSED, everywhere. An unreadable catalog, a failed listing and a missing repo
        inventory all raise: "AGP could not look" is not evidence of absence, and each of those
        degradations would offer to CREATE over something that already exists or to ADOPT
        something AGP owns.
        """
        org, base_url, token = self._resolve(connection_id)
        registered = self._registered_names(connection_id)
        seeds = set(self._base_template_names())

        agent_repos = self._agent_repo_names(connection_id)
        org_repos = self._list_org_repos(org, token, base_url)
        adoptable = org_repos - seeds - registered - agent_repos - {INFRA_REPO_NAME}

        rows: List[ReconcileItem] = []
        for name in sorted(seeds | registered):
            view = self._probe(org, name, token, base_url)
            rows.append(
                ReconcileItem(
                    name=name,
                    origin="seed" if name in seeds else "registry",
                    state=_state_for(registered=name in registered, present=view is not None),
                    default_branch=view.default_branch if view else None,
                    head_sha=_head_or_none(view),
                )
            )
        for name in sorted(adoptable):
            # Present by construction (it came out of the listing) and unregistered by the
            # subtraction above — so the state is fixed, and no probe is spent proving it.
            rows.append(
                ReconcileItem(name=name, origin="org", state="unregistered_present")
            )

        infra_view = self._probe(org, INFRA_REPO_NAME, token, base_url)
        return ReconcileView(
            templates=rows,
            infra_repo=ReconcileItem(
                name=INFRA_REPO_NAME,
                # Its seed is the S3-staged runtime module rather than a dir on disk, but it is a
                # seed: this row's action is "create/re-push it from what AGP ships".
                origin="seed",
                # Never registered, so only the two unregistered states are reachable — and both
                # are literally true rather than a special case invented for this row.
                state=_state_for(registered=False, present=infra_view is not None),
                default_branch=infra_view.default_branch if infra_view else None,
                head_sha=_head_or_none(infra_view),
            ),
        )

    def rollout(
        self,
        connection_id: str,
        *,
        template_names: List[str],
        overwrite: bool,
        overwrite_infra: bool = False,
    ) -> RolloutResult:
        """Push the selected seeds into the org + ALWAYS ensure the forced infra repo.

        EVERY DECISION HERE IS MADE ON THE SAME THREE-STATE TRUTH :meth:`reconcile` COMPUTES —
        provider state, not the registry alone. That is the whole correction of the E28C/T3 review:
        the states reached ``reconcile`` but the verbs that act on them were still guarding on
        ``name in registered``, and a two-state guard cannot express the case that matters
        (present, but not ours). See :meth:`_rollout_template` for the per-state table.

        ``overwrite`` decides ONE thing: whether an already-registered, present TEMPLATE is
        re-pushed. It is not an escalation — no value of it authorizes writing over a repository
        AGP has no record of, and (E28D) it no longer reaches the infra repo at all.

        ``overwrite_infra`` is the forced infra repo's OWN consent, defaulted OFF: an existing
        ``agp-runtime-infra`` is left exactly as it is unless the caller asks for the module to be
        re-pushed. Creating it when ABSENT stays unconditional — a tenant runtime cannot deploy
        without it.
        """
        org, base_url, token = self._resolve(connection_id)
        registered = self._registered_names(connection_id)

        result = RolloutResult()
        for name in template_names:
            result.items.append(
                self._rollout_template(
                    name, connection_id, org, base_url, token, registered, overwrite
                )
            )
        # FORCED infra repo — always ensured, independent of template_names. Its re-push consent is
        # its OWN (``overwrite_infra``), never the templates' flag.
        result.items.append(self._ensure_infra(org, base_url, token, overwrite_infra))
        return result

    def adopt(
        self,
        connection_id: str,
        *,
        repo_name: str,
        description: Optional[str] = None,
        created_by: str = "",
    ) -> TemplateView:
        """REGISTER AS IS: declare an existing org repository to be one of this org's templates.

        A governance statement, not a content check. There is deliberately NO content inspection
        and NO push: materialize reads the template repo at use-time (D-C2), so inspecting it here
        would only prove something about a tree that may have moved on by then — and pushing would
        overwrite the very repository the operator is adopting BECAUSE they wrote it.

        The one provider call is ``read_repo``, and it is a precondition rather than an
        inspection: adopting a name that is not in the org would write a record pointing at
        nothing, and the first materialize would be where the operator found out.

        ``created_by`` is the validated caller's identity, passed by the route from its
        ``Principal`` — never taken from the body. ``description`` is optional; everything else on
        the record is editable afterwards via ``patch_template``.

        THE PICKER'S SUBTRACTION IS ENFORCED HERE, NOT ONLY IN THE PICKER (E28C/T3 review, F2).
        :meth:`reconcile` omits three classes of repository from the adopt rows, and omitting a row
        from a list is a UI courtesy — this endpoint is reachable with a hand-made POST, and both
        exclusions had real consequences when only the picker knew about them:

        * the FORCED infra repo — adopting it produced a registry row for a name that also occupies
          ``ReconcileView.infra_repo``, so the surface showed it TWICE, and a materialize from it
          would ship Terraform as an agent;
        * a MATERIALIZED AGENT repo — registering a live agent's repository as a template means
          every materialize from it ships that agent's code. That sentence is why the subtraction
          exists; it must be true of the verb, not just of the list.

        Both answer ``conflict`` with a message naming WHICH rule refused, because "already
        registered" would send an operator looking for a catalog entry that is not there.
        """
        self._require_valid_name(repo_name)
        org, base_url, token = self._resolve(connection_id)

        if repo_name == INFRA_REPO_NAME:
            raise RolloutError(
                f"'{repo_name}' is the platform's forced runtime-infra repo, not a template",
                kind="conflict",
            )
        if self._find(connection_id, repo_name) is not None:
            raise RolloutError(
                f"Template '{repo_name}' is already registered", kind="conflict"
            )
        if repo_name in self._agent_repo_names(connection_id):
            raise RolloutError(
                f"'{repo_name}' is a materialized agent repository, not a template",
                kind="conflict",
            )
        if self._probe(org, repo_name, token, base_url) is None:
            raise RolloutError(
                f"Repository '{repo_name}' is not in the org", kind="repo_not_found"
            )

        record = self._put(
            TemplateRecord(
                id=template_id_for(repo_name),
                name=repo_name,
                description=description or "",
                # Display-only, and NEVER parsed back (D-C1) — the structural pair below is what
                # makes this pointer dereferenceable.
                source_url=_display_url(base_url, org, repo_name),
                version="1",
                connection_id=connection_id,
                created_at=self._now(),
                created_by=created_by,
                # BOTH HALVES OR NEITHER (T2 note N-1). A half-set pair is type-permitted and
                # meaningless: ``read_repo``/``read_tree`` take ``(org, repo)`` positionally, so an
                # org without a repo names nothing. This writer knows both, so it writes both.
                source_org=org,
                source_repo=repo_name,
            ),
            name=repo_name,
        )
        return _view_from_record(record)

    # ===================================================================== #
    # Steps
    # ===================================================================== #

    def _rollout_template(
        self, name, connection_id, org, base_url, token, registered, overwrite
    ) -> RolloutResultItem:
        """Roll out ONE seed, dispatching on the THREE-STATE truth rather than on the registry.

        ============================  ===================  =========================
        state                         overwrite=False      overwrite=True
        ============================  ===================  =========================
        ``registered_present``        skipped (in sync)    ``overwritten`` (re-push)
        ``registered_missing``        ``recreated``        ``recreated``
        ``unregistered_present``      **skipped → adopt**  **skipped → adopt**
        ``seed_absent``               ``created``          ``created``
        ============================  ===================  =========================

        THE ``unregistered_present`` ROW IS WHY THIS METHOD PROBES (E28C/T3 review, F1 CRITICAL).
        The guard used to be ``name in registered``, which cannot see the one case that does
        damage: a repository that already carries a seed's name and which AGP has NO record of.
        ``commit_files`` builds its tree with no ``base_tree`` — the branch ends up carrying
        EXACTLY the pushed files — so walking past that guard REPLACED a stranger's whole tree and
        reported ``action="created"``. Verified: a repo holding ``their_work.py`` came back holding
        only the seed's files.

        Requirement 3 already named the correct outcome — such a repo "reconciles as
        ``unregistered_present`` (adopt row)" — and adopt is the verb for it: an explicit human
        statement about one specific repository. So this REFUSES, and no ``overwrite`` value
        overrides that. Overwrite means "yes, re-push the template I already registered"; it is not
        consent to write over a repository nobody registered, and treating it as such would make a
        checkbox on a batch call the authority for a destructive act.

        ``registered_missing`` re-creates from seed (D-C3's offer) and needs no flag: the repo is
        gone, so the create can neither collide nor destroy. It used to answer
        ``skipped, reason="already in the template catalog"`` — a catalog-shaped answer about a
        repository that did not exist.
        """
        # The name authority runs FIRST — before any disk read and before any provider write.
        # ``template_id_for``'s ``#`` check was the only guard on this path, which let a name the
        # catalog would refuse reach the filesystem (requirement 4).
        self._require_valid_name(name)

        # ONE probe, and the SAME state function reconcile uses — so the surface an operator read
        # and the verb they clicked cannot disagree about what is in the org.
        is_registered = name in registered
        present = self._probe(org, name, token, base_url) is not None
        state = _state_for(registered=is_registered, present=present)

        if state == "unregistered_present":
            return RolloutResultItem(
                name=name,
                action="skipped",
                reason=(
                    f"a repository named '{name}' already exists in the org and is not a "
                    f"registered template — adopt it instead of pushing over it"
                ),
            )
        if state == "registered_present" and not overwrite:
            return RolloutResultItem(
                name=name, action="skipped", reason="already in the template catalog"
            )

        scaffold_dir = self._agent_templates_dir / name
        if not scaffold_dir.is_dir():
            raise RolloutError(f"Unknown base template '{name}'", kind="not_found")

        files = collect_scaffold_files(scaffold_dir)
        if not files:
            raise RolloutError(
                f"Base template '{name}' has no files on disk", kind="rollout_error"
            )
        # Read the seed's declared metadata BEFORE the push: a malformed ``template.json`` is a
        # defect in what AGP ships, and discovering it after the commit would leave a pushed repo
        # with no catalog entry.
        metadata = self._seed_metadata(scaffold_dir, name)

        html_url = self._push(org, name, files, token, base_url, _SEED_PUSH_MESSAGE)

        # REGISTER the pushed scaffold — this is what makes it a template now that the
        # is_template flip is gone. It runs AFTER the push so a failed push never leaves a catalog
        # entry pointing at a repo that does not exist.
        self._register(connection_id, name, html_url, org, metadata)

        # The word is derived from the STATE, so it cannot lie about what happened (review F5:
        # "created" was reported for a repository that already existed, which is also the string
        # that would have made the clobber visible).
        #   registered_present → "overwritten"  a re-push on top; nothing destroyed
        #   registered_missing → "recreated"    the repo was gone and was rebuilt from seed
        #   seed_absent        → "created"      genuinely new
        return RolloutResultItem(name=name, action=_ACTION_FOR_STATE[state])

    def _ensure_infra(self, org, base_url, token, overwrite_infra) -> RolloutResultItem:
        exists = self._probe(org, INFRA_REPO_NAME, token, base_url) is not None
        if exists and not overwrite_infra:
            return RolloutResultItem(
                name=INFRA_REPO_NAME, action="skipped", reason="already exists in org"
            )
        # Fetch AND unpack the module archive BEFORE touching the provider — a missing, failed or
        # corrupt object must NOT leave a half-created infra repo with no Terraform in it.
        files = self._fetch_runtime_module_files()
        self._push(org, INFRA_REPO_NAME, files, token, base_url, _INFRA_PUSH_MESSAGE)
        return RolloutResultItem(
            name=INFRA_REPO_NAME, action="overwritten" if exists else "created"
        )

    def _push(self, org, name, files, token, base_url, message) -> str:
        """Make ``org/name`` exist and put ``files`` on its default branch in ONE commit.

        This replaced delete+recreate (see the module docstring). Both calls are idempotent —
        ``create_repo`` reads back an existing repo, and ``commit_files`` is idempotent BY
        CONTENT, so a re-push of unchanged bytes writes no commit, moves no ref and fires no
        build.

        The branch is the repo's OWN default branch, READ rather than assumed: ``create_repo``'s
        ``auto_init`` names the seeded branch from the ORG's default-branch setting, and pushing
        to a guessed ``main`` on an org that defaults to something else would leave the template
        on one branch and ``default_branch`` on another — a repo that looks seeded and never
        builds.
        """
        try:
            html_url = self._provider.create_repo(
                org, name, private=True, token=token, base_url=base_url
            )
            view = self._provider.read_repo(org, name, token=token, base_url=base_url)
            branch = (view.default_branch if view else None) or _FALLBACK_BRANCH
            self._provider.commit_files(
                org,
                name,
                files,
                branch=branch,
                message=message,
                token=token,
                base_url=base_url,
            )
        except GitHubRepoError as err:
            logger.exception("[reconcile] seed push failed for %s/%s", org, name)
            raise RolloutError(
                f"Failed to roll out '{name}'", kind="rollout_error"
            ) from err
        return html_url

    def _register(
        self,
        connection_id: str,
        name: str,
        source_url: str,
        org: str,
        metadata: Dict,
    ) -> None:
        """Write the catalog record for a pushed seed. An UPSERT, so a re-push re-registers
        instead of duplicating.

        WHAT E28C/T3 CHANGED HERE. It used to write a blank ``description``, empty
        ``aws_services``/``tags`` and ``version="1"`` UNCONDITIONALLY — so a rolled-out card
        rendered as a metadata-less stub next to a fully described uploaded one, and a re-push
        silently RESET a versioned catalog entry to version 1. Now the seed's own
        ``template.json`` supplies the metadata (design 4e) and the version comes from what the
        seed declares, falling back to the recorded version rather than to a stamp.

        ``created_at``/``created_by`` belong to the FIRST registration and are carried through a
        re-push — the same rule ``upload_template`` follows, so the two write paths cannot
        disagree about who registered a template.
        """
        previous = self._find(connection_id, name)
        self._put(
            TemplateRecord(
                id=template_id_for(name),
                name=name,
                description=metadata.get("description", ""),
                source_url=source_url or "",
                version=_version_for(metadata, previous),
                connection_id=connection_id,
                created_at=previous.created_at if previous else self._now(),
                created_by=previous.created_by if previous else "rollout",
                framework=metadata.get("framework", ""),
                aws_services=list(metadata.get("aws_services") or []),
                tags=list(metadata.get("tags") or []),
                # BOTH HALVES OR NEITHER (T2 note N-1) — this verb just created/pushed the repo in
                # the connection's org, so it KNOWS the exact pair and records it rather than
                # leaving materialize to parse ``source_url``.
                source_org=org,
                source_repo=name,
            ),
            name=name,
        )

    def _seed_metadata(self, scaffold_dir: Path, name: str) -> Dict:
        """Read the seed's ``template.json`` — from DISK, which is allowed because it IS the seed.

        ABSENT is fine and means "this seed declares nothing" (an empty dict, so the caller's
        ``.get`` defaults apply). PRESENT BUT UNREADABLE is a defect in what AGP ships and fails
        loudly: silently falling back to defaults would publish a blank catalog card and report
        the rollout as a success.
        """
        path = scaffold_dir / SEED_METADATA_FILE
        if not path.is_file():
            return {}
        try:
            parsed = json.loads(path.read_text())
        except (OSError, ValueError):
            logger.exception("[reconcile] unreadable %s for seed %s", SEED_METADATA_FILE, name)
            raise RolloutError(
                f"Base template '{name}' has unreadable metadata", kind="rollout_error"
            ) from None
        if not isinstance(parsed, dict):
            raise RolloutError(
                f"Base template '{name}' has unreadable metadata", kind="rollout_error"
            )
        return parsed

    def _fetch_runtime_module_files(self) -> Dict[str, bytes]:
        """Download the S3-staged ``agentcore_runtime`` module zip (uploaded by ``terraform
        apply``) and UNPACK it into ``{path: bytes}``.

        The unpack is here because ``commit_files`` takes files, not an archive — the old path
        handed the raw zip to ``create_repo_from_zip``, which unpacked it provider-side. Missing
        config, any S3 failure and a corrupt archive all raise a SAFE ``RolloutError``: the S3
        error value (which can carry the bucket/key) is logged, never surfaced, and a corrupt
        archive must not be committed as one opaque file.
        """
        if not self._runtime_module_bucket or not self._runtime_module_key:
            raise RolloutError(
                "Runtime-infra module archive is not configured", kind="rollout_error"
            )
        try:
            resp = self._s3.get_object(
                Bucket=self._runtime_module_bucket, Key=self._runtime_module_key
            )
            zip_bytes = resp["Body"].read()
        except Exception:
            logger.exception(
                "[reconcile] failed to fetch runtime-infra module zip from s3://%s/%s",
                self._runtime_module_bucket,
                self._runtime_module_key,
            )
            raise RolloutError(
                "Runtime-infra module archive is not available", kind="rollout_error"
            ) from None
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r") as zf:
                files = {
                    info.filename: zf.read(info.filename)
                    for info in zf.infolist()
                    if not info.is_dir()
                }
        except (zipfile.BadZipFile, OSError, EOFError):
            logger.exception("[reconcile] runtime-infra module archive is not a readable zip")
            raise RolloutError(
                "Runtime-infra module archive is not readable", kind="rollout_error"
            ) from None
        if not files:
            raise RolloutError(
                "Runtime-infra module archive is empty", kind="rollout_error"
            )
        return files

    # ===================================================================== #
    # Internals
    # ===================================================================== #

    def _probe(self, org, name, token, base_url):
        """``read_repo`` as the reconcile probe. ``None`` is NOT-FOUND and nothing else — every
        other failure raises, because folding an outage into "absent" would tell an operator their
        templates are gone and offer to overwrite iterated repos with starter bytes."""
        try:
            return self._provider.read_repo(org, name, token=token, base_url=base_url)
        except GitHubRepoError as err:
            logger.exception("[reconcile] repo probe failed for %s/%s", org, name)
            raise RolloutError(
                f"Could not read '{name}' from the provider", kind="rollout_error"
            ) from err

    def _agent_repo_names(self, connection_id: str) -> set:
        """The MATERIALIZED AGENT repos AGP already accounts for — ONE authority, used by both
        :meth:`reconcile` (to omit adopt rows) and :meth:`adopt` (to refuse them).

        Shared deliberately: a picker that hides a row while the verb accepts it is exactly the
        gap the E28C/T3 review found, and two separate lookups could drift back into it.

        FAILS CLOSED in both directions. No inventory wired, or an inventory that cannot be read,
        means AGP cannot prove a repository is not one of its own agents' — and the failure that
        matters here is the permissive one (registering a live agent repo as a template), so
        "could not check" must never read as "nothing to exclude".
        """
        if self._known_repo_names is None:
            logger.error(
                "[reconcile] no repo inventory wired: cannot subtract AGP's own agent repos"
            )
            raise RolloutError(
                "Reconcile is not available on this deployment", kind="rollout_error"
            )
        try:
            return set(self._known_repo_names(connection_id))
        except Exception:
            logger.exception("[reconcile] could not read AGP's repo inventory")
            raise RolloutError(
                "Could not read the platform's repository inventory", kind="rollout_error"
            ) from None

    def _list_org_repos(self, org, token, base_url) -> set:
        """The org's repository names. A failure RAISES rather than reading as an empty org —
        that would offer to create seeds over repositories AGP simply could not see."""
        try:
            return set(self._provider.list_repos(org, token=token, base_url=base_url))
        except GitHubRepoError as err:
            logger.exception("[reconcile] could not list the repositories of org %s", org)
            raise RolloutError(
                "Could not list the org's repositories", kind="rollout_error"
            ) from err

    # Every registry call funnels through these three wrappers, and each maps the VALIDATION
    # subclass BEFORE its store-fault parent. Order is load-bearing: the subclass would otherwise
    # be swallowed by the parent clause and a malformed ``connection_id`` would tell the console to
    # retry a request that can never succeed.

    def _registered_names(self, connection_id: str) -> set:
        """The names already in the connection's template CATALOG.

        STRICT: a store failure raises rather than reading as an empty catalog. An unreadable
        catalog would make every template look unregistered, and the surface would offer to
        create what is already there."""
        try:
            return {r.name for r in self._registry.list_for_connection(connection_id)}
        except TemplateRegistryValidationError as err:
            raise RolloutError("Invalid connection id", kind="validation") from err
        except TemplateRegistryError as err:
            logger.exception(
                "[reconcile] could not read the template catalog for connection %s",
                connection_id,
            )
            raise RolloutError(
                "Could not read the template catalog", kind="rollout_error"
            ) from err

    def _find(self, connection_id: str, name: str) -> Optional[TemplateRecord]:
        try:
            return self._registry.get(connection_id, template_id_for(name))
        except TemplateRegistryValidationError as err:
            raise RolloutError(
                f"Invalid template name or connection for '{name}'", kind="validation"
            ) from err
        except TemplateRegistryError as err:
            logger.exception("[reconcile] could not read the catalog entry for %s", name)
            raise RolloutError(
                "Could not read the template catalog", kind="rollout_error"
            ) from err

    def _put(self, record: TemplateRecord, *, name: str) -> TemplateRecord:
        try:
            return self._registry.put(record)
        except TemplateRegistryValidationError as err:
            # Malformed connection_id / name — the caller's input, not a store fault. Must not be
            # reported as retryable (caught BEFORE the parent clause below).
            raise RolloutError(
                f"Invalid template name or connection for '{name}'", kind="validation"
            ) from err
        except TemplateRegistryError as err:
            logger.exception("[reconcile] could not register template %s", name)
            raise RolloutError(
                f"Failed to register template '{name}'", kind="rollout_error"
            ) from err

    @staticmethod
    def _require_valid_name(name: str) -> None:
        """The catalog's own name authority, applied on the rollout/adopt paths too.

        It also closes a traversal seam by construction: ``_rollout_template`` joins this name
        onto ``agent_templates_dir``, and a pattern anchored on ``^[a-z]`` with no ``/`` or ``.``
        in its class cannot express a traversal segment."""
        if not _TEMPLATE_NAME_RE.match(name or ""):
            raise RolloutError(
                f"Template name must match {_TEMPLATE_NAME_RE.pattern}", kind="validation"
            )

    def _base_template_names(self) -> List[str]:
        """On-disk seed names — the immediate subdirs of ``agent_templates_dir``."""
        if not self._agent_templates_dir.is_dir():
            return []
        return sorted(p.name for p in self._agent_templates_dir.iterdir() if p.is_dir())

    def _resolve(self, connection_id: str):
        """Resolve ``(org, base_url, token)`` for a connection. The token flows ONLY into the
        provider seam — never logged, never returned."""
        connection = self._conn.get_connection(connection_id)
        token = self._conn.get_bearer_token(connection_id)
        return connection.org, connection.base_url, token


# state → the word a rollout reports for it. ``unregistered_present`` is deliberately ABSENT:
# that state is refused, not acted on, so it has no push outcome to name (a KeyError here would
# mean a caller pushed a repo it had no record of — see ``_rollout_template``).
_ACTION_FOR_STATE = {
    "registered_present": "overwritten",
    "registered_missing": "recreated",
    "seed_absent": "created",
}


def _state_for(*, registered: bool, present: bool) -> str:
    """The three-state matrix (D-C3), in one place so no caller can invent a fourth answer."""
    if registered:
        return "registered_present" if present else "registered_missing"
    return "unregistered_present" if present else "seed_absent"


def _head_or_none(view) -> Optional[str]:
    """``read_repo``'s ``head_sha`` as the row reports it.

    ``""`` — a repository that exists with NO COMMIT yet (T1) — becomes ``None``. The row must
    still read as PRESENT (offering "create" would 422 on a repo that is already there), but an
    empty string must never travel on as a ``read_tree`` ref: that would mean "whatever HEAD is
    now", which is the mixed-tree read T1 refuses.
    """
    if view is None:
        return None
    return view.head_sha or None


def _version_for(metadata: Dict, previous: Optional[TemplateRecord]) -> str:
    """The version a seed registration records.

    The seed's DECLARED version wins (it is the thing being pushed). Failing that, the recorded
    version stands — a re-push must not RESET a versioned catalog entry, which is exactly what the
    unconditional ``version="1"`` stamp did. ``"1"`` only for a first registration of a seed that
    declares nothing.
    """
    declared = metadata.get("version")
    if isinstance(declared, str) and declared:
        return declared
    if previous is not None and previous.version:
        return previous.version
    return "1"


def _display_url(base_url: Optional[str], org: str, repo: str) -> str:
    """A human-facing URL for an adopted repo's ``source_url``.

    DISPLAY ONLY, and never parsed back (D-C1) — the structural ``source_org``/``source_repo``
    pair is what any code dereferences. Derived from the connection's API base for a GitHub
    Enterprise host so the console does not link an on-prem repo to github.com.
    """
    if not base_url:
        return f"https://github.com/{org}/{repo}"
    host = base_url.rstrip("/")
    for prefix in ("/api/v3", "/api"):
        if host.endswith(prefix):
            host = host[: -len(prefix)]
            break
    return f"{host}/{org}/{repo}"


def _view_from_record(record: TemplateRecord) -> TemplateView:
    """Project a record onto the catalog's read model — the SAME shape ``github_template_service``
    serves, so an adopted template renders identically to an uploaded one."""
    return TemplateView(
        name=record.name,
        description=record.description,
        framework=record.framework,
        aws_services=list(record.aws_services),
        tags=list(record.tags),
        html_url=record.source_url,
        updated_at=record.updated_at or record.created_at,
    )
