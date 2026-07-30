import os
import pytest

# Устанавливаем фейковые переменные ДО инициализации любых модулей (включая pydantic_settings)
os.environ["BOT_TOKEN"] = "dummy_bot_token"
os.environ["GEMINI_API_KEY"] = "dummy_gemini_key"
os.environ["DB_URL"] = "postgresql+asyncpg://user:password@localhost:5432/reels_db"
os.environ["REDIS_URL"] = "redis://localhost:6379/0"
os.environ["EXA_API_KEY"] = "dummy_exa_key"
os.environ["JINA_API_KEY"] = "dummy_jina_key"
os.environ["CHANNEL_CHAT_ID"] = "@dummy_channel"

@pytest.fixture(autouse=True)
def env_setup():
    pass
