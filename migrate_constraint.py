import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from app.core.config import settings

async def add_constraint():
    engine = create_async_engine(settings.db_url, echo=True)
    async with engine.begin() as conn:
        q = """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_jobs_status') THEN
                ALTER TABLE jobs ADD CONSTRAINT chk_jobs_status CHECK (status IN ('QUEUED', 'PROCESSING', 'DONE', 'ERROR', 'REVIEW_REQUIRED'));
            END IF;
        END $$;
        """
        await conn.execute(text(q))
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(add_constraint())
