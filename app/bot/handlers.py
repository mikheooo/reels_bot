import logging
from aiogram import Router, types
from aiogram.filters import CommandStart
from app.core.normalizer import clean_url, is_valid_url
from app.db.database import AsyncSessionLocal
from app.db.models import Job
from sqlalchemy import select
from arq import create_pool
from arq.connections import RedisSettings
from app.core.config import settings
import uuid

router = Router()
logger = logging.getLogger(__name__)

async def get_redis_pool():
    return await create_pool(RedisSettings.from_dsn(settings.redis_url))

@router.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer("Привет! Отправь мне ссылку на видео (TikTok, Instagram, YouTube), и я проанализирую его.")

@router.message()
async def handle_url(message: types.Message):
    url = message.text.strip()
    if not is_valid_url(url):
        await message.answer("Пожалуйста, отправь валидную ссылку на Instagram, TikTok или YouTube.")
        return

    cleaned_url, url_hash = clean_url(url)
    
    async with AsyncSessionLocal() as session:
        stmt = select(Job).where(Job.url_hash == url_hash, Job.status.in_(['QUEUED', 'PROCESSING', 'DONE'])).limit(1)
        result = await session.execute(stmt)
        existing_job = result.scalar_one_or_none()
        
        if existing_job:
            if existing_job.status == 'DONE':
                await message.answer("🎬 Это видео уже анализировалось. Вот результат:")
                await message.answer_video(existing_job.tg_file_id)
                if existing_job.analysis_text:
                    # Split long text into chunks
                    text = existing_job.analysis_text
                    while text:
                        await message.answer(text[:4096])
                        text = text[4096:].strip()
            else:
                await message.answer("Это видео уже в очереди или обрабатывается. Я пришлю результат, как только он будет готов.")
            return
        
        job_id = str(uuid.uuid4())
        new_job = Job(id=job_id, user_id=message.from_user.id, original_url=url, url_hash=url_hash, status='QUEUED')
        session.add(new_job)
        await session.commit()
        
        redis_pool = await get_redis_pool()
        await redis_pool.enqueue_job('process_video', job_id, cleaned_url, message.from_user.id)
        
        await message.answer("Принято в работу! Ждите результат.")
