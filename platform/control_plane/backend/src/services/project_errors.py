"""``ProjectError``, the one error type of the project service."""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from core.errors import AppError, Outcome


class ProjectError(AppError):
    """A project, repository or delivery operation was refused or failed.

    The message is client copy and the ``None`` rows send it to the client verbatim: ``mcp_refused``,
    ``mcp_not_promotable`` and ``eval_gate`` carry a fixed literal or one of ``project_service``'s constants;
    ``invalid_agent_name`` carries the model's own sentence over the caller's name. Two rows carry
    library text over the caller's own request and nothing else: ``invalid_agent_config`` is pydantic's
    refusal at the agent or MCP record's construction (the model name, the field, the rule and the
    request's ``input_value``) and ``invalid_data_classification`` is the enum's sentence naming the
    request's value; neither can hold a store, provider or build-service response, a token or a secret,
    because the only inputs to those constructions are the request body, the project's ids and
    ``project_service``'s constants. Every other kind has a fixed detail.
    """

    domain: ClassVar[str] = "project"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "not_found": Outcome(404, "Project not found"),
        "repo_not_found": Outcome(404, "Repository not found"),
        "no_repository": Outcome(404, "No repository for this agent"),
        "agent_not_found": Outcome(404, "Agent not found"),
        "has_repositories": Outcome(409, "Project has repositories; delete them first"),
        "invalid_template_name": Outcome(422, "invalid template name"),
        "invalid_agent_name": Outcome(400, None),
        "invalid_agent_config": Outcome(400, None),
        "invalid_data_classification": Outcome(400, None),
        "mcp_refused": Outcome(422, None),
        "unsupported": Outcome(409, "the project's tenant platform cannot host a governed agent"),
        "nothing_to_retry": Outcome(409, "Nothing to retry"),
        "no_prod_candidate": Outcome(409, "no prod candidate to promote"),
        "no_dev_build": Outcome(409, "no dev deployment to promote"),
        "mcp_not_promotable": Outcome(422, None),
        "eval_gate": Outcome(409, None),
        "promote_in_flight": Outcome(409, "a promotion is already in flight"),
        "conflict": Outcome(409, "a build is already running for this agent/stage, try again shortly"),
        "promote_failed": Outcome(502, "failed to start the promotion build"),
        "unknown_rollback_target": Outcome(409, "no such succeeded deployment to roll back to"),
        "rollback_failed": Outcome(502, "failed to start the rollback build"),
        "container_build": Outcome(422, "container repos build from CI on push"),
        "not_ready": Outcome(409, "this repo is still being created — try again when materialize finishes"),
        "materialize_failed": Outcome(409, "this repo's setup failed — retry the setup before building"),
        "no_commit": Outcome(409, "repo has no commit to build yet"),
        "build_failed": Outcome(502, "failed to start the build"),
        "record_unreadable": Outcome(409, "this repo's registry record can't be read — nothing was deleted"),
        "materialize_error": Outcome(502, "repository setup failed"),
    }

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message, kind)
