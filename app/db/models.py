from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.sql import func

from app.db.database import Base


class JobStatus:
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    PARTIAL = "PARTIAL"
    ERROR = "ERROR"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"

class Job(Base):
    __tablename__ = "jobs"
    id = Column(String, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    original_url = Column(String, nullable=False)
    url_hash = Column(String, index=True, nullable=False)
    status = Column(String, default=JobStatus.QUEUED, nullable=False)
    error_text = Column(String, nullable=True)
    tg_file_id = Column(String, nullable=True)
    analysis_text = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    # Set to now() when the job moves to PROCESSING. The reaper needs it:
    # created_at alone is unsafe, because a job can sit in QUEUED for hours
    # behind other jobs before it ever starts.
    started_at = Column(DateTime, nullable=True)
    tg_channel_message_id = Column(BigInteger, nullable=True)
    tg_user_message_id = Column(BigInteger, nullable=True)
    qa_reasons = Column(JSON, nullable=True)
    audit_scheduled_at = Column(DateTime, nullable=True)
    # Progress UX: single Telegram status message edited by the worker.
    # Both nullable so old rows keep working (no progress shown for them).
    tg_progress_chat_id = Column(BigInteger, nullable=True)
    tg_progress_message_id = Column(BigInteger, nullable=True)
    # Canonical immutable speech-to-text result. Written once right after
    # transcription succeeds, never overwritten by downstream stages.
    # TEXT = no character limit. NULL for jobs processed before this feature.
    full_transcript = Column(Text, nullable=True)
    transcription_model = Column(String, nullable=True)
    transcription_fallback_used = Column(String, nullable=True)
    transcription_status = Column(String, nullable=True)
    # Structured delivery outcome for channel, plan and Task side effects.
    delivery_status = Column(JSON, nullable=True)
    # Post-Publish Audit is currently deferred, including legacy due rows.
    audit_state = Column(String, nullable=True)

class Task(Base):
    __tablename__ = "tasks"
    id = Column(String, primary_key=True)
    job_id = Column(String, nullable=True)
    user_id = Column(BigInteger, nullable=False)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String, default='PENDING')  # PENDING / IN_PROGRESS / DONE
    created_at = Column(DateTime, server_default=func.now())
    completed_at = Column(DateTime, nullable=True)


class ContentPackageModel(Base):
    __tablename__ = "content_packages"

    id = Column(String, primary_key=True)
    job_id = Column(String, ForeignKey("jobs.id"), nullable=False, index=True)
    source_url = Column(String, nullable=False)
    contract_version = Column(String, default="content_package_v1", nullable=False)
    status = Column(String, default="GENERATED", nullable=False)
    language_context = Column(JSON, nullable=True)
    router_result = Column(JSON, nullable=True)
    priority_result = Column(JSON, nullable=True)
    canonical_content = Column(JSON, nullable=False)
    output_variants = Column(JSON, nullable=False)
    distribution_targets = Column(JSON, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class ContentDeliveryModel(Base):
    __tablename__ = "content_deliveries"

    id = Column(String, primary_key=True)
    package_id = Column(String, ForeignKey("content_packages.id"), nullable=False, index=True)
    target = Column(String, nullable=False)
    variant = Column(String, nullable=False)
    attempt_id = Column(Integer, default=1, nullable=False)
    status = Column(String, nullable=False)
    external_id = Column(String, nullable=True)
    idempotency_key = Column(String, unique=True, nullable=False)
    error_code = Column(String, nullable=True)
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime, server_default=func.now())
    finished_at = Column(DateTime, nullable=True)
    publication_key = Column(String, nullable=True, index=True)
    payload_hash = Column(String, nullable=True)
    provider_post_id = Column(String, nullable=True)
    provider_url = Column(String, nullable=True)
    retry_count = Column(Integer, default=0, nullable=True)
    next_retry_at = Column(DateTime, nullable=True)


