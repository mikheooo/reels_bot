import logging
import uuid

from aiogram import F, Router, types
from aiogram.filters import Command, CommandStart
from arq import create_pool
from arq.connections import RedisSettings
from sqlalchemy import select

from app.core.config import settings
from app.core.normalizer import clean_url, is_valid_url
from app.db.database import AsyncSessionLocal
from app.db.models import Job, Task

router = Router()
logger = logging.getLogger(__name__)

STATUS_EMOJI = {
    'PENDING': '⏳',
    'IN_PROGRESS': '🔄',
    'DONE': '✅',
}
STATUS_LABEL = {
    'PENDING': 'В ожидании',
    'IN_PROGRESS': 'В работе',
    'DONE': 'Готово',
}
NEXT_STATUS = {
    'PENDING': 'IN_PROGRESS',
    'IN_PROGRESS': 'DONE',
    'DONE': 'PENDING',
}

async def get_redis_pool():
    return await create_pool(RedisSettings.from_dsn(settings.redis_url))

@router.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Отправь мне ссылку на видео (TikTok, Instagram, YouTube), и я проанализирую его.\n\n"
        "Доступные команды:\n"
        "/tasks — список задач из всех рилз\n"
        "/tasks_pending — только ждущие задачи\n"
        "/tasks_done — только выполненные"
    )

@router.message(Command("tasks"))
async def cmd_tasks(message: types.Message):
    async with AsyncSessionLocal() as session:
        stmt = select(Task).where(Task.user_id == message.from_user.id).order_by(Task.created_at.desc())
        result = await session.execute(stmt)
        tasks = result.scalars().all()

    if not tasks:
        await message.answer("📋 Список задач пуст. Отправь рилз — и я сформирую задачи автоматически!")
        return

    pending = sum(1 for t in tasks if t.status == 'PENDING')
    in_progress = sum(1 for t in tasks if t.status == 'IN_PROGRESS')
    done = sum(1 for t in tasks if t.status == 'DONE')

    lines = ["📋 **Задачи из рилз**\n", f"Всего: {len(tasks)} | ⏳ {pending} | 🔄 {in_progress} | ✅ {done}\n"]
    for i, t in enumerate(tasks, 1):
        emoji = STATUS_EMOJI.get(t.status, '❓')
        lines.append(f"{i}. {emoji} **{t.title}**")
        if t.description and t.status != 'DONE':
            # description can be long — show first 200 chars
            short_desc = t.description[:200]
            if len(t.description) > 200:
                short_desc += "..."
            lines.append(f"   _{short_desc}_")

    text = "\n".join(lines)
    # Split if too long
    while text:
        await message.answer(text[:4096], parse_mode="Markdown")
        text = text[4096:].strip()

    # Send inline buttons for pending/in_progress tasks
    for t in tasks:
        if t.status in ('PENDING', 'IN_PROGRESS'):
            kb = types.InlineKeyboardMarkup(inline_keyboard=[[
                types.InlineKeyboardButton(
                    text=f"{STATUS_EMOJI[t.status]} → {STATUS_LABEL[NEXT_STATUS[t.status]]}",
                    callback_data=f"task_cycle:{t.id}"
                )
            ]])
            await message.answer(f"{STATUS_EMOJI[t.status]} {t.title}", reply_markup=kb)


@router.message(Command("tasks_pending"))
async def cmd_tasks_pending(message: types.Message):
    async with AsyncSessionLocal() as session:
        stmt = select(Task).where(
            Task.user_id == message.from_user.id,
            Task.status.in_(['PENDING', 'IN_PROGRESS'])
        ).order_by(Task.created_at.desc())
        result = await session.execute(stmt)
        tasks = result.scalars().all()

    if not tasks:
        await message.answer("🎉 Нет невыполненных задач!")
        return

    lines = ["📋 **Активные задачи:**\n"]
    for i, t in enumerate(tasks, 1):
        emoji = STATUS_EMOJI.get(t.status, '⏳')
        lines.append(f"{i}. {emoji} **{t.title}**")

    await message.answer("\n".join(lines), parse_mode="Markdown")

    for t in tasks:
        kb = types.InlineKeyboardMarkup(inline_keyboard=[[
            types.InlineKeyboardButton(
                text=f"{STATUS_EMOJI[t.status]} → {STATUS_LABEL[NEXT_STATUS[t.status]]}",
                callback_data=f"task_cycle:{t.id}"
            )
        ]])
        await message.answer(f"{STATUS_EMOJI[t.status]} {t.title}", reply_markup=kb)


@router.message(Command("tasks_done"))
async def cmd_tasks_done(message: types.Message):
    async with AsyncSessionLocal() as session:
        stmt = select(Task).where(
            Task.user_id == message.from_user.id,
            Task.status == 'DONE'
        ).order_by(Task.created_at.desc())
        result = await session.execute(stmt)
        tasks = result.scalars().all()

    if not tasks:
        await message.answer(" Пока нет выполненных задач.")
        return

    lines = ["✅ **Выполненные задачи:**\n"]
    for i, t in enumerate(tasks, 1):
        lines.append(f"{i}. ✅ **{t.title}**")

    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.callback_query(F.data.startswith("task_cycle:"))
async def task_cycle(callback: types.CallbackQuery):
    task_id = callback.data.split(":")[1]
    async with AsyncSessionLocal() as session:
        task = await session.get(Task, task_id)
        if not task:
            await callback.answer("Задача не найдена!")
            return

        old_status = task.status
        task.status = NEXT_STATUS.get(task.status, 'PENDING')
        if task.status == 'DONE':
            from datetime import datetime
            task.completed_at = datetime.utcnow()
        else:
            task.completed_at = None
        await session.commit()

    emoji = STATUS_EMOJI[task.status]
    await callback.answer(f"Статус: {STATUS_LABEL[task.status]}")

    # Update message text
    new_kb = None
    if task.status in ('PENDING', 'IN_PROGRESS'):
        new_kb = types.InlineKeyboardMarkup(inline_keyboard=[[
            types.InlineKeyboardButton(
                text=f"{emoji} → {STATUS_LABEL[NEXT_STATUS[task.status]]}",
                callback_data=f"task_cycle:{task.id}"
            )
        ]])
    await callback.message.edit_text(f"{emoji} {task.title}", reply_markup=new_kb)


@router.message()
async def handle_url(message: types.Message):
    url = message.text.strip()
    if not is_valid_url(url):
        await message.answer("Пожалуйста, отправь валидную ссылку на Instagram, TikTok или YouTube.\n\nИли используй /tasks для просмотра списка задач.")
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
                return
            elif existing_job.status in ('QUEUED', 'PROCESSING'):
                await message.answer("Это видео уже в очереди или обрабатывается. Я пришлю результат, как только он будет готов.")
                return
            else:
                # Если статус ERROR или другой, пробуем заново
                existing_job.status = 'QUEUED'
                await session.commit()
                redis_pool = await get_redis_pool()
                await redis_pool.enqueue_job('process_video', existing_job.id, cleaned_url, message.from_user.id)
                await message.answer("Повторная попытка обработки! Ждите результат.")
                return
        
        job_id = str(uuid.uuid4())
        new_job = Job(id=job_id, user_id=message.from_user.id, original_url=url, url_hash=url_hash, status='QUEUED')
        session.add(new_job)
        await session.commit()
        
        redis_pool = await get_redis_pool()
        await redis_pool.enqueue_job('process_video', job_id, cleaned_url, message.from_user.id)
        
        await message.answer("Принято в работу! Ждите результат.")
