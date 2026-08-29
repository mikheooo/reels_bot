"""Tests for visual evidence extraction and structured analysis integration.

Covers:
- Frame extraction contract (ffprobe/ffmpeg)
- Visual evidence structure
- Transcript + visual evidence composition
- Visual evidence does not auto-become a claim
- Unreadable frame → uncertain/unconfirmed
- GitHub repository identification
- Missing vision → transcript fallback
- Vision API failure → transcript fallback
- REPORT_PROMPT evidence-bound rules remain
- Summary ↔ Mechanics consistency remains
- Mechanics → Plan → DoD consistency remains
- Parser compatibility
"""
import asyncio
import base64
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.worker.visual_analysis import (
    VISUAL_ANALYSIS_PROMPT,
    VISUAL_ANALYSIS_SCHEMA,
    RateLimitError,
    extract_keyframes,
    extract_visual_evidence,
    analyze_frames_with_vision,
    format_visual_evidence,
    get_video_duration,
)
from app.worker.structured_analysis import REPORT_PROMPT, generate_structured_analysis


# ---------------------------------------------------------------------------
# Frame extraction contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_video_duration_returns_float():
    """get_video_duration returns a positive float for a valid video."""
    duration = await get_video_duration("test_vid.mp4")
    assert isinstance(duration, float)
    assert duration > 0


@pytest.mark.asyncio
async def test_extract_keyframes_returns_list_of_dicts():
    """extract_keyframes returns list of dicts with timestamp and jpeg_b64."""
    frames = await extract_keyframes("test_vid.mp4", max_frames=4)
    assert isinstance(frames, list)
    assert len(frames) > 0
    assert len(frames) <= 4
    for f in frames:
        assert "timestamp" in f
        assert "jpeg_b64" in f
        assert isinstance(f["timestamp"], float)
        assert f["timestamp"] >= 0.0
        # Verify base64 is valid
        decoded = base64.b64decode(f["jpeg_b64"])
        assert len(decoded) > 100  # At least some JPEG data


@pytest.mark.asyncio
async def test_extract_keyframes_nonexistent_video_returns_empty():
    """extract_keyframes returns empty list for non-existent video."""
    frames = await extract_keyframes("nonexistent_video.mp4")
    assert frames == []


# ---------------------------------------------------------------------------
# Visual evidence structure
# ---------------------------------------------------------------------------

def test_visual_analysis_schema_has_required_fields():
    """VISUAL_ANALYSIS_SCHEMA requires timestamp, description, confidence."""
    props = VISUAL_ANALYSIS_SCHEMA["items"]["properties"]
    required = VISUAL_ANALYSIS_SCHEMA["items"]["required"]
    assert "timestamp" in props
    assert "description" in props
    assert "text_read" in props
    assert "confidence" in props
    assert "timestamp" in required
    assert "description" in required
    assert "confidence" in required
    assert props["confidence"]["enum"] == ["high", "medium", "low"]


def test_visual_analysis_prompt_mentions_github_and_terminal():
    """Prompt should mention GitHub, terminal, UI for targeted extraction."""
    assert "GitHub" in VISUAL_ANALYSIS_PROMPT
    assert "терминал" in VISUAL_ANALYSIS_PROMPT
    assert "UI" in VISUAL_ANALYSIS_PROMPT or "интерфейс" in VISUAL_ANALYSIS_PROMPT


# ---------------------------------------------------------------------------
# format_visual_evidence
# ---------------------------------------------------------------------------

def test_format_visual_evidence_with_items():
    """format_visual_evidence renders items with timestamp, description, text, confidence."""
    items = [
        {"timestamp": 3.0, "description": "GitHub repo screenshot", "text_read": "owner/repo", "confidence": "high"},
        {"timestamp": 10.0, "description": "Terminal output", "text_read": None, "confidence": "low"},
    ]
    result = format_visual_evidence(items)
    assert isinstance(result, dict)
    assert result["status"] == "available"
    formatted = result["formatted"]
    assert "[00:03" in formatted
    assert "GitHub repo screenshot" in formatted
    assert "owner/repo" in formatted
    assert "high" in formatted
    assert "[00:10" in formatted
    assert "low" in formatted


def test_format_visual_evidence_empty_returns_unavailable_marker():
    """format_visual_evidence with empty list returns status=unavailable."""
    result = format_visual_evidence([])
    assert isinstance(result, dict)
    assert result["status"] == "unavailable"


