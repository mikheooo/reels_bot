import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from app.core.config import settings

async def run_migration():
    engine = create_async_engine(settings.db_url, echo=True)
    async with engine.begin() as conn:
        print("Running Phase 1 Migration...")
        # Add new columns to jobs table safely
        queries = [
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tg_channel_message_id BIGINT;",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tg_user_message_id BIGINT;",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS qa_reasons JSONB;",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS audit_scheduled_at TIMESTAMP WITHOUT TIME ZONE;"
        ]
        for q in queries:
            try:
                await conn.execute(text(q))
                print(f"Executed: {q}")
            except Exception as e:
                print(f"Failed or already exists: {e}")
        
        print("Migration complete!")
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(run_migration())
