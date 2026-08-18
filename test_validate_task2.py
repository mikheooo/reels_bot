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
async def test_validation_task2():
    # Normal task: Local audio transcription via whisper.cpp CLI
    claims = [
        Claim(
            statement="Утилита whisper.cpp позволяет транскрибировать аудиофайлы локально на CPU и GPU без отправки данных в облако.",
            claim_type="fact",
            status="не проверено"
        ),
        Claim(
            statement="whisper.cpp поддерживает квантованные модели Q5_0 и Q8_0 для быстрой работы на машинах с 8GB RAM.",
            claim_type="fact",
            status="не проверено"
        ),
        Claim(
            statement="Для запуска требуется скомпилировать бинарник через make или cmake и передать аудио в формате wav 16kHz.",
            claim_type="fact",
            status="не проверено"
        )
    ]
    
    search_data = {
        c.statement: [
            SearchResult(
                url="https://github.com/ggerganov/whisper.cpp",
                title="whisper.cpp GitHub",
                domain="github.com",
                source_type="official",
                published_date="2025-06-01",
                text_snippet="Port of OpenAI's Whisper model in C/C++. High performance inference of OpenAI's Whisper model.",
                retrieved_at="2026-08-05"
            )
        ] for c in claims
    }
    
    print("Running validate_claims for Task 2 (whisper.cpp)...")
    analysis = await validate_claims(claims, search_data)
    
    print("\n\n=== ЗАДАЧА И КРИТЕРИИ ГОТОВНОСТИ (TASK 2) ===\n")
    if analysis.task_description:
        print(analysis.task_description)
    else:
        print("No task description generated.")

if __name__ == "__main__":
    asyncio.run(test_validation_task2())
