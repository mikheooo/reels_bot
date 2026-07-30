
import json
import logging
from datetime import datetime, timedelta

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest

from app.core.config import settings
from app.worker.factcheck import get_vertex_token, search_exa_for_claim
from app.worker.schemas import Claim, VideoAnalysis

logger = logging.getLogger(__name__)

def format_analysis_markdown(analysis: VideoAnalysis, mechanics_text: str) -> str:
    parts = []
    if mechanics_text:
        parts.append(mechanics_text)
    
    parts.append("🔎 **Проверка фактов:**")
    for c in analysis.claims:
        if c.status == "подтверждено":
            parts.append(f"- ✅ [Подтверждено] {c.statement}\n  (Источник: [{c.source_type}] {c.source_url})")
        elif c.status == "опровергнуто":
            parts.append(f"- ❌ [Опровергнуто] {c.statement}\n  (Источник: {c.source_url})")
        elif c.status == "не проверено":
            parts.append(f"- 🟡 [Не проверено] {c.statement} ({c.unverified_reason or 'Нет надежных источников'})")
    
    if analysis.task_description:
        parts.append(f"\nЗАДАЧА:\n{analysis.task_description}")
        
    return "\n\n".join(parts)

async def check_claim_update(old_claim: Claim, search_results: list) -> dict:
    if not search_results:
        return {"changed": False, "reason": "No search results"}

    token = await get_vertex_token()
    project_id = "project-77ee3790-ced5-43a7-991"
    url = f"https://aiplatform.googleapis.com/v1beta1/projects/{project_id}/locations/global/publishers/google/models/gemini-2.5-flash:generateContent"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    sources_text = "\n".join([f"URL: {s.url}\nТекст: {s.text_snippet[:1500]}" for s in search_results[:3]])

    prompt = f"""
СТАРОЕ УТВЕРЖДЕНИЕ: {old_claim.statement}
СТАРЫЙ СТАТУС: {old_claim.status}
СТАРАЯ ЦИТАТА: {old_claim.exact_quote}
СТАРЫЙ ИСТОЧНИК: {old_claim.source_url}

СВЕЖИЕ ИСТОЧНИКИ:
{sources_text}

ПРАВИЛО: Изучи свежие источники. Изменился ли статус или факты?
Если данные реально изменились, верни changed: true, опиши diff_summary, и дай новые status, source_url, exact_quote.
Если всё по-прежнему, верни changed: false.
"""

    schema = {
        "type": "OBJECT",
        "properties": {
            "changed": {"type": "BOOLEAN"},
            "diff_summary": {"type": "STRING", "nullable": True},
            "new_status": {"type": "STRING", "enum": ["подтверждено", "опровергнуто", "не проверено"], "nullable": True},
            "new_source_url": {"type": "STRING", "nullable": True},
            "new_exact_quote": {"type": "STRING", "nullable": True}
        }, "required": ["changed"]
    }
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json", "responseSchema": schema}}

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = json.loads(resp.json()["candidates"][0]["content"]["parts"][0]["text"])
            return data
    except Exception as e:
        logger.error(f"check_claim_update API error: {e}")
        return {"changed": False, "error": str(e)}

