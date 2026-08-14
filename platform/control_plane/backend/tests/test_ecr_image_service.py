"""Tests for EcrImageService (Epic 23, Task 3).

Strategy: no moto — inject a ``unittest.mock.MagicMock`` boto3 ECR client via
the service constructor, mirroring the agent-registry / s3 service tests.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from services.ecr_image_service import EcrImageService, EcrImageError


def _client_error(code: str, op: str = "ListImages") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, op)


@pytest.fixture
def fake_ecr() -> MagicMock:
    ecr = MagicMock()
    ecr.list_images.return_value = {"imageIds": []}
    return ecr


def test_delete_images_deletes_matching_tag_prefix(fake_ecr):
    fake_ecr.list_images.return_value = {
        "imageIds": [
            {"imageTag": "ag1-aaa"},
            {"imageTag": "ag1-bbb"},
            {"imageTag": "other-ccc"},
        ]
    }
    svc = EcrImageService(repository="agp-agents", client=fake_ecr)

    n = svc.delete_images("ag1")

    assert n == 2
    called = fake_ecr.batch_delete_image.call_args.kwargs["imageIds"]
    assert {"imageTag": "ag1-aaa"} in called
    assert {"imageTag": "ag1-bbb"} in called
    assert {"imageTag": "other-ccc"} not in called


def test_delete_images_returns_zero_when_no_match(fake_ecr):
    fake_ecr.list_images.return_value = {"imageIds": [{"imageTag": "other-ccc"}]}
    svc = EcrImageService(repository="agp-agents", client=fake_ecr)

    assert svc.delete_images("ag1") == 0
    fake_ecr.batch_delete_image.assert_not_called()


def test_delete_images_skips_when_repository_unset(fake_ecr):
    svc = EcrImageService(repository="", client=fake_ecr)

    assert svc.delete_images("ag1") == 0
    fake_ecr.list_images.assert_not_called()


def test_delete_images_idempotent_on_repo_not_found(fake_ecr):
    fake_ecr.list_images.side_effect = _client_error("RepositoryNotFoundException")
    svc = EcrImageService(repository="agp-agents", client=fake_ecr)

    assert svc.delete_images("ag1") == 0  # must not raise


def test_delete_images_raises_on_unexpected_client_error(fake_ecr):
    fake_ecr.list_images.side_effect = _client_error("AccessDeniedException")
    svc = EcrImageService(repository="agp-agents", client=fake_ecr)

    with pytest.raises(EcrImageError) as exc:
        svc.delete_images("ag1")
    assert exc.value.message


def test_delete_images_paginates_on_next_token(fake_ecr):
    fake_ecr.list_images.side_effect = [
        {"imageIds": [{"imageTag": "ag1-aaa"}], "nextToken": "tok"},
        {"imageIds": [{"imageTag": "ag1-bbb"}]},
    ]
    svc = EcrImageService(repository="agp-agents", client=fake_ecr)

    assert svc.delete_images("ag1") == 2
    assert fake_ecr.list_images.call_count == 2


def test_count_images_counts_matching_tag_prefix(fake_ecr):
    fake_ecr.list_images.return_value = {
        "imageIds": [
            {"imageTag": "ag1-aaa"},
            {"imageTag": "ag1-bbb"},
            {"imageTag": "other-ccc"},
        ]
    }
    svc = EcrImageService(repository="agp-agents", client=fake_ecr)

    assert svc.count_images("ag1") == 2
    fake_ecr.batch_delete_image.assert_not_called()  # count is READ-ONLY


def test_count_images_returns_zero_when_no_match(fake_ecr):
    fake_ecr.list_images.return_value = {"imageIds": [{"imageTag": "other-ccc"}]}
    svc = EcrImageService(repository="agp-agents", client=fake_ecr)

    assert svc.count_images("ag1") == 0


def test_count_images_zero_when_repository_unset(fake_ecr):
    svc = EcrImageService(repository="", client=fake_ecr)

    assert svc.count_images("ag1") == 0
    fake_ecr.list_images.assert_not_called()


def test_count_images_idempotent_on_repo_not_found(fake_ecr):
    fake_ecr.list_images.side_effect = _client_error("RepositoryNotFoundException")
    svc = EcrImageService(repository="agp-agents", client=fake_ecr)

    assert svc.count_images("ag1") == 0  # must not raise


def test_repository_uri_reduced_to_name(fake_ecr):
    svc = EcrImageService(
        repository="123.dkr.ecr.us-east-1.amazonaws.com/agp-agents",
        client=fake_ecr,
    )

    svc.delete_images("ag1")

    assert fake_ecr.list_images.call_args.kwargs["repositoryName"] == "agp-agents"
