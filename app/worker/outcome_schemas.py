"""Typed schemas and contracts for Outcome Learning Dataset & Prioritization Calibration v1."""

from __future__ import annotations

import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.worker.content_package import OutputVariantType, TargetPlatform
from app.worker.priority_policy import PriorityTier


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


class AuditHorizon(str, Enum):
    """Canonical audit horizons. Outcomes of different horizons must never be mixed."""

    H_15M = "15m"
    H_2H = "2h"
    H_12H = "12h"
    H_24H = "24h"
    H_3D = "3d"
    H_7D = "7d"


class ContentIntegrityStatus(str, Enum):
    """Observation-level content integrity reflecting external verification."""

    MATCH = "MATCH"
    MODIFIED = "MODIFIED"
    DELETED_OR_NOT_FOUND = "DELETED_OR_NOT_FOUND"
    UNKNOWN = "UNKNOWN"


class DataQualityStatus(str, Enum):
    """Observation data quality status governing calibration eligibility."""

    VALID = "VALID"
    PARTIAL = "PARTIAL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    AUTH_UNAVAILABLE = "AUTH_UNAVAILABLE"
    CONTENT_MODIFIED = "CONTENT_MODIFIED"
    CONTENT_DELETED = "CONTENT_DELETED"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class CalibrationReadiness(str, Enum):
    """Evaluation dataset readiness for policy calibration."""

    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    OBSERVATION_ONLY = "OBSERVATION_ONLY"
    CALIBRATION_READY = "CALIBRATION_READY"


class OutcomeDerivedMetrics(BaseModel):
    """Normalized, platform-derived metrics.

    Never overwrite raw provider metrics. Denominators must be known before computing rates.
    """

    views: int | None = None
    likes: int | None = None
    replies: int | None = None
    reposts: int | None = None
    quotes: int | None = None
    bookmarks: int | None = None
    engagement_total: int | None = None
    engagement_rate: float | None = None
    delta_from_previous: dict[str, Any] | None = None
    velocity_views_per_hour: float | None = None
    velocity_engagement_per_hour: float | None = None


class OutcomeObservation(BaseModel):
    """Canonical typed contract tying an audit outcome back to original job and decision lineage."""

    outcome_id: str
    job_id: str
    package_id: str
    publication_key: str
    target: TargetPlatform
    variant_type: OutputVariantType
    content_type: str | None = None
    router_primary_category: str | None = None
    router_risk_level: str | None = None
    priority_score: float | None = None
    priority_band: PriorityTier | None = None
    language_code: str | None = None
    published_at: datetime.datetime | None = None
    audit_horizon: AuditHorizon
    audit_snapshot_id: str
    # Source provider metrics must remain immutable
    metrics: dict[str, Any] = Field(default_factory=dict)
    # Derived metrics calculated strictly separately
    derived_metrics: OutcomeDerivedMetrics = Field(default_factory=OutcomeDerivedMetrics)
    content_integrity_status: ContentIntegrityStatus = ContentIntegrityStatus.UNKNOWN
    data_quality_status: DataQualityStatus = DataQualityStatus.INSUFFICIENT_DATA
    created_at: datetime.datetime = Field(default_factory=_utc_now)


class DataSufficiencyPolicy(BaseModel):
    """Policy governing sample size requirements before emitting calibration recommendations."""

    min_samples_for_readiness: int = 20
    min_samples_per_cohort: int = 5
    min_distinct_packages: int = 5
    min_hours_between_runs: float = 1.0


class CalibrationRecommendation(BaseModel):
    """Shadow calibration suggestion. Must NEVER be applied automatically."""

    recommendation_id: str
    platform: TargetPlatform
    horizon: AuditHorizon
    category: str | None = None
    current_threshold: float
    suggested_threshold: float
    evidence_sample_size: int
    confidence: float = Field(ge=0.0, le=1.0)
    estimated_tradeoff: str
    reason: str
    # Invariant: Must remain False in this stage
    applied: Literal[False] = False


class CalibrationRun(BaseModel):
    """Record of a calibration evaluation run executed in observation mode."""

    run_id: str
    started_at: datetime.datetime
    completed_at: datetime.datetime
    readiness: CalibrationReadiness
    sample_count: int
    valid_sample_count: int
    recommendations: list[CalibrationRecommendation] = Field(default_factory=list)
    # Invariant: Must remain 0 in this stage
    applied_recommendation_count: Literal[0] = 0
    metrics_summary: dict[str, Any] = Field(default_factory=dict)
