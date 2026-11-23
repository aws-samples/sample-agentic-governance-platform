"""Langfuse mirror for eval runs.

Pushes an eval run's dataset items and per-item scores into the agent's OWN Langfuse project
(one agent = one project = one key), so a Langfuse user sees the golden dataset and the scores
next to the traces they belong to.

OPTIONAL MIRROR: the run record of truth is S3 + DynamoDB (``eval_store``). ``mirror_run``
never raises; it returns ``False`` when the agent has no Langfuse key, Langfuse is not
configured, or any call fails, and the runner carries on.

Endpoints (Langfuse public REST, https://cloud.langfuse.com/generated/api/openapi.yml):
  - ``POST /api/public/v2/datasets`` ``{name}`` — dataset create (the v1 path is deprecated).
    Re-creating an existing dataset is fine (200 upsert, or 409 on older servers).
  - ``POST /api/public/dataset-items`` ``{datasetName, id, input, expectedOutput, metadata}`` —
    upserted on ``id``, which must be unique per project (hence the ``repo_id:`` prefix).
  - ``POST /api/public/scores`` ``{sessionId, name, value, comment, dataType}`` — a session
    score; results without a ``session_id`` have no target and are skipped.
Auth is HTTP Basic with the agent's project key. Key values are never logged.
"""

from __future__ import annotations

import logging

import httpx

from core.config import settings
from models.eval import DatasetItem, EvalRun, ItemResult
from services.langfuse_metrics_service import LangfuseMetricsService

logger = logging.getLogger(__name__)

_DATASETS_PATH = "/api/public/v2/datasets"
_DATASET_ITEMS_PATH = "/api/public/dataset-items"
_SCORES_PATH = "/api/public/scores"
_HTTP_TIMEOUT_SECONDS = 10.0


def _load_keys(secret_name: str) -> tuple[str, str]:
    """The agent's ``(public_key, secret_key)`` via the metrics service's Secrets Manager loader."""
    svc = LangfuseMetricsService(
        langfuse_host=settings.LANGFUSE_HOST,
        region=settings.AGENT_REGISTRY_REGION or "us-east-1",
    )
    return svc._load_keys(secret_name)


def mirror_run(
    agent, run: EvalRun, items: list[DatasetItem], results: list[ItemResult], *, http=None
) -> bool:
    """Mirror ``items`` and the scored ``results`` of ``run`` into the agent's Langfuse project.

    Returns ``True`` when every call succeeded. Never raises."""
    secret_name = getattr(agent, "langfuse_key_secret_name", None)
    host = (settings.LANGFUSE_HOST or "").rstrip("/")
    if not secret_name or not host:
        return False

    client = http or httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS)
    try:
        _post_run(client, host, httpx.BasicAuth(*_load_keys(secret_name)), run, items, results)
    except Exception:
        logger.warning(
            "[eval_langfuse] mirror of run %s for agent %s failed — skipped",
            run.run_id,
            getattr(agent, "id", "?"),
            exc_info=True,
        )
        return False
    finally:
        if http is None:
            client.close()
    return True


def _post_run(
    client,
    host: str,
    auth: httpx.BasicAuth,
    run: EvalRun,
    items: list[DatasetItem],
    results: list[ItemResult],
) -> None:
    """The dataset, its items, then one score per (session, scored evaluator); a non-2xx raises."""

    def post(path: str, body: dict, ok: tuple[int, ...] = (200, 201)) -> None:
        resp = client.post(f"{host}{path}", json=body, auth=auth)
        if resp.status_code not in ok:
            raise RuntimeError(f"{path} returned status {resp.status_code}")

    dataset = f"agp-{run.repo_id}"
    post(_DATASETS_PATH, {"name": dataset}, ok=(200, 201, 409))
    for item in items:
        post(
            _DATASET_ITEMS_PATH,
            {
                "datasetName": dataset,
                "id": f"{run.repo_id}:{item.id}",
                "input": item.input,
                "expectedOutput": item.expected_output,
                "metadata": {
                    "assertions": item.assertions,
                    "expected_tools": [t.model_dump() for t in item.expected_tools],
                    "tags": item.tags,
                },
            },
        )
    for result in results:
        if not result.session_id:
            continue
        for score in result.scores:
            if score.value is None:
                continue
            post(
                _SCORES_PATH,
                {
                    "sessionId": result.session_id,
                    "name": score.evaluator,
                    "value": score.value,
                    "comment": score.explanation,
                    "dataType": "NUMERIC",
                },
            )