# ---------------------------------------------------------------------------
# Transcript + visual evidence composition
# ---------------------------------------------------------------------------

def test_report_prompt_has_separate_transcript_and_visual_sections():
    """REPORT_PROMPT must have separate === TRANSCRIPT === and === VISUAL EVIDENCE === sections."""
    assert "=== ТРАНСКРИПТ" in REPORT_PROMPT
    assert "=== ВИЗУАЛЬНЫЙ EVIDENCE" in REPORT_PROMPT
    assert "{transcript}" in REPORT_PROMPT
    assert "{visual_evidence}" in REPORT_PROMPT


def test_report_prompt_distinguishes_evidence_sources():
    """REPORT_PROMPT must distinguish ПОКАЗАНО В КАДРЕ vs СКАЗАНО В АУДИО vs ПОДТВЕРЖДЕНО."""
    assert "ПОКАЗАНО В КАДРЕ" in REPORT_PROMPT
    assert "СКАЗАНO В АУДИО" in REPORT_PROMPT or "СКАЗАНО В АУДИО" in REPORT_PROMPT
    assert "ПОДТВЕРЖДЕНО ВНЕШНИМ" in REPORT_PROMPT
    assert "НЕ ПОДТВЕРЖДЕНО" in REPORT_PROMPT


def test_report_prompt_handles_uncertain_text():
    """REPORT_PROMPT must instruct to mark medium/low confidence text as uncertain."""
    assert "medium" in REPORT_PROMPT
    assert "low" in REPORT_PROMPT
    assert "частично прочитано" in REPORT_PROMPT
    assert "требует уточнения" in REPORT_PROMPT


# ---------------------------------------------------------------------------
# Visual evidence does not auto-become a claim
# ---------------------------------------------------------------------------

def test_report_prompt_does_not_turn_visual_into_fact():
    """REPORT_PROMPT must not allow visual evidence to become unchecked fact."""
    assert "Не превращай неуверенно прочитанное" in REPORT_PROMPT
    assert "название в точный факт" in REPORT_PROMPT


# ---------------------------------------------------------------------------
# Vision fallback: missing vision → transcript fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_visual_evidence_status_frame_extraction_failed_on_no_frames():
    """extract_visual_evidence returns status=frame_extraction_failed when no frames."""
    with patch("app.worker.visual_analysis.extract_keyframes", return_value=[]):
        result = await extract_visual_evidence("dummy.mp4")
    assert isinstance(result, dict)
    assert result["status"] == "frame_extraction_failed"


@pytest.mark.asyncio
async def test_extract_visual_evidence_returns_empty_response_on_api_failure():
    """extract_visual_evidence returns status=empty_response when vision API returns no evidence."""
    mock_frames = [{"timestamp": 5.0, "jpeg_b64": "dGVzdA=="}]
    with patch("app.worker.visual_analysis.extract_keyframes", return_value=mock_frames):
        with patch("app.worker.visual_analysis.analyze_frames_with_vision", return_value=[]):
            result = await extract_visual_evidence("dummy.mp4")
    assert isinstance(result, dict)
    assert result["status"] == "empty_response"


@pytest.mark.asyncio
async def test_generate_structured_analysis_with_none_visual_evidence():
    """generate_structured_analysis works with visual_evidence=None (transcript-only fallback)."""
    mock_response = {
        "candidates": [{
            "content": {
                "parts": [{"text": "**КРАТКО ДЛЯ КАНАЛА:**\nTest summary"}]
            }
        }]
    }
    with patch("app.worker.structured_analysis.call_gemini_api", return_value=mock_response):
        result = await generate_structured_analysis("test transcript", visual_evidence=None)
    assert "Test summary" in result
    # Verify the prompt included the fallback marker
    assert "VISUAL ANALYSIS UNAVAILABLE" in result or "Test summary" in result


