"""Regression tests for get_raw_transcript upload-timeout protection.

Covers:
- a hung video upload fires the CALL_UPLOAD_TIMEOUT and ROTATES to the next key
- success path (fast upload) still works exactly as before
- when upload hangs on EVERY key it raises TimeoutError instead of hanging the job

Mocks genai + httpx so the logic is exercised deterministically without network.
"""
import json
import time
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.worker.tasks import get_raw_transcript


def _make_file(state: str = "ACTIVE"):
    f = MagicMock()
    f.name = "files/vid123"
    f.uri = "https://generativelanguage.googleapis.com/v1beta/files/vid123"
    f.mime_type = "video/mp4"
    f.state.name = state
    return f


class _FakeGenResp:
    status_code = 200
    request = None  # unused in 200 path
    text = '{"candidates":[{"content":{"parts":[{"text":"hello transcript"}]}}]}'

    def json(self):
        return json.loads(self.text)


def _fake_http():
    """Return a context manager that mocks httpx.AsyncClient to return a 200 body."""
    client = AsyncMock()
    client.post.return_value = _FakeGenResp()
    mgr = MagicMock()
    mgr.return_value.__aenter__ = AsyncMock(return_value=client)
    mgr.return_value.__aexit__ = AsyncMock(return_value=False)
    return patch("app.worker.tasks.httpx.AsyncClient", mgr)


def _pool_env(keys):
    env = {f"GEMINI_API_KEY_{i}": (keys[i - 1] if i <= len(keys) else "") for i in range(1, 10)}
    env["GEMINI_PAID_KEY"] = ""
    return env


def _base_cms(http_cm):
    """Context managers needed by get_raw_transcript's non-upload machinery."""
    return [
        http_cm,
        patch("app.worker.tasks.genai.configure"),
        patch("app.worker.tasks.genai.get_file"),
        patch("app.worker.tasks.genai.delete_file"),
        patch("app.worker.tasks.log_raw"),
        patch("app.worker.tasks.asyncio.sleep", new_callable=AsyncMock),
        patch("app.core.config.settings.gemini_api_key", ""),
    ]


@pytest.mark.asyncio
async def test_upload_timeout_rotates_to_next_key():
    """Upload hangs on KEY1 (past timeout) -> TimeoutError -> rotates to KEY2 which succeeds."""
    state = {"n": 0}

    def upload_behavior(path):
        state["n"] += 1
        if state["n"] == 1:
            time.sleep(0.4)  # longer than the patched 0.15s timeout -> hangs/rotates
        return _make_file()

    with ExitStack() as stack:
        for cm in _base_cms(_fake_http()):
            stack.enter_context(cm)
        stack.enter_context(patch("app.worker.tasks.CALL_UPLOAD_TIMEOUT", 0.15))
        stack.enter_context(patch("app.worker.tasks.genai.upload_file", side_effect=upload_behavior))
        stack.enter_context(patch.dict("os.environ", _pool_env(["KEY1", "KEY2"])))
        text = await get_raw_transcript("vid.mp4", max_rounds=1, base_delay=0.0, cap_delay=0.1)

    assert text == "hello transcript"
    # KEY1 upload timed out, KEY2 upload succeeded -> rotation happened
    assert state["n"] == 2


@pytest.mark.asyncio
async def test_upload_success_no_timeout():
    """Fast, healthy upload succeeds exactly as before: single attempt, no rotation."""
    with ExitStack() as stack:
        for cm in _base_cms(_fake_http()):
            stack.enter_context(cm)
        stack.enter_context(patch("app.worker.tasks.genai.upload_file", return_value=_make_file()))
        stack.enter_context(patch.dict("os.environ", _pool_env(["KEY1"])))
        text = await get_raw_transcript("vid.mp4", max_rounds=1, base_delay=0.0, cap_delay=0.1)

    assert text == "hello transcript"


@pytest.mark.asyncio
async def test_upload_timeout_every_key_then_raises():
    """If the upload hangs on EVERY key, get_raw_transcript raises TimeoutError
    rather than hanging the job until the global job_timeout."""
    def always_hang(path):
        time.sleep(0.4)
        return _make_file()

    with ExitStack() as stack:
        for cm in _base_cms(_fake_http()):
            stack.enter_context(cm)
        stack.enter_context(patch("app.worker.tasks.CALL_UPLOAD_TIMEOUT", 0.15))
        stack.enter_context(patch("app.worker.tasks.genai.upload_file", side_effect=always_hang))
        stack.enter_context(patch.dict("os.environ", _pool_env(["KEY1"])))
        with pytest.raises(TimeoutError):
            await get_raw_transcript("vid.mp4", max_rounds=1, base_delay=0.0, cap_delay=0.1)