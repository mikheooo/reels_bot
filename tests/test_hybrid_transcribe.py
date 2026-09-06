"""Deterministic tests for the hybrid transcription orchestrator.

The 3.5 model, the legacy path and ffprobe are all faked: no network,
no API keys, no media files.
"""

from unittest.mock import AsyncMock, patch

from app.worker import tasks
from app.worker.transcribe_35 import (
    assess_transcript_completeness,
    extract_transcription_text,
    is_transcript_complete,
)


def test_extract_joins_audio_transcription_parts():
    data = {"candidates": [{"content": {"parts": [
        {"audioTranscription": {"text": "Hello "}},
        {"audioTranscription": {"text": "world"}},
        {"text": "ignored plain part"},
    ]}}]}
    assert extract_transcription_text(data) == "Hello world"


def test_extract_empty_response():
    assert extract_transcription_text({}) == ""
    assert extract_transcription_text({"candidates": []}) == ""
    assert extract_transcription_text(
        {"candidates": [{"content": {"parts": []}}]}) == ""
    assert extract_transcription_text(
        {"candidates": [{"content": {"parts": [{"text": "nope"}]}}]}) == ""


def test_assess_failed_and_ok():
    assert assess_transcript_completeness("", 60.0) == "FAILED"
    assert assess_transcript_completeness("   ", 60.0) == "FAILED"
    assert assess_transcript_completeness("x", 60.0) == "FAILED"
    assert assess_transcript_completeness("x" * 1000, 60.0) == "OK"
    # unknown duration: trust non-empty
    assert assess_transcript_completeness("short", 0.0) == "OK"
    assert assess_transcript_completeness("short", None) == "OK"


def test_is_transcript_complete_strict():
    assert is_transcript_complete("x" * 1000, 60.0) is True
    assert is_transcript_complete("x" * 100, 60.0) is False
    assert is_transcript_complete("", 60.0) is False


def test_suspicious_is_short_for_duration():
    # 600s needs 4000 chars, 100 is suspicious, not failed
    assert assess_transcript_completeness("x" * 500, 600.0) == "SUSPICIOUS"
    assert assess_transcript_completeness("x" * 5000, 600.0) == "OK"


def _run(coro):
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


def test_primary_success_short_circuits_legacy():
    with patch("app.worker.visual_analysis.get_video_duration", new=AsyncMock(return_value=120.0)), \
         patch("app.worker.transcribe_35.transcribe_with_gemini_35",
               new=AsyncMock(return_value="w " * 500)) as primary, \
         patch("app.worker.tasks.transcribe_with_legacy_gemini", new=AsyncMock()) as legacy:
        out = _run(tasks.get_raw_transcript("vid.mp4"))
    assert out.startswith("w ")
    assert primary.await_count == 1
    assert legacy.await_count == 0


def test_suspicious_primary_keeps_primary_when_fallback_not_dramatically_longer():
    # primary 500 chars, fallback 600 chars (1.2x) -> keep primary
    with patch("app.worker.visual_analysis.get_video_duration", new=AsyncMock(return_value=600.0)), \
         patch("app.worker.transcribe_35.transcribe_with_gemini_35",
               new=AsyncMock(return_value="x" * 500)), \
         patch("app.worker.tasks.transcribe_with_legacy_gemini",
               new=AsyncMock(return_value="y" * 600)) as legacy:
        out = _run(tasks.get_raw_transcript("vid.mp4"))
    assert out == "x" * 500
    assert legacy.await_count == 1


def test_suspicious_overridden_when_fallback_much_longer():
    # primary 300 chars suspicious, fallback 5000 chars OK (3.3x+ and passes gate) -> overridden
    with patch("app.worker.visual_analysis.get_video_duration", new=AsyncMock(return_value=600.0)), \
         patch("app.worker.transcribe_35.transcribe_with_gemini_35",
               new=AsyncMock(return_value="x" * 300)), \
         patch("app.worker.tasks.transcribe_with_legacy_gemini",
               new=AsyncMock(return_value="y" * 5000)) as legacy:
        out = _run(tasks.get_raw_transcript("vid.mp4"))
    assert out == "y" * 5000
    assert legacy.await_count == 1


def test_primary_exception_falls_back():
    with patch("app.worker.visual_analysis.get_video_duration", new=AsyncMock(return_value=60.0)), \
         patch("app.worker.transcribe_35.transcribe_with_gemini_35",
               new=AsyncMock(side_effect=RuntimeError("boom"))), \
         patch("app.worker.tasks.transcribe_with_legacy_gemini",
               new=AsyncMock(return_value="legacy full text")) as legacy:
        out = _run(tasks.get_raw_transcript("vid.mp4"))
    assert out == "legacy full text"
    assert legacy.await_count == 1


def test_flag_off_skips_primary():
    with patch.object(tasks.settings, "transcription_primary_enabled", False), \
         patch("app.worker.transcribe_35.transcribe_with_gemini_35",
               new=AsyncMock(side_effect=AssertionError("should not be called"))), \
         patch("app.worker.tasks.transcribe_with_legacy_gemini",
               new=AsyncMock(return_value="legacy only")) as legacy:
        out = _run(tasks.get_raw_transcript("vid.mp4"))
    assert out == "legacy only"
    assert legacy.await_count == 1


def test_sparse_speech_not_forced_to_fallback():
    # 600s video but primary 700 chars -> suspicious, fallback 750 chars near same length -> keep primary (sparse speech OK)
    with patch("app.worker.visual_analysis.get_video_duration", new=AsyncMock(return_value=600.0)), \
         patch("app.worker.transcribe_35.transcribe_with_gemini_35",
               new=AsyncMock(return_value="w " * 350)), \
         patch("app.worker.tasks.transcribe_with_legacy_gemini",
               new=AsyncMock(return_value="w " * 375)) as legacy:
        out = _run(tasks.get_raw_transcript("vid.mp4"))
    assert out.startswith("w ")
    assert legacy.await_count == 1


def test_meta_persisted_via_get_transcript_with_meta():
    async def _inner():
        with patch("app.worker.visual_analysis.get_video_duration", new=AsyncMock(return_value=60.0)), \
             patch("app.worker.transcribe_35.transcribe_with_gemini_35",
                   new=AsyncMock(return_value="ok " * 200)):
            text, meta = await tasks.get_transcript_with_meta("vid.mp4")
            assert text.startswith("ok ")
            assert meta["model"] == tasks.settings.transcription_primary_model
            assert meta["status"] in ("OK", "SUSPICIOUS", "FAILED", "FALLBACK")
            assert "chars" in meta
    _run(_inner())


def test_orchestrator_keeps_legacy_signature():
    import inspect

    sig = inspect.signature(tasks.get_raw_transcript)
    assert list(sig.parameters) == ["file_path", "max_rounds", "base_delay", "cap_delay"]
