from sqlalchemy import Column, String, BigInteger, DateTime, JSON
from sqlalchemy.sql import func
from app.db.database import Base

class Job(Base):
    __tablename__ = "jobs"
    id = Column(String, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    original_url = Column(String, nullable=False)
    url_hash = Column(String, index=True, nullable=False)
    status = Column(String, nullable=False)
    error_text = Column(String, nullable=True)
    tg_file_id = Column(String, nullable=True)
    analysis_text = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
