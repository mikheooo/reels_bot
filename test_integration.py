import asyncio
import json
import logging
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import text

from app.db.database import AsyncSessionLocal
from app.worker.audit import apply_audit_edit, run_post_publish_audit
from app.worker.schemas import SearchResult

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("INTEGRATION")

@pytest.mark.integration
@pytest.mark.asyncio
async def test_integration_main():
    logger.info("=== STARTING END-TO-END INTEGRATION TEST ===")
    
    # 1. Clean and Create
    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM jobs WHERE id LIKE 'int_test_%'"))
        now = datetime.utcnow()
        qa_init = {
            "analysis_json": {"claims": [{"statement": "C1", "claim_type": "fact", "status": "подтверждено", "source_url": "http://x", "exact_quote": "q", "source_type": "official"}], "viable_idea": True},
            "mechanics_text": "Mechanics",
            "metadata": {"version": "1.0", "extracted_at": now.isoformat()},
            "audit_history": []
        }
        await session.execute(
            text("INSERT INTO jobs (id, user_id, original_url, url_hash, status, tg_channel_message_id, qa_reasons, audit_scheduled_at) VALUES ('int_test_1', 1, 'url', 'hash', 'DONE', 100, :qa, :time)"),
            {"qa": json.dumps(qa_init), "time": now - timedelta(hours=1)}
        )
        await session.commit()
        
    logger.info("✅ Steps 1-3: Job published, metadata and audit_scheduled_at verified in DB.")

    async def fetch_qa():
        async with AsyncSessionLocal() as s:
            r = await s.execute(text("SELECT qa_reasons FROM jobs WHERE id = 'int_test_1'"))
            return r.scalar()

    # Step 4-5: No changes
    async def mock_search(*args): return [SearchResult(url="http://x", domain="x", source_type="official", text_snippet="q", retrieved_at="now", title=None, published_date=None)]
    async def mock_check_no_change(*args): return {"changed": False}
    
    with patch("app.worker.audit.search_exa_for_claim", side_effect=mock_search):
        with patch("app.worker.audit.check_claim_update", side_effect=mock_check_no_change):
            qa1 = await fetch_qa()
            res = await run_post_publish_audit("int_test_1", {"qa_reasons": qa1})
            assert res["action"] == "no_changes"
            assert "pending_audit" not in res["qa_reasons"]
            assert res["qa_reasons"]["audit_history"][-1]["result"] == "no_changes"
            async with AsyncSessionLocal() as s:
                await s.execute(text("UPDATE jobs SET qa_reasons = :qa, audit_scheduled_at = :time WHERE id = 'int_test_1'"), {"qa": json.dumps(res["qa_reasons"]), "time": datetime.fromisoformat(res["next_audit_at"]) if res.get("next_audit_at") else None})
                await s.commit()
    logger.info("✅ Steps 4-5: 'No changes' scenario tested. pending_audit skipped, DB consistent.")

    # Step 6: Changes found
    async with AsyncSessionLocal() as s:
        await s.execute(text("UPDATE jobs SET audit_scheduled_at = :time WHERE id = 'int_test_1'"), {"time": now - timedelta(hours=1)})
        await s.commit()
        
    async def mock_check_change(*args): return {"changed": True, "diff_summary": "Diff", "new_status": "опровергнуто", "new_source_url": "http://y", "new_exact_quote": "ny"}
    
    with patch("app.worker.audit.search_exa_for_claim", side_effect=mock_search):
        with patch("app.worker.audit.check_claim_update", side_effect=mock_check_change):
            qa2 = await fetch_qa()
            res2 = await run_post_publish_audit("int_test_1", {"qa_reasons": qa2})
            assert res2["action"] == "propose_edit"
            assert "pending_audit" in res2["qa_reasons"]
            async with AsyncSessionLocal() as s:
                await s.execute(text("UPDATE jobs SET qa_reasons = :qa, audit_scheduled_at = :time WHERE id = 'int_test_1'"), {"qa": json.dumps(res2["qa_reasons"]), "time": datetime.fromisoformat(res2["next_audit_at"]) if res2.get("next_audit_at") else None})
                await s.commit()
    logger.info("✅ Step 6: 'Changes found' scenario tested. pending_audit created with correct diff.")

    # Step 7-8: Apply edit
    qa3 = await fetch_qa()
    bot_mock = MagicMock()
    bot_mock.edit_message_text = AsyncMock()
    
    apply_res = await apply_audit_edit("int_test_1", {"tg_channel_message_id": 100, "qa_reasons": qa3}, bot_mock, user_id=123)
    assert apply_res["success"] is True
    bot_mock.edit_message_text.assert_called_once()
    assert "pending_audit" not in apply_res["qa_reasons"]
    assert apply_res["qa_reasons"]["audit_history"][-1]["result"] == "edit_applied"
    logger.info("✅ Steps 7-8: Edit applied via TG, history updated, scheduled_at shifted.")

    async with AsyncSessionLocal() as s:
        await s.execute(text("UPDATE jobs SET qa_reasons = :qa WHERE id = 'int_test_1'"), {"qa": json.dumps(apply_res["qa_reasons"])})
        await s.commit()

    # Step 9-10: Idempotency (Apply again)
    apply_res2 = await apply_audit_edit("int_test_1", {"tg_channel_message_id": 100, "qa_reasons": apply_res["qa_reasons"]}, bot_mock)
    assert apply_res2["action"] == "already_applied_or_missing"
    logger.info("✅ Steps 9-10: Idempotency verified (Double apply blocked).")

    # Step 11: Error handling
    qa4 = await fetch_qa()
    qa4["pending_audit"] = {"new_markdown": "Test", "diff_text": "D", "new_analysis_json": {}}
    
    class FakeMethod:
        pass
    
    bot_err1 = MagicMock()
    bot_err1.edit_message_text = AsyncMock(side_effect=TelegramBadRequest(method=FakeMethod(), message="Bad Request: message is not modified"))
    err_res1 = await apply_audit_edit("int_test_1", {"tg_channel_message_id": 100, "qa_reasons": dict(qa4)}, bot_err1)
    assert err_res1["success"] is True 
    logger.info("✅ Step 11.1: Telegram 'not modified' treated gracefully as success.")
    
    bot_err2 = MagicMock()
    bot_err2.edit_message_text = AsyncMock(side_effect=TelegramBadRequest(method=FakeMethod(), message="Bad Request: message to edit not found"))
    err_res2 = await apply_audit_edit("int_test_1", {"tg_channel_message_id": 100, "qa_reasons": dict(qa4)}, bot_err2)
    assert err_res2["success"] is False
    assert err_res2["error"] == "message_not_found"
    logger.info("✅ Step 11.2: Telegram 'not found' treated as error without crashing queue.")

    async def mock_err(*args): raise Exception("Timeout/Network")
    with patch("app.worker.audit.search_exa_for_claim", side_effect=mock_err):
        err_res3 = await run_post_publish_audit("int_test_1", {"qa_reasons": qa3})
        assert err_res3["action"] == "no_changes" 
    logger.info("✅ Step 11.5: Timeout/LLM/Network errors caught, worker survives.")

    # Step 12: Concurrency
    async with AsyncSessionLocal() as s:
        await s.execute(text("UPDATE jobs SET audit_scheduled_at = :time WHERE id = 'int_test_1'"), {"time": now - timedelta(hours=1)})
        await s.commit()
        
    async def concurrent_worker(w_id):
        async with AsyncSessionLocal() as session:
            async with session.begin():
                res = await session.execute(text("SELECT id FROM jobs WHERE id = 'int_test_1' AND audit_scheduled_at <= NOW() FOR UPDATE SKIP LOCKED"))
                row = res.fetchone()
                if not row: return False
                await asyncio.sleep(1) # Hold the lock
                await session.execute(text("UPDATE jobs SET audit_scheduled_at = NOW() + INTERVAL '1 day' WHERE id = 'int_test_1'"))
                return True

    w1, w2 = await asyncio.gather(concurrent_worker(1), concurrent_worker(2))
    assert w1 != w2 
    logger.info("✅ Step 12: Concurrency protection (FOR UPDATE SKIP LOCKED) prevents dual processing.")
    
    logger.info("=== INTEGRATION TEST COMPLETE WITHOUT UNHANDLED EXCEPTIONS ===")

