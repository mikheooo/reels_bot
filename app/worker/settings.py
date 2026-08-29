import logging
import shutil

from arq.connections import RedisSettings

from app.core.config import settings
from app.db.database import init_db
from app.worker.key_health import log_key_health
from app.worker.tasks import process_video

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

async def startup(ctx):
    logging.info("Worker starting up...")
    log_key_health()  # warn in main log if any API key is a placeholder
    await init_db()
    shutil.rmtree('/tmp/reels_bot', ignore_errors=True)

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