async def run_post_publish_audit(job_id: str, job_dict: dict) -> dict:
    logger.info(f"Starting post-publish audit for job {job_id}")
    qa_reasons = job_dict.get("qa_reasons", {})
    
    # Idempotency: skip if an audit is already pending user action
    if "pending_audit" in qa_reasons:
        logger.info(f"Job {job_id} already has a pending audit. Skipping.")
        return {"action": "skip", "reason": "already_pending"}
        
    analysis_data = qa_reasons.get("analysis_json")
    if not analysis_data:
        logger.warning(f"Job {job_id} has no analysis_json. Skipping.")
        return {"action": "skip", "reason": "no_analysis_data"}
        
    analysis = VideoAnalysis(**analysis_data)
    mechanics_text = qa_reasons.get("mechanics_text", "")
    
    changes_found = []
    new_claims = []
    
    for c in analysis.claims:
        if c.status in ["подтверждено", "опровергнуто"]:
            try:
                search_res = await search_exa_for_claim(c)
                eval_res = await check_claim_update(c, search_res)
                if eval_res.get("changed"):
                    changes_found.append({
                        "statement": c.statement,
                        "diff_summary": eval_res.get("diff_summary"),
                        "old_status": c.status,
                        "new_status": eval_res.get("new_status")
                    })
                    c.status = eval_res["new_status"]
                    c.source_url = eval_res["new_source_url"]
                    c.exact_quote = eval_res["new_exact_quote"]
            except Exception as e:
                logger.error(f"Audit search/eval error for job {job_id}, claim '{c.statement}': {e}")
        new_claims.append(c)

    analysis.claims = new_claims
    # Next audit in 7 days
    next_audit_at = (datetime.utcnow() + timedelta(days=7)).isoformat()
    
    if not changes_found:
        logger.info(f"Audit completed for job {job_id}: No changes found.")
        qa_reasons.setdefault("audit_history", []).append({
            "date": datetime.utcnow().isoformat(),
            "result": "no_changes",
            "reason": "All facts remain up to date."
        })
        return {"action": "no_changes", "qa_reasons": qa_reasons, "next_audit_at": next_audit_at}
        
    logger.info(f"Audit completed for job {job_id}: Changes found ({len(changes_found)}).")
    new_markdown = format_analysis_markdown(analysis, mechanics_text)
    
    diff_text = "🔄 **Отложенный Аудит нашел изменения:**\n"
    for ch in changes_found:
        diff_text += f"- {ch['statement']}\n  *{ch['old_status']}* -> *{ch['new_status']}*\n  Причина: {ch['diff_summary']}\n"
        
    qa_reasons["pending_audit"] = {
        "diff_text": diff_text,
        "new_markdown": new_markdown,
        "new_analysis_json": analysis.model_dump(),
        "created_at": datetime.utcnow().isoformat()
    }
    
    return {
        "action": "propose_edit",
        "diff_text": diff_text,
        "qa_reasons": qa_reasons,
        "next_audit_at": next_audit_at
    }

async def apply_audit_edit(job_id: str, job_dict: dict, bot: Bot, user_id: int = None) -> dict:
    logger.info(f"Applying audit edit for job {job_id} by user {user_id}")
    qa_reasons = job_dict.get("qa_reasons", {})
    pending = qa_reasons.get("pending_audit")
    
    # Idempotency: if already applied or missing
    if not pending:
        logger.warning(f"Apply edit called for job {job_id} but no pending_audit exists.")
        return {"success": True, "action": "already_applied_or_missing"}
        
    channel_msg_id = job_dict.get("tg_channel_message_id")
    if not channel_msg_id:
        logger.error(f"Cannot apply edit for job {job_id}: tg_channel_message_id is missing.")
        return {"success": False, "error": "missing_channel_msg_id"}
        
    try:
        await bot.edit_message_text(
            chat_id=settings.channel_chat_id,
            message_id=channel_msg_id,
            text=pending["new_markdown"]
        )
        logger.info(f"Message {channel_msg_id} successfully edited in channel.")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            logger.warning(f"Message {channel_msg_id} was not modified (already identical). Treating as success.")
        elif "message to edit not found" in str(e).lower():
            logger.error(f"Message {channel_msg_id} not found in channel. It might have been deleted.")
            return {"success": False, "error": "message_not_found"}
        else:
            logger.error(f"Telegram bad request editing job {job_id}: {e}")
            return {"success": False, "error": str(e)}
    except TelegramAPIError as e:
        logger.error(f"Telegram API error editing job {job_id}: {e}")
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"Unexpected error editing job {job_id}: {e}")
        return {"success": False, "error": str(e)}
        
    qa_reasons.setdefault("audit_history", []).append({
        "date": datetime.utcnow().isoformat(),
        "result": "edit_applied",
        "diff": pending["diff_text"],
        "applied_by": user_id
    })
    qa_reasons["analysis_json"] = pending["new_analysis_json"]
    del qa_reasons["pending_audit"]
    
    logger.info(f"Audit edit applied successfully for job {job_id}.")
    return {"success": True, "qa_reasons": qa_reasons}

# --- EXAMPLE CRON JOB CONCURRENCY QUERY ---
# This is a reference implementation for the cron scheduler (e.g., ARQ cron or AsyncIOScheduler)
# async def cron_audit_jobs(db_session):
#     # FOR UPDATE SKIP LOCKED prevents multiple workers from grabbing the same job simultaneously
#     query = """
#         SELECT * FROM jobs 
#         WHERE status = 'DONE' AND audit_scheduled_at <= NOW()
#         FOR UPDATE SKIP LOCKED
#         LIMIT 5;
#     """
#     # Execute query, for each job run `run_post_publish_audit`, then update `qa_reasons` and `audit_scheduled_at`.
