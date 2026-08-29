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

    class Config:
        env_file = ".env"
        extra = "allow"

settings = Settings()
