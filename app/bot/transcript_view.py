"""Full-transcript delivery: button + chunked/file sending.

Reads the canonical immutable `Job.full_transcript` from the database.
Never re-runs transcription, never calls external APIs.
"""

import logging

from aiogram import types
from aiogram.types import BufferedInputFile

logger = logging.getLogger(__name__)

# Above this length a .txt document is kinder than a wall of messages.
# 12000 chars ~= 3 full Telegram messages.
DOC_THRESHOLD = 12000
CHUNK_LIMIT = 4096

LEGACY_TEXT = "Полная расшифровка для этого старого анализа не сохранена."


def transcript_button(job_id: str) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(
            text="📄 Полная расшифровка",
            callback_data=f"full:{job_id}",
        )
    ]])


def split_transcript(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Ordered chunks, split on blank lines/sentences first, hard cut last.

    Joining the result reproduces a prefix of the input: chunking may drop
    nothing except leading/trailing whitespace stripped per chunk.
    """
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        split_at = rest.rfind("\n\n", 0, limit)
        if split_at < limit // 2:
            split_at = rest.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = rest.rfind(". ", 0, limit)
            if split_at != -1:
                split_at += 1  # keep the period with the sentence
        if split_at < limit // 2:
            split_at = limit
        chunks.append(rest[:split_at].strip())
        rest = rest[split_at:].strip()
    if rest:
        chunks.append(rest)
    return chunks


def should_send_document(text: str, threshold: int = DOC_THRESHOLD) -> bool:
    return len(text) > threshold


async def send_full_transcript(bot, chat_id: int, text: str) -> str:
    """Deliver 100% of the transcript. Returns 'chunks' or 'document'."""
    if should_send_document(text):
        data = text.encode("utf-8")
        await bot.send_document(
            chat_id=chat_id,
            document=BufferedInputFile(data, filename="transcript.txt"),
            caption="📄 Полная расшифровка (файл, без сокращений)",
        )
        return "document"
    chunks = split_transcript(text)
    total = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        prefix = f"📄 Расшифровка {i}/{total}\n\n" if total > 1 else "📄 Полная расшифровка\n\n"
        await bot.send_message(chat_id=chat_id, text=prefix + chunk)
    return "chunks"
