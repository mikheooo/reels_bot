"""Inline detail controls for compact routed analyses."""

from aiogram import types

DETAIL_LABELS = {
    "fact": "🔎 Fact-check",
    "tech": "🧩 Как работает",
    "business": "💼 Business",
    "type": "📌 Подробнее",
    "relevance": "👤 Для меня",
}


def analysis_keyboard(
    job_id: str, sections: list[str] | None = None, include_transcript: bool = True
):
    available = set(sections or [])
    buttons = [
        types.InlineKeyboardButton(text=label, callback_data=f"detail:{key}:{job_id}")
        for key, label in DETAIL_LABELS.items()
        if key in available
    ]
    if include_transcript:
        buttons.append(
            types.InlineKeyboardButton(
                text="📄 Полный текст", callback_data=f"full:{job_id}"
            )
        )
    if not buttons:
        return None
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    return types.InlineKeyboardMarkup(inline_keyboard=rows)
