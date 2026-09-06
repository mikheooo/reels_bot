"""Telegram bot callback handlers for ContentPackage review, approval, rejection, and variant inspection."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import types
from sqlalchemy import select

from app.bot.analysis_view import (
    analysis_keyboard,
    package_menu_keyboard,
    package_variant_keyboard,
)
from app.db.database import AsyncSessionLocal
from app.db.models import (
    ContentDeliveryModel,
    ContentPackageModel,
    Job,
    PublicationIntentModel,
)
from app.worker.connectors import CapabilityStatus, ConnectorRegistry
from app.worker.content_package import (
    ContentPackage,
    OutputVariantType,
    TargetPlatform,
    approve_package,
    regenerate_variant_from_canonical,
    reject_package,
)
from app.worker.output_variants import CanonicalContentResult

logger = logging.getLogger(__name__)


def _to_naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt



async def handle_package_callback(callback: types.CallbackQuery) -> None:

    """Handle all 'pkg:*' callback queries with strict ownership verification."""
    data = callback.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    variant_name = ""
    job_id = ""
    if action in ("view", "regen") and len(parts) >= 4:
        variant_name = parts[2]
        job_id = parts[3]
    elif len(parts) >= 3:
        job_id = parts[2]
    else:
        await callback.answer("Неверный формат запроса.")
        return

    async with AsyncSessionLocal() as session:
        job = await session.get(Job, job_id)
        if not job:
            await callback.answer("Задача не найдена.", show_alert=True)
            return

        # Security check: User authorization
        if callback.from_user.id != job.user_id:
            logger.warning(
                "Unauthorized package action '%s' attempted on job %s by user %s (owner: %s)",
                action, job_id, callback.from_user.id, job.user_id,
            )
            await callback.answer(
                "⛔ Доступ запрещён: только автор запроса может управлять контент-пакетом.",
                show_alert=True,
            )
            return

        stmt = select(ContentPackageModel).where(ContentPackageModel.job_id == job_id).limit(1)
        pkg_row = (await session.execute(stmt)).scalars().first()

        if not pkg_row:
            await callback.answer("Контент-пакет для этого ролика пока не сформирован.", show_alert=True)
            return

        if action == "menu":
            targets = pkg_row.distribution_targets or {}
            x_conn = ConnectorRegistry.get_connector(TargetPlatform.X)
            x_badge = "[CONNECTED]" if x_conn.capabilities().status == CapabilityStatus.CONNECTED_SUPPORTED else "[НЕ НАСТРОЕН]"
            thr_conn = ConnectorRegistry.get_connector(TargetPlatform.THREADS)
            thr_badge = "[CONNECTED]" if thr_conn.capabilities().status == CapabilityStatus.CONNECTED_SUPPORTED else "[НЕ НАСТРОЕН]"
            yt_badge = "[РУЧНОЙ ЭКСПОРТ]"

            x_status = targets.get("X", {}).get("status", "PENDING_APPROVAL")
            thr_status = targets.get("THREADS", {}).get("status", "PENDING_APPROVAL")
            yt_status = targets.get("YOUTUBE_COMMUNITY", {}).get("status", "PENDING_APPROVAL")
            is_approved = pkg_row.status in ("APPROVED", "DELIVERED")
            is_rejected = pkg_row.status == "REJECTED"

            status_text = (
                f"📦 <b>Контент-пакет дистрибуции</b>\n\n"
                f"Статус пакета: <b>{pkg_row.status}</b>\n\n"
                f"• <b>Telegram (User):</b> ✅ Доставлено\n"
                f"• <b>Telegram (Канал):</b> {targets.get('TELEGRAM_CHANNEL', {}).get('status', 'NOT_APPLICABLE')}\n"
                f"• <b>X (Twitter) {x_badge}:</b> {x_status}\n"
                f"• <b>Threads {thr_badge}:</b> {thr_status}\n"
                f"• <b>YouTube Community {yt_badge}:</b> {yt_status}\n\n"
                f"<i>Выберите вариант для предпросмотра или одобрите экспорт на внешние площадки.</i>"
            )
            await callback.answer()
            try:
                await callback.message.edit_text(
                    status_text,
                    reply_markup=package_menu_keyboard(job_id, is_approved=is_approved, is_rejected=is_rejected),
                    parse_mode="HTML",
                )
            except Exception:
                await callback.message.answer(
                    status_text,
                    reply_markup=package_menu_keyboard(job_id, is_approved=is_approved, is_rejected=is_rejected),
                    parse_mode="HTML",
                )
            return

        elif action == "view":
            variants = (pkg_row.output_variants or {}).get("variants", {})
            var_data = variants.get(variant_name, {})
            v_status = var_data.get("status")
            v_text = var_data.get("text")
            char_count = var_data.get("character_count", 0)

            if v_status == "NOT_RENDERABLE":
                reason = var_data.get("failure_reason") or "Превышен лимит площадки."
                text_content = f"⚠️ <b>Вариант {variant_name} не сформирован</b>\n\nПричина: {reason}"
            elif v_text:
                if variant_name == "YOUTUBE_COMMUNITY":
                    text_content = (
                        f"📝 <b>Вариант: YOUTUBE_COMMUNITY</b> ({char_count} знаков)\n"
                        f"<i>ℹ️ Официальный API YouTube не поддерживает публикацию постов в Community. Текст готов для ручного копирования:</i>\n\n"
                        f"{v_text}"
                    )
                else:
                    text_content = (
                        f"📝 <b>Вариант: {variant_name}</b> ({char_count} знаков)\n\n"
                        f"{v_text}"
                    )
            else:
                text_content = f"⚠️ Вариант {variant_name} недоступен."

            await callback.answer()
            await callback.message.answer(
                text_content[:4096],
                reply_markup=package_variant_keyboard(job_id, variant_name),
            )
            return

        elif action == "approve_all":
            if pkg_row.status == "REJECTED":
                await callback.answer("Пакет был отклонён и не может быть одобрен.", show_alert=True)
                return

            if pkg_row.status in ("APPROVED", "DELIVERED"):
                await callback.answer("Пакет уже одобрен!", show_alert=False)
                return

            pkg_obj = ContentPackage.model_validate({
                "package_id": pkg_row.id,
                "job_id": pkg_row.job_id,
                "source_url": pkg_row.source_url,
                "contract_version": pkg_row.contract_version,
                "created_at": pkg_row.created_at,
                "status": pkg_row.status,
                "language_context": pkg_row.language_context or {},
                "router_result": pkg_row.router_result or {},
                "priority_result": pkg_row.priority_result or {},
                "canonical_content": pkg_row.canonical_content,
                "output_variants": pkg_row.output_variants,
                "distribution_targets": pkg_row.distribution_targets,
                "approval_state": pkg_row.status,
                "delivery_records": [],
                "publication_intents": [],
            })

            pkg_obj, new_recs = approve_package(pkg_obj, callback.from_user.id, job.user_id, use_connectors=True)
            pkg_row.status = pkg_obj.approval_state.value
            pkg_row.distribution_targets = {
                k: v.model_dump(mode="json") for k, v in pkg_obj.distribution_targets.items()
            }
            pkg_row.updated_at = _to_naive_utc(datetime.now(timezone.utc))

            for r in new_recs:
                deliv_model = ContentDeliveryModel(
                    id=r.delivery_id,
                    package_id=r.package_id,
                    target=r.target.value,
                    variant=r.variant.value,
                    attempt_id=r.attempt_id,
                    status=r.status.value,
                    external_id=r.external_id,
                    idempotency_key=r.idempotency_key,
                    error_code=r.error_code,
                    error_message=r.error_message,
                    publication_key=r.publication_key,
                    payload_hash=r.payload_hash,
                    provider_post_id=r.provider_post_id,
                    provider_url=r.provider_url,
                    retry_count=r.retry_count,
                    next_retry_at=_to_naive_utc(r.next_retry_at),
                    started_at=_to_naive_utc(r.started_at) or _to_naive_utc(datetime.now(timezone.utc)),
                    finished_at=_to_naive_utc(r.finished_at),
                )
                session.add(deliv_model)

            for intent in pkg_obj.publication_intents:
                intent_model = PublicationIntentModel(
                    id=intent.intent_id,
                    package_id=intent.package_id,
                    job_id=intent.job_id,
                    target=intent.target.value,
                    variant=intent.variant.value,
                    approved_by=intent.approved_by,
                    approved_at=_to_naive_utc(intent.approved_at) or _to_naive_utc(datetime.now(timezone.utc)),
                    payload_hash=intent.payload_hash,
                    publication_key=intent.publication_key,
                    status=intent.status.value,
                    attempt_count=intent.attempt_count,
                    next_retry_at=_to_naive_utc(intent.next_retry_at),
                    last_error_code=intent.last_error_code,
                    last_error_message=intent.last_error_message,
                    provider_post_id=intent.provider_post_id,
                    provider_url=intent.provider_url,
                    created_at=_to_naive_utc(datetime.now(timezone.utc)),
                    updated_at=_to_naive_utc(datetime.now(timezone.utc)),
                )
                session.add(intent_model)

            await session.commit()

            has_pending = any(i.status.value == "PENDING" for i in pkg_obj.publication_intents)
            if has_pending:
                import asyncio

                from app.worker.tasks import execute_publication_intents
                asyncio.create_task(execute_publication_intents(pkg_obj.package_id))

            await callback.answer("✅ Решение по дистрибуции зафиксировано!")

            targets = pkg_row.distribution_targets
            status_text = (
                f"✅ <b>Контент-пакет одобрен для внешнего распространения</b>\n\n"
                f"Статус пакета: <b>{pkg_row.status}</b>\n\n"
                f"• <b>X (Twitter):</b> {targets.get('X', {}).get('status')}\n"
                f"• <b>Threads:</b> {targets.get('THREADS', {}).get('status')}\n"
                f"• <b>YouTube Community:</b> {targets.get('YOUTUBE_COMMUNITY', {}).get('status')}\n\n"
                f"<i>Материалы готовы для экспорта или доступны для копирования через кнопки выше.</i>"
            )
            try:
                await callback.message.edit_text(
                    status_text,
                    reply_markup=package_menu_keyboard(job_id, is_approved=True, is_rejected=False),
                    parse_mode="HTML",
                )
            except Exception:
                await callback.message.answer(
                    status_text,
                    reply_markup=package_menu_keyboard(job_id, is_approved=True, is_rejected=False),
                    parse_mode="HTML",
                )
            return

        elif action == "reject":
            if pkg_row.status in ("APPROVED", "DELIVERED"):
                await callback.answer("Нельзя отклонить уже одобренный пакет.", show_alert=True)
                return

            pkg_obj = ContentPackage.model_validate({
                "package_id": pkg_row.id,
                "job_id": pkg_row.job_id,
                "source_url": pkg_row.source_url,
                "contract_version": pkg_row.contract_version,
                "created_at": pkg_row.created_at,
                "status": pkg_row.status,
                "language_context": pkg_row.language_context or {},
                "router_result": pkg_row.router_result or {},
                "priority_result": pkg_row.priority_result or {},
                "canonical_content": pkg_row.canonical_content,
                "output_variants": pkg_row.output_variants,
                "distribution_targets": pkg_row.distribution_targets,
                "approval_state": pkg_row.status,
                "delivery_records": [],
            })

            pkg_obj = reject_package(pkg_obj, callback.from_user.id, job.user_id, "Rejected by user")
            pkg_row.status = pkg_obj.approval_state.value
            pkg_row.distribution_targets = {
                k: v.model_dump(mode="json") for k, v in pkg_obj.distribution_targets.items()
            }
            pkg_row.updated_at = _to_naive_utc(datetime.now(timezone.utc))
            await session.commit()

            await callback.answer("❌ Контент-пакет отклонён.")
            status_text = (
                "❌ <b>Контент-пакет отклонён</b>\n\n"
                "Экспорт на внешние площадки заблокирован."
            )
            try:
                await callback.message.edit_text(
                    status_text,
                    reply_markup=package_menu_keyboard(job_id, is_approved=False, is_rejected=True),
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return

        elif action == "regen":
            canonical = CanonicalContentResult.model_validate(pkg_row.canonical_content)
            v_type = OutputVariantType(variant_name)
            rendered = regenerate_variant_from_canonical(canonical, v_type)

            ov = dict(pkg_row.output_variants or {})
            if "variants" in ov:
                variants_dict = dict(ov["variants"])
                variants_dict[variant_name] = rendered.model_dump(mode="json")
                ov["variants"] = variants_dict
                pkg_row.output_variants = ov
                await session.commit()

            await callback.answer("🔄 Вариант пересоздан из канонических фактов!")
            new_text = (
                f"📝 <b>Вариант: {variant_name} (обновлён)</b>\n\n"
                f"{rendered.text or 'Не сформирован'}"
            )
            await callback.message.answer(
                new_text[:4096],
                reply_markup=package_variant_keyboard(job_id, variant_name),
                link_preview_options=types.LinkPreviewOptions(is_disabled=True),
            )
            return

        elif action == "back":
            payload = job.qa_reasons if job and isinstance(job.qa_reasons, dict) else {}
            sections = list((payload.get("detail_sections") or {}).keys())
            kb = analysis_keyboard(job_id, sections, bool(job.full_transcript))
            await callback.answer()
            if job.analysis_text:
                try:
                    await callback.message.edit_text(
                        job.analysis_text[:4096],
                        reply_markup=kb,
                        link_preview_options=types.LinkPreviewOptions(is_disabled=True),
                    )
                except Exception:
                    await callback.message.answer(
                        job.analysis_text[:4096],
                        reply_markup=kb,
                        link_preview_options=types.LinkPreviewOptions(is_disabled=True),
                    )
            return