class PublicationIntentModel(Base):
    __tablename__ = "publication_intents"

    id = Column(String, primary_key=True)
    package_id = Column(String, ForeignKey("content_packages.id"), nullable=False, index=True)
    job_id = Column(String, ForeignKey("jobs.id"), nullable=False, index=True)
    target = Column(String, nullable=False)
    variant = Column(String, nullable=False)
    approved_by = Column(BigInteger, nullable=False)
    approved_at = Column(DateTime, nullable=False)
    payload_hash = Column(String, nullable=False)
    publication_key = Column(String, nullable=False, index=True)
    status = Column(String, default="PENDING", nullable=False)
    attempt_count = Column(Integer, default=0, nullable=False)
    next_retry_at = Column(DateTime, nullable=True)
    last_error_code = Column(String, nullable=True)
    last_error_message = Column(Text, nullable=True)
    provider_post_id = Column(String, nullable=True)
    provider_url = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class AuditTargetModel(Base):
    __tablename__ = "audit_targets"

    id = Column(String, primary_key=True)
    package_id = Column(String, ForeignKey("content_packages.id"), nullable=False, index=True)
    delivery_id = Column(String, nullable=False, index=True)
    target = Column(String, nullable=False)
    variant = Column(String, nullable=True)
    provider_post_id = Column(String, nullable=False)
    provider_url = Column(String, nullable=True)
    publication_key = Column(String, nullable=False, index=True)
    approved_payload_hash = Column(String, nullable=False)
    approved_payload_text = Column(Text, nullable=False)
    status = Column(String, default="SCHEDULED", nullable=False, index=True)
    tier = Column(Integer, default=0, nullable=False)
    next_audit_at = Column(DateTime, nullable=True, index=True)
    last_checked_at = Column(DateTime, nullable=True)
    last_result_status = Column(String, nullable=True)
    last_error_code = Column(String, nullable=True)
    attempt_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class AuditSnapshotModel(Base):
    __tablename__ = "audit_snapshots"

    id = Column(String, primary_key=True)
    audit_id = Column(String, ForeignKey("audit_targets.id"), nullable=False, index=True)
    occurrence_key = Column(String, unique=True, nullable=False, index=True)
    scheduled_for = Column(DateTime, nullable=True)
    checked_at = Column(DateTime, nullable=False)
    object_exists = Column(Boolean, nullable=False)
    content_hash = Column(String, nullable=True)
    content_match = Column(Boolean, nullable=True)
    metrics = Column(JSON, nullable=False, default=dict)
    normalized_metrics = Column(JSON, nullable=False, default=dict)
    metric_deltas = Column(JSON, nullable=True)
    provider_http_status = Column(Integer, nullable=True)
    latency_ms = Column(Integer, default=0, nullable=False)
    status = Column(String, nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class AuditEventModel(Base):
    __tablename__ = "audit_events"

    id = Column(String, primary_key=True)
    event_type = Column(String, nullable=False, default="EDIT_OBSERVED")
    channel_id = Column(String, nullable=False)
    message_id = Column(BigInteger, nullable=False)
    observed_at = Column(DateTime, nullable=False)
    previous_hash = Column(String, nullable=True)
    new_hash = Column(String, nullable=False)
    payload_text = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class OutcomeObservationModel(Base):
    __tablename__ = "outcome_observations"

    id = Column(String, primary_key=True)
    outcome_id = Column(String, unique=True, nullable=False)
    job_id = Column(String, ForeignKey("jobs.id"), nullable=False, index=True)
    package_id = Column(String, ForeignKey("content_packages.id"), nullable=False, index=True)
    publication_key = Column(String, nullable=False, index=True)
    target = Column(String, nullable=False)
    variant_type = Column(String, nullable=False)
    content_type = Column(String, nullable=True)
    router_primary_category = Column(String, nullable=True)
    router_risk_level = Column(String, nullable=True)
    priority_score = Column(Float, nullable=True)
    priority_band = Column(String, nullable=True)
    language_code = Column(String, nullable=True)
    published_at = Column(DateTime, nullable=True)
    audit_horizon = Column(String, nullable=False, index=True)
    audit_snapshot_id = Column(String, ForeignKey("audit_snapshots.id"), nullable=False, index=True)
    metrics = Column(JSON, nullable=False, default=dict)
    derived_metrics = Column(JSON, nullable=False, default=dict)
    content_integrity_status = Column(String, nullable=False)
    data_quality_status = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, server_default=func.now())


class CalibrationRunModel(Base):
    __tablename__ = "calibration_runs"

    id = Column(String, primary_key=True)
    run_id = Column(String, unique=True, nullable=False)
    readiness = Column(String, nullable=False)
    sample_count = Column(Integer, default=0, nullable=False)
    valid_sample_count = Column(Integer, default=0, nullable=False)
    metrics_summary = Column(JSON, nullable=False, default=dict)
    recommendations = Column(JSON, nullable=False, default=list)
    applied_count = Column(Integer, default=0, nullable=False)
    started_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, server_default=func.now())


