import pytest
import asyncio
import sys
import json
from unittest.mock import patch, MagicMock

import os








from app.worker.factcheck import qa_audit
from app.worker.schemas import Claim, VideoAnalysis, SearchResult

@pytest.mark.asyncio
async def test_qa():
    c1 = Claim(
        statement="Google Search Grounding costs $10 per 1k requests", 
        claim_type="fact", 
        status="подтверждено",
        source_url="https://old.blog/pricing",
        exact_quote="Costs 10 dollars",
        source_type="other"
    )
    analysis = VideoAnalysis(claims=[c1], viable_idea=True, task_description="Task")

    async def mock_search_exa(c):
        return [SearchResult(
            url="https://cloud.google.com/new-pricing",
            title="New Pricing",
            domain="cloud.google.com",
            source_type="official",
            published_date="2026",
            text_snippet="Pricing changed",
            retrieved_at="now"
        )]

    class MockAsyncClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, **kwargs):
            if "old.blog" in url:
                return MagicMock(text="Costs 10 dollars old data", raise_for_status=lambda: None)
            if "cloud.google.com" in url:
                return MagicMock(text="Search Grounding is now FREE.", raise_for_status=lambda: None)
            return MagicMock(text="", raise_for_status=lambda: None)
            
        async def post(self, url, **kwargs):
            return MagicMock(json=lambda: {
                "candidates": [{"content": {"parts": [{"text": '{"approved": false, "reason": "Fresh official source cloud.google.com says it is free, contradicting the old blog."}'}]}}]
            }, raise_for_status=lambda: None)

    with patch('google.auth.default', return_value=(MagicMock(token="dummy"), None)):
        with patch('app.worker.factcheck.search_exa_for_claim', side_effect=mock_search_exa):
            with patch('httpx.AsyncClient', new=MockAsyncClient):
                res = await qa_audit(analysis)
                assert res.approved is False
                assert "Fresh official source" in res.reasons[0]
                print("Test Passed: QA correctly rejected outdated claim based on fresh independent search.")

