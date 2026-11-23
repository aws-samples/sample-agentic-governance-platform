"""Guardrail templates: one row per template in ``core.state_store.StateStore`` plus the Bedrock
guardrail each template provisions.

An empty ``table_name`` selects the store's in-memory double; the Bedrock and CloudWatch clients
come from ``core.aws_clients`` on first use. A template is written whole, the item ``_to_item``
serializes plus a storage-only ``row_version``, and read back through ``_from_item``: a create is
``put_new`` at version 1, and every later save is conditional on the version its read saw, so a
save whose read predates another write is ``GuardrailError`` ``changed``. A failed AWS call puts
the template in ``FAILED`` with a fixed status-history message; the cause goes to the log only.
"""

import json
import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import ClassVar, Dict, Iterator, List, Optional

from botocore.exceptions import ClientError

from core import aws_clients
from core.errors import AppError, Outcome
from core.state_store import StateStore, VersionMismatch
from models.guardrail import (
    ContentFilterConfig,
    ContextualGroundingConfig,
    DeniedTopic,
    FilterStrength,
    FilterType,
    GuardrailConfig,
    GuardrailMetrics,
    GuardrailPreset,
    GuardrailStatus,
    GuardrailTemplate,
    GuardrailTemplateCreate,
    GuardrailTemplateUpdate,
    GuardrailTimePoint,
    PiiAction,
    PiiEntityConfig,
    PiiEntityType,
    StatusHistoryEntry,
    WordFilterConfig,
)

logger = logging.getLogger(__name__)

# The PII entity types Bedrock accepts: https://docs.aws.amazon.com/bedrock/latest/APIReference/API_GuardrailPiiEntityConfig.html
_VALID_BEDROCK_PII_TYPES = {
    "NAME",
    "EMAIL",
    "PHONE",
    "ADDRESS",
    "AGE",
    "USERNAME",
    "PASSWORD",
    "DRIVER_ID",
    "LICENSE_PLATE",
    "US_SOCIAL_SECURITY_NUMBER",
    "US_PASSPORT_NUMBER",
    "CREDIT_DEBIT_CARD_NUMBER",
    "CREDIT_DEBIT_CARD_CVV",
    "CREDIT_DEBIT_CARD_EXPIRY",
    "PIN",
    "SWIFT_CODE",
    "INTERNATIONAL_BANK_ACCOUNT_NUMBER",
    "IP_ADDRESS",
    "MAC_ADDRESS",
    "URL",
    "AWS_ACCESS_KEY",
    "AWS_SECRET_KEY",
    "US_BANK_ACCOUNT_NUMBER",
    "US_BANK_ROUTING_NUMBER",
    "CA_HEALTH_NUMBER",
    "CA_SOCIAL_INSURANCE_NUMBER",
    "UK_NATIONAL_HEALTH_SERVICE_NUMBER",
    "UK_NATIONAL_INSURANCE_NUMBER",
    "UK_UNIQUE_TAXPAYER_REFERENCE_NUMBER",
    "US_INDIVIDUAL_TAX_IDENTIFICATION_NUMBER",
    "VEHICLE_IDENTIFICATION_NUMBER",
}
# A denied topic carries at most five examples: https://docs.aws.amazon.com/bedrock/latest/APIReference/API_GuardrailTopicConfig.html
_MAX_TOPIC_EXAMPLES = 5

_METRICS_NAMESPACE = "AWS/Bedrock/Guardrails"


class GuardrailError(AppError):
    """A guardrail route refused a request or its provisioning failed. A missing template and a
    foreign tenant's template are one ``not_found`` body; ``creation_failed`` stands in for the
    Bedrock failure recorded on the template; ``changed`` is a save whose read predates another
    write. Every kind has a fixed detail; the message names the template for the log and never
    carries an AWS response body."""

    domain: ClassVar[str] = "guardrail"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "not_found": Outcome(404, "Guardrail template not found"),
        "creation_failed": Outcome(502, "Guardrail creation failed"),
        "not_publishable": Outcome(404, "Guardrail template not found or has no Bedrock resource"),
        "no_bedrock_resource": Outcome(400, "Guardrail has no Bedrock resource (still in draft)"),
        "changed": Outcome(409, "Guardrail template changed while this request ran; reload and retry"),
    }


