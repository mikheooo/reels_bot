import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from app.worker.factcheck import TARGET_MODEL, call_gemini_api
from app.worker.tasks import get_raw_transcript


@pytest.mark.asyncio
@respx.mock
@patch("app.worker.factcheck.get_gemini_keys", return_value=["TEST_KEY_1"])
@patch("app.worker.factcheck.get_next_gemini_key", return_value="TEST_KEY_1")
@patch("asyncio.sleep", new_callable=AsyncMock)
async def test_call_gemini_api_429_retry_success(mock_sleep, mock_next_key, mock_get_keys):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{TARGET_MODEL}:generateContent?key=TEST_KEY_1"
    route = respx.post(url)
    route.side_effect = [
        httpx.Response(429, text='{"error": {"code": 429, "message": "Resource exhausted"}}'),
        httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"result": "ok"}'}]}}]})
    ]
    
    payload = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
    res = await call_gemini_api(payload, max_rounds=3, base_delay=1.0)
    
    assert res["candidates"][0]["content"]["parts"][0]["text"] == '{"result": "ok"}'
    assert route.call_count == 2
    mock_sleep.assert_called_once()

@pytest.mark.asyncio
@respx.mock
@patch("app.worker.factcheck.get_gemini_keys", return_value=["KEY_A", "KEY_B"])
@patch("asyncio.sleep", new_callable=AsyncMock)
async def test_call_gemini_api_key_rotation_on_429(mock_sleep, mock_get_keys):
    url_a = f"https://generativelanguage.googleapis.com/v1beta/models/{TARGET_MODEL}:generateContent?key=KEY_A"
    url_b = f"https://generativelanguage.googleapis.com/v1beta/models/{TARGET_MODEL}:generateContent?key=KEY_B"
    
    respx.post(url_a).mock(return_value=httpx.Response(429, text='{"error": "Rate limit"}'))
    respx.post(url_b).mock(return_value=httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "key_b_success"}]}}]}))
    
    payload = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
    res = await call_gemini_api(payload, max_rounds=2)
    
    assert res["candidates"][0]["content"]["parts"][0]["text"] == "key_b_success"

@pytest.mark.asyncio
@respx.mock
@patch("app.worker.factcheck.get_gemini_keys", return_value=["TEST_KEY_1"])
@patch("app.worker.factcheck.get_next_gemini_key", return_value="TEST_KEY_1")
@patch("asyncio.sleep", new_callable=AsyncMock)
async def test_call_gemini_api_retry_after_header(mock_sleep, mock_next_key, mock_get_keys):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{TARGET_MODEL}:generateContent?key=TEST_KEY_1"
    route = respx.post(url)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "5"}, text="Rate limited"),
        httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "success"}]}}]})
    ]
    
    payload = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
    res = await call_gemini_api(payload, max_rounds=3)
    
    assert res["candidates"][0]["content"]["parts"][0]["text"] == "success"
    assert mock_sleep.call_count == 1
    slept_duration = mock_sleep.call_args[0][0]
    # Should be at least 5s due to Retry-After: 5
    assert slept_duration >= 5.0

@pytest.mark.asyncio
@respx.mock
@patch("app.worker.factcheck.get_gemini_keys", return_value=["TEST_KEY_1"])
@patch("app.worker.factcheck.get_next_gemini_key", return_value="TEST_KEY_1")
@patch("asyncio.sleep", new_callable=AsyncMock)
async def test_call_gemini_api_503_retry(mock_sleep, mock_next_key, mock_get_keys):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{TARGET_MODEL}:generateContent?key=TEST_KEY_1"
    route = respx.post(url)
    route.side_effect = [
        httpx.Response(503, text='{"error": {"code": 503, "message": "high demand"}}'),
        httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok_after_503"}]}}]})
    ]
    
    payload = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
    res = await call_gemini_api(payload, max_rounds=3)
    assert res["candidates"][0]["content"]["parts"][0]["text"] == "ok_after_503"
    assert mock_sleep.call_count == 1

@pytest.mark.asyncio
@patch("app.worker.tasks.genai.upload_file")
@patch("app.worker.tasks.genai.get_file")
@patch("app.worker.tasks.genai.delete_file")
@patch("app.worker.tasks.genai.GenerativeModel")
@patch("app.worker.tasks.asyncio.sleep", new_callable=AsyncMock)
async def test_get_raw_transcript_429_retry(mock_sleep, mock_model_cls, mock_delete, mock_get_file, mock_upload):
    mock_file = MagicMock()
    mock_file.name = "files/test_vid_123"
    mock_file.state.name = "ACTIVE"
    mock_upload.return_value = mock_file
    mock_get_file.return_value = mock_file
    
    mock_model_instance = MagicMock()
    mock_response = MagicMock()
    mock_response.text = "Transcribed text from video"
    
    # First call raises 429, second succeeds
    mock_model_instance.generate_content.side_effect = [
        Exception("429 ResourceExhausted: Quota exceeded for quota metric 'Generate Content API requests'"),
        mock_response
    ]
    mock_model_cls.return_value = mock_model_instance
    
    env_vars = {f"GEMINI_API_KEY_{i}": "" for i in range(1, 10)}
    env_vars["GEMINI_API_KEY_1"] = "KEY1"
    
    with patch.dict("os.environ", env_vars), patch("app.core.config.settings.gemini_api_key", ""):
        transcript = await get_raw_transcript("test_vid.mp4", max_rounds=3, base_delay=1.0)
        
    assert transcript == "Transcribed text from video"
    assert mock_model_instance.generate_content.call_count == 2
    mock_sleep.assert_called()
    assert mock_delete.call_count == 2
