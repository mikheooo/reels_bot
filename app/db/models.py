from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
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
