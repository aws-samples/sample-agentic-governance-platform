"""The errors of the Entra, Graph and On-Behalf-Of adapter: ``GraphError`` for
a non-2xx Graph or Entra response, ``NotAssignedError`` and ``OboConfigError`` for the two OBO refusals."""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from core.errors import AppError, Outcome


class NotAssignedError(AppError):
    """Entra refused the OBO exchange with AADSTS50105: the caller holds no app role on the agent app.

    The message is client copy, composed at the raise site as ``f"{caller} is not assigned to this
    agent"`` from the ``caller`` the route passes to ``obo_exchange`` (the principal's email or oid,
    "the caller" when it passes none); the Entra response body never reaches it.
    """

    domain: ClassVar[str] = "obo"
    kinds: ClassVar[Mapping[str, Outcome]] = {"not_assigned": Outcome(403, None)}


class OboConfigError(AppError):
    """Entra refused the OBO exchange for a configuration reason, not an assignment one.

    AADSTS65001 is a missing delegated grant from the backend to the agent app; AADSTS500011 is an
    unresolved resource or scope. Distinct from ``NotAssignedError`` so the client is told to
    re-provision instead of being shown a permission refusal.
    """

    domain: ClassVar[str] = "obo"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "misconfigured": Outcome(500, "agent identity is misconfigured — re-provision")
    }


class GraphError(Exception):
    """A non-2xx Graph or Entra response: the status, the error code and an optional safe message.

    The message is set only from a Graph resource endpoint's ``error.message``
    (``GraphService._graph_error``): those bodies (``/applications``, ``/servicePrincipals``,
    ``/oauth2PermissionGrants``, ``appRoleAssignedTo``, ``$search``) carry no token and no client
    secret, because Graph never echoes the Authorization header. The Entra ``/token`` path
    (``GraphService._token_error``) never sets a message, because its ``error_description`` can echo
    the assertion or the secret. A plain exception, not an ``AppError``: the status is upstream's,
    and the service that called Graph translates it into its own kind where the meaning is known.
    """

    def __init__(self, status: int, code: str, message: str | None = None) -> None:
        self.status = status
        self.code = code
        self.message = message
        base = f"Graph/Entra error (status={status}, code={code}"
        if message:
            base += f", message={message}"
        base += ")"
        super().__init__(base)
