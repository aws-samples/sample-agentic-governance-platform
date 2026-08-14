"""
ECR image service — delete an agent's built container images (Epic 23, Task 3).

An agent's images are pushed to the shared project ECR repo tagged
``<agent_id>-<short_sha>``. This service deletes every image tagged for a given
agent as part of the repo-teardown cascade. It is idempotent and safe: an unset
repository or a not-found repository yields 0 deletions, not an error.
"""

import logging

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# ECR error codes that mean "nothing to delete" rather than a real failure.
_NOT_FOUND_CODES = {"RepositoryNotFoundException", "ImageNotFoundException"}


class EcrImageError(Exception):
    """Raised on an unexpected ECR failure (never on a not-found condition)."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class EcrImageService:
    """Service for deleting an agent's built images from the shared ECR repo."""

    def __init__(self, *, repository: str = "", region: str = "us-east-1", client=None) -> None:
        """Initialize the ECR image service.

        Args:
            repository: ECR repo URI or name (e.g. settings.PROJECT_ECR_REPOSITORY,
                a full URI like ``<acct>.dkr.ecr.<region>.amazonaws.com/<name>``).
                Reduced to the repo name (segment after the last ``/``). Empty ⇒
                deletes become no-ops.
            region: AWS region for the ECR client.
            client: Optional pre-built boto3 ECR client (for testing/injection).
        """
        # Reduce a full repo URI to just the repo name.
        self.repository = repository.rsplit("/", 1)[-1] if repository else ""
        self._ecr = client or boto3.client("ecr", region_name=region)

    def delete_images(self, agent_id: str) -> int:
        """Delete every image tagged ``<agent_id>-*`` in the repo.

        Args:
            agent_id: The agent whose images to delete.

        Returns:
            The number of image tags deleted.

        Raises:
            EcrImageError: On an unexpected ECR failure (not on not-found).
        """
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
                f"Deleted {len(matched)} image(s) for agent {agent_id} "
                f"from ECR repo {self.repository}"
            )
            return len(matched)

        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in _NOT_FOUND_CODES:
                logger.info(
                    f"ECR repo {self.repository} not found ({code}); "
                    f"nothing to delete for agent {agent_id}"
                )
                return 0
            logger.error(f"Failed to delete ECR images for agent {agent_id}: {e}")
            raise EcrImageError(f"ECR image deletion failed: {str(e)}")

    def count_images(self, agent_id: str) -> int:
        """Count the images tagged ``<agent_id>-*`` in the repo (E23/T11 reachability probe).

        READ-ONLY: the delete-preview endpoint uses this to decide the image artifact's
        state (>0 → present, 0 → gone). Reuses the same pagination + prefix logic as
        :meth:`delete_images` but deletes nothing. An unset repo or a not-found repo
        yields 0 (idempotent); an unexpected ``ClientError`` propagates so the caller can
        report the state as ``unknown``.
        """
        if not self.repository:
            return 0
        try:
            return len(self._matching_tags(agent_id))
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in _NOT_FOUND_CODES:
                return 0
            raise EcrImageError(f"ECR image count failed: {str(e)}")

    def _matching_tags(self, agent_id: str) -> list[str]:
        """Paginate ``list_images`` (TAGGED) and collect the tags starting ``<agent_id>-``."""
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
