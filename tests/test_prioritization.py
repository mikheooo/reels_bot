import json

import pytest
from pydantic import ValidationError

from app.core.config import settings
from app.worker.prioritization import (
    DEFAULT_PUBLISH_THRESHOLD,
    WEIGHTS,
    _parse_json_response,
    decide_publish,
    score_content,
)
from app.worker.schemas import PriorityScore

SCORE_FIELDS = ["importance", "virality", "novelty", "views_potential", "audience_value"]


def _base_score(value: float = 0.5) -> dict:
    return {field: value for field in SCORE_FIELDS}


# --- Schema validation ---


def test_priority_score_accepts_boundaries():
    s = PriorityScore(
        importance=0.0,
        virality=1.0,
        novelty=0.0,
        views_potential=1.0,
        audience_value=0.0,
        overall=0.5,
        publish=True,
        reasons=["ok"],
    )
    assert s.importance == 0.0
    assert s.virality == 1.0
    assert s.overall == 0.5
    assert s.publish is True
    assert s.reasons == ["ok"]


@pytest.mark.parametrize("field", SCORE_FIELDS)
def test_priority_score_rejects_above_one(field):
    kwargs = {**{f: 0.5 for f in SCORE_FIELDS}, field: 1.01}
    with pytest.raises(ValidationError):
        PriorityScore(overall=0.5, publish=True, reasons=[], **kwargs)


@pytest.mark.parametrize("field", SCORE_FIELDS)
def test_priority_score_rejects_below_zero(field):
    kwargs = {**{f: 0.5 for f in SCORE_FIELDS}, field: -0.01}
    with pytest.raises(ValidationError):
        PriorityScore(overall=0.5, publish=True, reasons=[], **kwargs)


# --- decide_publish (pure, no LLM) ---


def test_decide_publish_all_max_publishes():
    ok, overall = decide_publish(_base_score(1.0), 0.6)
    assert ok is True
    assert overall == pytest.approx(1.0)


def test_decide_publish_all_zero_not_published():
    ok, overall = decide_publish(_base_score(0.0), 0.6)
    assert ok is False
    assert overall == pytest.approx(0.0)


def test_decide_publish_weighted_sum_matches_weights():
    scores = {"importance": 1.0, "virality": 0.5, "novelty": 0.0, "views_potential": 0.25, "audience_value": 0.75}
    expected = (
        1.0 * WEIGHTS["importance"]
        + 0.5 * WEIGHTS["virality"]
        + 0.0 * WEIGHTS["novelty"]
        + 0.25 * WEIGHTS["views_potential"]
        + 0.75 * WEIGHTS["audience_value"]
    )
    _, overall = decide_publish(scores, 0.6)
    assert overall == pytest.approx(expected)


def test_decide_publish_threshold_boundary_publishes():
    scores = {"importance": 1.0, "virality": 0.0, "novelty": 1.0, "views_potential": 0.0, "audience_value": 1.0}
    ok, overall = decide_publish(scores, 0.6)
    assert overall == pytest.approx(0.6)
    assert ok is True


def test_decide_publish_just_below_threshold_not_published():
    scores = {"importance": 1.0, "virality": 0.0, "novelty": 1.0, "views_potential": 0.0, "audience_value": 0.0}
    ok, overall = decide_publish(scores, 0.6)
    assert overall == pytest.approx(0.25 + 0.15)
    assert overall < 0.6
    assert ok is False


def test_decide_publish_missing_key_treated_as_zero():
    ok, overall = decide_publish({"importance": 1.0}, 0.6)
    assert overall == pytest.approx(WEIGHTS["importance"])
    assert ok is False


def test_default_threshold_constant_matches_config():
    assert DEFAULT_PUBLISH_THRESHOLD == 0.6
    assert settings.publish_threshold == 0.6


# --- score_content (mocked LLM, no real API) ---


