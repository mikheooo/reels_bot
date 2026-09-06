import json

import httpx
import pytest

from app.core.config import settings
from app.worker import factcheck
from app.worker.language import (
    detect_language_context,
    language_delivery_outcome,
    normalize_language_code,
    resolve_language_context,
)
from app.worker.schemas import Claim, ClaimSearchQuery
from app.worker.tasks import determine_completion_status


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Это подробная инструкция о том, как сохранить резервную копию.", "ru"),
        ("This is a detailed guide about how to create a backup.", "en"),
        ("นี่คือคำแนะนำโดยละเอียดเกี่ยวกับวิธีสร้างข้อมูลสำรอง", "th"),
        ("Це пояснення про те, як потрібно перевірити результат.", "uk"),
        ("Esta es una guía para crear una copia de seguridad.", "es"),
    ],
)
def test_language_detection(text, code):
    assert detect_language_context(text).detected_language_code == code


@pytest.mark.parametrize(
    ("text", "dominant", "additional"),
    [
        ("Показываю подробный workflow для GitHub Actions and deployment pipeline.", "ru", "en"),
        ("นี่คือขั้นตอนการตั้งค่าระบบ deployment pipeline with GitHub Actions", "th", "en"),
    ],
)
def test_mixed_language_detection(text, dominant, additional):
    context = detect_language_context(text)
    assert context.detected_language_code == dominant
    assert context.is_mixed_language is True
    assert context.source_languages[0] == dominant
    assert additional in context.source_languages


def test_unknown_language_fallback_and_output_policy():
    context = detect_language_context("123 --- ???")
    assert context.detected_language_code == "unknown"
    assert context.fallback_reason
    assert context.analysis_language_code == "ru"
    assert context.user_output_language_code == "ru"
    assert context.channel_output_language_code == "ru"


def test_unsupported_language_falls_back_without_error():
    context = detect_language_context("Bonjour monde système rapide")
    assert context.detected_language_code == "unknown"
    assert language_delivery_outcome(context) == "SUCCEEDED_UNKNOWN_FALLBACK"


def test_visible_original_text_can_add_secondary_language():
    context = detect_language_context(
        "Это подробное объяснение настройки системы.",
        visual_evidence={
            "evidence": [{"text_read": "GitHub Actions deployment workflow"}]
        },
    )
    assert context.detected_language_code == "ru"
    assert context.is_mixed_language is True
    assert "en" in context.source_languages


def test_provider_metadata_precedes_transcript_detection():
    context = detect_language_context("This is an English text.", {"language": "th-TH"})
    assert context.detected_language_code == "th"
    assert context.detection_method == "provider_metadata"
    assert normalize_language_code("uk-UA") == "uk"


def test_original_transcript_hash_proves_source_preservation():
    transcript = "Texto original sin traducción."
    context = detect_language_context(transcript)
    assert context.original_transcript_sha256 == detect_language_context(transcript).original_transcript_sha256
    assert transcript == "Texto original sin traducción."


@pytest.mark.asyncio
async def test_detection_failure_is_observable_and_not_false_done(monkeypatch):
    def explode(*args, **kwargs):
        raise TimeoutError("detector timeout")

    monkeypatch.setattr("app.worker.language.detect_language_context", explode)
    context = await resolve_language_context("safe analyzable transcript")
    assert context.detected_language_code == "unknown"
    assert context.failure_code == "TimeoutError"
    outcome = language_delivery_outcome(context)
    assert outcome == "FAILED_FALLBACK:TimeoutError"
    assert determine_completion_status({"language": outcome, "user": "SUCCEEDED"}) == "PARTIAL"


@pytest.mark.asyncio
async def test_claim_translation_keeps_original(monkeypatch):
    context = detect_language_context("This tool reduces processing time by half.")

    async def fake_call(payload):
        return {
            "candidates": [{"content": {"parts": [{"text": json.dumps([{
                "statement": "Инструмент сокращает время обработки вдвое.",
                "original_statement": "This tool reduces processing time by half.",
                "analysis_statement": "Инструмент сокращает время обработки вдвое.",
                "original_language_code": "en",
                "english_search_query": None,
                "claim_type": "fact",
                "status": "не проверено",
                "semantic_category": "PERFORMANCE_CLAIM",
                "relevance_score": 0.9,
                "nature": "UNVERIFIED_PUBLIC",
            }])}]}}]
        }

    monkeypatch.setattr(factcheck, "call_gemini_api", fake_call)
    claim = (await factcheck.extract_claims(
        "This tool reduces processing time by half.", context
    ))[0]
    assert claim.original_statement == "This tool reduces processing time by half."
    assert claim.source_quote == claim.original_statement
    assert claim.statement == "Инструмент сокращает время обработки вдвое."
    assert claim.translation_applied is True
    assert claim.translation_status == "PROVIDED"


@pytest.mark.asyncio
async def test_non_english_claim_uses_original_and_english_search(monkeypatch):
    monkeypatch.setattr(settings, "exa_api_key", "test-key")
    captured = []

    def handler(request):
        payload = json.loads(request.content)
        captured.append(payload["query"])
        return httpx.Response(
            200,
            json={"results": [{
                "url": f"https://example.com/{len(captured)}",
                "title": "source",
                "text": "evidence",
            }]},
        )

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        return original_client(transport=transport)

    monkeypatch.setattr(factcheck.httpx, "AsyncClient", client_factory)
    claim = Claim(
        statement="Заявлена новая функция.",
        original_statement="Було заявлено нову функцію.",
        original_language_code="uk",
        claim_type="fact",
        status="не проверено",
        search_queries=[
            ClaimSearchQuery(
                text="Було заявлено нову функцію.",
                language_code="uk",
                purpose="original",
            ),
            ClaimSearchQuery(
                text="new feature announcement",
                language_code="en",
                purpose="english_coverage",
            ),
        ],
    )
    results = await factcheck.search_exa_for_claim(claim)
    assert captured == ["Було заявлено нову функцію.", "new feature announcement"]
    assert {item.query_language_code for item in results} == {"uk", "en"}