FSI_PRESETS: List[GuardrailPreset] = [
    GuardrailPreset(
        id="fsi-standard",
        name="FSI Standard",
        description=(
            "Comprehensive protection for financial services — content filtering, PII detection for "
            "financial data, and profanity filtering."
        ),
        tags=["banking", "insurance", "compliance"],
        config=GuardrailConfig(
            name="FSI Standard Guardrail",
            description="Standard financial services guardrail with content filtering and PII protection",
            content_filters=[
                ContentFilterConfig(
                    type=FilterType.HATE,
                    input_strength=FilterStrength.HIGH,
                    output_strength=FilterStrength.HIGH,
                ),
                ContentFilterConfig(
                    type=FilterType.INSULTS,
                    input_strength=FilterStrength.HIGH,
                    output_strength=FilterStrength.HIGH,
                ),
                ContentFilterConfig(
                    type=FilterType.SEXUAL,
                    input_strength=FilterStrength.HIGH,
                    output_strength=FilterStrength.HIGH,
                ),
                ContentFilterConfig(
                    type=FilterType.VIOLENCE,
                    input_strength=FilterStrength.MEDIUM,
                    output_strength=FilterStrength.MEDIUM,
                ),
                ContentFilterConfig(
                    type=FilterType.MISCONDUCT,
                    input_strength=FilterStrength.HIGH,
                    output_strength=FilterStrength.HIGH,
                ),
                ContentFilterConfig(
                    type=FilterType.PROMPT_ATTACK,
                    input_strength=FilterStrength.HIGH,
                    output_strength=FilterStrength.NONE,
                ),
            ],
            pii_entities=[
                PiiEntityConfig(type=PiiEntityType.CREDIT_DEBIT_CARD_NUMBER, action=PiiAction.ANONYMIZE),
                PiiEntityConfig(type=PiiEntityType.CREDIT_DEBIT_CARD_CVV, action=PiiAction.BLOCK),
                PiiEntityConfig(type=PiiEntityType.SSN, action=PiiAction.ANONYMIZE),
                PiiEntityConfig(
                    type=PiiEntityType.INTERNATIONAL_BANK_ACCOUNT_NUMBER, action=PiiAction.ANONYMIZE
                ),
                PiiEntityConfig(type=PiiEntityType.SWIFT_CODE, action=PiiAction.ANONYMIZE),
            ],
            word_filter=WordFilterConfig(enable_profanity=True, blocked_words=[]),
        ),
    ),
    GuardrailPreset(
        id="market-surveillance",
        name="Market Surveillance",
        description=(
            "Designed for trading and capital markets — blocks insider trading advice and unauthorized "
            "financial recommendations."
        ),
        tags=["trading", "capital-markets", "compliance"],
        config=GuardrailConfig(
            name="Market Surveillance Guardrail",
            description="Guardrail for capital markets with denied topics for trading compliance",
            content_filters=[
                ContentFilterConfig(
                    type=FilterType.HATE,
                    input_strength=FilterStrength.MEDIUM,
                    output_strength=FilterStrength.MEDIUM,
                ),
                ContentFilterConfig(
                    type=FilterType.INSULTS,
                    input_strength=FilterStrength.MEDIUM,
                    output_strength=FilterStrength.MEDIUM,
                ),
                ContentFilterConfig(
                    type=FilterType.MISCONDUCT,
                    input_strength=FilterStrength.HIGH,
                    output_strength=FilterStrength.HIGH,
                ),
                ContentFilterConfig(
                    type=FilterType.PROMPT_ATTACK,
                    input_strength=FilterStrength.HIGH,
                    output_strength=FilterStrength.NONE,
                ),
            ],
            denied_topics=[
                DeniedTopic(
                    name="Insider Trading Advice",
                    definition=(
                        "Any advice or instructions related to trading based on material non-public "
                        "information"
                    ),
                    examples=["Buy stock before the earnings announcement", "I heard they're about to merge"],
                ),
                DeniedTopic(
                    name="Market Manipulation",
                    definition="Strategies or techniques for artificially influencing security prices",
                    examples=["How to pump and dump a stock", "Coordinating trades to move the price"],
                ),
                DeniedTopic(
                    name="Unauthorized Financial Advice",
                    definition="Specific investment recommendations without proper licensing or disclaimers",
                    examples=["You should definitely buy this stock", "Put all your money into crypto"],
                ),
            ],
        ),
    ),
    GuardrailPreset(
        id="customer-service",
        name="Customer Service",
        description=(
            "Moderate filtering with PII anonymization — ideal for customer-facing AI assistants in "
            "banking and insurance."
        ),
        tags=["customer-facing", "banking", "insurance"],
        config=GuardrailConfig(
            name="Customer Service Guardrail",
            description="Balanced guardrail for customer-facing applications with PII anonymization",
            content_filters=[
                ContentFilterConfig(
                    type=FilterType.HATE,
                    input_strength=FilterStrength.MEDIUM,
                    output_strength=FilterStrength.HIGH,
                ),
                ContentFilterConfig(
                    type=FilterType.INSULTS,
                    input_strength=FilterStrength.LOW,
                    output_strength=FilterStrength.HIGH,
                ),
                ContentFilterConfig(
                    type=FilterType.SEXUAL,
                    input_strength=FilterStrength.HIGH,
                    output_strength=FilterStrength.HIGH,
                ),
                ContentFilterConfig(
                    type=FilterType.VIOLENCE,
                    input_strength=FilterStrength.MEDIUM,
                    output_strength=FilterStrength.HIGH,
                ),
                ContentFilterConfig(
                    type=FilterType.MISCONDUCT,
                    input_strength=FilterStrength.MEDIUM,
                    output_strength=FilterStrength.HIGH,
                ),
                ContentFilterConfig(
                    type=FilterType.PROMPT_ATTACK,
                    input_strength=FilterStrength.HIGH,
                    output_strength=FilterStrength.NONE,
                ),
            ],
            pii_entities=[
                PiiEntityConfig(type=PiiEntityType.EMAIL, action=PiiAction.ANONYMIZE),
                PiiEntityConfig(type=PiiEntityType.PHONE, action=PiiAction.ANONYMIZE),
                PiiEntityConfig(type=PiiEntityType.ADDRESS, action=PiiAction.ANONYMIZE),
                PiiEntityConfig(type=PiiEntityType.SSN, action=PiiAction.BLOCK),
                PiiEntityConfig(type=PiiEntityType.CREDIT_DEBIT_CARD_NUMBER, action=PiiAction.BLOCK),
            ],
            word_filter=WordFilterConfig(enable_profanity=True, blocked_words=[]),
            contextual_grounding=ContextualGroundingConfig(
                enabled=True, grounding_threshold=0.7, relevance_threshold=0.7
            ),
        ),
    ),
]


