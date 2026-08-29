
import asyncio
import time
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from app.worker.factcheck import post_vertex_with_retry

URL = "https://aiplatform.googleapis.com/v1beta1/projects/fake/locations/global/publishers/google/models/gemini:generateContent"
HEADERS = {"Authorization": "Bearer fake"}
PAYLOAD = {"test": "data"}

@pytest.mark.asyncio
@respx.mock
async def test_success():
    respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    res = await post_vertex_with_retry(URL, HEADERS, PAYLOAD, 10.0, 30.0)
    assert res == {"ok": True}
    assert respx.calls.call_count == 1

@pytest.mark.asyncio
@respx.mock
@patch("asyncio.sleep", new_callable=AsyncMock)
async def test_retry_after_429(mock_sleep):
    route = respx.post(URL)
    route.side_effect = [
        httpx.Response(429, text="Rate limited"),
        httpx.Response(200, json={"ok": True})
    ]
    res = await post_vertex_with_retry(URL, HEADERS, PAYLOAD, 10.0, 30.0)
    assert res == {"ok": True}
    assert respx.calls.call_count == 2
    mock_sleep.assert_called_once()

@pytest.mark.asyncio
@respx.mock
@patch("asyncio.sleep", new_callable=AsyncMock)
async def test_retry_after_500(mock_sleep):
    route = respx.post(URL)
    route.side_effect = [
        httpx.Response(500, text="Internal Server Error"),
        httpx.Response(200, json={"ok": True})
    ]
    res = await post_vertex_with_retry(URL, HEADERS, PAYLOAD, 10.0, 30.0)
    assert res == {"ok": True}
    assert respx.calls.call_count == 2
    mock_sleep.assert_called_once()

@pytest.mark.asyncio
@respx.mock
@patch("asyncio.sleep", new_callable=AsyncMock)
async def test_network_error(mock_sleep):
    route = respx.post(URL)
    route.side_effect = [
        httpx.ConnectTimeout("Timeout"),
        httpx.Response(200, json={"ok": True})
    ]
    res = await post_vertex_with_retry(URL, HEADERS, PAYLOAD, 10.0, 30.0)
    assert res == {"ok": True}
    assert respx.calls.call_count == 2
    mock_sleep.assert_called_once()

@pytest.mark.asyncio
@respx.mock
@patch("asyncio.sleep", new_callable=AsyncMock)
async def test_exhaustion(mock_sleep):
    respx.post(URL).mock(return_value=httpx.Response(429, text="Rate limited"))
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await post_vertex_with_retry(URL, HEADERS, PAYLOAD, 10.0, 30.0)
    assert exc.value.response.status_code == 429
    assert respx.calls.call_count == 5
    assert mock_sleep.call_count == 4

@pytest.mark.asyncio
@respx.mock
@patch("asyncio.sleep", new_callable=AsyncMock)
@patch("time.monotonic")
async def test_deadline_exceeded(mock_monotonic, mock_sleep):
    current_time = 1000.0
    def side_effect():
        nonlocal current_time
        current_time += 10.0
        return current_time
    mock_monotonic.side_effect = side_effect
    
    respx.post(URL).mock(return_value=httpx.Response(429, text="Rate limited"))
    
    # We expect HTTPStatusError because the code calls last_response.raise_for_status() 
    # when deadline is hit during backoff.
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await post_vertex_with_retry(URL, HEADERS, PAYLOAD, 10.0, 15.0)
    assert exc.value.response.status_code == 429

@pytest.mark.asyncio
@respx.mock
@patch("asyncio.sleep", new_callable=AsyncMock)
async def test_retry_after_headers(mock_sleep):
    route = respx.post(URL)
    future_date = datetime.now(timezone.utc) + timedelta(seconds=10)
    http_date = formatdate(future_date.timestamp(), usegmt=True)
    
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "5"}),
        httpx.Response(429, headers={"Retry-After": http_date}),
        httpx.Response(200, json={"ok": True})
    ]
    res = await post_vertex_with_retry(URL, HEADERS, PAYLOAD, 10.0, 60.0)
    assert res == {"ok": True}
    assert respx.calls.call_count == 3
    assert mock_sleep.call_count == 2
    
    args1 = mock_sleep.call_args_list[0][0][0]
    args2 = mock_sleep.call_args_list[1][0][0]
    assert args1 >= 5.0
    assert args2 >= 8.0

@pytest.mark.asyncio
@patch("httpx.AsyncClient.post", side_effect=asyncio.CancelledError("Cancelled"))
async def test_cancelled_error(mock_post):
    # Testing CancelledError directly using unittest.mock to bypass respx assertion errors
    with pytest.raises(asyncio.CancelledError):
        await post_vertex_with_retry(URL, HEADERS, PAYLOAD, 10.0, 30.0)
    mock_post.assert_called_once()

@pytest.mark.asyncio
@respx.mock
async def test_request_ids(caplog):
    import logging
    caplog.set_level(logging.INFO)
    
    route = respx.post(URL)
    route.side_effect = [
        httpx.Response(200, headers={"x-goog-request-id": "req-123"}, json={"ok": True}),
        httpx.Response(200, headers={"x-request-id": "req-456"}, json={"ok": True}),
        httpx.Response(200, headers={"x-cloud-trace-context": "req-789"}, json={"ok": True}),
        httpx.Response(200, json={"ok": True})
    ]
    
    await post_vertex_with_retry(URL, HEADERS, PAYLOAD, 10.0, 30.0)
    assert "ReqID: req-123" in caplog.text
    caplog.clear()
    
    await post_vertex_with_retry(URL, HEADERS, PAYLOAD, 10.0, 30.0)
    assert "ReqID: req-456" in caplog.text
    caplog.clear()
    
    await post_vertex_with_retry(URL, HEADERS, PAYLOAD, 10.0, 30.0)
    assert "ReqID: req-789" in caplog.text
    caplog.clear()
    
    await post_vertex_with_retry(URL, HEADERS, PAYLOAD, 10.0, 30.0)
    assert "ReqID: unknown" in caplog.text
