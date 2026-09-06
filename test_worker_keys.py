from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.worker.tasks import process_video


@pytest.mark.asyncio
async def test_pipeline_error_is_persisted_and_reported_without_live_download():
    with patch("app.worker.tasks.update_job_status", new_callable=AsyncMock) as mock_update:
        with patch("app.worker.tasks.set_progress", new_callable=AsyncMock):
            with patch(
                "app.worker.tasks.download_video",
                new_callable=AsyncMock,
                side_effect=RuntimeError("offline download failure"),
            ):
                with patch('app.worker.tasks.Bot', autospec=True) as MockBot:
                    mock_bot_instance = MockBot.return_value
                    mock_bot_instance.send_message = AsyncMock()
                    mock_bot_instance.session = MagicMock()
                    mock_bot_instance.session.close = AsyncMock()

                    await process_video(ctx=None, job_id="test_job_123", url="http://test", user_id=123)

            assert mock_update.await_count == 2
            assert mock_update.await_args_list[0].args == ("test_job_123", "PROCESSING")
            error_call = mock_update.await_args_list[1]
            assert error_call.args == ("test_job_123", "ERROR")
            assert "offline download failure" in error_call.kwargs["error_text"]

            mock_bot_instance.send_message.assert_awaited_once()