def _content_policy(template: GuardrailTemplate) -> Optional[dict]:
    if not template.content_filters:
        return None
    filters = []
    for cf in template.content_filters:
        entry = {
            "type": cf.type.value,
            "inputStrength": cf.input_strength.value,
            "outputStrength": cf.output_strength.value,
        }
        # PROMPT_ATTACK takes no output strength: https://docs.aws.amazon.com/bedrock/latest/APIReference/API_GuardrailContentFilterConfig.html
        if cf.type == FilterType.PROMPT_ATTACK:
            entry["outputStrength"] = "NONE"
        filters.append(entry)
    return {"filtersConfig": filters}


def _topic_policy(template: GuardrailTemplate) -> Optional[dict]:
    if not template.denied_topics:
        return None
    topics = []
    for topic in template.denied_topics:
        entry: dict = {"name": topic.name, "definition": topic.definition, "type": "DENY"}
        if topic.examples:
            entry["examples"] = topic.examples[:_MAX_TOPIC_EXAMPLES]
        topics.append(entry)
    return {"topicsConfig": topics}


def _sensitive_information_policy(template: GuardrailTemplate) -> Optional[dict]:
    pii_config = []
    for entity in template.pii_entities or []:
        if entity.type.value not in _VALID_BEDROCK_PII_TYPES:
            logger.warning(f"Skipping invalid PII type for Bedrock API: {entity.type.value}")
            continue
        pii_config.append({"type": entity.type.value, "action": entity.action.value})

    regex_config = []
    for regex in template.sensitive_regexes or []:
        entry = {"name": regex.name, "pattern": regex.pattern, "action": regex.action.value}
        if regex.description:
            entry["description"] = regex.description
        regex_config.append(entry)

    if not (pii_config or regex_config):
        return None
    policy: Dict = {}
    if pii_config:
        policy["piiEntitiesConfig"] = pii_config
    if regex_config:
        policy["regexesConfig"] = regex_config
    return policy


