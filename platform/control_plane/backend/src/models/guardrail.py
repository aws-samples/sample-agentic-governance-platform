"""Guardrail template models: the policy shape, its create and update payloads, the stored template,
presets, and the event and metrics read models."""

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field, PrivateAttr

from models.base import WireModel


class GuardrailStatus(str, Enum):
    DRAFT = "draft"
    CREATING = "creating"
    ACTIVE = "active"
    UPDATING = "updating"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"


class FilterStrength(str, Enum):
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class FilterType(str, Enum):
    HATE = "HATE"
    INSULTS = "INSULTS"
    SEXUAL = "SEXUAL"
    VIOLENCE = "VIOLENCE"
    MISCONDUCT = "MISCONDUCT"
    PROMPT_ATTACK = "PROMPT_ATTACK"


class PiiAction(str, Enum):
    BLOCK = "BLOCK"
    ANONYMIZE = "ANONYMIZE"


class PiiEntityType(str, Enum):
    """The Bedrock PII entity types a template may name. ``DATE_TIME`` and ``PASSPORT_NUMBER`` are
    legacy members kept so stored records validate; the guardrail service filters them out before a
    Bedrock call."""

    CREDIT_DEBIT_CARD_NUMBER = "CREDIT_DEBIT_CARD_NUMBER"
    CREDIT_DEBIT_CARD_CVV = "CREDIT_DEBIT_CARD_CVV"
    CREDIT_DEBIT_CARD_EXPIRY = "CREDIT_DEBIT_CARD_EXPIRY"
    PIN = "PIN"
    SWIFT_CODE = "SWIFT_CODE"
    INTERNATIONAL_BANK_ACCOUNT_NUMBER = "INTERNATIONAL_BANK_ACCOUNT_NUMBER"
    NAME = "NAME"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    ADDRESS = "ADDRESS"
    AGE = "AGE"
    SSN = "US_SOCIAL_SECURITY_NUMBER"
    US_PASSPORT_NUMBER = "US_PASSPORT_NUMBER"
    DRIVER_ID = "DRIVER_ID"
    LICENSE_PLATE = "LICENSE_PLATE"
    DATE_TIME = "DATE_TIME"
    PASSPORT_NUMBER = "PASSPORT_NUMBER"
    IP_ADDRESS = "IP_ADDRESS"
    MAC_ADDRESS = "MAC_ADDRESS"
    URL = "URL"
    USERNAME = "USERNAME"
    PASSWORD = "PASSWORD"  # noqa: S105 — a PII entity-type name, not a credential; expires 2027-10-23; added E49A-50A/T3
    AWS_ACCESS_KEY = "AWS_ACCESS_KEY"
    AWS_SECRET_KEY = "AWS_SECRET_KEY"  # noqa: S105 — a PII entity-type name, not a credential; expires 2027-10-23; added E49A-50A/T3


class ContentFilterConfig(WireModel):
    type: FilterType
    input_strength: FilterStrength = FilterStrength.MEDIUM
    output_strength: FilterStrength = FilterStrength.MEDIUM


class DeniedTopic(WireModel):
    name: str = Field(..., min_length=1, max_length=100)
    definition: str = Field(..., min_length=1, max_length=500)
    examples: List[str] = Field(default_factory=list)


class PiiEntityConfig(WireModel):
    type: PiiEntityType
    action: PiiAction = PiiAction.ANONYMIZE


class SensitiveRegexConfig(WireModel):
    name: str = Field(..., min_length=1, max_length=100)
    pattern: str = Field(..., min_length=1)
    description: Optional[str] = None
    action: PiiAction = PiiAction.BLOCK


class WordFilterConfig(WireModel):
    enable_profanity: bool = True
    blocked_words: List[str] = Field(default_factory=list)


class ContextualGroundingConfig(WireModel):
    enabled: bool = False
    grounding_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    relevance_threshold: float = Field(default=0.7, ge=0.0, le=1.0)


class GuardrailConfig(WireModel):
    """The policy shape of a guardrail, with no tenant dimension: a preset is a catalog entry, so
    ``GuardrailPreset.config`` stays constructible without a tenant, while ``tenant_id`` is required to
    create a template."""

    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    content_filters: List[ContentFilterConfig] = Field(default_factory=list)
    denied_topics: List[DeniedTopic] = Field(default_factory=list)
    pii_entities: List[PiiEntityConfig] = Field(default_factory=list)
    sensitive_regexes: List[SensitiveRegexConfig] = Field(default_factory=list)
    word_filter: Optional[WordFilterConfig] = None
    contextual_grounding: Optional[ContextualGroundingConfig] = None


class GuardrailTemplateCreate(GuardrailConfig):
    """Create payload. ``tenant_id`` is required on a new template, the same rule ``AgentCreate`` and
    ``McpServerCreate`` carry."""

    tenant_id: str = Field(..., min_length=1)


class GuardrailTemplateUpdate(WireModel):
    name: Optional[str] = Field(default=None, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    content_filters: Optional[List[ContentFilterConfig]] = None
    denied_topics: Optional[List[DeniedTopic]] = None
    pii_entities: Optional[List[PiiEntityConfig]] = None
    sensitive_regexes: Optional[List[SensitiveRegexConfig]] = None
    word_filter: Optional[WordFilterConfig] = None
    contextual_grounding: Optional[ContextualGroundingConfig] = None


class StatusHistoryEntry(WireModel):
    status: str
    timestamp: str
    message: Optional[str] = None


class GuardrailTemplate(WireModel):
    template_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: Optional[str] = None
    status: GuardrailStatus = GuardrailStatus.DRAFT
    guardrail_id: Optional[str] = None
    guardrail_arn: Optional[str] = None
    guardrail_version: Optional[str] = None
    content_filters: List[ContentFilterConfig] = Field(default_factory=list)
    denied_topics: List[DeniedTopic] = Field(default_factory=list)
    pii_entities: List[PiiEntityConfig] = Field(default_factory=list)
    sensitive_regexes: List[SensitiveRegexConfig] = Field(default_factory=list)
    word_filter: Optional[WordFilterConfig] = None
    contextual_grounding: Optional[ContextualGroundingConfig] = None
    status_history: List[StatusHistoryEntry] = Field(default_factory=list)
    tenant_id: Optional[str] = Field(
        default=None, description="The owning tenant; optional on stored records, required on create"
    )
    created_by: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    _row_version: int | None = PrivateAttr(default=None)


class GuardrailPreset(WireModel):
    id: str
    name: str
    description: str
    tags: List[str] = Field(default_factory=list)
    config: GuardrailConfig


class GuardrailEvent(WireModel):
    timestamp: str
    guardrail_id: str
    guardrail_name: Optional[str] = None
    action: str = Field(description="BLOCKED, ALLOWED or ANONYMIZED")
    filter_type: Optional[str] = None
    input_snippet: Optional[str] = None
    details: Optional[Dict[str, Any]] = None


class GuardrailTimePoint(WireModel):
    timestamp: str
    invocations: int


class GuardrailMetrics(WireModel):
    guardrail_id: str
    total_invocations: int = 0
    blocked_count: int = 0
    allowed_count: int = 0
    anonymized_count: int = 0
    block_rate: float = 0.0
    top_triggered_filter: Optional[str] = None
    filter_breakdown: Dict[str, int] = Field(default_factory=dict)
    time_series: List[GuardrailTimePoint] = Field(default_factory=list)
    recent_events: List[GuardrailEvent] = Field(default_factory=list)
