import shutil
import logging
from arq.connections import RedisSettings
from app.core.config import settings
from app.worker.tasks import process_video
from app.db.database import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

async def startup(ctx):
    logging.info("Worker starting up...")
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
    job_timeout = 600