def _word_policy(template: GuardrailTemplate) -> Optional[dict]:
    if not template.word_filter:
        return None
    policy: Dict = {}
    if template.word_filter.enable_profanity:
        policy["managedWordListsConfig"] = [{"type": "PROFANITY"}]
    if template.word_filter.blocked_words:
        policy["wordsConfig"] = [{"text": w} for w in template.word_filter.blocked_words]
    return policy or None


def _contextual_grounding_policy(template: GuardrailTemplate) -> Optional[dict]:
    grounding = template.contextual_grounding
    if not (grounding and grounding.enabled):
        return None
    return {
        "filtersConfig": [
            {"type": "GROUNDING", "threshold": grounding.grounding_threshold},
            {"type": "RELEVANCE", "threshold": grounding.relevance_threshold},
        ]
    }


def _metric_query(qid: str, metric_name: str, dimensions: list) -> dict:
    return {
        "Id": qid,
        "MetricStat": {
            "Metric": {"Namespace": _METRICS_NAMESPACE, "MetricName": metric_name, "Dimensions": dimensions},
            "Period": 3600,
            "Stat": "Sum",
        },
        "ReturnData": True,
    }


def _add_metric_result(metrics: GuardrailMetrics, result: dict) -> None:
    points = zip(result.get("Timestamps", []), result.get("Values", []), strict=False)
    if result.get("Id") == "invocations":
        for ts, value in points:
            metrics.total_invocations += int(value)
            metrics.time_series.append(GuardrailTimePoint(timestamp=ts.isoformat(), invocations=int(value)))
    elif result.get("Id") == "intervened":
        for _, value in points:
            metrics.blocked_count += int(value)


