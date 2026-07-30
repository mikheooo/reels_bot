import pytest
import asyncio
import os
import sys
import json
from datetime import datetime
from unittest.mock import patch, MagicMock




from app.worker.factcheck import validate_claims
from app.worker.schemas import Claim, SearchResult

@pytest.mark.asyncio
async def test_relative_dates():
    # It is 2026-07-30
    c1 = Claim(statement="Вчера вышла новая модель Gemini 1.5 Pro.", claim_type="fact", status="не проверено")
    c2 = Claim(statement="В прошлом году Google анонсировал Bard.", claim_type="fact", status="не проверено")
    
    search_data = {
        c1.statement: [
            SearchResult(
                url="https://blog.google/gemini-1-5",
                title="Gemini 1.5",
                domain="blog.google",
                source_type="official",
                published_date="2024-02-15", # Way too old to be "yesterday"
                text_snippet="We are releasing Gemini 1.5 Pro today.",
                retrieved_at="now"
            )
        ],
        c2.statement: [
            SearchResult(
                url="https://blog.google/bard",
                title="Bard",
                domain="blog.google",
                source_type="official",
                published_date="2023-02-06", # 2023 is not "last year" relative to 2026
                text_snippet="Introducing Bard.",
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
                                    {"statement": c1.statement, "claim_type": "fact", "status": "опровергнуто", "source_url": "https://blog.google/gemini-1-5", "exact_quote": "We are releasing Gemini 1.5 Pro today.", "source_type": "official", "unverified_reason": "Источник от 2024 года, а не вчера."},
                                    {"statement": c2.statement, "claim_type": "fact", "status": "опровергнуто", "source_url": "https://blog.google/bard", "exact_quote": "Introducing Bard.", "source_type": "official", "unverified_reason": "Источник от 2023 года, а не в прошлом (2025) году."}
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
            analysis = await validate_claims([c1, c2], search_data)
            
            assert analysis.claims[0].status in ["опровергнуто", "не проверено"], f"Expected 'опровергнуто' or 'не проверено', got {analysis.claims[0].status}"
            assert analysis.claims[1].status in ["опровергнуто", "не проверено"], f"Expected 'опровергнуто' or 'не проверено', got {analysis.claims[1].status}"
            print("Relative Dates Test Passed: LLM correctly rejects outdated sources for 'yesterday' and 'last year'.")

