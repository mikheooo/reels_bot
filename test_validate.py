import logging
logging.basicConfig(level=logging.INFO)
import asyncio
import os
import sys
import pytest
from dotenv import load_dotenv

pytestmark = pytest.mark.asyncio

os.chdir(os.path.abspath("C:/Users/Misha/reels_bot"))
sys.path.insert(0, os.path.abspath("C:/Users/Misha/reels_bot"))
load_dotenv(os.path.abspath("C:/Users/Misha/reels_bot/.env"))

from app.worker.factcheck import validate_claims
from app.worker.schemas import Claim, SearchResult

@pytest.mark.integration
async def test_validation():
    # Mock claims based on the video about 5 Claude Code plugins
    claims = [
        Claim(
            statement="Существует плагин OmniRoute для Claude Code, который маршрутизирует запросы через бесплатные API.",
            claim_type="fact",
            status="не проверено"
        ),
        Claim(
            statement="Claude Mem сохраняет контекст проекта между сессиями.",
            claim_type="fact",
            status="не проверено"
        ),
        Claim(
            statement="Headroom сжимает контекст на 70%.",
            claim_type="fact",
            status="не проверено"
        ),
        Claim(
            statement="Claude Code Setup сканирует проект и рекомендует хуки.",
            claim_type="fact",
            status="не проверено"
        ),
        Claim(
            statement="Task Observer работает в фоне и оптимизирует промпты.",
            claim_type="fact",
            status="не проверено"
        )
    ]
    
    # Mock search data to simulate successful verification
    search_data = {
        c.statement: [
            SearchResult(
                url="https://github.com/example/plugin-repo",
                title="GitHub Repo",
                domain="github.com",
                source_type="official",
                published_date="2025-01-01",
                text_snippet="Официальный репозиторий инструмента. Подтверждает его функции.",
                retrieved_at="2026-08-05"
            )
        ] for c in claims
    }
    
    print("Running validate_claims with new prompt...")
    analysis = await validate_claims(claims, search_data)
    
    print("\n\n=== ЗАДАЧА И КРИТЕРИИ ГОТОВНОСТИ ===\n")
    if analysis.task_description:
        print(analysis.task_description)
    else:
        print("No task description generated.")

if __name__ == "__main__":
    asyncio.run(test_validation())
