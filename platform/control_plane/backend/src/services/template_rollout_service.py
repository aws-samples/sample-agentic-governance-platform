"""The org reconcile service: AGP's template registry compared with the connected org's repositories.

Every row it returns carries the state of that comparison, and each state has one honest action:

===========================  ====================================  ===========================
state                        meaning                               the operator's one action
===========================  ====================================  ===========================
``registered_present``       in sync                               nothing, or re-push the seed
``registered_missing``       the repo behind the record is gone    re-create from seed, or deregister
``unregistered_present``     a repo AGP does not know about        adopt
``seed_absent``              a seed with nothing in the org        create
===========================  ====================================  ===========================

``read_repo``, whose ``None`` means not-found and nothing else, is the probe; the registry alone
cannot say whether a repository exists. The same three-state truth binds the write verbs:
``_rollout_template`` dispatches on :func:`_state_for` and refuses ``unregistered_present``, and
:meth:`RolloutService.adopt` enforces the picker's whole subtraction server-side. A re-push is
``create_repo`` (idempotent) then ``commit_files`` of the seed bytes, one commit on top with
history preserved, idempotent by content; nothing here deletes a repository.

The cost model: no list or page route pays for a provider call. :meth:`RolloutService.reconcile`
is the only read that talks to a provider (one paginated ``list_repos`` plus a ``read_repo`` per
seed-or-registered row); the write verbs probe too, because an operator clicked them.

The infra repos are catalog rows (:class:`~services.infra_template_registry.InfraTemplateRecord`):
a rollout seeds the connection's infra catalog, then ensures one org repo per infra template its
registered agent templates reference, plus the default always, each filled from the module zip at
the record's ``module_source``.

Collaborators mirror ``project_service``: a ``RepoProvider``, a ``ConnectionService``, an S3 client
for the staged modules, the two registries and a ``known_repo_names`` callable naming the
materialized agent repos AGP already accounts for. The connection credential flows only into the
provider seam: never logged, never returned, never put on a read model, never in a message.
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar, Dict, List, Literal, Optional, Tuple

from core.errors import AppError, Outcome
from core.state_store import VersionMismatch
from services.github_repo_errors import GitHubRepoError
from services.github_template_service import _NAME_RE as _TEMPLATE_NAME_RE
from services.github_template_service import TemplateView
from services.infra_template_registry import (
    SEED_INFRA_TEMPLATES,
    InfraTemplateRecord,
    InfraTemplateRegistryError,
    InfraTemplateRegistryValidationError,
)
from services.scaffold_files import collect_scaffold_files
from services.template_registry import (
    TemplateRecord,
    TemplateRegistryError,
    TemplateRegistryValidationError,
    template_id_for,
)

logger = logging.getLogger(__name__)

DEFAULT_INFRA_TEMPLATE_ID = "agentcore-runtime"
SEED_METADATA_FILE = "template.json"
_INFRA_SEED_WIRE_FIELDS = ("id", "name", "description", "platform", "iac_type", "build_kind", "repo_name")
_SEED_PUSH_MESSAGE = "chore: seed template contents from AGP [skip ci]"
_INFRA_PUSH_MESSAGE = "chore: seed infra module from AGP [skip ci]"
_FALLBACK_BRANCH = "main"


class RolloutError(AppError):
    """A rollout, reconcile or adopt operation failed. The message is a fixed literal of this
    module, at most carrying a template, repository or catalog name, never a store or provider
    message; every kind renders a fixed detail.

    ``not_found`` is a seed AGP does not ship. ``repo_not_found`` is a repository absent from the
    org (adopt), a different fact with a different detail. ``conflict`` is a name already registered
    or reserved (adopt). ``validation`` is malformed caller input, permanent. ``rollout_error`` is a
    store or provider fault, retryable as sent. ``changed`` is a catalog row that a concurrent write
    moved between this rollout's read and its write, retryable after a reload.
    """

    domain: ClassVar[str] = "rollout"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "not_found": Outcome(404, "Unknown base template"),
        "repo_not_found": Outcome(404, "Repository not found in the org"),
        "conflict": Outcome(409, "Template already registered"),
        "validation": Outcome(422, "Invalid template name or connection id"),
        "rollout_error": Outcome(502, "Template rollout failed"),
        "changed": Outcome(409, "Rollout changed while this request ran; reload and retry"),
    }

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message, kind)


ReconcileOrigin = Literal["seed", "registry", "org"]
ReconcileState = Literal["registered_present", "registered_missing", "unregistered_present", "seed_absent"]


@dataclass
class ReconcileItem:
    """One row of a :class:`ReconcileView`: the registry-versus-org comparison for one name.

    ``origin`` says why the row exists: ``"seed"`` (AGP ships a scaffold under this name),
    ``"registry"`` (AGP has a record but no seed: an uploaded or adopted template) or ``"org"`` (a
    repository AGP found and does not account for). Seed wins over registry when both apply,
    because only a seed row can offer a re-push or a re-create from seed.

    ``state`` is the comparison (see the module docstring). ``default_branch`` and ``head_sha``
    are what ``read_repo`` found, both ``None`` when there was nothing to find: an absent repo, an
    org-origin row (known present from the listing, not re-probed) or a repository with no commit
    yet, whose empty head must never travel on as a ``read_tree`` ref. ``infra_template_id`` is the
    infra a seed row deploys through, off its ``template.json``; ``None`` for every other row and
    for a seed whose metadata is unreadable.
    """

    name: str
    origin: ReconcileOrigin
    state: ReconcileState
    default_branch: Optional[str] = None
    head_sha: Optional[str] = None
    infra_template_id: Optional[str] = None


@dataclass
class ReconcileView:
    """The reconcile surface's whole answer.

    ``infra_repo`` is its own field rather than a flagged row in ``templates``: forced, never a
    choice, so it is never offered as an adopt row and cannot be deselected from a rollout. The
    view does not stop a client posting the infra name to the adopt route, which is why
    :meth:`RolloutService.adopt` refuses it explicitly. ``infra_seeds`` are the infra catalog seeds
    ``rollout()`` upserts before it reads the catalog, projected without ``module_source``, so the
    console can name the infra repos a roll-out will create.
    """

    templates: List[ReconcileItem]
    infra_repo: ReconcileItem
    infra_seeds: List[dict] = field(default_factory=list)


@dataclass
class RolloutResultItem:
    """One rollout receipt. ``action`` is derived from the observed state, never from the registry
    alone: ``created`` (genuinely new), ``recreated`` (a record existed, the repo was gone),
    ``overwritten`` (a re-push on top, nothing destroyed), ``unchanged`` (a re-push that wrote no
    commit), ``skipped`` (nothing was needed), ``adopted`` (registered as is) or ``failed``, the
    one word that reports an error: the write was attempted and did not happen."""

    name: str
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
        infra_template_registry=None,
        known_repo_names=None,
        now=lambda: datetime.now(timezone.utc).isoformat(),
    ) -> None:
        self._provider = repo_provider
        self._conn = connection_service
        self._agent_templates_dir = Path(agent_templates_dir)
        self._registry = template_registry
        self._now = now
        self._s3 = s3_client
        self._runtime_module_bucket = runtime_module_bucket
        self._runtime_module_key = runtime_module_key
        self._infra = infra_template_registry
        self._known_repo_names = known_repo_names

    def reconcile(self, connection_id: str) -> ReconcileView:
        """Compare AGP's registry against the org's repositories; return one row per name.

        The row universe is the union of the seed names, the registered names and the org's
        repositories minus what AGP already accounts for: registered templates, this connection's
        materialized agent repos and the infra repos. Seed and registered rows are probed with
        ``read_repo``; org rows are not re-probed, the listing already proved them present.

        Fails closed everywhere: an unreadable catalog, a failed listing and a missing repo
        inventory all raise, because "AGP could not look" is not evidence of absence, and each
        degradation would offer to create over something that exists or to adopt something AGP owns.
        """
        org, base_url, token = self._resolve(connection_id)
        registered = self._registered_names(connection_id)
        seeds = set(self._base_template_names())

        agent_repos = self._agent_repo_names(connection_id)
        org_repos = self._list_org_repos(org, token, base_url)
        adoptable = org_repos - seeds - registered - agent_repos - self._infra_repo_names(connection_id)

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
                    infra_template_id=self._seed_infra_id(name) if name in seeds else None,
                )
            )
        for name in sorted(adoptable):
            rows.append(ReconcileItem(name=name, origin="org", state="unregistered_present"))

        infra_repo_name = self._default_infra_repo_name(connection_id)
        infra_view = self._probe(org, infra_repo_name, token, base_url)
        return ReconcileView(
            templates=rows,
            infra_repo=ReconcileItem(
                name=infra_repo_name,
                origin="seed",
                state=_state_for(registered=False, present=infra_view is not None),
                default_branch=infra_view.default_branch if infra_view else None,
                head_sha=_head_or_none(infra_view),
            ),
            infra_seeds=[
                {k: v for k, v in seed.items() if k in _INFRA_SEED_WIRE_FIELDS}
                for seed in SEED_INFRA_TEMPLATES
            ],
        )

    def _seed_infra_id(self, name: str) -> Optional[str]:
        """The infra a seed declares, with the same ``or DEFAULT`` fold :meth:`_register` applies.
        A malformed ``template.json`` answers ``None`` rather than failing the read: the compare
        view must still open, and the roll-out itself refuses that seed before any push."""
        try:
            metadata = self._seed_metadata(self._agent_templates_dir / name, name)
        except RolloutError:
            return None
        return metadata.get("infra_template_id") or DEFAULT_INFRA_TEMPLATE_ID

    def rollout(
        self,
        connection_id: str,
        *,
        template_names: List[str],
        overwrite: bool,
        overwrite_infra: bool = False,
    ) -> RolloutResult:
        """Push the selected seeds into the org and always ensure the forced infra repos.

        Every decision is made on the three-state truth :meth:`reconcile` computes, never on the
        registry alone (see :meth:`_rollout_template` for the per-state table). ``overwrite`` decides
        one thing: whether an already-registered, present template is re-pushed; no value of it
        authorizes writing over a repository AGP has no record of, and it never reaches the infra
        repos. ``overwrite_infra`` is the infra repos' own consent, off by default; creating an
        absent infra repo stays unconditional.

        The infra catalog is seeded first, because the reference read below is empty on a
        connection's first rollout until that write. Then one org repo is ensured per infra template
        the registered agent templates reference, plus the default always, after the template loop so
        a template rolled out in this call pulls its infra repo into the same run. Every infra target
        is attempted before the failure is raised: one unavailable module zip must not cost the org
        the others, and the run still raises naming the failed infra templates.
        """
        org, base_url, token = self._resolve(connection_id)
        registered = self._registered_names(connection_id)
        self._seed_infra_templates(connection_id)

        result = RolloutResult()
        for name in template_names:
            result.items.append(
                self._rollout_template(name, connection_id, org, base_url, token, registered, overwrite)
            )
        infra_failures: List[str] = []
        for record in self._referenced_infra_records(connection_id):
            try:
                result.items.append(self._ensure_infra(record, org, base_url, token, overwrite_infra))
            except RolloutError:
                logger.exception("[reconcile] could not ensure the infra repo for %s (continuing)", record.id)
                result.items.append(
                    RolloutResultItem(
                        name=record.repo_name,
                        action="failed",
                        reason="the infra module could not be rolled out",
                    )
                )
                infra_failures.append(record.id)
        if infra_failures:
            raise RolloutError(
                f"Failed to roll out the infra module(s) for: {', '.join(infra_failures)}",
                kind="rollout_error",
            )
        return result

    def adopt(
        self,
        connection_id: str,
        *,
        repo_name: str,
        description: Optional[str] = None,
        created_by: str = "",
    ) -> TemplateView:
        """Register an existing org repository as one of this org's templates, as is.

        A governance statement, not a content check: no content inspection and no push, since
        materialize reads the template repo at use-time and a push would overwrite the very
        repository the operator is adopting because they wrote it. The one provider call is
        ``read_repo``, a precondition: adopting a name that is not in the org would write a record
        pointing at nothing. ``created_by`` is the validated caller's identity, passed by the route.

        The picker's subtraction is enforced here, not only in the picker: a forced infra repo and a
        materialized agent repo both answer ``conflict``, with a message naming which rule refused.
        The registration is a first write, so a concurrent adopt of the same name that lands first
        answers ``conflict`` too.
        """
        self._require_valid_name(repo_name)
        org, base_url, token = self._resolve(connection_id)

        if repo_name in self._infra_repo_names(connection_id):
            raise RolloutError(
                f"'{repo_name}' is one of the platform's forced infra repos, not a template",
                kind="conflict",
            )
        if self._find(connection_id, repo_name) is not None:
            raise RolloutError(f"Template '{repo_name}' is already registered", kind="conflict")
        if repo_name in self._agent_repo_names(connection_id):
            raise RolloutError(
                f"'{repo_name}' is a materialized agent repository, not a template",
                kind="conflict",
            )
        if self._probe(org, repo_name, token, base_url) is None:
            raise RolloutError(f"Repository '{repo_name}' is not in the org", kind="repo_not_found")

        record = TemplateRecord(
            id=template_id_for(repo_name),
            name=repo_name,
            description=description or "",
            source_url=_display_url(base_url, org, repo_name),
            version="1",
            connection_id=connection_id,
            created_at=self._now(),
            created_by=created_by,
            source_org=org,
            source_repo=repo_name,
        )
        if not self._create(record, name=repo_name):
            raise RolloutError(f"Template '{repo_name}' is already registered", kind="conflict")
        return _view_from_record(record)

    def _rollout_template(
        self, name, connection_id, org, base_url, token, registered, overwrite
    ) -> RolloutResultItem:
        """Roll out one seed, dispatching on the three-state truth rather than on the registry.

        ============================  ===================  =========================
        state                         overwrite=False      overwrite=True
        ============================  ===================  =========================
        ``registered_present``        skipped (in sync)    ``overwritten`` (re-push)
        ``registered_missing``        ``recreated``        ``recreated``
        ``unregistered_present``      skipped, adopt it    skipped, adopt it
        ``seed_absent``               ``created``          ``created``
        ============================  ===================  =========================

        ``unregistered_present`` is refused whatever ``overwrite`` says: ``commit_files`` builds its
        tree with no base, so a push would replace a stranger's whole tree, and overwrite means "yes,
        re-push the template I registered", not consent to write over a repository nobody
        registered. ``registered_missing`` re-creates from seed with no flag: the repo is gone, so
        the create can neither collide nor destroy.

        The name authority runs first, before any disk read or provider write; the seed's metadata
        is read before the push, so a malformed ``template.json`` never leaves a pushed repo without
        a catalog entry; and the record is written after the push, so a failed push never leaves a
        catalog entry pointing at a repo that does not exist.
        """
        self._require_valid_name(name)

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
            return RolloutResultItem(name=name, action="skipped", reason="already in the template catalog")

        scaffold_dir = self._agent_templates_dir / name
        if not scaffold_dir.is_dir():
            raise RolloutError(f"Unknown base template '{name}'", kind="not_found")

        files = collect_scaffold_files(scaffold_dir)
        if not files:
            raise RolloutError(f"Base template '{name}' has no files on disk", kind="rollout_error")
        metadata = self._seed_metadata(scaffold_dir, name)

        html_url, changed = self._push(org, name, files, token, base_url, _SEED_PUSH_MESSAGE)
        self._register(connection_id, name, html_url, org, metadata)

        action = _ACTION_FOR_STATE[state]
        if action == "overwritten" and not changed:
            action = "unchanged"
        return RolloutResultItem(name=name, action=action)

    def _ensure_infra(
        self, record: InfraTemplateRecord, org, base_url, token, overwrite_infra
    ) -> RolloutResultItem:
        """Ensure one infra template's org repo, filled from its own module zip. The archive is
        fetched and unpacked before the provider is touched, so a missing or corrupt object never
        leaves a half-created infra repo with no Terraform in it."""
        repo_name = record.repo_name
        exists = self._probe(org, repo_name, token, base_url) is not None
        if exists and not overwrite_infra:
            return RolloutResultItem(name=repo_name, action="skipped", reason="already exists in org")
        files = self._fetch_infra_module_files(record)
        _, changed = self._push(org, repo_name, files, token, base_url, _INFRA_PUSH_MESSAGE)
        if not exists:
            return RolloutResultItem(name=repo_name, action="created")
        return RolloutResultItem(name=repo_name, action="overwritten" if changed else "unchanged")

    def _push(self, org, name, files, token, base_url, message) -> Tuple[str, bool]:
        """Make ``org/name`` exist and put ``files`` on its default branch in one commit.

        Returns ``(html_url, changed)``; ``changed`` is ``False`` when the branch tip did not move,
        because ``commit_files`` is idempotent by content. The branch is the repo's own default
        branch, read rather than assumed: ``create_repo``'s ``auto_init`` names it from the org's
        setting, and pushing to a guessed ``main`` elsewhere would leave a template that looks
        seeded and never builds.
        """
        try:
            html_url = self._provider.create_repo(org, name, private=True, token=token, base_url=base_url)
            view = self._provider.read_repo(org, name, token=token, base_url=base_url)
            branch = (view.default_branch if view else None) or _FALLBACK_BRANCH
            head_before = _head_or_none(view)
            head_after = self._provider.commit_files(
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
            raise RolloutError(f"Failed to roll out '{name}'", kind="rollout_error") from err
        return html_url, head_before is None or head_after != head_before

    def _register(
        self,
        connection_id: str,
        name: str,
        source_url: str,
        org: str,
        metadata: Dict,
    ) -> None:
        """Write the catalog record for a pushed seed: an upsert, so a re-push re-registers
        instead of duplicating.

        The seed's own ``template.json`` supplies the metadata; the version is what the seed
        declares, falling back to the recorded version rather than to a stamp; ``created_at`` and
        ``created_by`` belong to the first registration and are carried through a re-push, the same
        rule ``upload_template`` follows. An omitted ``infra_template_id`` or ``artifact_kind``
        folds to the default, because ``""`` is not a chain and dereferences to nothing.
        """
        previous = self._find(connection_id, name, consistent=True)
        record = TemplateRecord(
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
            infra_template_id=(metadata.get("infra_template_id") or DEFAULT_INFRA_TEMPLATE_ID),
            preview=bool(metadata.get("preview", False)),
            artifact_kind=metadata.get("artifact_kind") or "agent",
            source_org=org,
            source_repo=name,
        )
        landed = (
            self._create(record, name=name)
            if previous is None
            else self._save(previous.model_copy(update=record.model_dump()), name=name)
        )
        if not landed:
            raise RolloutError(f"Template '{name}' changed during the rollout", kind="changed")

    def _seed_metadata(self, scaffold_dir: Path, name: str) -> Dict:
        """Read the seed's ``template.json`` from disk, which is allowed because it is the seed.
        Absent means "this seed declares nothing" (an empty dict); present but unreadable is a
        defect in what AGP ships and fails loudly rather than publishing a blank catalog card."""
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
            raise RolloutError(f"Base template '{name}' has unreadable metadata", kind="rollout_error")
        return parsed

    def _fetch_infra_module_files(self, record: InfraTemplateRecord) -> Dict[str, bytes]:
        """Download one infra template's S3-staged module zip and unpack it into ``{path: bytes}``.

        Missing config, any S3 failure and a corrupt archive all raise a ``RolloutError`` whose
        message names only the catalog id: the S3 error value, which can carry the bucket and key,
        is logged and never surfaced. The key comes from ``record.module_source``; the bucket is the
        one control-plane value every module is staged into.
        """
        key = self._module_key_for(record)
        if not self._runtime_module_bucket or not key:
            raise RolloutError(
                f"Infra module archive for '{record.id}' is not configured",
                kind="rollout_error",
            )
        try:
            resp = self._s3.get_object(Bucket=self._runtime_module_bucket, Key=key)
            zip_bytes = resp["Body"].read()
        except Exception:
            logger.exception(
                "[reconcile] failed to fetch infra module zip from s3://%s/%s",
                self._runtime_module_bucket,
                key,
            )
            raise RolloutError(
                f"Infra module archive for '{record.id}' is not available",
                kind="rollout_error",
            ) from None
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r") as zf:
                files = {info.filename: zf.read(info.filename) for info in zf.infolist() if not info.is_dir()}
        except (zipfile.BadZipFile, OSError, EOFError):
            logger.exception("[reconcile] infra module archive is not a readable zip")
            raise RolloutError(
                f"Infra module archive for '{record.id}' is not readable",
                kind="rollout_error",
            ) from None
        if not files:
            raise RolloutError(f"Infra module archive for '{record.id}' is empty", kind="rollout_error")
        return files

    def _module_key_for(self, record: InfraTemplateRecord) -> str:
        """The S3 key ``record``'s module zip is staged under: ``module_source``, except that a
        configured ``runtime_module_key`` overrides the default chain's, because a deployment that
        staged the runtime module under a custom key must keep resolving it."""
        if record.id == DEFAULT_INFRA_TEMPLATE_ID and self._runtime_module_key:
            return self._runtime_module_key
        return record.module_source

    def _probe(self, org, name, token, base_url):
        """``read_repo`` as the reconcile probe. ``None`` is not-found and nothing else; every other
        failure raises, because folding an outage into "absent" would tell an operator their
        templates are gone and offer to overwrite iterated repos with starter bytes."""
        try:
            return self._provider.read_repo(org, name, token=token, base_url=base_url)
        except GitHubRepoError as err:
            logger.exception("[reconcile] repo probe failed for %s/%s", org, name)
            raise RolloutError(f"Could not read '{name}' from the provider", kind="rollout_error") from err

    def _seed_infra_templates(self, connection_id: str) -> None:
        """Upsert :data:`SEED_INFRA_TEMPLATES` under ``connection_id``, supplying the per-connection
        halves the connection-agnostic seeds omit. ``created_at`` is carried from the existing row,
        the same rule :meth:`_register` follows. A no-op when no registry is wired."""
        if self._infra is None:
            return
        for seed in SEED_INFRA_TEMPLATES:
            previous = self._infra_get(connection_id, seed["id"], consistent=True)
            record = InfraTemplateRecord(
                **seed,
                connection_id=connection_id,
                created_at=previous.created_at if previous else self._now(),
                created_by=previous.created_by if previous else "rollout",
            )
            landed = (
                self._infra_create(record)
                if previous is None
                else self._infra_save(previous.model_copy(update=record.model_dump()))
            )
            if not landed and not _holds_seed(
                self._infra_get(connection_id, seed["id"], consistent=True), record, seed
            ):
                raise RolloutError(
                    f"Infra template '{seed['id']}' changed during the rollout", kind="changed"
                )

    def _referenced_infra_records(self, connection_id: str) -> List[InfraTemplateRecord]:
        """The infra templates whose org repo this rollout must ensure: those the connection's
        registered agent templates reference, plus the default always, in a stable order by id. A
        referenced id with no record names no repo and contributes nothing; the rollout continues."""
        ids = {
            (r.infra_template_id or DEFAULT_INFRA_TEMPLATE_ID)
            for r in self._registered_records(connection_id)
        } | {DEFAULT_INFRA_TEMPLATE_ID}
        records = [self._infra_record(connection_id, infra_id) for infra_id in sorted(ids)]
        return [r for r in records if r is not None]

    def _infra_repo_names(self, connection_id: str) -> set:
        """Every repo name that is infra rather than a template: the union of the connection's
        catalog rows and the seeds. The seeds half makes the guard hold on a connection that has
        never been rolled out, which would otherwise have no infra rows to refuse."""
        names = {seed["repo_name"] for seed in SEED_INFRA_TEMPLATES}
        return names | {r.repo_name for r in self._infra_records(connection_id)}

    def _default_infra_repo_name(self, connection_id: str) -> str:
        """The repo name behind :data:`DEFAULT_INFRA_TEMPLATE_ID`, the one ``ReconcileView``'s
        single ``infra_repo`` field reports on; the seed's own name when the record is absent."""
        record = self._infra_record(connection_id, DEFAULT_INFRA_TEMPLATE_ID)
        return record.repo_name if record else _seed_repo_name(DEFAULT_INFRA_TEMPLATE_ID)

    def _infra_record(self, connection_id: str, infra_template_id: str) -> Optional[InfraTemplateRecord]:
        """One infra template as a record: the catalog, or the seed when no registry is wired. The
        seed is not a fallback for a record absent from a wired catalog; that case is ``None``."""
        if self._infra is None:
            seed = _seed_for(infra_template_id)
            if seed is None:
                return None
            return InfraTemplateRecord(**seed, connection_id=connection_id, created_at=self._now())
        return self._infra_get(connection_id, infra_template_id)

    def _infra_records(self, connection_id: str) -> List[InfraTemplateRecord]:
        """The connection's whole infra catalog; ``[]`` when nothing is wired."""
        if self._infra is None:
            return []
        try:
            return list(self._infra.list(connection_id))
        except InfraTemplateRegistryValidationError as err:
            raise RolloutError("Invalid connection id", kind="validation") from err
        except InfraTemplateRegistryError as err:
            logger.exception(
                "[reconcile] could not read the infra template catalog for connection %s",
                connection_id,
            )
            raise RolloutError("Could not read the infra template catalog", kind="rollout_error") from err

    def _infra_get(
        self, connection_id: str, infra_template_id: str, *, consistent: bool = False
    ) -> Optional[InfraTemplateRecord]:
        if self._infra is None:
            return None
        try:
            return self._infra.get(connection_id, infra_template_id, consistent=consistent)
        except InfraTemplateRegistryValidationError as err:
            raise RolloutError("Invalid connection id", kind="validation") from err
        except InfraTemplateRegistryError as err:
            logger.exception("[reconcile] could not read infra template %s", infra_template_id)
            raise RolloutError("Could not read the infra template catalog", kind="rollout_error") from err

    def _infra_create(self, record: InfraTemplateRecord) -> bool:
        if self._infra is None:
            return True
        try:
            return self._infra.create(record)
        except InfraTemplateRegistryValidationError as err:
            raise RolloutError("Invalid connection id", kind="validation") from err
        except InfraTemplateRegistryError as err:
            logger.exception("[reconcile] could not seed infra template %s", record.id)
            raise RolloutError("Could not write the infra template catalog", kind="rollout_error") from err

    def _infra_save(self, record: InfraTemplateRecord) -> bool:
        if self._infra is None:
            return True
        try:
            self._infra.save(record)
        except VersionMismatch:
            return False
        except InfraTemplateRegistryValidationError as err:
            raise RolloutError("Invalid connection id", kind="validation") from err
        except InfraTemplateRegistryError as err:
            logger.exception("[reconcile] could not seed infra template %s", record.id)
            raise RolloutError("Could not write the infra template catalog", kind="rollout_error") from err
        return True

    def _agent_repo_names(self, connection_id: str) -> set:
        """The materialized agent repos AGP already accounts for, the one authority both
        :meth:`reconcile` (to omit adopt rows) and :meth:`adopt` (to refuse them) use. Fails closed
        in both directions: no inventory wired, or one that cannot be read, must never read as
        "nothing to exclude", because the permissive failure registers a live agent repo as a
        template."""
        if self._known_repo_names is None:
            logger.error("[reconcile] no repo inventory wired: cannot subtract AGP's own agent repos")
            raise RolloutError("Reconcile is not available on this deployment", kind="rollout_error")
        try:
            return set(self._known_repo_names(connection_id))
        except Exception:
            logger.exception("[reconcile] could not read AGP's repo inventory")
            raise RolloutError(
                "Could not read the platform's repository inventory", kind="rollout_error"
            ) from None

    def _list_org_repos(self, org, token, base_url) -> set:
        """The org's repository names. A failure raises rather than reading as an empty org, which
        would offer to create seeds over repositories AGP simply could not see."""
        try:
            return set(self._provider.list_repos(org, token=token, base_url=base_url))
        except GitHubRepoError as err:
            logger.exception("[reconcile] could not list the repositories of org %s", org)
            raise RolloutError("Could not list the org's repositories", kind="rollout_error") from err

    def _registered_names(self, connection_id: str) -> set:
        """The names already in the connection's template catalog. A store failure raises rather
        than reading as an empty catalog, which would offer to create what is already there."""
        return {r.name for r in self._registered_records(connection_id)}

    def _registered_records(self, connection_id: str) -> List[TemplateRecord]:
        """The connection's template catalog as records: one read serves both the names and the
        infra references, so the two cannot disagree mid-rollout."""
        try:
            return list(self._registry.list_for_connection(connection_id))
        except TemplateRegistryValidationError as err:
            raise RolloutError("Invalid connection id", kind="validation") from err
        except TemplateRegistryError as err:
            logger.exception(
                "[reconcile] could not read the template catalog for connection %s",
                connection_id,
            )
            raise RolloutError("Could not read the template catalog", kind="rollout_error") from err

    def _find(self, connection_id: str, name: str, *, consistent: bool = False) -> Optional[TemplateRecord]:
        try:
            return self._registry.get(connection_id, template_id_for(name), consistent=consistent)
        except TemplateRegistryValidationError as err:
            raise RolloutError(
                f"Invalid template name or connection for '{name}'", kind="validation"
            ) from err
        except TemplateRegistryError as err:
            logger.exception("[reconcile] could not read the catalog entry for %s", name)
            raise RolloutError("Could not read the template catalog", kind="rollout_error") from err

    def _create(self, record: TemplateRecord, *, name: str) -> bool:
        try:
            return self._registry.create(record)
        except TemplateRegistryValidationError as err:
            raise RolloutError(
                f"Invalid template name or connection for '{name}'", kind="validation"
            ) from err
        except TemplateRegistryError as err:
            logger.exception("[reconcile] could not register template %s", name)
            raise RolloutError(f"Failed to register template '{name}'", kind="rollout_error") from err

    def _save(self, record: TemplateRecord, *, name: str) -> bool:
        try:
            self._registry.save(record)
        except VersionMismatch:
            return False
        except TemplateRegistryValidationError as err:
            raise RolloutError(
                f"Invalid template name or connection for '{name}'", kind="validation"
            ) from err
        except TemplateRegistryError as err:
            logger.exception("[reconcile] could not register template %s", name)
            raise RolloutError(f"Failed to register template '{name}'", kind="rollout_error") from err
        return True

    @staticmethod
    def _require_valid_name(name: str) -> None:
        """The catalog's own name authority, applied on the rollout and adopt paths too. It also
        closes a traversal seam by construction: ``_rollout_template`` joins this name onto
        ``agent_templates_dir``, and a pattern anchored on ``^[a-z]`` with no ``/`` or ``.`` in its
        class cannot express a traversal segment."""
        if not _TEMPLATE_NAME_RE.match(name or ""):
            raise RolloutError(f"Template name must match {_TEMPLATE_NAME_RE.pattern}", kind="validation")

    def _base_template_names(self) -> List[str]:
        """On-disk seed names: the immediate subdirectories of ``agent_templates_dir``."""
        if not self._agent_templates_dir.is_dir():
            return []
        return sorted(p.name for p in self._agent_templates_dir.iterdir() if p.is_dir())

    def _resolve(self, connection_id: str):
        """``(org, base_url, token)`` for a connection. The token flows only into the provider
        seam, never logged and never returned."""
        connection = self._conn.get_connection(connection_id)
        token = self._conn.get_bearer_token(connection_id)
        return connection.org, connection.base_url, token