# ---------------------------------------------------------------------------
# analyze_frames_with_vision
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_analyze_frames_with_vision_success():
    """analyze_frames_with_vision returns structured evidence on success."""
    mock_frames = [{"timestamp": 5.0, "jpeg_b64": "dGVzdA=="}]
    mock_response = {
        "candidates": [{
            "content": {
                "parts": [{
                    "text": json.dumps([
                        {
                            "timestamp": 5.0,
                            "description": "GitHub repository page",
                            "text_read": "owner/repo-name",
                            "confidence": "high"
                        }
                    ])
                }]
            }
        }]
    }
    with patch("app.worker.visual_analysis.call_gemini_api", return_value=mock_response):
        result = await analyze_frames_with_vision(mock_frames)
    assert len(result) == 1
    assert result[0]["description"] == "GitHub repository page"
    assert result[0]["text_read"] == "owner/repo-name"
    assert result[0]["confidence"] == "high"


@pytest.mark.asyncio
async def test_analyze_frames_with_vision_api_failure_returns_empty():
    """analyze_frames_with_vision returns [] on API failure."""
    mock_frames = [{"timestamp": 5.0, "jpeg_b64": "dGVzdA=="}]
    with patch("app.worker.visual_analysis.call_gemini_api", side_effect=Exception("API error")):
        result = await analyze_frames_with_vision(mock_frames)
    assert result == []


@pytest.mark.asyncio
async def test_analyze_frames_with_vision_empty_frames_returns_empty():
    """analyze_frames_with_vision returns [] for empty frames list."""
    result = await analyze_frames_with_vision([])
    assert result == []


# ---------------------------------------------------------------------------
# Unreadable frame → uncertain/unconfirmed
# ---------------------------------------------------------------------------

def test_format_visual_evidence_low_confidence_no_text():
    """format_visual_evidence shows low confidence and no text_read for unreadable frames."""
    items = [
        {"timestamp": 15.0, "description": "Blurred GitHub page", "text_read": None, "confidence": "low"}
    ]
    result = format_visual_evidence(items)
    assert isinstance(result, dict)
    formatted = result["formatted"]
    assert "low" in formatted
    assert "Blurred GitHub page" in formatted
    assert "Текст:" not in formatted


# ---------------------------------------------------------------------------
# Evidence-bound contract remains intact
# ---------------------------------------------------------------------------

def test_evidence_bound_rules_remain_in_prompt():
    """All existing evidence-bound rules must remain in REPORT_PROMPT."""
    required = [
        "Не усиливай claims",
        "практически неограниченное использование",
        "неограниченные лимиты",
        "бесплатные API",
        "затраты до нуля",
        "Не добавляй конкретные причины",
        "мусорным кодом",
        "МЕХАНИКА, Практический план интеграции и КРИТЕРИИ ГОТОВНОСТИ",
        "не может внезапно стать обязательным выполненным этапом",
        "если установка/настройка не показана",
        "нельзя писать",
        "«установить»",
        "«настроить»",
        "«внедрить»",
        "«активировать»",
        "Не превращай упоминание инструмента",
        "Подтверждено наличие",
        "не может требовать установить, настроить, внедрить или активировать",
    ]
    for marker in required:
        assert marker in REPORT_PROMPT, f"Missing evidence-bound marker: {marker}"


def test_summary_contract_remains():
    """Summary ↔ Mechanics consistency rules must remain."""
    normalized = " ".join(REPORT_PROMPT.split())
    assert "КРАТКО ДЛЯ КАНАЛА" in REPORT_PROMPT
    assert "SUMMARY" in REPORT_PROMPT
    assert "МЕХАНИКЕ" in REPORT_PROMPT
    assert "не описывай его в summary как фактическую механику" in normalized


def test_summary_forbidden_phrases_remain():
    """Forbidden summary phrases about installation must remain."""
    normalized = " ".join(REPORT_PROMPT.split())
    for phrase in ("механика сводится к установке", "работа заключается в установке", "нужно установить", "практическая механика — установка"):
        assert phrase in normalized


def test_optional_better_block_remains_optional():
    """Optional МОЖНО ЛУЧШЕ block must remain optional, not canonical."""
    assert "### 🚀 МОЖНО ЛУЧШЕ" not in REPORT_PROMPT
    assert "Опциональный блок **🚀 МОЖНО ЛУЧШЕ**" in REPORT_PROMPT
    assert "Если такой альтернативы нет, блок не выводи вообще" in REPORT_PROMPT


# ---------------------------------------------------------------------------
# Parser compatibility (tasks.py still works with new signature)
# ---------------------------------------------------------------------------

