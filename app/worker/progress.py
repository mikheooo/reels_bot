"""Best-effort Telegram progress updates for long video jobs.

One status message per job, edited in place as the pipeline advances.
A progress failure (deleted message, timeout, rate limit, bad id) must
NEVER break the analysis itself: every error is logged and swallowed.

Plain text only — no Markdown, so no escaping issues.
"""

import logging

logger = logging.getLogger(__name__)

# Fixed user-facing stages. Internal technical names are never shown.
STAGES = {
    "QUEUED": "⏳ Принято в работу",
    "DOWNLOAD": "⬇️ Загружаю видео…",
    "TRANSCRIPT": "🎙 Распознаю содержание…",
    "ANALYSIS": "🧠 Анализирую видео…",
    "VERIFY": "🔎 Проверяю факты и источники…",
    "FINALIZE": "📝 Собираю итоговый отчёт…",
    "COMPLETE": "✅ Анализ готов",
    "PARTIAL": "⚠️ Анализ готов, но часть доставки не завершена",
    "ERROR": "❌ Не удалось обработать видео",
    "REVIEW_REQUIRED": "⚠️ Анализ готов, но часть выводов требует проверки",
}


def stage_text(stage: str) -> str:
    """User-visible text for a stage. Unknown stages fall back to QUEUED."""
    return STAGES.get(stage, STAGES["QUEUED"])


async def set_progress(job_id: str, stage: str, session_factory=None, bot_factory=None, reply_markup=None) -> bool:
    """Edit the job's progress message. Returns True if edited.

    Never raises: returns False when the job has no progress message
    yet (old rows), when ids are missing, or when Telegram edit fails.
    """
    from aiogram import Bot

    from app.core.config import settings
    from app.db.database import AsyncSessionLocal
    from app.db.models import Job

    session_factory = session_factory or AsyncSessionLocal
    try:
        async with session_factory() as session:
            job = await session.get(Job, job_id)
            if job is None:
                return False
            chat_id = getattr(job, "tg_progress_chat_id", None)
            message_id = getattr(job, "tg_progress_message_id", None)
        if not chat_id or not message_id:
            return False
        text = stage_text(stage)
        bot = bot_factory() if bot_factory is not None else Bot(token=settings.bot_token)
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup
            )
        finally:
            try:
                await bot.session.close()
            except Exception:
                pass
        return True
    except Exception as e:
        logger.warning(f"Progress update {stage} for job {job_id} failed (non-blocking): {e}")
        return False
