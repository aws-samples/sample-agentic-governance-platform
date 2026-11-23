"""ECR image service: delete or count an agent's built container images.

An agent's images are pushed to the shared project ECR repository tagged ``<agent_id>-<short_sha>``,
so the tag prefix is the only agent boundary inside the registry. ``delete_images`` is one leg of
the repository teardown cascade and ``count_images`` the delete preview's reachability probe. Both
are idempotent: an unset repository and a not-found repository yield zero, not an error.
"""

import logging

from botocore.exceptions import ClientError

from core import aws_clients

logger = logging.getLogger(__name__)

_NOT_FOUND_CODES = {"RepositoryNotFoundException", "ImageNotFoundException"}


class EcrImageError(Exception):
    """An ECR call failed for a reason other than a not-found condition. The message names the
    operation, never the ECR error. No route lets it escape: the teardown cascade and the delete
    preview both fold it into their own per-step outcome, so it stays a plain exception."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class EcrImageService:
    """Deletes and counts an agent's built images in the shared ECR repository."""

    def __init__(self, *, repository: str = "", region: str | None = None, client=None) -> None:
        """``repository`` is an ECR repository URI or name, reduced to the name (the segment after
        the last ``/``); empty makes every operation a no-op. ``client`` is an injectable ECR client."""
        self.repository = repository.rsplit("/", 1)[-1] if repository else ""
        self._ecr = client or aws_clients.client("ecr", region=region)

    def delete_images(self, agent_id: str) -> int:
        """Delete every image tagged ``<agent_id>-*`` in the repository and return how many tags
        went. Raises :class:`EcrImageError` on an unexpected ECR failure, never on not-found."""
        if not self.repository:
            return 0

        try:
            matched = self._matching_tags(agent_id)
            if not matched:
                return 0

            self._ecr.batch_delete_image(
                repositoryName=self.repository,
                imageIds=[{"imageTag": t} for t in matched],
            )

            logger.info(
                f"Deleted {len(matched)} image(s) for agent {agent_id} from ECR repo {self.repository}"
            )
            return len(matched)

        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in _NOT_FOUND_CODES:
                logger.info(
                    f"ECR repo {self.repository} not found ({code}); nothing to delete for agent {agent_id}"
                )
                return 0
            logger.exception("[ecr-image] failed to delete the images of agent %s", agent_id)
            raise EcrImageError("ECR image deletion failed") from e

    def count_images(self, agent_id: str) -> int:
        """Count the images tagged ``<agent_id>-*`` in the repository, deleting nothing: the delete
        preview reads more than zero as present and zero as gone. An unset or not-found repository
        yields zero; an unexpected ECR failure raises so the caller can report the state as unknown."""
        if not self.repository:
            return 0
        try:
            return len(self._matching_tags(agent_id))
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in _NOT_FOUND_CODES:
                return 0
            logger.exception("[ecr-image] failed to count the images of agent %s", agent_id)
            raise EcrImageError("ECR image count failed") from e

    def _matching_tags(self, agent_id: str) -> list[str]:
        """Paginate ``list_images`` (tagged only) and collect the tags starting ``<agent_id>-``."""
        prefix = f"{agent_id}-"
        matched: list[str] = []
        next_token = None
        while True:
            kwargs = {
                "repositoryName": self.repository,
                "filter": {"tagStatus": "TAGGED"},
            }
            if next_token:
                kwargs["nextToken"] = next_token
            resp = self._ecr.list_images(**kwargs)

            for image in resp.get("imageIds", []):
                tag = image.get("imageTag")
                if tag and tag.startswith(prefix):
                    matched.append(tag)

            next_token = resp.get("nextToken")
            if not next_token:
                break
        return matched
