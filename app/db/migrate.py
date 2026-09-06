import asyncio
import logging
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def apply_migrations(engine) -> None:
    """Apply all additive migrations. Safe to run on every worker startup.

    Kept separate from run_migration() so the worker can reuse the *existing*
    engine instead of opening a second one — and so a failure can be logged
    instead of calling sys.exit() inside a running process.
    """
    async with engine.begin() as conn:
        queries = [
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tg_channel_message_id BIGINT;",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tg_user_message_id BIGINT;",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS qa_reasons JSONB;",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS audit_scheduled_at TIMESTAMP WITHOUT TIME ZONE;",
            # Reaper support: create_all() never adds columns to an existing
            # table, so the column has to come from here on a live database.
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS started_at TIMESTAMP WITHOUT TIME ZONE;",
            # Progress UX: single editable Telegram status message per job.
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tg_progress_chat_id BIGINT;",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tg_progress_message_id BIGINT;",
            # Canonical transcript: immutable source artifact, no length limit.
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS full_transcript TEXT;",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS transcription_model VARCHAR;",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS transcription_fallback_used VARCHAR;",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS transcription_status VARCHAR;",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS delivery_status JSONB;",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS audit_state VARCHAR;",
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
                IF EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_jobs_status'
                      AND pg_get_constraintdef(oid) NOT LIKE '%PARTIAL%'
                ) THEN
                    ALTER TABLE jobs DROP CONSTRAINT chk_jobs_status;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_jobs_status') THEN
                    ALTER TABLE jobs ADD CONSTRAINT chk_jobs_status CHECK (status IN ('QUEUED', 'PROCESSING', 'DONE', 'PARTIAL', 'ERROR', 'REVIEW_REQUIRED'));
                END IF;
            END $$;
            """,
            # Option B: preserve legacy due timestamps, but explicitly quarantine
            # them so they no longer imply an active scheduler capability.
            "UPDATE jobs SET audit_state = 'DEFERRED_LEGACY' WHERE audit_scheduled_at IS NOT NULL AND audit_state IS NULL;",
            "UPDATE jobs SET audit_state = 'NOT_SCHEDULED' WHERE audit_scheduled_at IS NULL AND audit_state IS NULL;",
            """
            CREATE TABLE IF NOT EXISTS content_packages (
                id VARCHAR PRIMARY KEY,
                job_id VARCHAR NOT NULL REFERENCES jobs(id),
                source_url VARCHAR NOT NULL,
                contract_version VARCHAR NOT NULL DEFAULT 'content_package_v1',
                status VARCHAR NOT NULL DEFAULT 'GENERATED',
                language_context JSONB,
                router_result JSONB,
                priority_result JSONB,
                canonical_content JSONB NOT NULL,
                output_variants JSONB NOT NULL,
                distribution_targets JSONB NOT NULL,
                created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS content_deliveries (
                id VARCHAR PRIMARY KEY,
                package_id VARCHAR NOT NULL REFERENCES content_packages(id),
                target VARCHAR NOT NULL,
                variant VARCHAR NOT NULL,
                attempt_id INTEGER NOT NULL DEFAULT 1,
                status VARCHAR NOT NULL,
                external_id VARCHAR,
                idempotency_key VARCHAR UNIQUE NOT NULL,
                error_code VARCHAR,
                error_message TEXT,
                started_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
                finished_at TIMESTAMP WITHOUT TIME ZONE
            );
            """,
            "CREATE INDEX IF NOT EXISTS ix_content_packages_job_id ON content_packages (job_id);",
            "CREATE INDEX IF NOT EXISTS ix_content_deliveries_package_id ON content_deliveries (package_id);",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_content_deliveries_idempotency_key ON content_deliveries (idempotency_key);",
            """
            CREATE TABLE IF NOT EXISTS publication_intents (
                id VARCHAR PRIMARY KEY,
                package_id VARCHAR NOT NULL REFERENCES content_packages(id),
                job_id VARCHAR NOT NULL REFERENCES jobs(id),
                target VARCHAR NOT NULL,
                variant VARCHAR NOT NULL,
                approved_by BIGINT NOT NULL,
                approved_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                payload_hash VARCHAR NOT NULL,
                publication_key VARCHAR NOT NULL,
                status VARCHAR NOT NULL DEFAULT 'PENDING',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_retry_at TIMESTAMP WITHOUT TIME ZONE,
                last_error_code VARCHAR,
                last_error_message TEXT,
                provider_post_id VARCHAR,
                provider_url VARCHAR,
                created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
            );
            """,
            "CREATE INDEX IF NOT EXISTS ix_publication_intents_package_id ON publication_intents (package_id);",
            "CREATE INDEX IF NOT EXISTS ix_publication_intents_job_id ON publication_intents (job_id);",
            "CREATE INDEX IF NOT EXISTS ix_publication_intents_publication_key ON publication_intents (publication_key);",
            "ALTER TABLE content_deliveries ADD COLUMN IF NOT EXISTS publication_key VARCHAR;",
            "ALTER TABLE content_deliveries ADD COLUMN IF NOT EXISTS payload_hash VARCHAR;",
            "ALTER TABLE content_deliveries ADD COLUMN IF NOT EXISTS provider_post_id VARCHAR;",
            "ALTER TABLE content_deliveries ADD COLUMN IF NOT EXISTS provider_url VARCHAR;",
            "ALTER TABLE content_deliveries ADD COLUMN IF NOT EXISTS retry_count INTEGER DEFAULT 0;",
            "ALTER TABLE content_deliveries ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMP WITHOUT TIME ZONE;",
            "CREATE INDEX IF NOT EXISTS ix_content_deliveries_publication_key ON content_deliveries (publication_key);",
        ]

        for q in queries:
            await conn.execute(text(q))
        logger.info("Migrations applied successfully.")


async def run_migration():
    """CLI entrypoint: python -m app.db.migrate"""
    engine = create_async_engine(settings.db_url, echo=False)
    try:
        await apply_migrations(engine)
    except Exception as e:
        logger.error(f"Migration failed during execution. Transaction rolled back. Error: {e}")
        sys.exit(1)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run_migration())