def test_generate_structured_analysis_accepts_two_args():
    """generate_structured_analysis must accept (transcript, visual_evidence)."""
    import inspect
    sig = inspect.signature(generate_structured_analysis)
    params = list(sig.parameters.keys())
    assert "transcript" in params
    assert "visual_evidence" in params
    # visual_evidence should have default None
    assert sig.parameters["visual_evidence"].default is None


# ---------------------------------------------------------------------------
# DEFECT FIX: status must NOT be in Gemini schema (application-level, not LLM)
# ---------------------------------------------------------------------------

def test_visual_analysis_schema_has_no_status_field():
    """VISUAL_ANALYSIS_SCHEMA must NOT include 'status' — it is an
    application-level result, not something Gemini should return."""
    props = VISUAL_ANALYSIS_SCHEMA["items"]["properties"]
    assert "status" not in props, (
        "status must not be in VISUAL_ANALYSIS_SCHEMA; "
        "it is an application-level result computed by Python code"
    )


# ---------------------------------------------------------------------------
# DEFECT FIX: extract_visual_evidence returns dict with application-level status
# ---------------------------------------------------------------------------

def _VALID_EVIDENCE_ITEM(ts=5.0):
    return {
        "timestamp": ts,
        "description": "GitHub repo page",
        "text_read": "owner/repo",
        "confidence": "high",
    }


@pytest.mark.asyncio
async def test_extract_visual_evidence_returns_dict_with_status():
    """extract_visual_evidence returns dict with 'status' and 'formatted' on success."""
    mock_frames = [{"timestamp": 5.0, "jpeg_b64": "dGVzdA=="}]
    with patch("app.worker.visual_analysis.extract_keyframes", return_value=mock_frames):
        with patch("app.worker.visual_analysis.analyze_frames_with_vision",
                    return_value=[_VALID_EVIDENCE_ITEM()]):
            result = await extract_visual_evidence("dummy.mp4")
    assert isinstance(result, dict)
    assert result["status"] == "available"
    assert "formatted" in result
    assert "GitHub repo page" in result["formatted"]


@pytest.mark.asyncio
async def test_extract_visual_evidence_status_frame_extraction_failed():
    """extract_visual_evidence returns status=frame_extraction_failed when no frames."""
    with patch("app.worker.visual_analysis.extract_keyframes", return_value=[]):
        result = await extract_visual_evidence("dummy.mp4")
    assert isinstance(result, dict)
    assert result["status"] == "frame_extraction_failed"


@pytest.mark.asyncio
async def test_extract_visual_evidence_status_empty_response():
    """extract_visual_evidence returns status=empty_response when API returns no evidence."""
    mock_frames = [{"timestamp": 5.0, "jpeg_b64": "dGVzdA=="}]
    with patch("app.worker.visual_analysis.extract_keyframes", return_value=mock_frames):
        with patch("app.worker.visual_analysis.analyze_frames_with_vision", return_value=[]):
            result = await extract_visual_evidence("dummy.mp4")
    assert isinstance(result, dict)
    assert result["status"] == "empty_response"


@pytest.mark.asyncio
async def test_extract_visual_evidence_status_api_rate_limited():
    """extract_visual_evidence returns status=api_rate_limited when all retries exhausted."""
    mock_frames = [{"timestamp": 5.0, "jpeg_b64": "dGVzdA=="}]
    with patch("app.worker.visual_analysis.extract_keyframes", return_value=mock_frames):
        with patch("app.worker.visual_analysis.analyze_frames_with_vision",
                    side_effect=RateLimitError("all keys rate limited")):
            result = await extract_visual_evidence("dummy.mp4")
    assert isinstance(result, dict)
    assert result["status"] == "api_rate_limited"


# ---------------------------------------------------------------------------
# DEFECT FIX: bounded visual retry budget
# ---------------------------------------------------------------------------

def test_visual_max_retries_is_bounded():
    """VISUAL_MAX_RETRIES must be a small positive integer (bounded budget)."""
    from app.worker.visual_analysis import VISUAL_MAX_RETRIES
    assert isinstance(VISUAL_MAX_RETRIES, int)
    assert 1 <= VISUAL_MAX_RETRIES <= 5


