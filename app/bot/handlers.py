import logging
import uuid

from aiogram import F, Router, types
from aiogram.filters import Command, CommandStart
from arq import create_pool
from arq.connections import RedisSettings
from sqlalchemy import select

from app.bot.analysis_view import DETAIL_LABELS, analysis_keyboard
from app.bot.package_handlers import handle_package_callback
from app.bot.transcript_view import LEGACY_TEXT, send_full_transcript, transcript_button
from app.core.config import settings
from app.core.normalizer import clean_url, is_valid_url
from app.db.database import AsyncSessionLocal
from app.db.models import Job, Task

router = Router()
logger = logging.getLogger(__name__)


def existing_job_query(url_hash: str):
    """Return the newest attempt regardless of status so ERROR retry is reachable."""
    return select(Job).where(Job.url_hash == url_hash).order_by(Job.created_at.desc()).limit(1)

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


@router.callback_query(F.data.startswith("full:"))
async def full_transcript(callback: types.CallbackQuery):
    job_id = callback.data.split(":", 1)[1]
    async with AsyncSessionLocal() as session:
        job = await session.get(Job, job_id)
        text = job.full_transcript if job else None
    if not text:
        await callback.answer(LEGACY_TEXT, show_alert=True)
        return
    await callback.answer("Отправляю расшифровку…")
    try:
        await send_full_transcript(callback.bot, callback.message.chat.id, text)
    except Exception:
        logger.exception(f"Could not deliver transcript for job {job_id}")
        await callback.message.answer("Не получилось отправить расшифровку, попробуйте позже.")


@router.callback_query(F.data.startswith("detail:"))
async def analysis_detail(callback: types.CallbackQuery):
    _, section, job_id = callback.data.split(":", 2)
    async with AsyncSessionLocal() as session:
        job = await session.get(Job, job_id)
        payload = job.qa_reasons if job and isinstance(job.qa_reasons, dict) else {}
        text = (payload.get("detail_sections") or {}).get(section)
    if not text:
        await callback.answer("Этот раздел для ролика не создавался.", show_alert=True)
        return
    await callback.answer(DETAIL_LABELS.get(section, "Открываю…"))
    remaining = text
    while remaining:
        chunk = remaining[:4096]
        split_at = chunk.rfind("\n\n")
        if len(remaining) > 4096 and split_at > 1500:
            chunk = chunk[:split_at]
        await callback.message.answer(chunk)
        remaining = remaining[len(chunk):].strip()


router.callback_query(F.data.startswith("pkg:"))(handle_package_callback)


@router.message()
async def handle_url(message: types.Message):
    url = message.text.strip()
    if not is_valid_url(url):
        await message.answer("Пожалуйста, отправь валидную ссылку на Instagram, TikTok или YouTube.\n\nИли используй /tasks для просмотра списка задач.")
        return

    cleaned_url, url_hash = clean_url(url)
    
    async with AsyncSessionLocal() as session:
        stmt = existing_job_query(url_hash)
        result = await session.execute(stmt)
        existing_job = result.scalar_one_or_none()
        
        if existing_job:
            if existing_job.status in ('DONE', 'PARTIAL'):
                await message.answer("🎬 Это видео уже анализировалось. Вот результат:")
                await message.answer_video(existing_job.tg_file_id)
                if existing_job.analysis_text:
                    payload = existing_job.qa_reasons if isinstance(existing_job.qa_reasons, dict) else {}
                    sections = list((payload.get("detail_sections") or {}).keys())
                    text = existing_job.analysis_text
                    first = True
                    while text:
                        await message.answer(
                            text[:4096],
                            reply_markup=(
                                analysis_keyboard(existing_job.id, sections, bool(existing_job.full_transcript))
                                if first else None
                            ),
                        )
                        first = False
                        text = text[4096:].strip()
                elif existing_job.full_transcript:
                    await message.answer("Нужен весь текст ролика?", reply_markup=transcript_button(existing_job.id))
                if existing_job.status == 'PARTIAL':
                    await message.answer("⚠️ Анализ завершён, но часть delivery-шагов требует проверки.")
                return
            elif existing_job.status in ('QUEUED', 'PROCESSING'):
                await message.answer("Это видео уже в очереди или обрабатывается. Я пришлю результат, как только он будет готов.")
                return
            elif existing_job.status == 'ERROR':
                # Explicit retry policy: reuse the failed job and clear only
                # transient execution/delivery state.
                existing_job.status = 'QUEUED'
                existing_job.error_text = None
                existing_job.started_at = None
                existing_job.delivery_status = None
                await session.commit()
                redis_pool = await get_redis_pool()
                await redis_pool.enqueue_job('process_video', existing_job.id, cleaned_url, message.from_user.id)
                try:
                    from app.worker.progress import stage_text
                    status_msg = await message.answer(stage_text("QUEUED"))
                    existing_job.tg_progress_chat_id = message.chat.id
                    existing_job.tg_progress_message_id = status_msg.message_id
                    await session.commit()
                except Exception:
                    logger.exception(f"Could not send retry progress message for job {existing_job.id}")
                return
            elif existing_job.status == 'REVIEW_REQUIRED':
                await message.answer(
                    "⚠️ Этот ролик уже анализировался и требует ручной проверки. "
                    "Повторный автоматический запуск не создан."
                )
                return
        
        job_id = str(uuid.uuid4())
        new_job = Job(id=job_id, user_id=message.from_user.id, original_url=url, url_hash=url_hash, status='QUEUED')
        session.add(new_job)
        await session.commit()

        redis_pool = await get_redis_pool()
        await redis_pool.enqueue_job('process_video', job_id, cleaned_url, message.from_user.id)

        # Single status message for the whole job; the worker edits it
        # in place as the pipeline advances. Best-effort: if sending
        # fails, the job is already queued and analysis proceeds anyway.
        try:
            from app.worker.progress import stage_text
            status_msg = await message.answer(stage_text("QUEUED"))
            new_job.tg_progress_chat_id = message.chat.id
            new_job.tg_progress_message_id = status_msg.message_id
            await session.commit()
        except Exception:
            logger.exception(f"Could not send progress message for job {job_id}")


@router.edited_channel_post()
async def handle_edited_channel_post(message: types.Message):
    """Observational telemetry handler for edited channel posts."""
    try:
        from app.worker.post_publish_audit import record_telegram_edit_event
        channel_id = str(message.chat.id)
        message_id = message.message_id
        new_text = message.text or message.caption or ""
        observed_at = message.edit_date or message.date
        async with AsyncSessionLocal() as session:
            await record_telegram_edit_event(
                channel_id=channel_id,
                message_id=message_id,
                new_text=new_text,
                observed_at=observed_at,
                session=session,
            )
            await session.commit()
    except Exception as e:
        logger.warning("Error recording telegram edit event: %s", e)
