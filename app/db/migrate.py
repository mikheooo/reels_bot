import asyncio
import logging
import sys

from app.core.config import settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def run_migration():
    engine = create_async_engine(settings.db_url, echo=False)
    try:
        async with engine.begin() as conn:
            queries = [
                "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tg_channel_message_id BIGINT;",
                "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tg_user_message_id BIGINT;",
                "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS qa_reasons JSONB;",
                "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS audit_scheduled_at TIMESTAMP WITHOUT TIME ZONE;",
                "CREATE INDEX IF NOT EXISTS ix_jobs_tg_channel_message_id ON jobs (tg_channel_message_id);",
                "CREATE INDEX IF NOT EXISTS ix_jobs_audit_scheduled_at ON jobs (audit_scheduled_at);",
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_jobs_tg_channel_message_id') THEN
                        ALTER TABLE jobs ADD CONSTRAINT uq_jobs_tg_channel_message_id UNIQUE (tg_channel_message_id);
                    END IF;
                END $$;
                """,
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_jobs_status') THEN
                        ALTER TABLE jobs ADD CONSTRAINT chk_jobs_status CHECK (status IN ('QUEUED', 'PROCESSING', 'DONE', 'ERROR', 'REVIEW_REQUIRED'));
                    END IF;
                END $$;
                """
            ]
            for q in queries:
                await conn.execute(text(q))
            logger.info("Phase 1 migrations applied successfully.")
    except Exception as e:
        logger.error(f"Migration failed during execution. Transaction rolled back. Error: {e}")
        sys.exit(1)
    finally:
        await engine.dispose()

if __name__ == "__main__":
    asyncio.run(run_migration())
