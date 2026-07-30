from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.worker.audit import apply_audit_edit, run_post_publish_audit
from app.worker.schemas import SearchResult


@pytest.mark.asyncio
async def test_audit_scenarios():
    print("=== TEST 1: NO CHANGES ===")
    job_no_changes = {
        "qa_reasons": {
            "analysis_json": {
                "claims": [
                    {"statement": "Test Fact", "claim_type": "fact", "status": "подтверждено", "source_url": "http://x", "exact_quote": "X", "source_type": "official"}
                ],
                "viable_idea": True
            }
        }
    }
    
    async def mock_search_exa(*args): return [SearchResult(url="http://x", domain="x", source_type="official", text_snippet="X", retrieved_at="now", title=None, published_date=None)]
    async def mock_check_no_change(*args): return {"changed": False}
    
    with patch("app.worker.audit.search_exa_for_claim", side_effect=mock_search_exa):
        with patch("app.worker.audit.check_claim_update", side_effect=mock_check_no_change):
            res = await run_post_publish_audit("job_1", job_no_changes)
            print("Action:", res["action"])
            assert res["action"] == "no_changes"
            assert res["qa_reasons"]["audit_history"][0]["result"] == "no_changes"

    print("\n=== TEST 2: EDIT FOUND & APPLIED ===")
    job_edit = {
        "tg_channel_message_id": 999,
        "qa_reasons": {
            "mechanics_text": "Mechanics",
            "analysis_json": {
                "claims": [
                    {"statement": "OpenAI Sora is private", "claim_type": "fact", "status": "подтверждено", "source_url": "http://x", "exact_quote": "Private", "source_type": "official"}
                ],
                "viable_idea": True
            }
        }
    }
    
    async def mock_check_change(*args): 
        return {
            "changed": True, 
            "diff_summary": "Sora is now public", 
            "new_status": "опровергнуто", 
            "new_source_url": "http://y", 
            "new_exact_quote": "Publicly available"
        }
        
    with patch("app.worker.audit.search_exa_for_claim", side_effect=mock_search_exa):
        with patch("app.worker.audit.check_claim_update", side_effect=mock_check_change):
            res_edit = await run_post_publish_audit("job_2", job_edit)
            print("Action:", res_edit["action"])
            print("Diff Text:\\n", res_edit["diff_text"])
            assert res_edit["action"] == "propose_edit"
            assert "pending_audit" in res_edit["qa_reasons"]
            
            # Now simulate User clicking "Apply"
            mock_bot = MagicMock()
            mock_bot.edit_message_text = AsyncMock()
            
            # apply_audit_edit needs job_dict with pending_audit
            job_dict_with_pending = {"tg_channel_message_id": 999, "qa_reasons": res_edit["qa_reasons"]}
            apply_res = await apply_audit_edit("job_2", job_dict_with_pending, mock_bot, user_id=123)
            
            assert apply_res["success"] is True
            mock_bot.edit_message_text.assert_called_once()
            print("Bot edit_message_text called successfully.")
            assert apply_res["qa_reasons"]["audit_history"][-1]["result"] == "edit_applied"
            assert "pending_audit" not in apply_res["qa_reasons"]

    print("\\n=== TEST 3: SEARCH ERROR ===")
    async def mock_search_error(*args): raise Exception("Network Timeout")
    
    with patch("app.worker.audit.search_exa_for_claim", side_effect=mock_search_error):
        res_err = await run_post_publish_audit("job_3", job_edit)
        # Should gracefully return no_changes because exceptions are caught
        print("Action on Error:", res_err["action"])
        assert res_err["action"] == "no_changes"
        