def _mock_response(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


@pytest.mark.asyncio
async def test_score_content_publishes(monkeypatch):
    async def fake_call_gemini_api(payload):
        assert payload["generationConfig"]["responseMimeType"] == "application/json"
        assert "Транскрипт" in payload["contents"][0]["parts"][0]["text"]
        assert "some summary" in payload["contents"][0]["parts"][0]["text"]
        return _mock_response(
            json.dumps({"importance": 0.8, "virality": 0.9, "novelty": 0.7,
                        "views_potential": 0.6, "audience_value": 0.5, "reasons": ["Актуально"]})
        )

    monkeypatch.setattr("app.worker.prioritization.call_gemini_api", fake_call_gemini_api)
    result = await score_content("transcript text", "some summary")

    assert isinstance(result, PriorityScore)
    expected = (
        0.8 * WEIGHTS["importance"] + 0.9 * WEIGHTS["virality"] + 0.7 * WEIGHTS["novelty"]
        + 0.6 * WEIGHTS["views_potential"] + 0.5 * WEIGHTS["audience_value"]
    )
    assert result.overall == pytest.approx(expected)
    assert result.publish is True
    assert result.reasons == ["Актуально"]


@pytest.mark.asyncio
async def test_score_content_not_published(monkeypatch):
    async def fake_call_gemini_api(payload):
        return _mock_response(
            json.dumps({"importance": 0.1, "virality": 0.1, "novelty": 0.1,
                        "views_potential": 0.1, "audience_value": 0.1, "reasons": ["Слабая тема"]})
        )

    monkeypatch.setattr("app.worker.prioritization.call_gemini_api", fake_call_gemini_api)
    result = await score_content("transcript text")

    assert result.overall == pytest.approx(0.1)
    assert result.publish is False
    assert result.reasons == ["Слабая тема"]


@pytest.mark.asyncio
async def test_score_content_without_summary(monkeypatch):
    async def fake_call_gemini_api(payload):
        text = payload["contents"][0]["parts"][0]["text"]
        assert "Результаты предыдущего анализа" not in text
        return _mock_response(
            json.dumps({"importance": 0.5, "virality": 0.5, "novelty": 0.5,
                        "views_potential": 0.5, "audience_value": 0.5, "reasons": []})
        )

    monkeypatch.setattr("app.worker.prioritization.call_gemini_api", fake_call_gemini_api)
    result = await score_content("transcript text")
    assert result.overall == pytest.approx(0.5)
    assert result.reasons == []


@pytest.mark.asyncio
async def test_score_content_handles_markdown_fences(monkeypatch):
    async def fake_call_gemini_api(payload):
        return _mock_response(
            "```json\n" + json.dumps({"importance": 0.8, "virality": 0.8, "novelty": 0.8,
                                      "views_potential": 0.8, "audience_value": 0.8, "reasons": ["ok"]}) + "\n```"
        )

    monkeypatch.setattr("app.worker.prioritization.call_gemini_api", fake_call_gemini_api)
    result = await score_content("transcript text")
    assert result.overall == pytest.approx(0.8)
    assert result.publish is True


@pytest.mark.asyncio
async def test_score_content_rejects_out_of_range_score(monkeypatch):
    async def fake_call_gemini_api(payload):
        return _mock_response(
            json.dumps({"importance": 1.5, "virality": 0.5, "novelty": 0.5,
                        "views_potential": 0.5, "audience_value": 0.5, "reasons": []})
        )

    monkeypatch.setattr("app.worker.prioritization.call_gemini_api", fake_call_gemini_api)
    with pytest.raises(ValidationError):
        await score_content("transcript text")


# --- _parse_json_response ---


def test_parse_json_response_clean_object():
    data = _parse_json_response('{"importance": 0.8, "virality": 0.9}')
    assert data == {"importance": 0.8, "virality": 0.9}


def test_parse_json_response_markdown_fence_with_language():
    text = '```json\n{"importance": 0.8, "virality": 0.9}\n```'
    assert _parse_json_response(text) == {"importance": 0.8, "virality": 0.9}


def test_parse_json_response_markdown_fence_without_language():
    text = '```\n{"importance": 0.8}\n```'
    assert _parse_json_response(text) == {"importance": 0.8}


def test_parse_json_response_whitespace_and_newlines():
    text = '  \n\t{"importance": 0.8}\n  '
    assert _parse_json_response(text) == {"importance": 0.8}


def test_parse_json_response_invalid_json_raises():
    with pytest.raises(json.JSONDecodeError):
        _parse_json_response('{"importance": 0.8')


def test_parse_json_response_preserves_nested_structures():
    text = '{"importance": 0.8, "reasons": ["a", "b"], "meta": {"nested": {"key": [1, 2]}}}'
    assert _parse_json_response(text) == {
        "importance": 0.8,
        "reasons": ["a", "b"],
        "meta": {"nested": {"key": [1, 2]}},
    }
