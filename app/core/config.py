from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    bot_token: str
    gemini_api_key: str
    db_url: str
    redis_url: str
    channel_chat_id: str | None = None
    expected_bot_username: str | None = None
    exa_api_key: str | None = None
    jina_api_key: str | None = None
    publish_threshold: float = 0.6
    # Was hardcoded as "gemini-3.7-flash" in tasks.py and factcheck.py.
    # Override via GEMINI_MODEL in .env to switch models without touching code.
    gemini_model: str = "gemini-3.7-flash"

    class Config:
        env_file = ".env"
        extra = "allow"

settings = Settings()
