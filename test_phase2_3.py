import pytest
import asyncio
import os
import sys
import json
from unittest.mock import patch, MagicMock




from app.worker.factcheck import validate_claims
from app.worker.schemas import Claim, SearchResult

@pytest.mark.asyncio
async def test_secondary_source_refutation():
    c = Claim(statement="OpenAI полностью закрывает бесплатный доступ к ChatGPT.", claim_type="fact", status="не проверено")
    
    # Mocking Exa giving ONLY a secondary source (vc.ru) that strongly contradicts
    search_data = {
        c.statement: [
            SearchResult(
                url="https://vc.ru/ai/news",
                title="News",
                domain="vc.ru",
                source_type="other",
                published_date="2026-07-09",
                text_snippet="Это ложь, OpenAI не закрывает доступ, а переводит всех на новую бесплатную модель.",
                retrieved_at="now"
            )
        ]
    }
    
    class MockAsyncClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, exc_type, exc_val, exc_tb): pass
        async def post(self, url, **kwargs):
            return MagicMock(json=lambda: {
                "candidates": [{
                    "content": {
                        "parts": [{
                            "text": json.dumps({
                                "claims": [
                                    {"statement": c.statement, "claim_type": "fact", "status": "не проверено", "source_type": "other", "unverified_reason": "Только вторичный источник. Нельзя опровергнуть."}
                                ],
                                "viable_idea": False,
                                "task_description": None
                            })
                        }]
                    }
                }]
            }, raise_for_status=lambda: None)

    with patch('google.auth.default', return_value=(MagicMock(token="dummy"), None)):
        with patch('httpx.AsyncClient', new=MockAsyncClient):
            analysis = await validate_claims([c], search_data)
            
            assert analysis.claims[0].status == "не проверено", f"Expected 'не проверено', got {analysis.claims[0].status}"
            print("Rule Test Passed: Secondary source cannot refute a claim. Status remained 'не проверено'.")