_ACTION_FOR_STATE = {
    "registered_present": "overwritten",
    "registered_missing": "recreated",
    "seed_absent": "created",
}


def _seed_for(infra_template_id: str) -> Optional[dict]:
    """The :data:`SEED_INFRA_TEMPLATES` entry with this id, or ``None``."""
    return next((s for s in SEED_INFRA_TEMPLATES if s["id"] == infra_template_id), None)


def _holds_seed(
    row: Optional[InfraTemplateRecord], record: InfraTemplateRecord, seed: Mapping[str, object]
) -> bool:
    keys = {*seed, "connection_id"}
    return row is not None and row.model_dump(include=keys) == record.model_dump(include=keys)


def _seed_repo_name(infra_template_id: str) -> str:
    """The repo name the seed declares for an infra template id; ``""`` for an id AGP ships no
    seed for, a name nothing can be pushed to."""
    seed = _seed_for(infra_template_id)
    return seed["repo_name"] if seed else ""


def _state_for(*, registered: bool, present: bool) -> ReconcileState:
    """The four-state matrix, in one place so no caller can invent a fifth answer."""
    if registered:
        return "registered_present" if present else "registered_missing"
    return "unregistered_present" if present else "seed_absent"


def _head_or_none(view) -> Optional[str]:
    """``read_repo``'s ``head_sha`` as the row reports it: ``""``, a repository with no commit yet,
    becomes ``None``, so the row reads as present while an empty string never travels on as a
    ``read_tree`` ref."""
    if view is None:
        return None
    return view.head_sha or None


