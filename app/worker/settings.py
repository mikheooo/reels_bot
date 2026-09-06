import logging
import shutil

from arq import cron
from arq.connections import RedisSettings

from app.core.config import settings
from app.db.database import engine, init_db
from app.db.migrate import apply_migrations
from app.worker.key_health import log_key_health
from app.worker.reaper import expire_stale_processing, reap_stale_jobs
from app.worker.tasks import process_video

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

async def startup(ctx):
    logging.info("Worker starting up...")
    log_key_health()  # warn in main log if any API key is a placeholder
    await init_db()
    # create_all() never adds columns to an existing table, so started_at —
    # which the reaper depends on — has to come from the migration every boot.
    try:
        await apply_migrations(engine)
    except Exception as e:
        logging.error(f"Migrations failed: {e}")
    shutil.rmtree('/tmp/reels_bot', ignore_errors=True)
    # Anything still PROCESSING at boot is dead by definition: this worker was
    # down, so nobody was running it. Clear the zombies before new work arrives.
    try:
        expired = await expire_stale_processing()
        if expired:
            logging.warning(f"REAPER: expired {expired} stale job(s) at startup")
    except Exception as e:
        logging.error(f"Reaper failed at startup: {e}")

async def shutdown(ctx):
    logging.info("Worker shutting down...")

class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    functions = [process_video]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 2
    # The full pipeline (transcript -> structured -> factcheck -> business check)
    # plus Gemini key-rotation/backoff legitimately takes 10-20+ min, especially
    # when free-tier keys are rate-limited. 600s silently killed healthy jobs
    # (CancelledError is not caught -> status stuck in PROCESSING, no user message).
    job_timeout = 1800
    # Catch what job_timeout still misses (OOM-kill, container restart, deploy):
    # reaper turns stale PROCESSING rows into ERROR with a real reason.
    # REAPER_MAX_AGE_SECONDS defaults to 3600 — twice job_timeout, so the reaper
    # can never outrun a job that is legitimately still working.
    cron_jobs = [
        cron(
            reap_stale_jobs,
            minute={0, 10, 20, 30, 40, 50},
            run_at_startup=False,
            unique=True,
        )
    ]
