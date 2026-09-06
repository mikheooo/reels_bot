from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.worker.connectors import RateLimitInfo
from app.worker.content_package import OutputVariantType, TargetPlatform


class AuditTargetStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    ACTIVE = "ACTIVE"
    VERIFIED = "VERIFIED"
    MODIFIED = "MODIFIED"
    DELETED_OR_NOT_FOUND = "DELETED_OR_NOT_FOUND"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
    NOT_APPLICABLE_NOT_CONFIGURED = "NOT_APPLICABLE_NOT_CONFIGURED"
    NOT_APPLICABLE_MANUAL_EXPORT = "NOT_APPLICABLE_MANUAL_EXPORT"
    DELETION_VERIFICATION_UNSUPPORTED = "DELETION_VERIFICATION_UNSUPPORTED"
    DEFERRED_LEGACY = "DEFERRED_LEGACY"
    FAILED = "FAILED"


class AuditResultStatus(str, Enum):
    VERIFIED = "VERIFIED"
    MODIFIED = "MODIFIED"
    DELETED_OR_NOT_FOUND = "DELETED_OR_NOT_FOUND"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"


class NormalizedMetrics(BaseModel):
    views: int | None = None
    likes: int | None = None
    replies: int | None = None
    reposts: int | None = None
    quotes: int | None = None
    bookmarks: int | None = None


class MetricDelta(BaseModel):
    previous: int | None = None
    current: int | None = None
    absolute_delta: int | None = None
    percentage_delta: float | None = None


class AuditPolicy(BaseModel):
    intervals_seconds: list[int] = Field(
        default_factory=lambda: [
            900,       # 15 minutes
            7200,      # 2 hours
            43200,     # 12 hours
            86400,     # 24 hours (1 day)
            259200,    # 72 hours (3 days)
            604800,    # 7 days
        ]
    )
    max_retries_transient: int = 3
    transient_retry_backoff_seconds: int = 300  # 5 min fallback


class AuditTarget(BaseModel):
    audit_id: str
    package_id: str
    delivery_id: str
    target: TargetPlatform
    variant: OutputVariantType | None = None
    provider_post_id: str
    provider_url: str | None = None
    publication_key: str
    approved_payload_hash: str
    approved_payload_text: str
    status: AuditTargetStatus = AuditTargetStatus.SCHEDULED
    tier: int = 0
    next_audit_at: datetime | None = None
    last_checked_at: datetime | None = None
    last_result_status: AuditResultStatus | None = None
    last_error_code: str | None = None
    attempt_count: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AuditResult(BaseModel):
    status: AuditResultStatus
    checked_at: datetime
    object_exists: bool
    content_match: bool | None = None
    current_content_hash: str | None = None
    approved_content_hash: str | None = None
    modified: bool = False
    provider_error: str | None = None
    provider_http_status: int | None = None
    raw_metrics: dict[str, Any] = Field(default_factory=dict)
    normalized_metrics: NormalizedMetrics = Field(default_factory=NormalizedMetrics)
    metrics_available: bool = False
    metric_deltas: dict[str, MetricDelta] = Field(default_factory=dict)
    next_audit_at: datetime | None = None
    scheduled_for: datetime | None = None
    occurrence_key: str | None = None
    rate_limit_info: RateLimitInfo | None = None
    latency_ms: int = 0


class AuditSnapshot(BaseModel):
    snapshot_id: str
    audit_id: str
    occurrence_key: str
    scheduled_for: datetime | None = None
    checked_at: datetime
    object_exists: bool
    content_hash: str | None = None
    content_match: bool | None = None
    raw_metrics: dict[str, Any] = Field(default_factory=dict)
    normalized_metrics: NormalizedMetrics = Field(default_factory=NormalizedMetrics)
    metric_deltas: dict[str, MetricDelta] = Field(default_factory=dict)
    provider_http_status: int | None = None
    latency_ms: int = 0
    status: AuditResultStatus
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TelegramEditEvent(BaseModel):
    event_type: str = "EDIT_OBSERVED"
    channel_id: str
    message_id: int
    observed_at: datetime
    previous_hash: str | None = None
    new_hash: str
    payload_text: str | None = None
