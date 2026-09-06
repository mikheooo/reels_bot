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
    job_id: str,
    sections: list[str] | None = None,
    include_transcript: bool = True,
    include_package: bool = True,
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
    if include_package:
        buttons.append(
            types.InlineKeyboardButton(
                text="📦 Пакет дистрибуции", callback_data=f"pkg:menu:{job_id}"
            )
        )
    if not buttons:
        return None
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def package_menu_keyboard(
    job_id: str, is_approved: bool = False, is_rejected: bool = False
) -> types.InlineKeyboardMarkup:
    """Inline keyboard for inspecting and approving ContentPackage distribution targets."""
    rows = [
        [
            types.InlineKeyboardButton(text="⚡ TLDR", callback_data=f"pkg:view:TLDR:{job_id}"),
            types.InlineKeyboardButton(text="🐦 X (Twitter)", callback_data=f"pkg:view:X_POST:{job_id}"),
        ],
        [
            types.InlineKeyboardButton(text="🧵 Threads", callback_data=f"pkg:view:THREADS_POST:{job_id}"),
            types.InlineKeyboardButton(text="▶️ YouTube", callback_data=f"pkg:view:YOUTUBE_COMMUNITY:{job_id}"),
        ],
    ]

    action_row = []
    if not is_approved and not is_rejected:
        action_row.append(
            types.InlineKeyboardButton(text="✅ Одобрить экспорт", callback_data=f"pkg:approve_all:{job_id}")
        )
        action_row.append(
            types.InlineKeyboardButton(text="❌ Отклонить", callback_data=f"pkg:reject:{job_id}")
        )
    elif is_approved:
        action_row.append(
            types.InlineKeyboardButton(text="✅ Одобрено (готово)", callback_data=f"pkg:approve_all:{job_id}")
        )
    elif is_rejected:
        action_row.append(
            types.InlineKeyboardButton(text="❌ Отклонено", callback_data=f"pkg:reject:{job_id}")
        )

    if action_row:
        rows.append(action_row)

    rows.append([
        types.InlineKeyboardButton(text="🔙 К анализу", callback_data=f"pkg:back:{job_id}")
    ])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def package_variant_keyboard(job_id: str, variant_type: str) -> types.InlineKeyboardMarkup:
    """Inline keyboard when previewing a specific variant."""
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text="🔄 Пересоздать", callback_data=f"pkg:regen:{variant_type}:{job_id}"),
                types.InlineKeyboardButton(text="🔙 К пакету", callback_data=f"pkg:menu:{job_id}"),
            ]
        ]
    )