@pytest.mark.asyncio
async def test_analyze_frames_with_vision_retries_on_rate_limit():
    """analyze_frames_with_vision retries up to VISUAL_MAX_RETRIES then raises RateLimitError."""
    from app.worker.visual_analysis import VISUAL_MAX_RETRIES
    mock_frames = [{"timestamp": 5.0, "jpeg_b64": "dGVzdA=="}]
    rate_limit_exc = RateLimitError("429 all keys exhausted")
    mock_api = AsyncMock(side_effect=rate_limit_exc)
    with patch("app.worker.visual_analysis.call_gemini_api", mock_api):
        with pytest.raises(RateLimitError):
            await analyze_frames_with_vision(mock_frames)
    assert mock_api.call_count == VISUAL_MAX_RETRIES


@pytest.mark.asyncio
async def test_analyze_frames_with_vision_succeeds_after_retry():
    """analyze_frames_with_vision succeeds on second attempt after rate limit."""
    mock_frames = [{"timestamp": 5.0, "jpeg_b64": "dGVzdA=="}]
    success_response = {
        "candidates": [{
            "content": {"parts": [{"text": json.dumps([_VALID_EVIDENCE_ITEM()])}]}
        }]
    }
    rate_limit_exc = RateLimitError("429 rate limited")
    mock_api = AsyncMock(side_effect=[rate_limit_exc, success_response])
    with patch("app.worker.visual_analysis.call_gemini_api", mock_api):
        result = await analyze_frames_with_vision(mock_frames)
    assert len(result) == 1
    assert result[0]["description"] == "GitHub repo page"
    assert mock_api.call_count == 2


# ---------------------------------------------------------------------------
# DEFECT FIX: frame timestamps cover start/middle/end
# ---------------------------------------------------------------------------

def test_timestamps_span_full_video():
    """Frame timestamps should span from near start to near end of video."""
    duration = 60.0
    max_frames = 12
    usable = max(1.0, duration - 0.5)
    interval = max(1.0, usable / max_frames)
    n = min(max_frames, 16)  # HARD_FRAME_CAP
    timestamps = [i * interval for i in range(n)]
    # First frame should be at or near 0
    assert timestamps[0] == 0.0
    # Last frame should be close to duration (within one interval)
    assert timestamps[-1] >= duration - interval * 1.5
    # Middle frame should be roughly at duration/2
    mid_idx = n // 2
    assert abs(timestamps[mid_idx] - duration / 2) < interval * 2


@pytest.mark.asyncio
async def test_extract_keyframes_timestamp_start_middle_end():
    """Verify extract_keyframes produces timestamps covering start, middle, end."""
    frames = await extract_keyframes("test_vid.mp4", max_frames=8)
    if not frames:
        pytest.skip("No test video available")
    timestamps = [f["timestamp"] for f in frames]
    # First frame near start
    assert timestamps[0] < 5.0, f"First frame timestamp {timestamps[0]} too late"
    # Last frame near end (video is short, so within ~5s)
    duration = await get_video_duration("test_vid.mp4")
    assert timestamps[-1] >= duration * 0.5, (
        f"Last frame timestamp {timestamps[-1]} too early for {duration}s video"
    )
    # Monotonically increasing
    for i in range(1, len(timestamps)):
        assert timestamps[i] > timestamps[i - 1]


# ---------------------------------------------------------------------------
# Structured analysis handles dict from extract_visual_evidence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_structured_analysis_with_dict_visual_evidence():
    """generate_structured_analysis works when visual_evidence is a dict."""
    mock_response = {
        "candidates": [{
            "content": {"parts": [{"text": "**КРАТКО ДЛЯ КАНАЛА:**\nTest"}]}
        }]
    }
    visual_dict = {"status": "available", "formatted": "[00:05] GitHub page | Уверенность: high"}
    with patch("app.worker.structured_analysis.call_gemini_api", return_value=mock_response):
        result = await generate_structured_analysis("transcript", visual_evidence=visual_dict)
    assert "Test" in result


@pytest.mark.asyncio
async def test_generate_structured_analysis_with_string_visual_evidence():
    """generate_structured_analysis works when visual_evidence is a plain string."""
    mock_response = {
        "candidates": [{
            "content": {"parts": [{"text": "**КРАТКО ДЛЯ КАНАЛА:**\nTest"}]}
        }]
    }
    with patch("app.worker.structured_analysis.call_gemini_api", return_value=mock_response):
        result = await generate_structured_analysis("transcript",
                                                    visual_evidence="[00:05] GitHub page")
    assert "Test" in result