def _version_for(metadata: Dict, previous: Optional[TemplateRecord]) -> str:
    """The version a seed registration records: the seed's declared version, else the recorded
    one (a re-push must not reset a versioned entry), else ``"1"`` for a first registration."""
    declared = metadata.get("version")
    if isinstance(declared, str) and declared:
        return declared
    if previous is not None and previous.version:
        return previous.version
    return "1"


def _display_url(base_url: Optional[str], org: str, repo: str) -> str:
    """A human-facing URL for an adopted repo's ``source_url``: display only, never parsed back.
    Derived from the connection's API base for a GitHub Enterprise host so the console does not
    link an on-prem repo to github.com."""
    if not base_url:
        return f"https://github.com/{org}/{repo}"
    host = base_url.rstrip("/")
    for prefix in ("/api/v3", "/api"):
        if host.endswith(prefix):
            host = host[: -len(prefix)]
            break
    return f"{host}/{org}/{repo}"


def _view_from_record(record: TemplateRecord) -> TemplateView:
    """Project a record onto the catalog's read model, the same shape ``github_template_service``
    serves, so an adopted template renders identically to an uploaded one. The record-owned fields
    are copied, never left to the model's defaults; ``platform`` and ``build_kind`` belong to the
    infra record and are joined by the route."""
    return TemplateView(
        name=record.name,
        description=record.description,
        framework=record.framework,
        aws_services=list(record.aws_services),
        tags=list(record.tags),
        html_url=record.source_url,
        updated_at=record.updated_at or record.created_at,
        infra_template_id=record.infra_template_id,
        preview=record.preview,
        artifact_kind=record.artifact_kind,
    )