class GuardrailService:
    def __init__(self, table_name: str = "", region: str | None = None):
        self.table_name = table_name
        self.region = region
        self._store = StateStore(table_name, region=region)
        self._bedrock_client = None
        self._cloudwatch_client = None

    @property
    def bedrock_client(self):
        if self._bedrock_client is None:
            self._bedrock_client = aws_clients.client("bedrock", region=self.region)
        return self._bedrock_client

    @property
    def cloudwatch_client(self):
        if self._cloudwatch_client is None:
            self._cloudwatch_client = aws_clients.client("cloudwatch", region=self.region)
        return self._cloudwatch_client

    def _save(self, template: GuardrailTemplate) -> None:
        version = (template._row_version or 0) + 1
        item = {**self._to_item(template), "row_version": version}
        try:
            self._store.put_if_version(item, "row_version", template._row_version)
        except VersionMismatch:
            raise GuardrailError(
                f"guardrail template {template.template_id} changed", kind="changed"
            ) from None
        template._row_version = version

    def _get(self, template_id: str, *, consistent: bool = False) -> Optional[dict]:
        return self._store.get((f"GUARDRAIL#{template_id}", "META"), consistent=consistent)

    def _scan(self, status: Optional[GuardrailStatus] = None) -> List[dict]:
        items = self._store.scan_prefix("GUARDRAIL#")
        if status:
            items = [item for item in items if item.get("status") == status.value]
        return items

    def _to_item(self, template: GuardrailTemplate) -> dict:
        data = template.model_dump()
        data["pk"] = f"GUARDRAIL#{template.template_id}"
        data["sk"] = "META"
        return json.loads(json.dumps(data, default=str), parse_float=Decimal)

    def _from_item(self, item: dict) -> GuardrailTemplate:
        item.pop("pk", None)
        item.pop("sk", None)
        template = GuardrailTemplate(**item)
        version = item.get("row_version")
        template._row_version = None if version is None else int(version)
        return template

    def _add_status(self, template: GuardrailTemplate, status: GuardrailStatus, message: str = ""):
        now = datetime.utcnow().isoformat()
        template.status = status
        template.updated_at = now
        template.status_history.append(
            StatusHistoryEntry(status=status.value, timestamp=now, message=message)
        )

    def create_template(self, req: GuardrailTemplateCreate, created_by: str = "system") -> GuardrailTemplate:
        template = GuardrailTemplate(
            name=req.name,
            description=req.description,
            content_filters=req.content_filters,
            denied_topics=req.denied_topics,
            pii_entities=req.pii_entities,
            sensitive_regexes=req.sensitive_regexes,
            word_filter=req.word_filter,
            contextual_grounding=req.contextual_grounding,
            tenant_id=req.tenant_id,
            created_by=created_by,
        )
        self._add_status(template, GuardrailStatus.CREATING, "Creating Bedrock guardrail")

        try:
            result = self._create_bedrock_guardrail(template)
            template.guardrail_id = result["guardrailId"]
            template.guardrail_arn = result["guardrailArn"]
            template.guardrail_version = result.get("version", "DRAFT")
            self._add_status(template, GuardrailStatus.ACTIVE, "Guardrail created successfully")
        except Exception:
            logger.exception("Failed to create Bedrock guardrail for template %s", template.template_id)
            self._add_status(template, GuardrailStatus.FAILED, "Bedrock API error")

        if not self._store.put_new({**self._to_item(template), "row_version": 1}):
            raise RuntimeError(f"guardrail template {template.template_id} already exists")
        template._row_version = 1
        logger.info(f"Created guardrail template {template.template_id} (bedrock_id={template.guardrail_id})")
        return template

    def get_template(self, template_id: str, *, consistent: bool = False) -> Optional[GuardrailTemplate]:
        item = self._get(template_id, consistent=consistent)
        if not item:
            return None
        return self._from_item(item)

    def list_templates(self, status: Optional[GuardrailStatus] = None) -> List[GuardrailTemplate]:
        templates = [self._from_item(item) for item in self._scan(status=status)]
        templates.sort(key=lambda t: t.created_at, reverse=True)
        return templates

    def update_template(self, template_id: str, req: GuardrailTemplateUpdate) -> Optional[GuardrailTemplate]:
        template = self.get_template(template_id, consistent=True)
        if not template:
            return None

        update_data = req.model_dump(exclude_none=True)
        for field, value in update_data.items():
            setattr(template, field, value)

        if template.guardrail_id and template.status == GuardrailStatus.ACTIVE:
            self._add_status(template, GuardrailStatus.UPDATING, "Updating Bedrock guardrail")
            try:
                self._update_bedrock_guardrail(template)
                self._add_status(template, GuardrailStatus.ACTIVE, "Guardrail updated successfully")
            except Exception:
                logger.exception("Failed to update Bedrock guardrail for template %s", template.template_id)
                self._add_status(template, GuardrailStatus.FAILED, "Update error")

        self._save(template)
        return template

    def delete_template(self, template_id: str) -> Optional[GuardrailTemplate]:
        template = self.get_template(template_id, consistent=True)
        if not template:
            return None

        if template.guardrail_id:
            self._add_status(template, GuardrailStatus.DELETING, "Deleting Bedrock guardrail")
            try:
                self._delete_bedrock_guardrail(template.guardrail_id)
                self._add_status(template, GuardrailStatus.DELETED, "Guardrail deleted")
            except Exception:
                logger.exception(
                    "Failed to delete Bedrock guardrail %s for template %s",
                    template.guardrail_id,
                    template.template_id,
                )
                self._add_status(template, GuardrailStatus.FAILED, "Delete error")
        else:
            self._add_status(template, GuardrailStatus.DELETED, "Draft deleted")

        self._save(template)
        return template

    def publish_version(self, template_id: str) -> Optional[GuardrailTemplate]:
        template = self.get_template(template_id, consistent=True)
        if not template or not template.guardrail_id:
            return None

        try:
            resp = self.bedrock_client.create_guardrail_version(
                guardrailIdentifier=template.guardrail_id,
                description=f"Published from AGP control plane at {datetime.utcnow().isoformat()}",
            )
            template.guardrail_version = resp.get("version", template.guardrail_version)
            self._add_status(
                template, GuardrailStatus.ACTIVE, f"Published version {template.guardrail_version}"
            )
        except Exception:
            logger.exception(
                "Failed to publish Bedrock guardrail %s for template %s",
                template.guardrail_id,
                template.template_id,
            )
            self._add_status(template, GuardrailStatus.FAILED, "Publish error")

        self._save(template)
        return template

    def _build_bedrock_params(self, template: GuardrailTemplate) -> dict:
        """The ``create_guardrail`` request: the operator-facing name and messaging, then one
        policy block per configured section."""
        params: Dict = {
            "name": f"agp-{template.template_id[:8]}-{template.name[:30].replace(' ', '-').lower()}",
            "description": template.description or f"AGP Guardrail: {template.name}",
            "blockedInputMessaging": (
                "Your request was blocked by the guardrail policy. Please rephrase your input."
            ),
            "blockedOutputsMessaging": (
                "The response was blocked by the guardrail policy as it may contain restricted content."
            ),
        }
        policies = {
            "contentPolicyConfig": _content_policy(template),
            "topicPolicyConfig": _topic_policy(template),
            "sensitiveInformationPolicyConfig": _sensitive_information_policy(template),
            "wordPolicyConfig": _word_policy(template),
            "contextualGroundingPolicyConfig": _contextual_grounding_policy(template),
        }
        params.update({key: policy for key, policy in policies.items() if policy is not None})
        return params

    def _create_bedrock_guardrail(self, template: GuardrailTemplate) -> dict:
        params = self._build_bedrock_params(template)
        resp = self.bedrock_client.create_guardrail(**params)
        return resp

    def _update_bedrock_guardrail(self, template: GuardrailTemplate):
        """``update_guardrail`` with the same policies; the name is set at creation and not
        resent."""
        params = self._build_bedrock_params(template)
        params["guardrailIdentifier"] = template.guardrail_id
        params.pop("name", None)
        self.bedrock_client.update_guardrail(**params)

    def _delete_bedrock_guardrail(self, guardrail_id: str):
        try:
            self.bedrock_client.delete_guardrail(guardrailIdentifier=guardrail_id)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise

    def _metric_data_pages(
        self, dimensions: list, start_time: datetime, end_time: datetime
    ) -> Iterator[dict]:
        kwargs = {
            "MetricDataQueries": [
                _metric_query("invocations", "Invocations", dimensions),
                _metric_query("intervened", "InvocationsIntervened", dimensions),
            ],
            "StartTime": start_time,
            "EndTime": end_time,
        }
        while True:
            resp = self.cloudwatch_client.get_metric_data(**kwargs)
            yield from resp.get("MetricDataResults", [])
            if not resp.get("NextToken"):
                return
            kwargs["NextToken"] = resp["NextToken"]

    def get_metrics(
        self,
        guardrail_id: str,
        hours: int = 24,
        guardrail_arn: Optional[str] = None,
        guardrail_version: str = "DRAFT",
    ) -> GuardrailMetrics:
        """Guardrail metrics from CloudWatch, through ``GetMetricData``, the one CloudWatch action the
        task role grants (``GetMetricStatistics`` is denied and read as zero).

        ``AWS/Bedrock/Guardrails`` publishes ``Invocations`` and ``InvocationsIntervened`` keyed by
        ``GuardrailArn`` and ``GuardrailVersion``. A CloudWatch failure reads as zero.
        """
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(hours=hours)

        metrics = GuardrailMetrics(guardrail_id=guardrail_id)
        dimensions = [
            {"Name": "GuardrailArn", "Value": guardrail_arn or guardrail_id},
            {"Name": "GuardrailVersion", "Value": guardrail_version or "DRAFT"},
        ]

        try:
            for result in self._metric_data_pages(dimensions, start_time, end_time):
                _add_metric_result(metrics, result)
            metrics.allowed_count = metrics.total_invocations - metrics.blocked_count
            if metrics.total_invocations > 0:
                metrics.block_rate = round(metrics.blocked_count / metrics.total_invocations * 100, 1)
            metrics.time_series.sort(key=lambda point: point.timestamp)
        except Exception:
            logger.exception("Failed to fetch CloudWatch metrics for guardrail %s", guardrail_id)

        return metrics

    def get_presets(self) -> List[GuardrailPreset]:
        return FSI_PRESETS
