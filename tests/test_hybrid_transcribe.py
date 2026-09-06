"""Deterministic tests for the hybrid transcription orchestrator.

The 3.5 model, the legacy path and ffprobe are all faked: no network,
no API keys, no media files.
"""

from unittest.mock import AsyncMock, patch

from app.worker import tasks
from app.worker.transcribe_35 import (
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


def test_completeness_gate():
    assert is_transcript_complete("x" * 1000, 60.0) is True
    assert is_transcript_complete("x" * 100, 60.0) is False
    assert is_transcript_complete("", 60.0) is False
    assert is_transcript_complete("   ", 60.0) is False
    # unknown duration: trust the model
    assert is_transcript_complete("short", 0.0) is True


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


def test_suspicious_primary_falls_back():
    with patch("app.worker.visual_analysis.get_video_duration", new=AsyncMock(return_value=600.0)), \
         patch("app.worker.transcribe_35.transcribe_with_gemini_35",
               new=AsyncMock(return_value="too short")), \
         patch("app.worker.tasks.transcribe_with_legacy_gemini",
               new=AsyncMock(return_value="legacy full text")) as legacy:
        out = _run(tasks.get_raw_transcript("vid.mp4"))
    assert out == "legacy full text"
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


def test_orchestrator_keeps_legacy_signature():
    import inspect

    sig = inspect.signature(tasks.get_raw_transcript)
    assert list(sig.parameters) == ["file_path", "max_rounds", "base_delay", "cap_delay"]
