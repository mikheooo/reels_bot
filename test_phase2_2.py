import pytest
import asyncio
import sys
import json
from unittest.mock import patch, MagicMock

import os








from app.worker.factcheck import extract_claims, search_exa_for_claim, validate_claims, qa_audit
from app.worker.schemas import Claim, SearchResult, VideoAnalysis

@pytest.mark.asyncio
async def test_refinements():
    c1 = Claim(statement="Google closed down", claim_type="fact", status="не проверено")
    c2 = Claim(statement="Gemini context window is 1M", claim_type="fact", status="не проверено")

    class MockAsyncClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, exc_type, exc_val, exc_tb): pass
        async def post(self, url, **kwargs):
            if "api.exa.ai" in url:
                if "closed down" in kwargs.get("json", {}).get("query", ""):
                    return MagicMock(json=lambda: {"results": []}, raise_for_status=lambda: None)
                return MagicMock(json=lambda: {"results": [{"url": "https://randomblog.com/post1", "title": "Blog", "text": "Gemini 1M context", "publishedDate": "2026"}]}, raise_for_status=lambda: None)
            
            if "generateContent" in url:
                return MagicMock(json=lambda: {
                    "candidates": [{
                        "content": {
                            "parts": [{
                                "text": json.dumps({
                                    "claims": [
                                        {"statement": "Google closed down", "claim_type": "fact", "status": "не проверено", "unverified_reason": "No sources"},
                                        {"statement": "Gemini context window is 1M", "claim_type": "fact", "status": "не проверено", "source_type": "other", "unverified_reason": "Not an official source"}
                                    ],
                                    "viable_idea": False,
                                    "task_description": None
                                })
                            }]
                        }
                    }]
                }, raise_for_status=lambda: None)
            raise ValueError()

    with patch('google.auth.default', return_value=(MagicMock(token="dummy"), None)):
        with patch('httpx.AsyncClient', new=MockAsyncClient):
            s1 = await search_exa_for_claim(c1)
            s2 = await search_exa_for_claim(c2)
            
            assert len(s1) == 0, "Empty results should be 0 length"
            assert len(s2) == 1, "Fallback results should be captured"
            assert s2[0].domain == "randomblog.com"
            assert s2[0].source_type == "other"
            
            search_data = {c1.statement: s1, c2.statement: s2}
            analysis = await validate_claims([c1, c2], search_data)
            
            assert analysis.claims[0].status == "не проверено"
            assert analysis.claims[1].status == "не проверено"
            assert analysis.viable_idea is False
            print("Step C Refinement Tests Passed (Empty Results & Broad Fallbacks gracefully handled).")

