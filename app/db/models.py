from app.db.database import Base
from sqlalchemy import JSON, BigInteger, Column, DateTime, String, Text
from sqlalchemy.sql import func


class JobStatus:
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
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
    tg_channel_message_id = Column(BigInteger, nullable=True)
    tg_user_message_id = Column(BigInteger, nullable=True)
    qa_reasons = Column(JSON, nullable=True)
    audit_scheduled_at = Column(DateTime, nullable=True)

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
