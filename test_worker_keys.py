from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings

# Fake env for importing tasks
from app.worker.tasks import process_video


@pytest.mark.asyncio
async def test_worker_keys_graceful():
    settings.exa_api_key = None
    settings.jina_api_key = None

    with patch('app.worker.tasks.update_job_status', new_callable=AsyncMock) as mock_update:
        with patch('app.worker.tasks.Bot', autospec=True) as MockBot:
            mock_bot_instance = MockBot.return_value
            mock_bot_instance.send_message = AsyncMock()
            mock_bot_instance.session = MagicMock()
            mock_bot_instance.session.close = AsyncMock()

            await process_video(ctx=None, job_id="test_job_123", url="http://test", user_id=123)

            mock_update.assert_called_once()
            args, kwargs = mock_update.call_args
            assert args[0] == "test_job_123"
            assert args[1] == "REVIEW_REQUIRED"
            assert "отсутствуют ключи EXA_API_KEY" in kwargs["error_text"]

            mock_bot_instance.send_video = AsyncMock()
            mock_bot_instance.send_video.assert_not_called()

            print("Worker Graceful Degradation Test Passed: Real process_video exited early and set REVIEW_REQUIRED.")

